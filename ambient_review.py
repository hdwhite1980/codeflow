"""
codeflow.ambient_review
=======================

Proactive concern generation from guardian summaries (Turn E).

The guardian's file-level and symbol-level summaries already contain
risk_notes — concrete things a reviewer would flag. Until this turn,
those notes only mattered when a user asked the risk analyzer something
specific. Now they bubble up automatically as project-level findings.

Workflow
--------
1. After a guardian_index pass completes, scan all current summaries.
2. Classify each risk_note by severity (heuristic — see below).
3. Group notes by topic. A topic is a short phrase distilled from the
   note's keywords (e.g. "credentials in code", "missing tenant scoping",
   "unbounded retries"). Multiple files sharing a topic become a
   single multi-file finding.
4. Score findings: severity ceiling × file count × pattern weight.
5. Write findings to the ledger under `ambient_finding:<pid>:<digest>`.
   Digest is a stable hash of the topic + files involved so re-runs
   don't multiply the same finding.

Dismissal
---------
A user can mark a finding as dismissed via POST. Dismissed findings
have a `dismissed_at` timestamp and an optional reason. They don't
reappear unless the evidence (file set, severity) materially changes.

Severity heuristic
------------------
We score on three axes:
  - Keyword strength in the note text (CRITICAL/secret/credential/etc.)
  - Number of files affected (1 file = isolated; 3+ = systemic)
  - Cross-validation: same note text from multiple files = stronger signal

Tiers map to the same low/medium/high/critical labels the risk analyzer
uses, so the UI shows consistent vocabulary across both surfaces.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# --- Keyword tables for severity scoring -----------------------------------
#
# Each keyword/phrase contributes points. Multiple matches in one note
# stack. Score buckets (after summing): 0–1 → low, 2–4 → medium,
# 5–8 → high, 9+ → critical.

_CRITICAL_KEYWORDS = [
    # Explicit severity markers
    r"\bCRITICAL\b", r"\bsevere\b",
    # Secrets and credentials
    r"\bhard[- ]?coded\b.{0,40}\b(creden|secret|token|key|password)",
    r"\bplain[- ]?text\b.{0,40}\b(creden|secret|password)",
    r"\bAPI key\b", r"\bAPI token\b",
    # Tenant isolation (MSSP critical)
    r"\bmulti[- ]?tenant\b.{0,40}\b(leak|isolation|scoping)",
    r"\bcross[- ]?tenant\b",
    # Injection
    r"\bSQL injection\b", r"\bcommand injection\b", r"\beval\b.{0,30}\binput\b",
    r"\bRCE\b", r"\bremote code execution\b",
    # Catastrophic patterns
    r"\brm -rf\b", r"\bdestructive\b.{0,30}\b(without|no check)",
]

_HIGH_KEYWORDS = [
    r"\bauth(?:entication|orization)?\s+(?:bypass|missing|skipped)",
    r"\bunbounded\b.{0,20}\b(retry|loop|recursion|memory|queue)",
    r"\brace condition\b", r"\bdeadlock\b",
    r"\bsilently\s+(?:fails|swallowed|ignored)",
    r"\bswallow(?:s|ed|ing)?\s+(?:error|exception)",
    r"\bgeneric exception\b", r"\bcatches everything\b",
    r"\b(?:missing|no)\s+rate[- ]?limit",
    r"\b(?:no|missing|skipped)\s+input validation\b",
    r"\bnever\s+(?:expires|times out|cleans up)",
    r"\bbroad\s+(?:scope|permission|grant)",
    r"\bover[- ]?privileged\b",
]

_MEDIUM_KEYWORDS = [
    r"\bdeprecated\b", r"\blegacy\b",
    r"\bN\+1\b", r"\bslow\b.{0,20}\bquery\b",
    r"\bmemory leak\b", r"\bfd leak\b", r"\bfile descriptor\b",
    r"\b(?:no|missing)\s+timeout",
    r"\binconsistent state\b", r"\bpartial commit\b",
    r"\b(?:no|missing)\s+test", r"\buntested\b",
    r"\bedge case\b", r"\bcorner case\b",
    r"\bnot thread[- ]safe\b", r"\bglobal mutable\b",
]


def _compile_patterns(rules: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in rules]


_CRITICAL_RE = _compile_patterns(_CRITICAL_KEYWORDS)
_HIGH_RE = _compile_patterns(_HIGH_KEYWORDS)
_MEDIUM_RE = _compile_patterns(_MEDIUM_KEYWORDS)


def _score_note_text(text: str) -> int:
    """Return a numeric severity score for one risk-note string.

    Higher = more severe. Buckets:
      0-1   low
      2-4   medium
      5-8   high
      9+    critical
    """
    score = 0
    for pat in _CRITICAL_RE:
        if pat.search(text):
            score += 5
    for pat in _HIGH_RE:
        if pat.search(text):
            score += 3
    for pat in _MEDIUM_RE:
        if pat.search(text):
            score += 1
    # Baseline: every risk note has at least 1 point of signal.
    if score == 0 and text.strip():
        score = 1
    return score


def _severity_from_score(score: int) -> str:
    if score >= 9:
        return "critical"
    if score >= 5:
        return "high"
    if score >= 2:
        return "medium"
    return "low"


# --- Topic extraction ------------------------------------------------------
#
# Groups similar risk notes across files. Approach: pull a small set
# of meaningful tokens out of each note, sort them, and use the joined
# string as the topic key. Notes about "credentials hardcoded" and
# "hard-coded API token" both map to the same topic via token overlap.
#
# Not a semantic match — purely lexical. Good enough for clustering and
# robust to model output variance.

_STOPWORDS = frozenset([
    "the", "a", "an", "and", "or", "but", "for", "in", "on", "at",
    "to", "of", "is", "are", "was", "were", "be", "been", "being",
    "this", "that", "these", "those", "with", "without", "from", "by",
    "should", "could", "would", "may", "might", "can", "will",
    "if", "then", "else", "when", "where", "how", "why",
    "no", "not", "only", "some", "any", "all", "each", "every",
])

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")


def _topic_tokens(text: str) -> list[str]:
    """Pull meaningful tokens from a risk note for clustering. Lowercased,
    deduplicated, stopwords removed. Returns at most 6 tokens — past
    that the topic key becomes too specific to ever match."""
    seen: set[str] = set()
    out: list[str] = []
    for tok in _TOKEN_RE.findall(text.lower()):
        if tok in _STOPWORDS:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= 6:
            break
    return out


def _topic_key(tokens: list[str]) -> str:
    """Stable key for grouping. Sort tokens so order-variance doesn't
    create spurious clusters."""
    return "|".join(sorted(tokens))


@dataclass(frozen=True)
class AmbientFinding:
    """One project-level concern aggregated from guardian summaries."""
    digest: str            # stable hash of topic + files; doubles as artifact key suffix
    severity: str          # low | medium | high | critical
    score: int             # numeric score (higher = worse)
    title: str             # short headline (derived from highest-severity note)
    description: str       # human-readable summary
    file_paths: list[str]  # files contributing to this finding
    evidence: list[dict[str, Any]]  # per-file evidence: {path, note, score}
    detected_at: float
    dismissed_at: Optional[float] = None
    dismissed_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Core generation.
# ---------------------------------------------------------------------------

# Minimum score to surface anything. Below this we consider the note
# unactionable noise (typos, "consider adding tests", etc.).
DEFAULT_MIN_SCORE = 2

# Max findings returned per generation pass. Keeps the panel readable.
DEFAULT_MAX_FINDINGS = 25


def generate_findings(
    summaries: list[dict[str, Any]],
    *,
    min_score: int = DEFAULT_MIN_SCORE,
    max_findings: int = DEFAULT_MAX_FINDINGS,
) -> list[AmbientFinding]:
    """Scan a list of file summaries and produce ambient findings.

    Each summary is a dict with at least file_path and risk_notes
    (matching the FileSummary persistence shape). Returns findings
    sorted by score descending.

    Algorithm:
      1. Flatten all risk notes into (path, note_text, score) tuples
      2. Group by topic key (token overlap clustering)
      3. For each group, compute severity from max score and file count
      4. Build AmbientFinding objects, sorted by score desc

    No LLM call — pure heuristic over already-indexed data. Cheap to
    run on every guardian_index completion.
    """
    # Flatten.
    notes: list[dict[str, Any]] = []
    for s in summaries:
        path = s.get("file_path", "")
        if not path:
            continue
        for note in (s.get("risk_notes") or []):
            text = (note or "").strip()
            if not text:
                continue
            notes.append({
                "path": path,
                "text": text,
                "score": _score_note_text(text),
                "tokens": _topic_tokens(text),
            })

    if not notes:
        return []

    # Group by topic key. Notes with empty token lists (all stopwords)
    # bucket separately as one "miscellaneous" group keyed by the path
    # so they don't all merge.
    groups: dict[str, list[dict[str, Any]]] = {}
    for n in notes:
        key = _topic_key(n["tokens"]) if n["tokens"] else f"misc:{n['path']}"
        groups.setdefault(key, []).append(n)

    findings: list[AmbientFinding] = []
    for key, group in groups.items():
        # File-count bonus. Cross-file evidence is a strong signal —
        # one file flagged for "no rate limiting" might be the auth
        # route; three files flagged for it is a systemic issue.
        unique_paths = sorted({n["path"] for n in group})
        file_count = len(unique_paths)
        max_note_score = max(n["score"] for n in group)
        if file_count >= 3:
            score_bonus = 4
        elif file_count == 2:
            score_bonus = 2
        else:
            score_bonus = 0

        total_score = max_note_score + score_bonus
        if total_score < min_score:
            continue

        severity = _severity_from_score(total_score)

        # Title: take the highest-scoring note's first sentence (or up
        # to 120 chars). It's the most concrete representation of the
        # group.
        best = max(group, key=lambda n: n["score"])
        title = _truncate_title(best["text"])

        # Description: short blurb summarizing scope.
        if file_count == 1:
            description = (
                f"Flagged in {unique_paths[0]}: {best['text'][:160]}"
            )
        else:
            description = (
                f"Similar concern raised across {file_count} file"
                f"{'s' if file_count > 1 else ''}: "
                f"{', '.join(unique_paths[:3])}"
                f"{f' (+{file_count - 3} more)' if file_count > 3 else ''}"
            )

        evidence = [
            {
                "path": n["path"],
                "note": n["text"],
                "score": n["score"],
            }
            for n in group
        ]

        # Stable digest: sorted topic key + sorted paths. Hashing this
        # gives us a deterministic artifact-key suffix that survives
        # re-indexing — same concern won't accumulate duplicate entries.
        digest_seed = f"{key}::{':'.join(unique_paths)}"
        digest = hashlib.sha256(
            digest_seed.encode("utf-8")
        ).hexdigest()[:16]

        findings.append(AmbientFinding(
            digest=digest,
            severity=severity,
            score=total_score,
            title=title,
            description=description,
            file_paths=unique_paths,
            evidence=evidence,
            detected_at=time.time(),
        ))

    # Sort: severity tier first (critical → low), then numeric score
    # descending within tier.
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda f: (severity_rank[f.severity], -f.score))
    return findings[:max_findings]


def _truncate_title(text: str, *, limit: int = 120) -> str:
    """First sentence or up to `limit` chars, whichever shorter. Strips
    leading/trailing punctuation."""
    # Take first sentence boundary.
    m = re.search(r"^(.+?)([.!?](?:\s|$)|$)", text.strip())
    headline = m.group(1) if m else text
    headline = headline.strip(" .;:,\"'")
    if len(headline) > limit:
        headline = headline[:limit - 1].rstrip() + "…"
    return headline
