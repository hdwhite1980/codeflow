"""
codeflow.import_extractor
=========================

Parse generated source code to extract two kinds of references:

  1. **file → file imports** — e.g. ``from app.csv_utils import parse_csv``
     in ``app/main.py`` produces an edge ``app/main.py → app/csv_utils.py``.
  2. **file → service uses** — e.g. ``os.environ["DATABASE_URL"]`` in any
     file produces an edge to a service whose config contains
     ``DATABASE_URL``.

These edges populate the graph_edges table and surface in the React
Flow visualization as connecting lines.

Design choices
--------------
* **Pure function, no I/O.** The extractor takes a path + content + the
  set of project files and services, and returns lists of references.
  All ledger writes happen in the caller (build_pipeline). Easy to test.

* **Python via ast, everything else via regex.** Python's import grammar
  is rich (relative imports, star imports, multi-target imports, etc.)
  and `ast` parses it exactly. For JS/TS/Markdown/text, a tolerant regex
  catches the common cases and tolerates malformed input — the
  extractor must never crash a build because of a parse error.

* **Conservative resolution.** We only emit an edge to file X if file X
  is already in the project's known file list. Imports of external
  packages (fastapi, csv, etc.) are ignored — they're not graph nodes.

* **Service matching via config substring.** A service is "used" by a
  file if any service.config string appears verbatim in the file
  content. Crude but effective: when a user adds a Postgres service
  with config ``DATABASE_URL=postgres://...``, any file that mentions
  ``DATABASE_URL`` will be linked. We pick the longest matching
  substring to avoid false positives on common short strings.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Data shapes returned to the caller.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExtractionResult:
    """What the extractor found in one file.

    file_imports
        Project-relative paths (strings like "app/csv_utils.py") that
        this file imports from. Only paths in `known_files` are
        included — external packages are filtered out.

    service_uses
        artifact_keys (strings like "service:postgres:main-db") of
        services this file references via their config substring.
    """
    file_imports: list[str]
    service_uses: list[str]


@dataclass(frozen=True)
class ServiceRef:
    """One service as the extractor sees it: just enough to match
    its config string against file content."""
    artifact_key: str
    # The substrings to search for. Usually one (the service's config
    # string), but we split on common separators so a config like
    # "DATABASE_URL=postgres://..." matches files that reference just
    # the env var name.
    needles: tuple[str, ...]


def make_service_refs(services: Iterable[dict]) -> list[ServiceRef]:
    """Build matchable ServiceRefs from the raw service dicts the
    project stored at creation time.

    Each service input looks like:
        {"kind": "postgres", "label": "Main DB", "config": "DATABASE_URL=postgres://..."}

    The output ServiceRef needles are tried as substrings against file
    content. We extract env-var-shaped tokens from the config because
    those are the most reliable identifiers — a file mentions
    ``DATABASE_URL``, not the raw connection string."""
    refs: list[ServiceRef] = []
    for s in services:
        kind = s.get("kind", "other")
        label = s.get("label", "")
        config = s.get("config") or ""
        # Compute the artifact_key the same way app.create_project does.
        key_label = label.strip().lower().replace(" ", "-")[:60]
        artifact_key = f"service:{kind}:{key_label}"
        # Mine the config for env-var-looking tokens. Pattern matches
        # ALL_CAPS_WITH_UNDERSCORES at least 4 chars long. This catches
        # DATABASE_URL, REDIS_URL, STRIPE_SECRET_KEY, etc.
        env_vars = re.findall(r"\b[A-Z][A-Z0-9_]{3,}\b", config)
        # Also include URLs as needles when present — sometimes the
        # file references the full URL, not just an env var.
        urls = re.findall(r"https?://[^\s\"'<>]+", config)
        needles = tuple(sorted(set(env_vars + urls)))
        if not needles:
            # No identifiable needles; skip this service for matching.
            # The node still exists in the graph, just no auto edges.
            continue
        refs.append(ServiceRef(artifact_key=artifact_key, needles=needles))
    return refs


# ---------------------------------------------------------------------------
# Main entrypoint.
# ---------------------------------------------------------------------------

def extract_references(
    *,
    file_path: str,
    content: str,
    known_files: Iterable[str],
    service_refs: Iterable[ServiceRef],
) -> ExtractionResult:
    """Find all references from this file to other graph nodes.

    Parameters
    ----------
    file_path : the path of THIS file (e.g. "app/main.py"). Used for
        resolving relative imports.
    content : the file's source text.
    known_files : all file paths in this project. Imports to anything
        outside this set are ignored.
    service_refs : pre-built ServiceRefs (call make_service_refs() once
        per project and reuse across files for efficiency).

    Returns ExtractionResult with deduplicated lists.
    """
    known_set = set(known_files)
    file_imports = _extract_file_imports(file_path, content, known_set)
    service_uses = _extract_service_uses(content, list(service_refs))
    # Deduplicate while preserving discovery order so test assertions
    # are deterministic.
    return ExtractionResult(
        file_imports=_dedupe(file_imports),
        service_uses=_dedupe(service_uses),
    )


def _dedupe(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ---------------------------------------------------------------------------
# Per-language import extraction.
# ---------------------------------------------------------------------------

def _extract_file_imports(
    file_path: str, content: str, known_files: set[str],
) -> list[str]:
    """Dispatch on file extension. Python uses ast; everything else
    uses a tolerant regex pass."""
    if file_path.endswith(".py"):
        modules = _python_imported_modules(content)
        return _resolve_python_modules_to_files(modules, file_path, known_files)
    if file_path.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")):
        return _javascript_imports(content, file_path, known_files)
    # No extractor for other types yet. Return nothing rather than
    # guessing — false edges would be worse than missing edges.
    return []


# -- Python ---------------------------------------------------------

def _python_imported_modules(content: str) -> list[str]:
    """Return module names imported by this Python source.

    Handles:
      * ``import x``                  -> ['x']
      * ``import x, y``               -> ['x', 'y']
      * ``import x.y as z``           -> ['x.y']
      * ``from x import y``           -> ['x']
      * ``from x.y import z``         -> ['x.y']
      * ``from . import x``           -> ['.x']  (relative; level=1)
      * ``from .sibling import x``    -> ['.sibling']
      * ``from ..pkg import x``       -> ['..pkg']

    Relative imports are prefixed with '.' per level so the caller can
    resolve them against the file's own path.

    If the file doesn't parse (e.g. truncated by a max_tokens hit), we
    return an empty list rather than raising. The build still succeeds
    and the audit pipeline will catch the truncation."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []

    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * (node.level or 0)
            modules.append(prefix + (node.module or ""))
    return modules


def _resolve_python_modules_to_files(
    modules: list[str], from_path: str, known_files: set[str],
) -> list[str]:
    """Map module names to file paths in `known_files`.

    Resolution rules
    ----------------
    * ``a.b.c`` -> try ``a/b/c.py``, then ``a/b/c/__init__.py``,
      then ``a/b.py`` (sometimes "c" is a symbol exported from b),
      then ``a/__init__.py``. First hit wins.
    * Relative imports: count leading dots, walk up that many dirs
      from `from_path`, then resolve the remainder.
    * Modules not matching any known_files entry are dropped silently
      (they're external packages).
    """
    out: list[str] = []
    from_dir = str(PurePosixPath(from_path).parent)
    if from_dir == ".":
        from_dir = ""

    for mod in modules:
        # Resolve relative import to absolute-style.
        if mod.startswith("."):
            level = len(mod) - len(mod.lstrip("."))
            rest = mod[level:]
            # Walk up `level - 1` directories: level=1 means same dir.
            parts = from_dir.split("/") if from_dir else []
            up = max(0, level - 1)
            if up > len(parts):
                continue  # invalid relative import; skip
            base_parts = parts[: len(parts) - up] if up else parts
            base = "/".join(base_parts)
            if rest:
                resolved = f"{base}/{rest}" if base else rest
            else:
                resolved = base
        else:
            resolved = mod

        # Try candidate file paths for this dotted module.
        dotted = resolved.replace(".", "/") if resolved else ""
        candidates: list[str] = []
        if dotted:
            candidates.append(f"{dotted}.py")
            candidates.append(f"{dotted}/__init__.py")
            # Trim last component — sometimes the module name is the
            # *symbol* and the file is the parent.
            if "/" in dotted:
                parent, _ = dotted.rsplit("/", 1)
                candidates.append(f"{parent}.py")
                candidates.append(f"{parent}/__init__.py")
        for c in candidates:
            if c in known_files and c != from_path:
                out.append(c)
                break
    return out


# -- JavaScript / TypeScript ----------------------------------------

_JS_IMPORT_PATTERNS = [
    # import x from 'path'    or   import { x } from 'path'
    re.compile(r"""\bimport\b[^;\n]*?\bfrom\s+['"]([^'"]+)['"]"""),
    # import 'path'           (side-effect import)
    re.compile(r"""\bimport\s+['"]([^'"]+)['"]"""),
    # const x = require('path')
    re.compile(r"""\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
    # dynamic: import('path')
    re.compile(r"""\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
]


def _javascript_imports(
    content: str, from_path: str, known_files: set[str],
) -> list[str]:
    """Find imports in JS/TS source via regex. Only emits edges for
    relative paths that resolve to known files; bare module names
    ('react', '@radix-ui/react-slot') are ignored as external."""
    raw_imports: list[str] = []
    for pat in _JS_IMPORT_PATTERNS:
        raw_imports.extend(pat.findall(content))
    out: list[str] = []
    from_dir = str(PurePosixPath(from_path).parent)
    if from_dir == ".":
        from_dir = ""
    for spec in raw_imports:
        # External package (no leading . or /).
        if not spec.startswith((".", "/")):
            continue
        # Resolve relative to from_path's directory.
        if spec.startswith("/"):
            base = spec.lstrip("/")
        else:
            base = str(PurePosixPath(from_dir) / spec) if from_dir else spec
            # PurePosixPath collapses "./" but preserves "../" weirdly;
            # normalize.
            base = _normalize_relative(base)
        # Candidates: the literal path, plus common extension/index forms.
        for cand in _js_candidates(base):
            if cand in known_files and cand != from_path:
                out.append(cand)
                break
    return out


def _normalize_relative(p: str) -> str:
    """Collapse '..' segments. PurePosixPath does most of this for us
    but not consistently across mixed inputs, so do it manually."""
    parts: list[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


def _js_candidates(base: str) -> list[str]:
    """Generate file path candidates for a JS/TS import. JS resolution
    rules let `./foo` resolve to ./foo.ts, ./foo.tsx, ./foo/index.ts,
    etc. We check the common forms in order."""
    if not base:
        return []
    # If the base already has an extension, use it as-is.
    if base.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json")):
        return [base]
    out: list[str] = []
    for ext in ("ts", "tsx", "js", "jsx", "mjs", "cjs"):
        out.append(f"{base}.{ext}")
    for ext in ("ts", "tsx", "js", "jsx"):
        out.append(f"{base}/index.{ext}")
    return out


# ---------------------------------------------------------------------------
# Service-reference extraction.
# ---------------------------------------------------------------------------

def _extract_service_uses(
    content: str, service_refs: list[ServiceRef],
) -> list[str]:
    """Substring search of every service's needles against the file
    content. Returns artifact_keys of services that match.

    If multiple services share an env var name (rare but possible —
    two Postgres instances both labeled DATABASE_URL), we emit an edge
    to each. The user can see both and disambiguate in the side panel.
    """
    out: list[str] = []
    for ref in service_refs:
        for needle in ref.needles:
            if needle and needle in content:
                out.append(ref.artifact_key)
                break  # one hit per service is enough
    return out
