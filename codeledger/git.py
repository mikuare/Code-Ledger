from __future__ import annotations

import subprocess
from pathlib import Path

def run(root: Path, *args: str) -> str | None:
    try:
        return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None

def head(root: Path) -> str | None:
    return run(root, "rev-parse", "HEAD")

def status(root: Path) -> dict[str, str]:
    out = run(root, "status", "--porcelain") or ""
    return {line[3:].strip(): line[:2].strip() or "modified" for line in out.splitlines() if len(line) >= 4}

def history(root: Path, path: str | None = None, limit: int = 20) -> list[dict[str, str]]:
    args = ["log", f"-{limit}", "--date=iso", "--format=%H%x09%ad%x09%an%x09%s"]
    if path: args += ["--", path]
    out = run(root, *args) or ""
    return [dict(zip(("commit", "date", "author", "subject"), line.split("\t", 3))) for line in out.splitlines() if line]

def commits(root: Path, limit: int = 200) -> list[dict[str, str]]:
    out = run(root, "log", f"-{limit}", "--date=iso-strict", "--format=%H%x09%P%x09%aI%x09%an%x09%s") or ""
    return [dict(zip(("commit_hash", "parent_hash", "timestamp", "author", "subject"), line.split("\t", 4))) for line in out.splitlines() if line]

def commit_files(root: Path, commit_hash: str) -> list[dict[str, str]]:
    out = run(root, "diff-tree", "--no-commit-id", "--name-status", "-r", commit_hash) or ""
    return [{"status": parts[0], "path": parts[-1]} for line in out.splitlines() if (parts := line.split("\t")) and len(parts) >= 2]
