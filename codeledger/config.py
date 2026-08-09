from __future__ import annotations

import json
import fnmatch
from dataclasses import dataclass, asdict, fields
from pathlib import Path

DEFAULT_IGNORES = [".git", ".ai/codeledger", "node_modules", "dist", "build", ".next", ".nuxt", ".cache", "coverage", ".tmp", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", "env", "target", "vendor", "tmp", "temp", "logs"]
SECRET_NAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}
DEFAULT_SOURCE_EXTENSIONS = [".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".java", ".kt", ".go", ".rs", ".rb", ".php", ".cs", ".cpp", ".c", ".h", ".hpp", ".swift", ".dart", ".vue", ".svelte", ".css", ".scss", ".html", ".sql"]

@dataclass
class Config:
    project_name: str
    root: str
    ignores: list[str]
    max_file_size: int = 2_000_000
    source_extensions: list[str] | None = None
    follow_symlinks: bool = False
    # An agent can legitimately think for minutes, so idleness is not death.
    # IDLE still counts as a live session; only STALE stops counting.
    session_idle_seconds: int = 900
    session_stale_seconds: int = 3600

    @classmethod
    def load(cls, root: Path) -> "Config":
        path = root / ".ai" / "codeledger" / "config.json"
        if path.exists():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                stored = {}
            # Keys this version does not know about are dropped rather than
            # raising, so a config written by a newer CodeLedger — or edited by
            # hand — degrades to defaults instead of making the tool unusable.
            known = {f.name for f in fields(cls)}
            settings = {"project_name": root.name, "root": str(root), "ignores": DEFAULT_IGNORES.copy()}
            settings.update({key: value for key, value in stored.items() if key in known})
            return cls(**settings)
        return cls(root.name, str(root), DEFAULT_IGNORES.copy(), 2_000_000, DEFAULT_SOURCE_EXTENSIONS.copy(), False)

    def __post_init__(self):
        self.ignores = list(dict.fromkeys(DEFAULT_IGNORES + (self.ignores or [])))
        self.source_extensions = [x.lower() if x.startswith(".") else "." + x.lower() for x in (self.source_extensions or DEFAULT_SOURCE_EXTENSIONS)]

    def save(self, root: Path) -> None:
        path = root / ".ai" / "codeledger" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    def ignore_patterns(self, root: Path) -> list[str]:
        patterns = list(self.ignores)
        ignore_file = root / "codeledger.ignore"
        if ignore_file.exists():
            try:
                patterns.extend(x.strip() for x in ignore_file.read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#"))
            except OSError:
                pass
        return list(dict.fromkeys(patterns))

    def is_ignored(self, path: Path, root: Path, patterns: list[str] | None = None) -> bool:
        rel = path.relative_to(root).as_posix() if path.is_absolute() else path.as_posix(); name = path.name
        if name in SECRET_NAMES or name.startswith(".env."):
            return True
        for item in patterns or self.ignore_patterns(root):
            item = item.rstrip("/")
            if rel == item or rel.startswith(item + "/") or name == item or fnmatch.fnmatch(rel, item):
                return True
        return False

    def is_source_file(self, name: str) -> bool:
        return Path(name).suffix.lower() in (self.source_extensions or DEFAULT_SOURCE_EXTENSIONS)
