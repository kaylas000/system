"""
Source code parsers (``specs/04_knowledge/ingestion/PARSERS.py``; rewritten by the agent).

The spec uses ``tree_sitter_languages``, which is incompatible with ``tree-sitter>=0.22``
(ISSUES KN-02). Here each grammar comes from its own wheel (``tree-sitter-typescript``,
``tree-sitter-python``, ``tree-sitter-go``) and the syntax tree is walked directly instead of
using version-specific query APIs. Unsupported languages (or a missing grammar) produce a
``ParsedFile`` without symbols; the chunker then emits a whole-file chunk.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from pathlib import PurePosixPath
from typing import Any

from ..models import ImportRef, ParsedFile, Symbol, SymbolType

logger = logging.getLogger(__name__)

EXTENSION_LANGUAGE: dict[str, str] = {
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".py": "python",
    ".go": "go",
}

MAX_SYMBOL_CODE_CHARS = 12_000  # very long symbols are truncated in the chunk (full code stays in the repo)


def detect_language(path: str) -> str | None:
    return EXTENSION_LANGUAGE.get(PurePosixPath(path).suffix.lower())


def _load_language(name: str) -> Any | None:
    try:
        from tree_sitter import Language

        if name == "typescript":
            import tree_sitter_typescript as ts_ts

            return Language(ts_ts.language_typescript())
        if name in ("tsx", "javascript"):
            import tree_sitter_typescript as ts_ts

            return Language(ts_ts.language_tsx())  # TSX grammar parses plain JS/JSX as well
        if name == "python":
            import tree_sitter_python as ts_py

            return Language(ts_py.language())
        if name == "go":
            import tree_sitter_go as ts_go

            return Language(ts_go.language())
    except ImportError as exc:  # pragma: no cover - depends on installed extras
        logger.warning("tree-sitter grammar for %s unavailable: %s", name, exc)
    return None


class _Src:
    """Source bytes + helpers (tree-sitter offsets are byte offsets)."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def text(self, node: Any) -> str:
        if node is None:
            return ""
        return self.data[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _walk(node: Any) -> Iterator[Any]:
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        stack.extend(reversed(cur.children))


def _first_line(code: str, limit: int = 240) -> str:
    line = code.strip().splitlines()[0] if code.strip() else ""
    return line[:limit]


def _signature_until_body(src: _Src, node: Any, body: Any | None) -> str:
    if body is None:
        return _first_line(src.text(node))
    raw = src.data[node.start_byte : body.start_byte].decode("utf-8", errors="replace")
    return " ".join(raw.split())[:400]


def _clean_comment(text: str) -> str:
    text = text.strip()
    if text.startswith("/**") or text.startswith("/*"):
        text = text[3:] if text.startswith("/**") else text[2:]
        text = text[:-2] if text.endswith("*/") else text
        lines = [ln.strip().lstrip("*").strip() for ln in text.splitlines()]
        return "\n".join(ln for ln in lines if ln).strip()
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln.lstrip("/#").strip() for ln in lines).strip()


def _leading_comment(src: _Src, node: Any) -> str | None:
    """Contiguous comment block right before ``node`` (JSDoc / ``//`` / Go ``//``)."""
    parts: list[str] = []
    prev = node.prev_sibling
    expected_row = node.start_point[0]
    while prev is not None and prev.type == "comment" and prev.end_point[0] >= expected_row - 1:
        parts.append(src.text(prev))
        expected_row = prev.start_point[0]
        prev = prev.prev_sibling
    if not parts:
        return None
    return _clean_comment("\n".join(reversed(parts))) or None


def _is_pascal(name: str) -> bool:
    return bool(name) and name[0].isupper()


def _is_hook(name: str) -> bool:
    return bool(re.match(r"^use[A-Z0-9]", name))


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


# =============================================================================
# TypeScript / TSX / JavaScript
# =============================================================================

_TS_FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}


class _TSExtractor:
    def __init__(self, src: _Src, path: str) -> None:
        self.src = src
        self.path = path
        self.symbols: list[Symbol] = []
        self.imports: list[ImportRef] = []

    # --- calls -----------------------------------------------------------------
    def calls_in(self, node: Any) -> list[str]:
        names: list[str] = []
        for n in _walk(node):
            if n.type == "call_expression":
                fn = n.child_by_field_name("function")
                names.extend(self._callee_names(fn))
            elif n.type == "new_expression":
                ctor = n.child_by_field_name("constructor")
                if ctor is not None and ctor.type == "identifier":
                    names.append(self.src.text(ctor))
            elif n.type in ("jsx_opening_element", "jsx_self_closing_element"):
                name = n.child_by_field_name("name")
                text = self.src.text(name)
                if text and _is_pascal(text.split(".")[-1]):
                    names.append(text.split(".")[-1])
        return _dedupe(names)

    def _callee_names(self, fn: Any) -> list[str]:
        if fn is None:
            return []
        if fn.type == "identifier":
            return [self.src.text(fn)]
        if fn.type == "member_expression":
            prop = fn.child_by_field_name("property")
            return [self.src.text(prop)] if prop is not None else []
        return []

    # --- top level -------------------------------------------------------------
    def run(self, root: Any) -> None:
        for child in root.children:
            self.top_level(child, exported=False, doc_anchor=child)

    def top_level(self, node: Any, exported: bool, doc_anchor: Any) -> None:
        t = node.type
        if t == "import_statement":
            self.import_stmt(node)
        elif t == "export_statement":
            source = node.child_by_field_name("source")
            if source is not None:  # export { a } from "./x"  -> dependency edge
                self.imports.append(
                    ImportRef(source=self._unquote(self.src.text(source)), line=node.start_point[0] + 1)
                )
            decl = node.child_by_field_name("declaration")
            if decl is not None:
                self.top_level(decl, exported=True, doc_anchor=node)
            else:
                for c in node.children:
                    if c.type in _TS_FUNCTION_VALUES or c.type in ("class", "class_declaration"):
                        self.top_level(c, exported=True, doc_anchor=node)
        elif t in ("function_declaration", "generator_function_declaration"):
            self.function(node, exported, doc_anchor)
        elif t in ("lexical_declaration", "variable_declaration"):
            self.variables(node, exported, doc_anchor)
        elif t in ("class_declaration", "abstract_class_declaration", "class"):
            self.klass(node, exported, doc_anchor)
        elif t == "interface_declaration":
            self.simple(node, SymbolType.INTERFACE, exported, doc_anchor)
        elif t == "type_alias_declaration":
            self.simple(node, SymbolType.TYPE_ALIAS, exported, doc_anchor)
        elif t == "enum_declaration":
            self.simple(node, SymbolType.ENUM, exported, doc_anchor)

    @staticmethod
    def _unquote(s: str) -> str:
        return s.strip().strip("'\"`")

    def import_stmt(self, node: Any) -> None:
        source = node.child_by_field_name("source")
        ref = ImportRef(source=self._unquote(self.src.text(source)), line=node.start_point[0] + 1)
        for c in node.children:
            if c.type != "import_clause":
                continue
            for part in c.children:
                if part.type == "identifier":
                    ref.names.append(self.src.text(part))
                    ref.is_default = True
                elif part.type == "namespace_import":
                    ref.names.append("*")
                elif part.type == "named_imports":
                    for spec in part.children:
                        if spec.type == "import_specifier":
                            alias = spec.child_by_field_name("alias")
                            name = spec.child_by_field_name("name")
                            ref.names.append(self.src.text(alias or name))
        self.imports.append(ref)

    def _make(
        self,
        node: Any,
        name: str,
        stype: SymbolType,
        exported: bool,
        doc_anchor: Any,
        signature: str,
        parent: str | None = None,
        calls: list[str] | None = None,
        bases: list[str] | None = None,
        implements: list[str] | None = None,
        decorators: list[str] | None = None,
    ) -> Symbol:
        code_node = doc_anchor if doc_anchor is not None and doc_anchor.type == "export_statement" else node
        code = self.src.text(code_node)[:MAX_SYMBOL_CODE_CHARS]
        sym = Symbol(
            name=name,
            type=stype,
            file_path=self.path,
            start_line=code_node.start_point[0] + 1,
            end_line=code_node.end_point[0] + 1,
            code=code,
            signature=signature,
            docstring=_leading_comment(self.src, doc_anchor if doc_anchor is not None else node),
            parent=parent,
            exported=exported,
            calls=calls or [],
            bases=bases or [],
            implements=implements or [],
            decorators=decorators or [],
        )
        self.symbols.append(sym)
        return sym

    def _returns_jsx(self, node: Any) -> bool:
        return any(n.type in ("jsx_element", "jsx_self_closing_element", "jsx_fragment") for n in _walk(node))

    def _fn_type(self, name: str, node: Any) -> SymbolType:
        if _is_hook(name):
            return SymbolType.HOOK
        if _is_pascal(name) and self._returns_jsx(node):
            return SymbolType.COMPONENT
        return SymbolType.FUNCTION

    def function(self, node: Any, exported: bool, doc_anchor: Any) -> None:
        name_node = node.child_by_field_name("name")
        name = self.src.text(name_node) or ("default" if exported else "")
        if not name:
            return
        body = node.child_by_field_name("body")
        self._make(
            node,
            name,
            self._fn_type(name, node),
            exported,
            doc_anchor,
            _signature_until_body(self.src, node, body),
            calls=self.calls_in(body) if body is not None else [],
        )

    def variables(self, node: Any, exported: bool, doc_anchor: Any) -> None:
        for decl in node.children:
            if decl.type != "variable_declarator":
                continue
            name_node = decl.child_by_field_name("name")
            if name_node is None or name_node.type != "identifier":
                continue
            name = self.src.text(name_node)
            value = decl.child_by_field_name("value")
            # unwrap wrappers like memo(() => ...), forwardRef(function X() {...})
            fn_value = value
            if value is not None and value.type == "call_expression":
                args = value.child_by_field_name("arguments")
                inner = [a for a in (args.children if args is not None else []) if a.type in _TS_FUNCTION_VALUES]
                if inner:
                    fn_value = inner[0]
            if fn_value is not None and fn_value.type in _TS_FUNCTION_VALUES:
                body = fn_value.child_by_field_name("body")
                self._make(
                    node,
                    name,
                    self._fn_type(name, fn_value),
                    exported,
                    doc_anchor,
                    _signature_until_body(self.src, node, body),
                    calls=self.calls_in(value),
                )
            elif exported or name.isupper():
                stype = (
                    SymbolType.CONSTANT
                    if (node.children and self.src.text(node.children[0]) == "const")
                    else SymbolType.VARIABLE
                )
                self._make(
                    node,
                    name,
                    stype,
                    exported,
                    doc_anchor,
                    _first_line(self.src.text(node)),
                    calls=self.calls_in(value) if value is not None else [],
                )

    def klass(self, node: Any, exported: bool, doc_anchor: Any) -> None:
        name_node = node.child_by_field_name("name")
        name = self.src.text(name_node) or "default"
        bases: list[str] = []
        implements: list[str] = []
        body = node.child_by_field_name("body")
        decorators = [self.src.text(c) for c in node.children if c.type == "decorator"]
        for c in node.children:
            if c.type != "class_heritage":
                continue
            for h in c.children:
                if h.type == "extends_clause":
                    val = h.child_by_field_name("value")
                    if val is not None:
                        bases.append(self.src.text(val))
                elif h.type == "implements_clause":
                    implements.extend(
                        self.src.text(t)
                        for t in h.children
                        if t.type in ("type_identifier", "generic_type", "identifier")
                    )
        self._make(
            node,
            name,
            SymbolType.CLASS,
            exported,
            doc_anchor,
            _signature_until_body(self.src, node, body),
            bases=bases,
            implements=[i.split("<")[0] for i in implements],
            decorators=decorators,
        )
        if body is None:
            return
        for m in body.children:
            if m.type not in ("method_definition", "abstract_method_signature"):
                continue
            m_name = self.src.text(m.child_by_field_name("name"))
            m_body = m.child_by_field_name("body")
            self._make(
                m,
                m_name,
                SymbolType.METHOD,
                exported,
                m,
                _signature_until_body(self.src, m, m_body),
                parent=name,
                calls=self.calls_in(m_body) if m_body is not None else [],
            )

    def simple(self, node: Any, stype: SymbolType, exported: bool, doc_anchor: Any) -> None:
        name = self.src.text(node.child_by_field_name("name"))
        if not name:
            return
        body = node.child_by_field_name("body") if stype != SymbolType.TYPE_ALIAS else None
        self._make(node, name, stype, exported, doc_anchor, _signature_until_body(self.src, node, body))


# =============================================================================
# Python
# =============================================================================


class _PyExtractor:
    def __init__(self, src: _Src, path: str) -> None:
        self.src = src
        self.path = path
        self.symbols: list[Symbol] = []
        self.imports: list[ImportRef] = []

    def calls_in(self, node: Any) -> list[str]:
        names: list[str] = []
        for n in _walk(node):
            if n.type != "call":
                continue
            fn = n.child_by_field_name("function")
            if fn is None:
                continue
            if fn.type == "identifier":
                names.append(self.src.text(fn))
            elif fn.type == "attribute":
                attr = fn.child_by_field_name("attribute")
                if attr is not None:
                    names.append(self.src.text(attr))
        return _dedupe(names)

    def docstring(self, body: Any | None) -> str | None:
        if body is None:
            return None
        for stmt in body.children:
            if stmt.type == "expression_statement" and stmt.children and stmt.children[0].type == "string":
                raw = self.src.text(stmt.children[0])
                return raw.strip("rbuRBU").strip("\"'").strip() or None
            if stmt.type != "comment":
                break
        return None

    def run(self, root: Any) -> None:
        for child in root.children:
            self.stmt(child, parent=None)

    def stmt(self, node: Any, parent: str | None, decorators: list[str] | None = None) -> None:
        t = node.type
        if t == "import_statement":
            for c in node.children:
                if c.type == "dotted_name":
                    self.imports.append(
                        ImportRef(
                            source=self.src.text(c),
                            names=[self.src.text(c).split(".")[0]],
                            line=node.start_point[0] + 1,
                        )
                    )
                elif c.type == "aliased_import":
                    alias = c.child_by_field_name("alias")
                    name = c.child_by_field_name("name")
                    self.imports.append(
                        ImportRef(
                            source=self.src.text(name), names=[self.src.text(alias)], line=node.start_point[0] + 1
                        )
                    )
        elif t == "import_from_statement":
            module = node.child_by_field_name("module_name")
            ref = ImportRef(source=self.src.text(module), line=node.start_point[0] + 1)
            for c in node.children_by_field_name("name"):
                if c.type == "aliased_import":
                    ref.names.append(self.src.text(c.child_by_field_name("alias")))
                else:
                    ref.names.append(self.src.text(c).split(".")[-1])
            if any(c.type == "wildcard_import" for c in node.children):
                ref.names.append("*")
            self.imports.append(ref)
        elif t == "decorated_definition":
            decos = [self.src.text(c) for c in node.children if c.type == "decorator"]
            definition = node.child_by_field_name("definition")
            if definition is not None:
                self.stmt(definition, parent, decos)
                if self.symbols and self.symbols[-1].name == self.src.text(definition.child_by_field_name("name")):
                    # include decorators in code/lines
                    sym = self.symbols[-1]
                    sym.start_line = node.start_point[0] + 1
                    sym.code = self.src.text(node)[:MAX_SYMBOL_CODE_CHARS] if sym.type != SymbolType.CLASS else sym.code
        elif t == "function_definition":
            name = self.src.text(node.child_by_field_name("name"))
            body = node.child_by_field_name("body")
            self.symbols.append(
                Symbol(
                    name=name,
                    type=SymbolType.METHOD if parent else SymbolType.FUNCTION,
                    file_path=self.path,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    code=self.src.text(node)[:MAX_SYMBOL_CODE_CHARS],
                    signature=_signature_until_body(self.src, node, body).rstrip(":").strip(),
                    docstring=self.docstring(body),
                    parent=parent,
                    decorators=decorators or [],
                    exported=not name.startswith("_"),
                    calls=self.calls_in(body) if body is not None else [],
                )
            )
        elif t == "class_definition":
            name = self.src.text(node.child_by_field_name("name"))
            body = node.child_by_field_name("body")
            supers = node.child_by_field_name("superclasses")
            bases = [
                self.src.text(a)
                for a in (supers.children if supers is not None else [])
                if a.type in ("identifier", "attribute")
            ]
            self.symbols.append(
                Symbol(
                    name=name,
                    type=SymbolType.CLASS,
                    file_path=self.path,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    code=self.src.text(node)[:MAX_SYMBOL_CODE_CHARS],
                    signature=_signature_until_body(self.src, node, body).rstrip(":").strip(),
                    docstring=self.docstring(body),
                    parent=parent,
                    decorators=decorators or [],
                    exported=not name.startswith("_"),
                    bases=bases,
                )
            )
            if body is not None:
                for c in body.children:
                    if c.type in ("function_definition", "decorated_definition"):
                        self.stmt(c, parent=name)
        elif t == "expression_statement" and parent is None and node.children and node.children[0].type == "assignment":
            assign = node.children[0]
            left = assign.child_by_field_name("left")
            if left is not None and left.type == "identifier" and self.src.text(left).isupper():
                self.symbols.append(
                    Symbol(
                        name=self.src.text(left),
                        type=SymbolType.CONSTANT,
                        file_path=self.path,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        code=self.src.text(node)[:MAX_SYMBOL_CODE_CHARS],
                        signature=_first_line(self.src.text(node)),
                        exported=True,
                    )
                )


# =============================================================================
# Go
# =============================================================================


class _GoExtractor:
    def __init__(self, src: _Src, path: str) -> None:
        self.src = src
        self.path = path
        self.symbols: list[Symbol] = []
        self.imports: list[ImportRef] = []

    def calls_in(self, node: Any) -> list[str]:
        names: list[str] = []
        for n in _walk(node):
            if n.type != "call_expression":
                continue
            fn = n.child_by_field_name("function")
            if fn is None:
                continue
            if fn.type == "identifier":
                names.append(self.src.text(fn))
            elif fn.type == "selector_expression":
                field = fn.child_by_field_name("field")
                if field is not None:
                    names.append(self.src.text(field))
        return _dedupe(names)

    def run(self, root: Any) -> None:
        for node in root.children:
            t = node.type
            if t == "import_declaration":
                for n in _walk(node):
                    if n.type == "import_spec":
                        path = n.child_by_field_name("path")
                        alias = n.child_by_field_name("name")
                        src = self.src.text(path).strip('"`')
                        self.imports.append(
                            ImportRef(
                                source=src,
                                names=[self.src.text(alias) if alias is not None else src.split("/")[-1]],
                                line=n.start_point[0] + 1,
                            )
                        )
            elif t in ("function_declaration", "method_declaration"):
                name = self.src.text(node.child_by_field_name("name"))
                body = node.child_by_field_name("body")
                parent = None
                if t == "method_declaration":
                    recv = node.child_by_field_name("receiver")
                    types = (
                        [self.src.text(n) for n in _walk(recv) if n.type == "type_identifier"]
                        if recv is not None
                        else []
                    )
                    parent = types[0] if types else None
                self._add(
                    node,
                    name,
                    SymbolType.METHOD if parent else SymbolType.FUNCTION,
                    _signature_until_body(self.src, node, body),
                    parent,
                    self.calls_in(body) if body is not None else [],
                )
            elif t == "type_declaration":
                for spec in node.children:
                    if spec.type not in ("type_spec", "type_alias"):
                        continue
                    name = self.src.text(spec.child_by_field_name("name"))
                    typ = spec.child_by_field_name("type")
                    kind = {"struct_type": SymbolType.STRUCT, "interface_type": SymbolType.INTERFACE}.get(
                        typ.type if typ is not None else "", SymbolType.TYPE_ALIAS
                    )
                    self._add(node, name, kind, _first_line(self.src.text(node)), None, [])
            elif t in ("const_declaration", "var_declaration"):
                for spec in _walk(node):
                    if spec.type in ("const_spec", "var_spec"):
                        for nm in spec.children_by_field_name("name"):
                            name = self.src.text(nm)
                            if _is_pascal(name):
                                self._add(
                                    node,
                                    name,
                                    SymbolType.CONSTANT if t == "const_declaration" else SymbolType.VARIABLE,
                                    _first_line(self.src.text(spec)),
                                    None,
                                    [],
                                )

    def _add(
        self, node: Any, name: str, stype: SymbolType, signature: str, parent: str | None, calls: list[str]
    ) -> None:
        self.symbols.append(
            Symbol(
                name=name,
                type=stype,
                file_path=self.path,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                code=self.src.text(node)[:MAX_SYMBOL_CODE_CHARS],
                signature=signature,
                docstring=_leading_comment(self.src, node),
                parent=parent,
                exported=_is_pascal(name),
                calls=calls,
            )
        )


_EXTRACTORS: dict[str, Callable[[_Src, str], Any]] = {
    "typescript": _TSExtractor,
    "tsx": _TSExtractor,
    "javascript": _TSExtractor,
    "python": _PyExtractor,
    "go": _GoExtractor,
}


class MultiLanguageParser:
    """Parses TS/TSX/JS, Python and Go into symbols + imports."""

    def __init__(self) -> None:
        self._parsers: dict[str, Any] = {}

    def _parser(self, language: str) -> Any | None:
        if language not in self._parsers:
            lang = _load_language(language)
            if lang is None:
                self._parsers[language] = None
            else:
                from tree_sitter import Parser

                self._parsers[language] = Parser(lang)
        return self._parsers[language]

    def supports(self, path: str) -> bool:
        lang = detect_language(path)
        return lang is not None and self._parser(lang) is not None

    def parse_file(self, path: str, content: str) -> ParsedFile:
        language = detect_language(path) or "text"
        parsed = ParsedFile(path=path, language=language, content=content)
        parser = self._parser(language) if language in _EXTRACTORS else None
        if parser is None:
            return parsed
        data = content.encode("utf-8")
        try:
            tree = parser.parse(data)
            extractor = _EXTRACTORS[language](_Src(data), path)
            extractor.run(tree.root_node)
        except Exception:  # a broken file must never abort ingestion
            logger.exception("parse failed: %s", path)
            return parsed
        parsed.symbols = extractor.symbols
        parsed.imports = extractor.imports
        return parsed
