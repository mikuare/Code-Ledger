from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

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
    for line in text.splitlines():
        match = re.match(r"\s*(?:import|from)\s+([A-Za-z_$][\w$.-]*)", line)
        if match:
            result.append(("__module__", match.group(1).split(".")[0], "imports"))
    return sorted(set(result))
