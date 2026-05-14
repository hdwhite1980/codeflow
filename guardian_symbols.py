"""
codeflow.guardian_symbols
=========================

Symbol-grained source extraction for guardian indexing (Turn B).

Pulls function/class/method/constant symbols out of source files so the
guardian can produce per-symbol semantic summaries instead of just one
summary per file.

Distinct from `symbol_extractor.py` which extracts symbols for SQL column
tracking. Both deal with "symbols in source code" but for different
downstream uses. This module's output feeds Ollama prompts; that one
feeds the structural graph.

Supported languages
-------------------
Python uses stdlib `ast`. Everything else uses tree-sitter via the
tree-sitter-languages package, which ships pre-compiled grammars:

    Language     | tag(s)             | typical kinds
    -------------|--------------------|-----------------------------------
    Python       | python             | function, class, method, constant
    TypeScript   | typescript, tsx    | function, class, method, constant
    JavaScript   | javascript, jsx    | function, class, method, constant
    Go           | go                 | function, method, constant
    Rust         | rust               | function, struct, enum, impl_method
    Java         | java               | class, method, interface, constant
    C            | c                  | function
    C++          | cpp                | function, class, method
    Ruby         | ruby               | method, class, module
    PHP          | php                | function, class, method, constant

Languages NOT covered (and why)
-------------------------------
- PowerShell: tree-sitter grammar exists but isn't in tree-sitter-languages.
  Adding it requires shipping a custom grammar build. Pending separate work.
- KQL: no tree-sitter grammar. File-level only.
- bash: tree-sitter grammar exists but bash "symbols" are arguable.
  Functions are extractable but file-level is usually the right granularity.
- AppleScript: no maintained tree-sitter grammar.
- SQL: see KQL. Different shape (queries, views, procs) — file-level wins.

For any unsupported language `extract_guardian_symbols` returns `[]`,
falling back to Turn A file-level indexing.

Symbol shape
------------
GuardianSymbol(kind, name, qualified_name, start_line, end_line, signature)
  - kind: language-appropriate ("function", "method", "class", "constant",
          "struct", "enum", "interface", "module", ...)
  - name: short identifier
  - qualified_name: e.g. "AuthService.verify_password" or "MyImpl::method"
  - start_line / end_line: 1-indexed, inclusive
  - signature: first source line of the def, trimmed
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class GuardianSymbol:
    """One extracted symbol. Positions are 1-indexed inclusive."""
    kind: str
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    signature: str


# ---------------------------------------------------------------------------
# Python — stdlib `ast`. No tree-sitter dep needed.
# ---------------------------------------------------------------------------

def extract_symbols_python(content: str) -> list[GuardianSymbol]:
    """Extract top-level functions, classes, methods, and constants."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []

    lines = content.splitlines()
    symbols: list[GuardianSymbol] = []

    def _sig(node) -> str:
        if not hasattr(node, "lineno") or node.lineno - 1 >= len(lines):
            return ""
        return lines[node.lineno - 1].strip()[:200]

    def _end(node) -> int:
        return getattr(node, "end_lineno", node.lineno) or node.lineno

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(GuardianSymbol(
                kind="function", name=node.name,
                qualified_name=node.name,
                start_line=node.lineno, end_line=_end(node),
                signature=_sig(node),
            ))
        elif isinstance(node, ast.ClassDef):
            symbols.append(GuardianSymbol(
                kind="class", name=node.name,
                qualified_name=node.name,
                start_line=node.lineno, end_line=_end(node),
                signature=_sig(node),
            ))
            for inner in node.body:
                if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(GuardianSymbol(
                        kind="method", name=inner.name,
                        qualified_name=f"{node.name}.{inner.name}",
                        start_line=inner.lineno, end_line=_end(inner),
                        signature=_sig(inner),
                    ))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    symbols.append(GuardianSymbol(
                        kind="constant", name=target.id,
                        qualified_name=target.id,
                        start_line=node.lineno, end_line=_end(node),
                        signature=_sig(node),
                    ))

    return symbols


# ---------------------------------------------------------------------------
# Per-language tree-sitter queries.
# ---------------------------------------------------------------------------
#
# Each query captures @<role>.name for the identifier and (optionally)
# @<role>.def_node for the enclosing definition. The extractor walks up
# from the name to find the def for line numbers; capture names tell us
# what kind to emit.
#
# Defining queries per-language lets us tailor to each grammar's node
# names. Common patterns:
#   - "function declarations" → top-level functions
#   - "method definitions" → inside class bodies
#   - "class/struct/interface declarations"
#   - "uppercase constant bindings" (per-language convention)

# TypeScript + TSX share the same grammar with minor differences.
_QUERY_TYPESCRIPT = r"""
(program (function_declaration name: (identifier) @function.name))
(program (class_declaration name: (_) @class.name))
(program (interface_declaration name: (_) @interface.name))
(program
  (lexical_declaration
    (variable_declarator
      name: (identifier) @const_fn.name
      value: [(arrow_function) (function)])))
(program
  (lexical_declaration
    (variable_declarator name: (identifier) @value.name)))
(class_body (method_definition name: (property_identifier) @method.name))
"""

# JavaScript / JSX — similar to TS but no interface_declaration.
_QUERY_JAVASCRIPT = r"""
(program (function_declaration name: (identifier) @function.name))
(program (class_declaration name: (_) @class.name))
(program
  (lexical_declaration
    (variable_declarator
      name: (identifier) @const_fn.name
      value: [(arrow_function) (function)])))
(program
  (lexical_declaration
    (variable_declarator name: (identifier) @value.name)))
(class_body (method_definition name: (property_identifier) @method.name))
"""

# Go — has function_declaration for funcs, method_declaration for
# methods with a receiver, and uppercase-name const declarations.
_QUERY_GO = r"""
(source_file (function_declaration name: (identifier) @function.name))
(source_file (method_declaration name: (field_identifier) @method.name))
(source_file (type_declaration (type_spec name: (type_identifier) @type.name)))
(source_file (const_declaration (const_spec name: (identifier) @value.name)))
(source_file (var_declaration (var_spec name: (identifier) @value.name)))
"""

# Rust — fn, struct, enum, trait, impl items. impl methods are inside
# `impl_item.declaration_list.function_item`.
_QUERY_RUST = r"""
(source_file (function_item name: (identifier) @function.name))
(source_file (struct_item name: (type_identifier) @struct.name))
(source_file (enum_item name: (type_identifier) @enum.name))
(source_file (trait_item name: (type_identifier) @trait.name))
(source_file (const_item name: (identifier) @value.name))
(source_file (static_item name: (identifier) @value.name))
(declaration_list (function_item name: (identifier) @method.name))
"""

# Java — class, interface, enum at top level; methods inside class_body.
# Constants conventionally are public static final UPPERCASE fields.
_QUERY_JAVA = r"""
(program (class_declaration name: (identifier) @class.name))
(program (interface_declaration name: (identifier) @interface.name))
(program (enum_declaration name: (identifier) @enum.name))
(class_body (method_declaration name: (identifier) @method.name))
(interface_body (method_declaration name: (identifier) @interface_method.name))
(class_body (field_declaration declarator: (variable_declarator name: (identifier) @value.name)))
"""

# C — just functions. Structs and typedefs are namespaced separately
# (we don't index them as symbols; they're more like types in our model).
_QUERY_C = r"""
(translation_unit
  (function_definition
    declarator: (function_declarator declarator: (identifier) @function.name)))
"""

# C++ — functions, classes, methods, namespace-scoped functions.
_QUERY_CPP = r"""
(translation_unit
  (function_definition
    declarator: (function_declarator declarator: (identifier) @function.name)))
(translation_unit
  (class_specifier name: (type_identifier) @class.name))
(translation_unit
  (struct_specifier name: (type_identifier) @struct.name))
(field_declaration_list
  (function_definition
    declarator: (function_declarator declarator: (field_identifier) @method.name)))
"""

# Ruby — methods at file scope or inside class/module bodies. Classes
# and modules are also captured.
_QUERY_RUBY = r"""
(program (method name: (identifier) @function.name))
(program (class name: (constant) @class.name))
(program (module name: (constant) @module.name))
(body_statement (method name: (identifier) @method.name))
(body_statement (class name: (constant) @class.name))
"""

# PHP — namespaced or top-level functions, classes, methods.
_QUERY_PHP = r"""
(program (function_definition name: (name) @function.name))
(program (class_declaration name: (name) @class.name))
(program (interface_declaration name: (name) @interface.name))
(declaration_list (method_declaration name: (name) @method.name))
(program (const_declaration (const_element (name) @value.name)))
"""


# Map tree-sitter-languages tag → (query, language_id_for_ts_languages).
# The second element matters because some languages have different ts
# tags than the natural language name (tsx vs typescript share the same
# query but use a different grammar).
_TS_LANG_TABLE: dict[str, tuple[str, str]] = {
    "typescript": (_QUERY_TYPESCRIPT, "typescript"),
    "tsx":        (_QUERY_TYPESCRIPT, "tsx"),
    "javascript": (_QUERY_JAVASCRIPT, "javascript"),
    "jsx":        (_QUERY_JAVASCRIPT, "javascript"),
    "go":         (_QUERY_GO, "go"),
    "rust":       (_QUERY_RUST, "rust"),
    "java":       (_QUERY_JAVA, "java"),
    "c":          (_QUERY_C, "c"),
    "cpp":        (_QUERY_CPP, "cpp"),
    "ruby":       (_QUERY_RUBY, "ruby"),
    "php":        (_QUERY_PHP, "php"),
}


# Capture name → canonical kind name we emit on GuardianSymbol.
# Some capture roles produce constants only conditionally (uppercase).
_KIND_MAP = {
    "function":          "function",
    "const_fn":          "function",   # arrow funcs assigned to const
    "method":            "method",
    "interface_method":  "method",
    "class":             "class",
    "interface":         "interface",
    "enum":              "enum",
    "struct":            "struct",
    "trait":             "trait",
    "module":            "module",
    "type":              "type",
    # "value" is conditional — only constant if uppercase name.
    "value":             "constant",
}

# Capture roles whose definition we walk up to. Maps role → enclosing
# AST node types (tree-sitter grammar-specific). The walker stops at
# the first ancestor whose .type is in the set.
_DEF_NODE_TYPES = {
    "function":          {"function_declaration", "function_definition",
                          "function_item", "method"},
    "const_fn":          {"lexical_declaration"},
    "method":            {"method_definition", "method_declaration",
                          "function_item", "method", "function_definition"},
    "interface_method":  {"method_signature", "method_declaration"},
    "class":             {"class_declaration", "class_specifier",
                          "class", "class_specifier"},
    "interface":         {"interface_declaration"},
    "enum":              {"enum_declaration", "enum_item"},
    "struct":            {"struct_item", "struct_specifier"},
    "trait":             {"trait_item"},
    "module":            {"module"},
    "type":              {"type_declaration", "type_spec"},
    "value":             {"lexical_declaration", "const_declaration",
                          "var_declaration", "const_item", "static_item",
                          "field_declaration", "const_declaration",
                          "const_element"},
}


def _extract_via_treesitter(
    content: str, *, ts_language: str, query_source: str,
) -> list[GuardianSymbol]:
    """Run a tree-sitter query against `content` and turn captures into
    GuardianSymbols. Returns [] on any failure (missing dep, grammar
    error, parse error) so callers fall back to file-level summaries."""
    try:
        from tree_sitter_languages import get_language, get_parser
    except ImportError:
        return []

    try:
        ts_lang = get_language(ts_language)
        parser = get_parser(ts_language)
        tree = parser.parse(content.encode("utf-8"))
        query = ts_lang.query(query_source)
        captures = query.captures(tree.root_node)
    except Exception:
        # Grammar version mismatch, exotic syntax the grammar can't
        # parse, truncated file, etc. Quietly degrade to file-level.
        return []

    lines = content.splitlines()
    symbols: list[GuardianSymbol] = []
    seen: set[tuple[str, str, int]] = set()

    for node, capture_name in captures:
        if not capture_name.endswith(".name"):
            continue
        role = capture_name.split(".", 1)[0]
        try:
            name_text = node.text.decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            continue

        # Walk up to the enclosing definition for line numbers.
        target_types = _DEF_NODE_TYPES.get(role, set())
        ancestor = node.parent
        while ancestor and ancestor.type not in target_types:
            ancestor = ancestor.parent
        if ancestor is None:
            continue

        # Conditional constant emission: a top-level lexical/value
        # capture is only emitted if the name is UPPER_CASE. Otherwise
        # it's just a let/var binding and clutters the index.
        if role == "value" and not name_text.isupper():
            continue

        kind = _KIND_MAP.get(role)
        if kind is None:
            continue

        start_line = ancestor.start_point[0] + 1
        end_line = ancestor.end_point[0] + 1
        sig = (
            lines[start_line - 1].strip()[:200]
            if 0 <= start_line - 1 < len(lines) else ""
        )

        qualified = name_text
        if kind == "method":
            # Go's method_declaration carries the receiver as a child
            # (parameter_list > parameter_declaration > type: ...), NOT
            # as an enclosing container. Detect Go shape first and pull
            # the type name from the receiver. Other languages keep the
            # container-walk path below.
            if ancestor.type == "method_declaration" and ts_language == "go":
                receiver = None
                for child in ancestor.children:
                    if child.type == "parameter_list":
                        receiver = child
                        break
                if receiver is not None:
                    type_name = _go_extract_receiver_type(receiver)
                    if type_name:
                        qualified = f"{type_name}.{name_text}"
            else:
                # Walk up to the enclosing class/struct/impl/etc.
                container = ancestor.parent
                container_kinds = {
                    "class_declaration", "class_specifier", "class",
                    "struct_item", "impl_item", "interface_declaration",
                    "module",
                }
                while container and container.type not in container_kinds:
                    container = container.parent
                if container is not None:
                    for child in container.children:
                        if child.type in (
                            "type_identifier", "identifier",
                            "constant",  # Ruby constant for class name
                            "name",      # PHP
                        ):
                            try:
                                cls_name = child.text.decode("utf-8")
                                qualified = f"{cls_name}.{name_text}"
                            except (AttributeError, UnicodeDecodeError):
                                pass
                            break

        key = (kind, qualified, start_line)
        if key in seen:
            continue
        seen.add(key)

        symbols.append(GuardianSymbol(
            kind=kind, name=name_text, qualified_name=qualified,
            start_line=start_line, end_line=end_line, signature=sig,
        ))

    return symbols


# ---------------------------------------------------------------------------
# Dispatch.
# ---------------------------------------------------------------------------

def _go_extract_receiver_type(receiver_list_node) -> str:
    """Pull the receiver type name from a Go method's receiver list.

    The grammar shape is:
        parameter_list
          parameter_declaration
            name: (identifier)
            type: (pointer_type (type_identifier))  -- for `*Foo`
            type: (type_identifier)                 -- for `Foo`

    We walk down to find the bare type_identifier text. Returns "" on
    any structural surprise so the caller falls back to bare name.
    """
    for decl in receiver_list_node.children:
        if decl.type != "parameter_declaration":
            continue
        for child in decl.children:
            if child.type == "type_identifier":
                try:
                    return child.text.decode("utf-8")
                except (AttributeError, UnicodeDecodeError):
                    return ""
            if child.type == "pointer_type":
                # Walk one level deeper for the inner type_identifier.
                for inner in child.children:
                    if inner.type == "type_identifier":
                        try:
                            return inner.text.decode("utf-8")
                        except (AttributeError, UnicodeDecodeError):
                            return ""
    return ""

def _make_treesitter_extractor(
    ts_language: str, query: str,
) -> Callable[[str], list[GuardianSymbol]]:
    def extractor(content: str) -> list[GuardianSymbol]:
        return _extract_via_treesitter(
            content, ts_language=ts_language, query_source=query,
        )
    extractor.__name__ = f"extract_symbols_{ts_language}"
    return extractor


_EXTRACTORS: dict[str, Callable[[str], list[GuardianSymbol]]] = {
    "python": extract_symbols_python,
}
# Wire each tree-sitter language as its own dispatch entry.
for tag, (query, ts_lang) in _TS_LANG_TABLE.items():
    _EXTRACTORS[tag] = _make_treesitter_extractor(ts_lang, query)


def extract_guardian_symbols(
    content: str, language: str,
) -> list[GuardianSymbol]:
    """Extract symbols for any supported language. Returns [] for
    unsupported languages or on parse errors. The empty-list case is
    the signal for callers to fall back to file-level indexing."""
    extractor = _EXTRACTORS.get(language.lower())
    if extractor is None:
        return []
    return extractor(content)


def supported_symbol_languages() -> list[str]:
    """Diagnostic helper. Returns the language tags this module
    knows how to extract symbols from."""
    return sorted(_EXTRACTORS.keys())


def extract_symbol_body(content: str, symbol: GuardianSymbol) -> str:
    """Return the source lines making up one symbol. Used by the
    indexer to feed each symbol to the model individually."""
    lines = content.splitlines()
    if not lines:
        return ""
    start = max(0, symbol.start_line - 1)
    end = min(len(lines), symbol.end_line)
    return "\n".join(lines[start:end])
