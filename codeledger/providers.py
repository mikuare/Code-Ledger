"""Language analysis providers.

Each provider turns source text into symbols and dependency edges, and declares
how far it can be trusted. That declaration is the point: the rest of the system
must be able to tell "nothing depends on this symbol" apart from "this language
is not really analysed", and before providers existed it could not.

Resolution order per file: tree-sitter (if the optional grammars are installed),
then Python's own AST, then line-level regex. Nothing here may raise on bad
input; analysis degrades, it does not fail.
"""
from __future__ import annotations

import re
from pathlib import Path

from .parser import (SymbolData, ast_symbols, ast_edges, digest, language,
                     regex_symbols, regex_edges)

FULL = "full"        # real parse tree; ranges and call edges are trustworthy
SHALLOW = "shallow"  # line patterns; imports mostly, calls approximate
NONE = "none"        # indexed by hash only

# Only the first statement of a *body* can be a docstring. The check is scoped
# to these node types because the first named child of, say, an argument list is
# often a string too — dropping that would make f("a") and f("b") identical.
BODY_TYPES = {"block", "module", "program", "source_file", "statement_block", "class_body", "declaration_list"}

def _is_docstring(parent, node) -> bool:
    """A body's leading bare string literal — documentation, not code."""
    if not (parent.type in BODY_TYPES or parent.type.endswith("_body")):
        return False
    if node.type == "expression_statement":
        children = node.named_children
        return len(children) == 1 and "string" in children[0].type
    return "string" in node.type

class PythonAstProvider:
    name = "ast"
    coverage = FULL
    languages = ("python",)

    def symbols(self, path: Path, text: str) -> list[SymbolData]:
        parsed = ast_symbols(text)
        return regex_symbols(text) if parsed is None else parsed   # unparseable source still yields something

    def edges(self, path: Path, text: str, symbols: list[SymbolData]) -> list[tuple[str, str, str]]:
        return ast_edges(text)

class RegexProvider:
    name = "regex"
    coverage = SHALLOW
    languages = ()

    def symbols(self, path: Path, text: str) -> list[SymbolData]:
        return regex_symbols(text)

    def edges(self, path: Path, text: str, symbols: list[SymbolData]) -> list[tuple[str, str, str]]:
        return regex_edges(path, text, symbols)

# Extensions and language ids the grammar pack knows. Anything absent falls
# through to the regex provider rather than failing.
GRAMMARS = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".tsx": "tsx", ".go": "go", ".rs": "rust",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".cs": "csharp", ".rb": "ruby",
    ".php": "php", ".swift": "swift", ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
    ".cc": "cpp", ".scala": "scala", ".ex": "elixir", ".exs": "elixir", ".lua": "lua",
    ".dart": "dart", ".hs": "haskell", ".sql": "sql", ".vue": "vue", ".svelte": "svelte",
    ".sh": "bash", ".bash": "bash", ".zig": "zig", ".jl": "julia", ".pl": "perl",
    ".r": "r", ".m": "objc", ".mm": "objc", ".sol": "solidity", ".proto": "proto",
}

# Grammar authors follow strong conventions, so a generic walker carries most
# languages. These are the deviations worth naming explicitly.
DEFINITION_SUFFIXES = ("_definition", "_declaration", "_item", "_spec")
# `const x = () => {}` binds its name on the declarator, not the declaration.
FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "lambda", "closure_expression", "generator_function"}
# Suffix matching alone would treat `import_specifier` and `use_declaration` as
# definitions, inventing a symbol for every imported name.
NON_DEFINITION = {"import_specifier", "namespace_import", "import_clause", "export_clause", "export_specifier"}
DEFINITION_TYPES = {"function_definition", "function_declaration", "method_definition", "class_definition",
                    "class_declaration", "interface_declaration", "struct_item", "enum_item", "impl_item",
                    "type_spec", "method_declaration", "constructor_declaration", "trait_item",
                    "function_item", "module", "object_declaration", "protocol_declaration",
                    # Ruby names these without a suffix
                    "method", "singleton_method", "class"}
IDENTIFIERS = ("identifier", "type_identifier", "field_identifier", "constant", "simple_identifier",
               "property_identifier", "scoped_identifier", "word")
CALL_TYPES = {"call", "call_expression", "method_invocation", "invocation_expression",
              "function_call", "function_call_expression", "call_method", "new_expression"}
IMPORT_HINTS = ("import", "use_declaration", "package_clause", "require", "include", "using_directive")
KIND_BY_TYPE = {"class": "class", "struct": "class", "interface": "interface", "trait": "interface",
                "enum": "enum", "module": "module", "object": "class", "protocol": "interface"}

class TreeSitterProvider:
    """Parse-tree analysis for every installed grammar.

    Deliberately generic. Hand-writing queries per language does not scale to
    "every modern language" and rots as grammars change, so this walks node
    types by convention and treats an unrecognised grammar as a miss rather
    than an error.
    """
    name = "tree-sitter"
    coverage = FULL

    def __init__(self, grammar: str, parser):
        self.grammar = grammar; self._parser = parser

    def _tree(self, text: str):
        return self._parser.parse(text.encode("utf-8", errors="replace"))

    @staticmethod
    def _name_of(node) -> str | None:
        field = node.child_by_field_name("name")
        if field is not None:
            return field.text.decode("utf-8", errors="replace")
        # C and C++ nest the name inside a declarator chain:
        # function_definition -> function_declarator -> identifier
        declarator = node.child_by_field_name("declarator")
        for _ in range(4):
            if declarator is None:
                break
            if declarator.type in IDENTIFIERS:
                return declarator.text.decode("utf-8", errors="replace")
            declarator = declarator.child_by_field_name("declarator") or next((c for c in declarator.named_children if c.type in IDENTIFIERS), None)
        for child in node.named_children:            # Kotlin/Rust/Go name their identifier child directly
            if child.type in IDENTIFIERS:
                return child.text.decode("utf-8", errors="replace")
        return None

    @staticmethod
    def _is_definition(node) -> bool:
        kind = node.type
        if kind in NON_DEFINITION or any(hint in kind for hint in IMPORT_HINTS):
            return False
        if kind in DEFINITION_TYPES:
            return True
        if kind == "variable_declarator":
            return any(child.type in FUNCTION_VALUES for child in node.named_children)
        return kind.endswith(DEFINITION_SUFFIXES)

    def _kind(self, node) -> str:
        for token, kind in KIND_BY_TYPE.items():
            if token in node.type:
                return kind
        return "function"

    @staticmethod
    def _code_digest(node) -> str:
        """Hash a symbol's code with its comments removed.

        Every grammar names its comment nodes with "comment" in the type, so the
        parse tree identifies them exactly — no regex has to guess whether a `#`
        is a comment or part of a string. Whitespace is collapsed for the same
        reason the Python path dumps an AST: reformatting is not a code change.
        """
        pieces = []
        stack = [node]
        while stack:
            current = stack.pop()
            if "comment" in current.type:
                continue
            if not current.named_children:
                pieces.append((current.start_byte, current.text.decode("utf-8", errors="replace")))
                continue
            children = list(current.named_children)
            # A docstring is a bare string statement, not a comment node, so
            # nothing above catches it. Dropping the leading string of a body
            # keeps documentation out of the code hash, and makes this path agree
            # with the Python AST path — which strips docstrings by construction.
            # Without it the same file classified differently depending on
            # whether an optional grammar package happened to be installed.
            if children and _is_docstring(current, children[0]):
                children = children[1:]
            stack.extend(children)
        # Leaf text is kept verbatim — collapsing it would erase real differences
        # inside string literals. Joining leaves with a single space is what
        # normalizes the indentation and line breaks between them.
        return digest(" ".join(token for _, token in sorted(pieces)))

    def symbols(self, path: Path, text: str) -> list[SymbolData]:
        lines = text.splitlines(); found: list[SymbolData] = []
        stack = [(self._tree(text).root_node, [])]
        while stack:
            node, scope = stack.pop()
            child_scope = scope
            if self._is_definition(node):
                name = self._name_of(node)
                if name:
                    start, end = node.start_point[0] + 1, node.end_point[0] + 1
                    signature = lines[start - 1].strip() if start <= len(lines) else name
                    qualified = ".".join(scope + [name])
                    found.append(SymbolData(name, self._kind(node), start, end, signature, self._code_digest(node), qualified))
                    child_scope = scope + [name]
            for child in node.named_children:
                stack.append((child, child_scope))
        return sorted(found, key=lambda item: (item.start, item.name))

    def edges(self, path: Path, text: str, symbols: list[SymbolData]) -> list[tuple[str, str, str]]:
        result: list[tuple[str, str, str]] = []
        defined = {item.name for item in symbols}
        ranges = sorted(((item.start, item.end, item.name) for item in symbols), key=lambda item: item[0])

        def enclosing(line: int) -> str:
            best = None
            for start, end, name in ranges:
                if start <= line <= end and (best is None or start >= best[0]):
                    best = (start, name)
            return best[1] if best else "__module__"

        stack = [self._tree(text).root_node]
        while stack:
            node = stack.pop()
            if node.type in CALL_TYPES:
                target = node.child_by_field_name("function") or node.child_by_field_name("constructor") or (node.named_children[0] if node.named_children else None)
                if target is not None:
                    raw = target.text.decode("utf-8", errors="replace")
                    name = re.split(r"[.:]", raw.strip())[-1].split("(")[0].strip()
                    source = enclosing(node.start_point[0] + 1)
                    if re.fullmatch(r"[A-Za-z_$][\w$]*", name or "") and name != source:
                        result.append((source, name, "calls"))
            elif node.type in IDENTIFIERS and node.text.decode("utf-8", errors="replace") in defined:
                # Ruby and friends call methods without parentheses, so the call
                # is just an identifier. Restricting this to names defined in
                # the file keeps it from matching every variable.
                name = node.text.decode("utf-8", errors="replace")
                source = enclosing(node.start_point[0] + 1)
                if name != source:
                    result.append((source, name, "uses"))
            elif any(hint in node.type for hint in IMPORT_HINTS):
                for piece in re.findall(r"[A-Za-z_$][\w$]*", node.text.decode("utf-8", errors="replace")):
                    result.append(("__module__", piece, "imports"))
            for child in node.named_children:
                stack.append(child)
        return sorted(set(result))

_REGEX = RegexProvider()
_PYTHON = PythonAstProvider()
_cache: dict[str, object] = {}
_pack_state: str | None = None

def _tree_sitter_provider(path: Path):
    """Return a tree-sitter provider for this file, or None. Never raises."""
    global _pack_state
    grammar = GRAMMARS.get(path.suffix.lower())
    if not grammar:
        return None
    if grammar in _cache:
        return _cache[grammar]
    try:
        from tree_sitter_language_pack import get_parser
    except Exception:
        _pack_state = "not installed"; _cache[grammar] = None; return None
    try:
        provider = TreeSitterProvider(grammar, get_parser(grammar))
        _pack_state = "installed"
    except Exception:
        provider = None                       # grammar missing from the pack
    _cache[grammar] = provider
    return provider

def provider_for(path: Path):
    return _tree_sitter_provider(path) or (_PYTHON if language(path) == "python" else _REGEX)

def version_for(path: Path) -> str:
    """Analysis stamp for a file. A change here makes `refresh --changed`
    reparse the file, so installing grammars upgrades an index in place."""
    return f"{provider_for(path).name}:1"

def analyze(path: Path, text: str) -> tuple[list[SymbolData], list[tuple[str, str, str]], str, str]:
    """Symbols, edges, provider name, and coverage tier for one file."""
    provider = provider_for(path)
    try:
        symbols = provider.symbols(path, text)
        edges = provider.edges(path, text, symbols)
        if provider.coverage == FULL and not symbols:
            # A grammar that returns nothing must not be reported as full
            # coverage. But a file legitimately holding no symbols (an empty
            # __init__.py, a table of constants) is normal, and the line
            # patterns happily hallucinate one match out of a docstring. Only a
            # cluster of missed symbols indicates the grammar is really blind.
            fallback = _REGEX.symbols(path, text)
            if len(fallback) >= 2:
                return fallback, _REGEX.edges(path, text, fallback), _REGEX.name, _REGEX.coverage
        return symbols, edges, provider.name, provider.coverage
    except Exception:
        # A grammar that chokes on a file must not fail the refresh; fall back
        # and report the weaker coverage honestly.
        symbols = _REGEX.symbols(path, text)
        return symbols, _REGEX.edges(path, text, symbols), _REGEX.name, _REGEX.coverage

def capabilities() -> dict:
    """What analysis this installation can actually perform."""
    try:
        import tree_sitter_language_pack  # noqa: F401
        available = True
    except Exception:
        available = False
    languages = {}
    for suffix, grammar in sorted(GRAMMARS.items()):
        if not available:
            languages[grammar] = FULL if grammar == "python" else SHALLOW
        else:
            languages.setdefault(grammar, FULL)
    return {"tree_sitter_installed": available, "coverage_by_language": languages,
            "install_hint": None if available else "pip install 'code-ledger[languages]' for full analysis beyond Python"}
