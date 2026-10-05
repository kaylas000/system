# specs/04_knowledge/ingestion/PARSERS.py
"""
Multi-language AST Parsers using Tree-sitter.
Extracts: Symbols (Func, Class, Interface), Imports, Calls, Docstrings.
Output: Normalized Symbol objects for Chunking.
"""

from __future__ import annotations
import tree_sitter
from tree_sitter_languages import get_parser, get_language
from pathlib import Path
from typing import Dict, List, Any, Optional, Generator, Tuple
from dataclasses import dataclass, field
from enum import Enum


class SymbolType(str, Enum):
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    INTERFACE = "interface"
    TYPE_ALIAS = "type_alias"
    ENUM = "enum"
    CONSTANT = "constant"
    VARIABLE = "variable"
    IMPORT = "import"
    EXPORT = "export"
    COMPONENT = "component"  # React/Vue/Svelte specific
    HOOK = "hook"  # useXxx
    DECORATOR = "decorator"


@dataclass
class Symbol:
    name: str
    type: SymbolType
    file_path: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    code: str
    signature: str  # First line(s) - def foo(...): / class Foo / interface Foo
    docstring: Optional[str] = None
    parent: Optional[str] = None  # Parent class/function name
    decorators: List[str] = field(default_factory=list)
    # Relationships (populated later)
    imports: List[str] = field(default_factory=list)  # Resolved import paths
    calls: List[str] = field(default_factory=list)  # Function calls inside

    # Enrichment fields (filled by Enrichment step)
    intent: str = ""
    pattern: str = ""
    complexity: str = "O(1)"
    side_effects: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)


# --- Language Configs ---

LANGUAGE_CONFIG = {
    ".ts": {"lang": "typescript", "query_file": "ts_symbols.scm"},
    ".tsx": {"lang": "tsx", "query_file": "tsx_symbols.scm"},
    ".js": {"lang": "javascript", "query_file": "js_symbols.scm"},
    ".jsx": {"lang": "javascript", "query_file": "js_symbols.scm"},
    ".py": {"lang": "python", "query_file": "py_symbols.scm"},
    ".go": {"lang": "go", "query_file": "go_symbols.scm"},
    ".rs": {"lang": "rust", "query_file": "rs_symbols.scm"},
    ".java": {"lang": "java", "query_file": "java_symbols.scm"},
    ".cs": {"lang": "c_sharp", "query_file": "cs_symbols.scm"},
    ".php": {"lang": "php", "query_file": "php_symbols.scm"},
    ".rb": {"lang": "ruby", "query_file": "rb_symbols.scm"},
}

# Tree-sitter Queries (Simplified - real ones in .scm files)
# We define inline for spec completeness.
TS_QUERIES = {
    "typescript": """
    ; Functions
    (function_declaration name: (identifier) @name) @function
    (arrow_function) @function
    (method_definition name: (property_identifier) @name) @method

    ; Classes/Interfaces
    (class_declaration name: (type_identifier) @name) @class
    (interface_declaration name: (type_identifier) @name) @interface
    (type_alias_declaration name: (type_identifier) @name) @type_alias

    ; Imports
    (import_statement) @import

    ; Calls
    (call_expression function: (identifier) @name) @call
    """,
    "python": """
    (function_definition name: (identifier) @name) @function
    (class_definition name: (identifier) @name) @class
    (import_statement) @import
    (import_from_statement) @import
    (call function: (identifier) @name) @call
    """,
    # ... add for other languages
}


class MultiLanguageParser:
    def __init__(self):
        self._parsers: Dict[str, tree_sitter.Parser] = {}
        self._queries: Dict[str, tree_sitter.Query] = {}
        self._init_parsers()

    def _init_parsers(self):
        for ext, cfg in LANGUAGE_CONFIG.items():
            try:
                parser = get_parser(cfg["lang"])
                lang = get_language(cfg["lang"])
                # Load query from string or file
                query_str = TS_QUERIES.get(cfg["lang"], "")
                if query_str:
                    query = lang.query(query_str)
                    self._parsers[ext] = parser
                    self._queries[ext] = query
            except Exception as e:
                print(f"[Parser] Failed to init {ext}: {e}")

    def parse_file(self, file_path: Path, content: bytes) -> List[Symbol]:
        ext = file_path.suffix.lower()
        parser = self._parsers.get(ext)
        query = self._queries.get(ext)

        if not parser or not query:
            return [self._fallback_chunk(file_path, content)]

        tree = parser.parse(content)
        captures = query.captures(tree.root_node)

        symbols = []
        # Process captures (simplified logic)
        # Real implementation builds Symbol objects from node ranges
        for node, tag in captures:
            if tag in ("name", "function", "method", "class", "interface", "import", "call"):
                # Extract details...
                pass  # Detailed implementation below
        return symbols

    def _fallback_chunk(self, file_path: Path, content: bytes) -> Symbol:
        """Fallback for unsupported languages: treat whole file as one chunk."""
        text = content.decode("utf-8", errors="ignore")
        lines = text.splitlines()
        return Symbol(
            name=file_path.name,
            type=SymbolType.CONSTANT,  # Generic
            file_path=str(file_path),
            start_line=1,
            end_line=len(lines),
            start_byte=0,
            end_byte=len(content),
            code=text,
            signature=f"File: {file_path.name}",
            docstring=text[:500],
        )

    # --- Advanced Extraction Helpers ---

    def extract_symbol_details(self, node: tree_sitter.Node, code: bytes, file_path: str, tag: str) -> Optional[Symbol]:
        """Extracts a fully populated Symbol from a capture."""
        # This is where the heavy lifting happens per language.
        # Use node.type, node.children, node.start_point, node.end_point.
        # Use helper functions: get_node_text, find_child_by_type.
        pass
