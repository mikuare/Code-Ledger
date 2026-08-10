from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

NAME = r"[A-Za-z_$][\w$]*"
# `import a, { b as c } from "m"` / `export { d } from "m"`
ES_FROM = re.compile(rf"(?:^|[;\n])\s*(?:import|export)\s+(?P<body>(?:type\s+)?[^;'\"]*?)\s+from\s*['\"](?P<module>[^'\"]+)['\"]")
ES_BARE = re.compile(r"(?:^|[;\n])\s*import\s*['\"](?P<module>[^'\"]+)['\"]")          # import "./styles.css"
ES_REQUIRE = re.compile(r"require\(\s*['\"](?P<module>[^'\"]+)['\"]\s*\)")
GENERIC_IMPORT = re.compile(rf"(?:^|\n)\s*(?:import|use|require|include)\s+([A-Za-z_$][\w$.:/-]*)")
CALL_SITE = re.compile(rf"(?<![.\w$])({NAME})\s*\(")     # foo(...) but not obj.foo(...)
JSX_SITE = re.compile(rf"<({NAME})[\s/>]")               # <UserList ... />


def _imported_names(body: str) -> list[str]:
    """Extract bound names from an ES import/export clause."""
    body = re.sub(r"^\s*type\s+", "", body.strip())
    names = [match.group(1) for match in re.finditer(rf"\*\s+as\s+({NAME})", body)]
    for block in re.findall(r"\{([^}]*)\}", body):
        for piece in block.split(","):
            match = re.match(rf"(?:type\s+)?({NAME})(?:\s+as\s+{NAME})?\s*$", piece.strip())
            if match: names.append(match.group(1))          # bind the exported name, not the local alias
    head = re.split(r"[,{]", body, maxsplit=1)[0].strip()
    if re.fullmatch(NAME, head or ""): names.append(head)   # default import
    return names

LANGUAGES = {".py":"python", ".js":"javascript", ".jsx":"javascript", ".ts":"typescript", ".tsx":"typescript", ".java":"java", ".go":"go", ".rs":"rust", ".rb":"ruby", ".php":"php", ".cs":"csharp", ".cpp":"cpp", ".c":"c"}
@dataclass
class SymbolData:
    name: str; kind: str; start: int; end: int; signature: str; hash: str
    qualified: str = ""      # dotted scope path when the provider knows one

def language(path: Path) -> str:
    return LANGUAGES.get(path.suffix.lower(), "text")

def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

def digest_bytes(raw: bytes) -> str:
    """Hash raw file bytes.

    File identity must never go through a lossy decode: ``errors="replace"``
    maps every undecodable byte to U+FFFD, so two files that differ only in
    those bytes would share a hash and a real edit would be skipped by
    ``refresh --changed``.
    """
    return hashlib.sha256(raw).hexdigest()

def strip_docstrings(tree: ast.AST) -> ast.AST:
    """Remove docstrings so documentation edits do not read as code changes."""
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            node.body = body[1:]
    return tree

def code_digest(node: ast.AST) -> str:
    """Hash what a symbol *does*, ignoring how it is written or explained.

    Hashing the raw source lines made every comment, blank line and reindent
    look like a logic change, so `effect` reported `symbols-changed` for edits
    that changed no behaviour at all — the exact signal an agent relies on to
    tell a real fix from one that missed. The AST contains no comments and no
    formatting, so dumping it compares code to code. Attributes are excluded so
    that moving a function down a file is not a change to it.
    """
    return digest(ast.dump(node, include_attributes=False))

def ast_symbols(text: str) -> list[SymbolData] | None:
    """Python symbols via the AST. None when the source does not parse."""
    try:
        tree = strip_docstrings(ast.parse(text))
    except SyntaxError:
        return None
    lines = text.splitlines(); result = []
    scopes: dict[int, list[str]] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scopes[id(child)] = scopes.get(id(node), []) + [node.name]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start, end = node.lineno, getattr(node, "end_lineno", node.lineno)
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            signature = lines[start - 1].strip() if start <= len(lines) else node.name
            qualified = ".".join(scopes.get(id(node), []) + [node.name])
            result.append(SymbolData(node.name, kind, start, end, signature, code_digest(node), qualified))
    return sorted(result, key=lambda x: (x.start, x.name))

def parse_file(path: Path, text: str) -> list[SymbolData]:
    from .providers import analyze
    return analyze(path, text)[0]

# Tokens that can open a `keyword (...) {` statement. The `method` pattern below
# matches that shape, and a control-flow head wearing it is a statement, not a
# declaration — `if (ready) {` is not a method called `if`.
#
# This is a structural rule, not a list of names that looked wrong: none of these
# tokens can legally be an identifier in the position the pattern captures them,
# so excluding them cannot cost a real symbol. It is deliberately confined to
# that one pattern; a method genuinely named `move` or `submit` is untouched, and
# `function if(...)` is impossible so the declaration patterns never need it.
CONTROL_FLOW_HEADS = frozenset({
    "if", "else", "elif", "for", "foreach", "while", "do", "switch", "case", "default",
    "try", "catch", "except", "finally", "with", "using", "match", "when", "unless",
    "return", "yield", "await", "throw", "guard", "defer", "lock", "synchronized",
})
# Modifiers that may legally precede a type declaration. Requiring the keyword to
# sit at the head of a line behind only these is what separates a declaration
# from prose: `class Widget {` starts a line, "the class of service" does not.
TYPE_MODIFIERS = r"(?:export|default|public|private|protected|internal|abstract|sealed|final|static|partial|declare|open|data|inner)\s+"
# Comment spans, blanked (not deleted) so line numbers stay exact.
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
LINE_COMMENT = re.compile(r"(?<!:)//[^\n]*|#[^\n]*")

def _strip_comments(text: str) -> str:
    """Blank out comment text, preserving every line break and offset.

    Documentation is prose, and prose contains the words `type`, `class` and
    `interface`. Matching declaration patterns against it invented symbols named
    `the` and `of` out of English sentences. Replacing the span with spaces
    rather than removing it keeps `line_start`/`line_end` truthful.

    The `(?<!:)` guard leaves `http://` alone; nothing here tries to be a lexer,
    because the provider it serves is explicitly SHALLOW.
    """
    def blank(match: re.Match) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))
    return LINE_COMMENT.sub(blank, BLOCK_COMMENT.sub(blank, text))

def regex_symbols(text: str) -> list[SymbolData]:
    patterns = [
        ("class", re.compile(rf"^\s*(?:{TYPE_MODIFIERS})*class\s+([A-Za-z_$][\w$]*)")),
        ("interface", re.compile(rf"^\s*(?:{TYPE_MODIFIERS})*(?:interface|type)\s+([A-Za-z_$][\w$]*)")),
        ("function", re.compile(r"(?:function\s+|def\s+|fn\s+|func\s+)([A-Za-z_$][\w$]*)\s*\(")),
        ("function", re.compile(r"(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>")),
        ("method", re.compile(r"^\s*(?:public|private|protected|static|async|export\s+)*\s*([A-Za-z_$][\w$]*)\s*\([^;{}]*\)\s*[{:]")),
    ]
    result = []
    lines = text.splitlines()
    # Hashes still cover the original source: a comment edit inside a symbol is
    # a change to that symbol's text, and the shallow provider says so honestly
    # rather than pretending to the comment-awareness only a parse tree has.
    scan_lines = _strip_comments(text).splitlines()
    for i, line in enumerate(scan_lines, 1):
        for kind, pattern in patterns:
            match = pattern.search(line)
            if match:
                name = match.group(1)
                if kind == "method" and name in CONTROL_FLOW_HEADS:
                    continue          # `if (x) {` — a statement wearing a call's shape
                end = _block_end(lines, i)
                # Hash the whole block, not the declaration line. Hashing one
                # line meant an edit inside a function body never changed the
                # symbol's hash, so the edit was invisible to attribution.
                result.append(SymbolData(name, kind, i, end, lines[i-1].strip(), digest("\n".join(lines[i-1:end]))))
                break
    return result

def _block_end(lines: list[str], start: int) -> int:
    """Approximate the last line of a brace-delimited block (1-indexed).

    Brace counting, not parsing: braces inside strings or block comments can
    skew it. Ranges are only used to attribute a call to its enclosing symbol,
    where being approximately right beats having no scope at all.
    """
    depth = 0; opened = False
    for index in range(start - 1, len(lines)):
        for char in re.sub(r"//.*$", "", lines[index]):
            if char == "{":
                depth += 1; opened = True
            elif char == "}" and opened:
                depth -= 1
                if depth == 0:
                    return index + 1
        if not opened and index >= start:   # nothing opened on the declaration or the line after it
            return start
    return len(lines) if opened else start

def _enclosing(symbols: list[SymbolData], line: int) -> str:
    """Innermost symbol whose block contains this line."""
    best = None
    for item in symbols:
        if item.start <= line <= item.end and (best is None or item.start >= best.start):
            best = item
    return best.name if best else "__module__"

def dependencies(path: Path, text: str) -> list[tuple[str, str, str]]:
    """Return conservative (source symbol, target name, relationship) edges."""
    from .providers import analyze
    return analyze(path, text)[1]

def ast_env_edges(tree: ast.AST) -> list[tuple[str, str, str]]:
    """Environment variables this Python module reads, from the parse tree.

    Structural, never a string scan: the name has to appear as the subscript of
    `os.environ` or the first argument of `os.getenv`/`os.environ.get`, and it
    has to be a literal. `"DATABASE_URL"` sitting in a list is not a read of it,
    and `os.environ[name]` reads *something* whose identity is not in the source
    — reported as UNKNOWN rather than guessed at.

    Only the variable's name is ever recorded. Values live in the environment
    and in `.env`, and CodeLedger reads neither.
    """
    found: list[tuple[str, str, str]] = []
    def accessor(node) -> str:
        return ast.unparse(node) if hasattr(ast, "unparse") else ""
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and accessor(node.value) in ("os.environ", "environ"):
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                found.append(("__module__", key.value, "env"))
            else:
                found.append(("__module__", UNKNOWN_ENV, "env"))
        elif isinstance(node, ast.Call):
            name = accessor(node.func)
            if name in ("os.getenv", "getenv", "os.environ.get", "environ.get"):
                first = node.args[0] if node.args else None
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.append(("__module__", first.value, "env"))
                elif first is not None:
                    found.append(("__module__", UNKNOWN_ENV, "env"))
    return found

# Recorded when a module demonstrably reads the environment but the key is
# computed, so the dependency is real and its name is not knowable from source.
UNKNOWN_ENV = "<dynamic>"

def package_name(specifier: str, lang: str = "") -> str | None:
    """The installable package a quoted module specifier names, or None if local.

    Only ever called on a *path-shaped* specifier — the thing inside the quotes
    of an `import`/`require`. `./config` and `/etc/thing` are not packages, and
    a URL import names something no package manager resolves; returning None for
    those is the point, because the inventory would rather say nothing than name
    a dependency that does not exist.

    Go is cut differently and must be, because its import path *is* the
    dependency's identity: `github.com/gin-gonic/gin/binding` belongs to the
    module `github.com/gin-gonic/gin`, and applying the npm rule would collapse
    every Go dependency in the project into a single entry called `github.com`.
    """
    text = (specifier or "").strip().strip("'\"`").replace("\\", "/")
    if not text or text.startswith((".", "/")):
        return None
    parts = [part for part in text.split("/") if part]
    if not parts:
        return None
    if lang == "go":
        # A remote module is host/owner/repo; anything shorter, or with no dot
        # in the host position, is the standard library (`net/http`) and is
        # named in full.
        return "/".join(parts[:3]) if "." in parts[0] else text
    if ":" in text:
        return None                      # a URL or protocol import, not a package
    if parts[0].startswith("@"):
        # A scope on its own (`@scope`) is not installable; a scoped package is
        # always two segments.
        return "/".join(parts[:2]) if len(parts) > 1 else None
    return parts[0]

def dotted_root(token: str) -> str | None:
    """The first component of a dotted or scoped module path (`os.path` -> `os`).

    Used for languages that write module paths as identifiers rather than
    strings — Python, Java, Rust — where the leading component is the package
    and `.`/`::` separate.
    """
    root = re.split(r"[.:]+", (token or "").strip())[0]
    return root if root and re.fullmatch(r"[A-Za-z_$][\w$-]*", root) else None

def ast_edges(text: str) -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    scopes = [(node.name, node) for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    for source, node in scopes:
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                target = child.func.id if isinstance(child.func, ast.Name) else child.func.attr if isinstance(child.func, ast.Attribute) else None
                if target and target != source:
                    result.append((source, target, "calls"))
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) and child.id != source:
                result.append((source, child.id, "uses"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name.split(".")[0] for a in node.names]
            result.extend(("__module__", name, "imports") for name in names)
            # The distributed package this import comes from, kept apart from
            # the bound names above: `from x import y` binds `y` but depends on
            # `x`, and only the latter is something that can be missing from an
            # environment. A relative import (`level > 0`) is by definition
            # inside this project and is not a package.
            if isinstance(node, ast.ImportFrom):
                if not node.level and node.module:
                    result.append(("__module__", node.module.split(".")[0], "module"))
            else:
                result.extend(("__module__", name, "module") for name in names)
    result.extend(ast_env_edges(tree))
    return sorted(set(result))

def regex_edges(path: Path, text: str, symbols: list[SymbolData]) -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    imported, modules = set(), set()
    quoted: set[str] = set()             # specifiers that were genuinely in quotes
    for match in ES_FROM.finditer(text):
        imported.update(_imported_names(match.group("body"))); modules.add(match.group("module"))
        quoted.add(match.group("module"))
    for pattern in (ES_BARE, ES_REQUIRE):
        for match in pattern.finditer(text):
            modules.add(match.group("module")); quoted.add(match.group("module"))
    bare = {match.group(1) for match in GENERIC_IMPORT.finditer(text)}
    modules.update(bare)
    for module in modules:
        stem = PurePosixPath(module.replace("\\", "/")).name.split(".")[0]
        if stem: result.append(("__module__", stem, "imports"))
    # Package edges come only from specifiers whose shape is known. A quoted
    # specifier is a module path. A bare one is `GENERIC_IMPORT`'s first token,
    # which is the module in `import os` but the *bound name* in
    # `import React from "react"` — so for the languages that quote their
    # modules it is ignored here, and the quoted form above is used instead.
    lang = language(path)
    for module in quoted:
        package = package_name(module, lang)
        if package: result.append(("__module__", package, "module"))
    if lang not in ("javascript", "typescript"):
        for token in bare - quoted:
            root = dotted_root(token)
            if root: result.append(("__module__", root, "module"))
    for name in imported:
        result.append(("__module__", name, "imports"))
        # The name occurs once in its own import statement. A second occurrence
        # means the file actually uses it, which is what `impact` needs to
        # answer "who breaks if this changes?".
        if len(re.findall(r"\b" + re.escape(name) + r"\b", text)) > 1:
            result.append(("__module__", name, "uses"))
    # Symbol-level edges inside the file. Only names this file imports or
    # defines are considered, which keeps builtins and member calls out.
    known = imported | {item.name for item in symbols}
    for index, line in enumerate(text.splitlines(), 1):
        stripped = re.sub(r"//.*$", "", line)
        source = _enclosing(symbols, index)
        for pattern, kind in ((CALL_SITE, "calls"), (JSX_SITE, "uses")):
            for match in pattern.finditer(stripped):
                target = match.group(1)
                if target in known and target != source:
                    result.append((source, target, kind))
    return sorted(set(result))
