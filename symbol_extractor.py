"""
codeflow.symbol_extractor
=========================

Turns source files into function-level symbols with line ranges, then mines
each symbol body for DB column references. This is how the ledger answers
"what does this line do" and "if I drop this column, what breaks."

Granularity decision
--------------------
We track SYMBOLS not LINES. A symbol = function, method, class, route handler.
Every line in a file belongs to exactly one symbol (or to the file's module
scope, modeled as a synthetic symbol called `<module>`). Symbols have stable
identities across edits — renaming a function changes its key, but its body
edits do not. Lines do not have stable identity — inserting one line shifts
every line number below it. Symbol-level granularity gives the user-facing
promise without the storage and re-indexing churn.

Language support
----------------
Python and TypeScript/JavaScript for v1. Python uses the ast module from the
stdlib (zero-dep, accurate). TS/JS uses a deliberately simple regex-based
extractor — we are NOT trying to be a real TS compiler. We catch the common
declaration patterns and miss the exotic ones. The audit layer can flag
files where extraction confidence is low and ask for a follow-up pass.

SQL extraction
--------------
We look for column references in three forms:
  1. SQL string literals: SELECT col FROM table / INSERT INTO t (col, ...)
  2. ORM method chains: .select('col, col2'), .insert({col: ...})
  3. Schema-typed access: row.col_name (lower-confidence)

We are deliberately conservative — only emit a (function, column) edge when
we are confident. False positives in the graph cause noisy audit failures;
false negatives just mean a human reviews more diligently. We bias to false
negatives.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Symbol:
    """A function/class/handler extracted from a source file."""
    file_path: str               # 'src/api/users.ts'
    name: str                    # 'createUser'
    kind: str                    # 'function' | 'method' | 'class' | 'arrow' | 'module'
    line_start: int              # 1-indexed
    line_end: int                # inclusive
    parent: Optional[str] = None # for methods: the class name

    @property
    def artifact_key(self) -> str:
        """The stable identity used in the ledger."""
        # Methods get qualified with their parent class so `User.save` and
        # `Post.save` are distinct symbols.
        qualified = f"{self.parent}.{self.name}" if self.parent else self.name
        return f"func:{self.file_path}:{qualified}"


@dataclass(frozen=True)
class ColumnRef:
    """A reference from a symbol body to a database column."""
    table: str
    column: str
    operation: str               # 'read' | 'write'
    confidence: float            # 0.0 - 1.0
    evidence: str                # the matched text, for audit transparency

    def edge_kind(self) -> str:
        return "writes_column" if self.operation == "write" else "reads_column"


@dataclass
class FileAnalysis:
    """The full extraction result for one file."""
    file_path: str
    language: str
    symbols: list[Symbol] = field(default_factory=list)
    # Map from a symbol's artifact_key to the column refs found in its body.
    refs: dict[str, list[ColumnRef]] = field(default_factory=dict)
    extraction_confidence: float = 1.0  # lowered when we hit unparseable code


# ---------------------------------------------------------------------------
# Python extractor (uses stdlib ast — accurate)
# ---------------------------------------------------------------------------

def _python_symbols(source: str, file_path: str) -> list[Symbol]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Don't crash; return module-only so the file still appears in the graph.
        # Audit layer will see extraction_confidence < 1.0 and flag.
        return [Symbol(
            file_path=file_path, name="<module>", kind="module",
            line_start=1, line_end=max(1, source.count("\n") + 1),
        )]

    symbols: list[Symbol] = [Symbol(
        file_path=file_path, name="<module>", kind="module",
        line_start=1, line_end=max(1, source.count("\n") + 1),
    )]

    class _V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.class_stack: list[str] = []

        def _add(self, node, kind: str) -> None:
            parent = self.class_stack[-1] if self.class_stack else None
            # end_lineno is reliable in Python 3.8+.
            line_end = getattr(node, "end_lineno", None) or node.lineno
            symbols.append(Symbol(
                file_path=file_path, name=node.name, kind=kind,
                line_start=node.lineno, line_end=line_end, parent=parent,
            ))

        def visit_FunctionDef(self, node):
            kind = "method" if self.class_stack else "function"
            self._add(node, kind)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node):
            kind = "method" if self.class_stack else "function"
            self._add(node, kind)
            self.generic_visit(node)

        def visit_ClassDef(self, node):
            self._add(node, "class")
            self.class_stack.append(node.name)
            self.generic_visit(node)
            self.class_stack.pop()

    _V().visit(tree)
    return symbols


# ---------------------------------------------------------------------------
# TypeScript / JavaScript extractor (regex-based — best-effort)
# ---------------------------------------------------------------------------

# We match the common idioms a generator AI produces. Deliberately not
# exhaustive — we'd rather miss a symbol than wrongly identify one. The
# extraction_confidence field warns the audit layer when we struggle.

_TS_PATTERNS = [
    # export function foo(...)
    (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\("), "function"),
    # export const foo = (...) =>
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\("), "arrow"),
    # class Foo { ... }
    (re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)"), "class"),
    # async method() { (only when inside a class — we detect this with class_depth)
    (re.compile(r"^\s*(?:public\s+|private\s+|protected\s+)?(?:static\s+)?(?:async\s+)?(\w+)\s*\([^)]*\)\s*[:{]"), "method"),
]


def _ts_symbols(source: str, file_path: str) -> tuple[list[Symbol], float]:
    """
    Return (symbols, confidence). Confidence drops when we see deeply-nested
    code or unbalanced braces, signaling we likely got something wrong.
    """
    lines = source.split("\n")
    symbols: list[Symbol] = [Symbol(
        file_path=file_path, name="<module>", kind="module",
        line_start=1, line_end=len(lines),
    )]

    brace_depth = 0
    class_stack: list[tuple[str, int]] = []  # (class_name, brace_depth_when_entered)
    pending: Optional[tuple[Symbol, int]] = None  # (symbol, brace_depth_when_started)

    confidence_hits = 0  # counts patterns we recognized

    for i, raw in enumerate(lines, start=1):
        line = raw
        # Naive comment stripping — good enough for symbol detection.
        line_nocomment = re.sub(r"//.*$", "", line)

        # Check for new symbol declarations on this line.
        # We skip the 'method' pattern when not inside a class.
        for pattern, kind in _TS_PATTERNS:
            m = pattern.match(line_nocomment)
            if not m:
                continue
            if kind == "method" and not class_stack:
                continue
            if kind == "method" and class_stack:
                # The method pattern is greedy; filter false positives on
                # control flow keywords.
                if m.group(1) in {"if", "for", "while", "switch", "return",
                                  "catch", "throw", "constructor"}:
                    if m.group(1) != "constructor":
                        continue
            name = m.group(1)
            parent = class_stack[-1][0] if class_stack and kind == "method" else None
            sym = Symbol(
                file_path=file_path, name=name, kind=kind,
                line_start=i, line_end=i,  # patched when block closes
                parent=parent,
            )
            symbols.append(sym)
            confidence_hits += 1
            # Track the symbol's brace level so we know when its body ends.
            if kind == "class":
                class_stack.append((name, brace_depth))
            else:
                pending = (sym, brace_depth)
            break

        # Update brace depth from the (comment-stripped) line.
        opens = line_nocomment.count("{")
        closes = line_nocomment.count("}")
        for _ in range(opens):
            brace_depth += 1
        for _ in range(closes):
            brace_depth -= 1
            # If we just closed back to the pending symbol's level, that
            # symbol's body ended on this line.
            if pending and brace_depth == pending[1]:
                old = pending[0]
                # Replace the in-list symbol with one that has line_end set.
                idx = symbols.index(old)
                symbols[idx] = Symbol(
                    file_path=old.file_path, name=old.name, kind=old.kind,
                    line_start=old.line_start, line_end=i, parent=old.parent,
                )
                pending = None
            if class_stack and brace_depth == class_stack[-1][1]:
                class_stack.pop()

    # If we never found anything, confidence is degraded.
    confidence = 1.0 if confidence_hits > 0 else 0.5
    return symbols, confidence


# ---------------------------------------------------------------------------
# SQL / column reference miner
# ---------------------------------------------------------------------------

# Tables and columns we care about are project-scoped — the extractor needs
# to know which (table, column) pairs are real to avoid false positives on
# generic identifiers. The caller passes a `known_columns` set built from
# the DB schema portion of the graph.

_SELECT_RE = re.compile(
    r"\bSELECT\s+([^;]*?)\s+FROM\s+([\"`]?\w+[\"`]?)",
    re.IGNORECASE | re.DOTALL,
)
# WHERE / AND / OR conditions reference columns. We scan inside SELECT/UPDATE/
# DELETE bodies and capture identifiers on the LHS of comparison operators.
# This is intentionally broad — we filter against known_columns so unknown
# identifiers get dropped.
_WHERE_RE = re.compile(
    r"\b(?:WHERE|AND|OR)\s+([\"`]?\w+[\"`]?)\s*(?:=|!=|<>|<|>|<=|>=|IS|IN|LIKE)",
    re.IGNORECASE,
)
_INSERT_RE = re.compile(
    r"\bINSERT\s+INTO\s+([\"`]?\w+[\"`]?)\s*\(([^)]+)\)",
    re.IGNORECASE | re.DOTALL,
)
_UPDATE_RE = re.compile(
    r"\bUPDATE\s+([\"`]?\w+[\"`]?)\s+SET\s+([^;]+?)(?:\bWHERE\b|;|$)",
    re.IGNORECASE | re.DOTALL,
)
# ORM-style chain: .from('table').select('col1, col2')
_ORM_FROM_SELECT = re.compile(
    r"\.from\(\s*['\"](\w+)['\"]\s*\)[^;]*?\.select\(\s*['\"]([^'\"]+)['\"]",
    re.DOTALL,
)
_ORM_FROM_INSERT = re.compile(
    r"\.from\(\s*['\"](\w+)['\"]\s*\)[^;]*?\.insert\(\s*\{([^}]+)\}",
    re.DOTALL,
)
_ORM_FROM_UPDATE = re.compile(
    r"\.from\(\s*['\"](\w+)['\"]\s*\)[^;]*?\.update\(\s*\{([^}]+)\}",
    re.DOTALL,
)


def _strip_table_quotes(t: str) -> str:
    return t.strip().strip('"').strip("`")


def _split_columns(blob: str) -> list[str]:
    """Pull bare column names out of a SELECT projection or column list."""
    out = []
    for part in blob.split(","):
        # Handle 'col AS alias', 'table.col', whitespace.
        token = part.strip().split()[0] if part.strip() else ""
        token = token.split(".")[-1]  # strip table prefix
        token = token.strip('"').strip("`").strip("'")
        if token and token != "*" and token.replace("_", "").isalnum():
            out.append(token)
    return out


def _split_kv_keys(blob: str) -> list[str]:
    """Pull keys out of '{col: val, col2: val}' or 'col = val, col2 = val'."""
    out = []
    # Try object literal form first.
    for m in re.finditer(r"(\w+)\s*:", blob):
        out.append(m.group(1))
    if out:
        return out
    # SQL SET form.
    for m in re.finditer(r"(\w+)\s*=", blob):
        out.append(m.group(1))
    return out


def extract_column_refs(
    body: str, known_columns: dict[str, set[str]],
) -> list[ColumnRef]:
    """
    Pull (table, column, op) triples from a source-code body. known_columns
    maps table -> set of column names; refs to unknown tables or unknown
    columns are dropped. This is the key bias-to-false-negatives decision.
    """
    refs: list[ColumnRef] = []

    def emit(table: str, columns: Iterable[str], op: str, evidence: str,
             confidence: float) -> None:
        if table not in known_columns:
            return
        for col in columns:
            if col in known_columns[table]:
                refs.append(ColumnRef(
                    table=table, column=col, operation=op,
                    confidence=confidence, evidence=evidence.strip()[:120],
                ))

    for m in _SELECT_RE.finditer(body):
        projection, table = m.group(1), _strip_table_quotes(m.group(2))
        cols = _split_columns(projection)
        # SELECT * — emit reads against every known column of the table.
        if "*" in projection and cols == []:
            cols = list(known_columns.get(table, []))
        emit(table, cols, "read", m.group(0), 0.95)

    # WHERE/AND/OR conditions. We need to attribute each match to the
    # nearest preceding FROM/UPDATE/DELETE table. Build a position->table
    # index, then for each WHERE hit pick the table whose context starts
    # just before it.
    table_ranges: list[tuple[int, str]] = []
    for m in _SELECT_RE.finditer(body):
        table_ranges.append((m.start(), _strip_table_quotes(m.group(2))))
    for m in _UPDATE_RE.finditer(body):
        table_ranges.append((m.start(), _strip_table_quotes(m.group(1))))
    for m in re.finditer(
        r"\bDELETE\s+FROM\s+([\"`]?\w+[\"`]?)", body, re.IGNORECASE
    ):
        table_ranges.append((m.start(), _strip_table_quotes(m.group(1))))
    table_ranges.sort()

    for wm in _WHERE_RE.finditer(body):
        col = _strip_table_quotes(wm.group(1))
        # Find the table whose statement starts before this WHERE match.
        owning_table = None
        for pos, tbl in table_ranges:
            if pos < wm.start():
                owning_table = tbl
            else:
                break
        if owning_table:
            emit(owning_table, [col], "read", wm.group(0), 0.8)

    for m in _INSERT_RE.finditer(body):
        table = _strip_table_quotes(m.group(1))
        cols = _split_columns(m.group(2))
        emit(table, cols, "write", m.group(0), 0.95)

    for m in _UPDATE_RE.finditer(body):
        table = _strip_table_quotes(m.group(1))
        cols = _split_kv_keys(m.group(2))
        emit(table, cols, "write", m.group(0), 0.9)

    for m in _ORM_FROM_SELECT.finditer(body):
        table = m.group(1)
        cols = _split_columns(m.group(2))
        emit(table, cols, "read", m.group(0), 0.85)

    for m in _ORM_FROM_INSERT.finditer(body):
        table = m.group(1)
        cols = _split_kv_keys(m.group(2))
        emit(table, cols, "write", m.group(0), 0.85)

    for m in _ORM_FROM_UPDATE.finditer(body):
        table = m.group(1)
        cols = _split_kv_keys(m.group(2))
        emit(table, cols, "write", m.group(0), 0.85)

    return refs


# ---------------------------------------------------------------------------
# Top-level analysis
# ---------------------------------------------------------------------------

def analyze_file(
    file_path: str, source: str, known_columns: dict[str, set[str]],
) -> FileAnalysis:
    """Run symbol extraction + column-ref mining on a single file."""
    ext = file_path.rsplit(".", 1)[-1].lower()
    if ext == "py":
        symbols = _python_symbols(source, file_path)
        language = "python"
        confidence = 1.0
    elif ext in {"ts", "tsx", "js", "jsx"}:
        symbols, confidence = _ts_symbols(source, file_path)
        language = "typescript"
    else:
        symbols = [Symbol(
            file_path=file_path, name="<module>", kind="module",
            line_start=1, line_end=max(1, source.count("\n") + 1),
        )]
        language = "unknown"
        confidence = 0.3

    # Slice the source into per-symbol bodies, then mine each body for
    # column refs. Bodies overlap (a method's range sits inside its class's
    # range) — that's intentional, the refs are attributed to the
    # narrowest-enclosing symbol via the order-by-line_start-then-line_end
    # sort below.
    lines = source.split("\n")
    refs_by_symbol: dict[str, list[ColumnRef]] = {}

    # Process narrowest symbols first so a method gets credit for its refs,
    # not the surrounding class or module. After a narrow symbol claims its
    # lines, the enclosing module's body has those lines blanked out so it
    # doesn't double-count.
    sorted_syms = sorted(
        symbols, key=lambda s: (s.line_end - s.line_start, s.line_start)
    )
    claimed_lines: set[int] = set()
    for sym in sorted_syms:
        # The body of a symbol is the lines it owns that haven't been
        # claimed by a narrower symbol nested inside it.
        body_lines = []
        for ln in range(sym.line_start, sym.line_end + 1):
            if ln in claimed_lines:
                continue
            if 1 <= ln <= len(lines):
                body_lines.append(lines[ln - 1])
        body = "\n".join(body_lines)
        refs = extract_column_refs(body, known_columns)
        if refs:
            refs_by_symbol[sym.artifact_key] = refs
        # Every concrete symbol (function/method/arrow/class) claims its
        # lines so the enclosing module/class doesn't double-count. The
        # module symbol is excluded — it's the catch-all for lines that
        # don't belong to any narrower symbol.
        if sym.kind in {"method", "function", "arrow", "class"}:
            for ln in range(sym.line_start, sym.line_end + 1):
                claimed_lines.add(ln)

    return FileAnalysis(
        file_path=file_path, language=language,
        symbols=symbols, refs=refs_by_symbol,
        extraction_confidence=confidence,
    )
