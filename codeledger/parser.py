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

def parse_file(path: Path, text: str) -> list[SymbolData]:
    if language(path) == "python":
        try:
            tree = ast.parse(text)
            lines = text.splitlines()
            result = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    start, end = node.lineno, getattr(node, "end_lineno", node.lineno)
                    kind = "class" if isinstance(node, ast.ClassDef) else "function"
                    signature = lines[start - 1].strip() if start <= len(lines) else node.name
                    result.append(SymbolData(node.name, kind, start, end, signature, digest("\n".join(lines[start-1:end]))))
            return sorted(result, key=lambda x: (x.start, x.name))
        except SyntaxError:
            pass
    patterns = [
        ("class", re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)")),
        ("interface", re.compile(r"\b(?:interface|type)\s+([A-Za-z_$][\w$]*)")),
        ("function", re.compile(r"(?:function\s+|def\s+|fn\s+|func\s+)([A-Za-z_$][\w$]*)\s*\(")),
        ("function", re.compile(r"(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>")),
        ("method", re.compile(r"^\s*(?:public|private|protected|static|async|export\s+)*\s*([A-Za-z_$][\w$]*)\s*\([^;{}]*\)\s*[{:]")),
    ]
    result = []
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        for kind, pattern in patterns:
            match = pattern.search(line)
            if match:
                name = match.group(1)
                result.append(SymbolData(name, kind, i, i, line.strip(), digest(line)))
                break
    return result

def dependencies(path: Path, text: str) -> list[tuple[str, str, str]]:
    """Return conservative (source symbol, target name, relationship) edges."""
    result: list[tuple[str, str, str]] = []
    if language(path) == "python":
        try:
            tree = ast.parse(text)
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
            return sorted(set(result))
        except SyntaxError:
            return []
    imported, modules = set(), set()
    for match in ES_FROM.finditer(text):
        imported.update(_imported_names(match.group("body"))); modules.add(match.group("module"))
    for pattern in (ES_BARE, ES_REQUIRE):
        modules.update(match.group("module") for match in pattern.finditer(text))
    for match in GENERIC_IMPORT.finditer(text):
        modules.add(match.group(1))
    for module in modules:
        stem = PurePosixPath(module.replace("\\", "/")).name.split(".")[0]
        if stem: result.append(("__module__", stem, "imports"))
    for name in imported:
        result.append(("__module__", name, "imports"))
        # The name occurs once in its own import statement. A second occurrence
        # means the file actually uses it, which is what `impact` needs to
        # answer "who breaks if this changes?".
        if len(re.findall(r"\b" + re.escape(name) + r"\b", text)) > 1:
            result.append(("__module__", name, "uses"))
    return sorted(set(result))
