from __future__ import annotations

import json
import fnmatch
import os
from dataclasses import dataclass, asdict, fields
from pathlib import Path

DEFAULT_IGNORES = [".git", ".ai/codeledger", "node_modules", "dist", "build", ".next", ".nuxt", ".cache", "coverage", ".tmp", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", "env", "target", "vendor", "tmp", "temp", "logs"]
SECRET_NAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}
# Only kinds that observe something *running* get a lifetime. Code-scoped
# evidence is invalidated by the code changing, never by the clock.
DEFAULT_EVIDENCE_TTL = {"RUNTIME_PROBE": 3600, "DEPLOY": 86400}
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
    # Advice only. When a runtime reports its context usage and it passes this,
    # `session_state` says a checkpoint is recommended; CodeLedger never acts on
    # it by itself and never interrupts an agent mid-task.
    checkpoint_threshold_pct: int = 80
    # How long a verification of each kind describes the present, in seconds.
    #
    # Most evidence is invalidated by code changing, not by the clock: a TEST
    # that passed at this commit is still true tomorrow if nothing moved, so
    # TEST and BUILD are deliberately absent and never expire. An observation of
    # something *running* is different — it was true when it was taken and says
    # nothing about now — so those kinds carry a lifetime.
    #
    # A kind absent from this map never expires. Lookup is case-insensitive
    # because `kind` is free text.
    evidence_ttl_seconds: dict[str, int] | None = None

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
        stored = DEFAULT_EVIDENCE_TTL if self.evidence_ttl_seconds is None else self.evidence_ttl_seconds
        # `load` promises that a hand-edited or newer-written config degrades to
        # defaults rather than making the tool unusable, and that promise has to
        # survive a value of the wrong *shape*, not just an unknown key. A
        # non-mapping here would otherwise raise out of every command.
        if not isinstance(stored, dict):
            stored = DEFAULT_EVIDENCE_TTL
        lifetimes: dict[str, int] = {}
        for kind, seconds in stored.items():
            try:
                # JSON has one number type, so a lifetime may arrive as a float.
                # Dropping it silently would read as "never expires", which is
                # the least safe reading of an unclear value.
                value = int(float(seconds))
            except (TypeError, ValueError):
                continue
            if value > 0:
                lifetimes[str(kind).upper()] = value
        self.evidence_ttl_seconds = lifetimes

    def evidence_ttl(self, kind: str | None) -> int | None:
        """Seconds this kind of evidence stays current, or None for 'forever'."""
        return (self.evidence_ttl_seconds or {}).get((kind or "").upper())

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
        rel = path.relative_to(root).as_posix() if path.is_absolute() else path.as_posix()
        return self.is_ignored_rel(rel, path.name, patterns or self.ignore_patterns(root))

    def is_ignored_rel(self, rel: str, name: str, patterns: list[str]) -> bool:
        """The same decision as `is_ignored`, on strings the walk already has.

        Discovery asks this once per entry, so building a `Path` here to take it
        apart again was a measurable share of a no-op refresh on a large tree.
        Callers that already know the relative path and the name skip that.
        """
        if name in SECRET_NAMES or name.startswith(".env."):
            return True
        for item in patterns:
            item = item.rstrip("/")
            if rel == item or rel.startswith(item + "/") or name == item or fnmatch.fnmatch(rel, item):
                return True
        return False

    def is_source_file(self, name: str) -> bool:
        # os.path.splitext matches Path().suffix here and costs far less.
        return os.path.splitext(name)[1].lower() in (self.source_extensions or DEFAULT_SOURCE_EXTENSIONS)
