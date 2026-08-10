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

from .parser import (UNKNOWN_ENV, SymbolData, ast_symbols, ast_edges, digest, language,
                     package_name, regex_symbols, regex_edges)

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
# Storage slots, not definitions: a struct field, a class property and a
# parameter all end in a definition-like suffix, so the conventions above
# registered a Go method's receiver (`s`), a struct's `addr` and a TS class's
# `items` as project symbols. They are treated like `variable_declarator` —
# a definition only when the slot binds a function, which is what makes
# `handleClick = () => {}` a method by another name and `items: string[] = []`
# an implementation detail.
FIELD_TYPES = {"field_declaration", "parameter_declaration", "public_field_definition",
               "property_declaration", "field_definition"}
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
# Member access and subscripting, across the grammars — the two shapes an
# environment read can take (`process.env.X` and `process.env["X"]`).
ACCESS_TYPES = {"member_expression", "attribute", "subscript_expression", "subscript"}
KIND_BY_TYPE = {"class": "class", "struct": "class", "interface": "interface", "trait": "interface",
                "enum": "enum", "module": "module", "object": "class", "protocol": "interface"}

# Grammars whose node vocabulary collides with the conventions above. Only a
# grammar that actually collides gets an entry; every other language keeps the
# generic walker, which is what makes "every installed grammar" affordable.
#
# `exclusive` means the conventions are switched off for this grammar and only
# the listed types are definitions. That is the honest setting for a vocabulary
# that disagrees with the conventions rather than merely extending them:
# anything absent from the list is unsupported, and unsupported is a better
# answer than a confident wrong one.
GRAMMAR_RULES = {
    # tree-sitter-sql names a PL/pgSQL DECLARE-block local `function_declaration`
    # and a table column `column_definition` — both of which the conventions read
    # as definitions, producing `v_email`, `column_name` and `id` as project
    # symbols. Meanwhile the real definitions are `create_function`,
    # `create_table` and friends, which match no convention at all, so the actual
    # functions and tables were missing from the index entirely.
    "sql": {
        "definitions": {
            "create_function": "function", "create_table": "table", "create_view": "view",
            "create_materialized_view": "view", "create_trigger": "trigger", "create_type": "type",
            "create_index": "index", "create_policy": "policy", "create_schema": "schema",
            "create_sequence": "sequence",
        },
        "exclusive": True,
        # `CREATE TABLE users` names the table on an `object_reference`, while
        # `CREATE INDEX idx ON users` names the index on a bare `identifier` and
        # the table on the object_reference after it. Taking the first of either
        # in document order gets both right, and takes the trigger's own name
        # rather than the table it fires on.
        "name_from": ("object_reference", "identifier"),
    },
}
# Deliberately absent from the SQL list, and why: CREATE EXTENSION names an
# external dependency rather than something this project defines, and
# CREATE DATABASE / CREATE ROLE are server administration, not code. CREATE
# PROCEDURE and CREATE DOMAIN are not in the grammar at all — they parse to an
# ERROR node, so there is no structural evidence to extract and `analyze`
# reports the file as SHALLOW rather than inventing a symbol from the text.

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
        self.rules = GRAMMAR_RULES.get(grammar)

    def _tree(self, text: str):
        return self._parser.parse(text.encode("utf-8", errors="replace"))

    def parse_failed(self, text: str) -> bool:
        """Did the grammar fail to parse this file?

        Consulted only when no symbols were found, to keep a file the grammar
        choked on from being recorded as fully analysed. `impact` treats FULL
        coverage as licence to trust an empty dependent list, so claiming it
        here would turn "not parsed" into "nothing depends on this".
        """
        try:
            return bool(self._tree(text).root_node.has_error)
        except Exception:
            return True

    def _named_from(self, node, types: tuple[str, ...]) -> str | None:
        """First child of one of these types, in document order, as text."""
        for child in node.named_children:
            if child.type in types:
                identifier = next((sub for sub in child.named_children if sub.type in IDENTIFIERS), None)
                target = identifier if identifier is not None else child
                return target.text.decode("utf-8", errors="replace")
        return None

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

    def _is_definition(self, node) -> bool:
        kind = node.type
        if self.rules:
            if kind in self.rules["definitions"]:
                return True
            # A grammar whose vocabulary disagrees with the conventions gets no
            # fallthrough: everything outside its list is unsupported, not
            # guessed at.
            if self.rules.get("exclusive"):
                return False
        if kind in NON_DEFINITION or any(hint in kind for hint in IMPORT_HINTS):
            return False
        # Checked before DEFINITION_TYPES: a storage slot is only a definition
        # when it binds a function, whatever else its type name suggests.
        if kind in FIELD_TYPES or kind == "variable_declarator":
            return any(child.type in FUNCTION_VALUES for child in node.named_children)
        if kind in DEFINITION_TYPES:
            return True
        return kind.endswith(DEFINITION_SUFFIXES)

    def _kind(self, node) -> str:
        if self.rules and node.type in self.rules["definitions"]:
            return self.rules["definitions"][node.type]
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
                name = None
                if self.rules and self.rules.get("name_from"):
                    name = self._named_from(node, self.rules["name_from"])
                name = name or self._name_of(node)
                if name:
                    start, end = node.start_point[0] + 1, node.end_point[0] + 1
                    signature = lines[start - 1].strip() if start <= len(lines) else name
                    qualified = ".".join(scope + [name])
                    found.append(SymbolData(name, self._kind(node), start, end, signature, self._code_digest(node), qualified))
                    child_scope = scope + [name]
            for child in node.named_children:
                stack.append((child, child_scope))
        return sorted(found, key=lambda item: (item.start, item.name))

    # Object chains that mean "read the process environment". A member access
    # whose object is one of these, or a call to one of these, is a structural
    # environment read — as opposed to a string that merely spells a variable's
    # name, which no amount of scanning can tell apart.
    ENV_OBJECTS = ("import.meta.env", "process.env", "os.environ", "Deno.env")
    ENV_CALLS = ("os.getenv", "os.environ.get", "Deno.env.get", "process.env.get")

    @staticmethod
    def _env_key(tail: str) -> str | None:
        """The variable named by what follows an environment object.

        Three outcomes, and the difference between them matters. `.NAME` or
        `["NAME"]` names a variable. `[expr]` is a real read whose name is
        computed, so it is reported as unknown. Anything else — `.NAME.trim`,
        `.NAME.length` — is a chained access, where the *inner* node is the
        read and has already been recorded; reporting the outer one too would
        invent a second, dynamic-looking read out of ordinary code and make
        `plan` claim a runtime-computed key where none exists.
        """
        dotted = re.fullmatch(r"\.([A-Za-z_$][\w$]*)", tail)
        if dotted: return dotted.group(1)
        quoted = re.fullmatch(r"""\[\s*['"]([^'"]*)['"]\s*\]""", tail)
        if quoted: return quoted.group(1) or None
        return UNKNOWN_ENV if re.fullmatch(r"\[[^\[\]]*\]", tail) else None

    @staticmethod
    def _string_value(node) -> str | None:
        """The literal text of a string node, or None if any of it is computed.

        An interpolated string names a variable whose identity is only known at
        runtime. Stripping its quotes and recording the remainder would invent
        a `PROVEN` variable literally called `${prefix}_URL`.
        """
        if any("interpolation" in child.type or "substitution" in child.type
               for child in node.named_children):
            return None
        raw = node.text.decode("utf-8", errors="replace").strip()
        raw = re.sub(r"^[A-Za-z]+(?=['\"`])", "", raw)      # f/r/b/u string prefixes
        if "${" in raw: return None
        return raw.strip("'\"`") or None

    def _env_edges(self, node) -> list[tuple[str, str, str]]:
        """Environment variables read here, by structure rather than by text.

        Only the *name* is recorded, never a value: values live in the
        environment and in `.env`, and CodeLedger reads neither. A computed key
        (`import.meta.env[name]`) is recorded as a read whose identity is
        unknown, because the dependency is real even when its name is not
        discoverable from the source.
        """
        found: list[tuple[str, str, str]] = []
        stack = [node]
        while stack:
            current = stack.pop()
            # Decoding is deliberately inside the branches. `current.text` for a
            # node is its whole source span, so decoding every node on the way
            # past costs the file size once per level of nesting.
            if current.type in ACCESS_TYPES:
                text = current.text.decode("utf-8", errors="replace")
                for prefix in self.ENV_OBJECTS:
                    if not text.startswith(prefix) or len(text) == len(prefix):
                        continue
                    tail = text[len(prefix):]
                    if tail[0] not in ".[":
                        # `process.environment` merely begins with `process.env`.
                        break
                    name = self._env_key(tail)
                    if name: found.append(("__module__", name, "env"))
                    break
            elif current.type in CALL_TYPES:
                target = current.child_by_field_name("function")
                callee = target.text.decode("utf-8", errors="replace") if target is not None else ""
                if callee in self.ENV_CALLS:
                    argument = current.child_by_field_name("arguments")
                    literal = next((child for child in (argument.named_children if argument is not None else [])
                                    if "string" in child.type), None)
                    value = self._string_value(literal) if literal is not None else None
                    found.append(("__module__", value or UNKNOWN_ENV, "env"))
            stack.extend(current.named_children)
        return found

    # Roots that name a place inside the current crate or module tree rather
    # than a distributed package.
    LOCAL_MODULE_ROOTS = frozenset({"crate", "self", "super"})

    def _module_packages(self, node) -> list[str]:
        """The distributed packages one import statement depends on.

        Grammars spell a module two ways, and the difference decides how the
        name is cut. JavaScript and Go quote a *path*, where `/` separates and a
        leading `.` means local — `@supabase/supabase-js/dist` is one package.
        Python, Java and Rust write a *dotted or scoped path*, where the first
        component is the package and `.`/`::` separate.

        Reading the wrong one either way produces a plausible package name that
        does not exist, so each is handled on its own terms and anything that
        cannot be cut confidently is dropped.
        """
        def text(item) -> str:
            return item.text.decode("utf-8", errors="replace").strip().strip("'\"`")

        source = node.child_by_field_name("source")
        if source is not None:
            return [name for name in [package_name(text(source))] if name]

        # Go and the other string-based grammars: the strings in the statement
        # are the module paths.
        strings, stack = [], list(node.named_children)
        while stack:
            current = stack.pop(0)
            if "string" in current.type: strings.append(current)
            else: stack.extend(current.named_children)
        if strings:
            return [name for name in (package_name(text(item)) for item in strings) if name]

        # Dotted or scoped paths. A Python relative import has its own node type
        # and is by definition inside this project.
        module = node.child_by_field_name("module_name")
        if module is not None:
            candidates = [] if module.type == "relative_import" else [module]
        else:
            # A bare identifier is excluded deliberately. `IMPORT_HINTS` matches
            # the nested parts of an import too (`import_specifier`,
            # `import_clause`), whose identifier is the *bound name* —
            # accepting it turns `import { createClient } from "..."` into a
            # package called `createClient`. A module path is always dotted or
            # scoped, so requiring that shape costs nothing real.
            candidates = [child for child in node.named_children
                          if child.type in ("dotted_name", "scoped_identifier")]
            candidates += [child.child_by_field_name("name") for child in node.named_children
                           if child.type == "aliased_import"]
        found = []
        for item in candidates:
            if item is None: continue
            root = re.split(r"[.:]+", text(item))[0]
            if root and root not in self.LOCAL_MODULE_ROOTS and re.fullmatch(r"[A-Za-z_$][\w$-]*", root):
                found.append(root)
        return found

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

        root = self._tree(text).root_node
        stack = [root]
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
                # The identifier bag above is deliberately coarse: it feeds
                # name-matching in the dependency graph, where a stray `from`
                # or `js` matches nothing and costs nothing. It is useless as an
                # answer to "which packages does this need?", so the module
                # specifier is read structurally and kept separate.
                for package in self._module_packages(node):
                    result.append(("__module__", package, "module"))
            for child in node.named_children:
                stack.append(child)
        result.extend(self._env_edges(root))     # same parse: `_tree` has no cache
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
    reparse the file, so installing grammars upgrades an index in place.

    Bumped to 2 when keyword, comment and grammar-vocabulary exclusions landed:
    an index built before them holds symbols like `if` and `v_email`, and the
    stamp is what retires them on the next refresh without a migration.
    """
    return f"{provider_for(path).name}:2"

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
            # Nothing was extracted and the line patterns found nothing either.
            # If the grammar could not parse the file, that is not evidence the
            # file holds no symbols — it is evidence of no coverage, and saying
            # FULL here is what lets `impact` read an empty result as proof that
            # nothing depends on a symbol.
            if getattr(provider, "parse_failed", None) and provider.parse_failed(text):
                return symbols, edges, provider.name, SHALLOW
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
