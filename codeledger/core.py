from __future__ import annotations

import json
import re
import shutil
import sqlite3
import sys
import uuid
import os
import socket
import tempfile
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from pathlib import Path
from .agents import UNKNOWN, identify, resolve_model
from .config import Config
from .db import connect
from .git import commit_files, commits, head, status as git_status
from .parser import digest, digest_bytes, language
from .providers import FULL, SHALLOW, analyze as analyze_source, capabilities, version_for

LIKE_ESCAPE = str.maketrans({"\\": r"\\", "%": r"\%", "_": r"\_"})
# Words too generic to define a task boundary from a file path.
STOPWORDS = {"add", "also", "and", "any", "app", "back", "been", "code", "create", "change", "changes", "current", "delete", "each", "file", "files", "fix", "from", "have", "into", "make", "more", "must", "need", "new", "not", "only", "page", "please", "remove", "should", "some", "that", "the", "them", "then", "there", "this", "update", "use", "using", "when", "where", "which", "with", "without", "work"}

NOW = lambda: datetime.now(timezone.utc).isoformat()

# How discovery decides whether this filesystem is slow enough to be worth
# threading. The gap it separates is enormous — microseconds on a local volume
# against milliseconds across /mnt/c — so the exact threshold is not delicate.
SLOW_FS_SAMPLE = 48
SLOW_FS_SECONDS = 0.0001
STAT_WORKERS = 16
# How recently a file must have been modified for its size and mtime to be
# treated as insufficient evidence. It only has to exceed the filesystem's
# timestamp granularity; a couple of seconds is far past any of them and costs
# nothing, because a file this recent is one an agent just wrote.
RACE_WINDOW_NS = 2_000_000_000

# A session is only "live" while something is still happening in it. ACTIVE and
# IDLE both mean the agent is still there — an agent can legitimately think for
# minutes — so both count when deciding who is working on the project. The rest
# are terminal, and are kept rather than deleted: history is the product.
LIVE = ("active", "idle")
# What a checkpoint may hold. The first four are the agent's own words; the rest
# point at rows that already exist, so a checkpoint never becomes a second copy
# of the ledger that can disagree with it.
CHECKPOINT_KINDS = ("accomplished", "unresolved", "failed_attempt", "question",
                    "decision", "issue", "verification", "file", "symbol")
# Directories that hold siblings rather than being an area themselves. A change
# affecting `pages/Landing` and `pages/Payment` affects two areas, not one — so
# under these the file is the area, and everywhere else the directory is.
CONTAINER_DIRS = {"pages", "routes", "views", "screens", "components", "modules", "features",
                  "apps", "packages", "services", "controllers", "handlers", "widgets", "containers"}
# Where code that is shared on purpose tends to live, and what it tends to be
# called. Only ever a signal — never sufficient on its own to warn, because a
# path is not evidence of anything by itself.
SHARED_PATH_HINTS = {"shared", "common", "core", "global", "theme", "themes", "styles", "design-system",
                     "providers", "provider", "context", "contexts", "store", "stores", "state",
                     "hooks", "lib", "libs", "util", "utils", "config", "ui"}
SHARED_NAME_HINTS = ("provider", "context", "theme", "store", "config", "global", "shared", "registry", "layout")
# Thresholds for calling a request ambiguous. Deliberately conservative: a
# warning that fires on ordinary local work trains an agent to ignore warnings.
AMBIGUITY_MIN_AREAS = 2
AMBIGUITY_LONE_AREAS = 3
AMBIGUITY_LONE_FILES = 5
# Roughly four characters per token. Deliberately crude: CodeLedger takes no
# tokenizer dependency, and every figure derived from this is labelled an
# estimate rather than presented as a measurement.
CHARS_PER_TOKEN = 4
# How many files or symbols a single change record shows before it reports a
# count instead. One broad refactor or an initial index touches every file in
# the repository, and returning that list inside a change record made the
# answer to "is there anything to resume?" grow with the size of the project.
# The totals travel with the list, so a truncated answer never looks complete.
CHANGE_LIST_LIMIT = 20
# 'completed' and 'failed' predate this vocabulary and still exist in older
# databases. They mean the same as 'ended' and are never rewritten.
ENDED = ("ended", "completed", "failed")
TERMINAL = ENDED + ("stale", "crashed")

def mcp_launch_command(root: Path) -> list[str]:
    """The argv an external agent must use to launch *this* CodeLedger's MCP server.

    Registering the bare name `codeledger` was the defect: the agent resolves it
    against its own PATH, and a desktop app, an IDE extension or a
    service-started agent does not inherit the shell that activated a project
    venv. So a project-local install — the recommended layout — was exactly the
    case the registration could not express, and the failure was silent.

    Resolution prefers the console script beside the interpreter that is running
    right now, because that is the one install we can prove exists. Every branch
    returns an absolute path or an explicit interpreter invocation; none returns
    a bare name for someone else's PATH to resolve. Nothing here is specific to
    a machine — it is all derived from the running process.
    """
    # Deliberately NOT resolved: a virtualenv's `bin/python` is a symlink to the
    # system interpreter, so resolving it walks straight out of the venv and
    # loses the very install we are trying to find. `sys.executable` as given is
    # the venv path, and its sibling `bin/` is where the console script lives.
    interpreter = Path(sys.executable)
    script = interpreter.parent / ("codeledger.exe" if os.name == "nt" else "codeledger")
    if script.is_file():
        return [str(script), "mcp", "--root", str(root)]
    # Installed, but the console script is not beside this interpreter (a
    # `pipx` layout, or an interpreter invoked directly). argv[0] is the next
    # best evidence of how this process was actually started.
    argv0 = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if argv0 and argv0.is_file() and argv0.stem == "codeledger":
        return [str(argv0), "mcp", "--root", str(root)]
    found = shutil.which("codeledger")
    if found:
        # On PATH for us. Register the absolute path anyway, so the agent does
        # not have to have the same PATH we do.
        return [str(Path(found).absolute()), "mcp", "--root", str(root)]
    # Always available: this interpreter can import codeledger, because it is
    # running it. `-m` needs no console script and no PATH entry at all.
    return [str(interpreter), "-m", "codeledger.cli", "mcp", "--root", str(root)]

def parse_time(value: str | None) -> datetime | None:
    if not value: return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

def process_alive(pid: int | None) -> bool | None:
    """Is this process still running? None means the question cannot be answered.

    Signal 0 performs the permission and existence checks without delivering
    anything. A process owned by another user answers PermissionError, which
    still proves it exists. PIDs are recycled, so a live PID is never trusted on
    its own — `reconcile_sessions` also requires a recent heartbeat.
    """
    if not pid or pid <= 0: return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None

@dataclass
class DiscoveryMetrics:
    directories_visited: int = 0
    directories_skipped: int = 0
    files_discovered: int = 0
    files_ignored: int = 0
    files_skipped_large: int = 0
    files_skipped_type: int = 0
    permission_errors: int = 0
    broken_symlinks: int = 0
    files_stat: int = 0
    stat_mode: str = "serial"
    paths: list[str] = field(default_factory=list)
    large_files: list[tuple[str, int, int]] = field(default_factory=list)

    def as_dict(self):
        return {"directories_visited": self.directories_visited, "directories_skipped": self.directories_skipped, "files_discovered": self.files_discovered, "files_ignored": self.files_ignored, "files_skipped_large": self.files_skipped_large, "files_skipped_type": self.files_skipped_type, "permission_errors": self.permission_errors, "broken_symlinks": self.broken_symlinks, "files_stat": self.files_stat, "stat_mode": self.stat_mode}

class Ledger:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.config = Config.load(self.root)
        self.db = connect(self.root)
        self._last_discovery_metrics = DiscoveryMetrics()

    def init(self, quick: bool = False, verbose: bool = False) -> dict:
        self.config.save(self.root)
        self.db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('project_name',?)", (self.config.project_name,))
        self.db.commit()
        self._write_instructions()
        result = self.refresh(changed_only=False, record=False, analyze=not quick, verbose=verbose)
        result["initialized"] = True
        result["quick"] = quick
        return result

    def _write_instructions(self) -> None:
        text = """# CodeLedger Protocol

Before changing code, analyze the user request (CLI: `codeledger prompt \"<task>\"`; MCP: `codeledger_analyze_prompt`), then query CodeLedger pre-change intelligence (CLI: `codeledger plan \"<task>\"`; MCP: `codeledger_get_plan`) and context (MCP: `codeledger_get_context`). Review relevant symbols, history, issues, decisions, impact, risk, constraints, acceptance criteria, and suggested tests before inspecting broad source areas. State your proposed files, implementation, and tests, then submit it for alignment (CLI: `codeledger handshake \"<task>\" --ai-plan \"<plan>\"`; MCP: `codeledger_task_handshake`). If the handshake warns, revise the plan before editing. Prefer targeted inspection; avoid repository-wide scans when CodeLedger says they are unnecessary. After changing code, refresh/record changed files (CLI: `codeledger refresh --changed`; MCP: `codeledger_refresh`), review any scope `WARNING`, run affected tests, and record verification evidence. If verification fails after a previous pass, query regressions (MCP: `codeledger_get_regressions`).

## Before writing new code: what already exists, and what a change would reach

`plan` answers two questions the source alone does not, and both change what you should do next.

**`shared_dependencies` / `blast_radius`** — the symbol you are about to change is used elsewhere. The report names the files and the areas a change reaches. Read them before editing. Do not treat a small or empty dependent list as proof a change is contained: check `blast_radius.confidence` and `coverage_caveat` first, because a shallowly analysed file records its edges conservatively and `LOW` confidence means the evidence is incomplete, not that the change is safe.

**`scope_ambiguity`** — the request touches something shared and never says how widely it should apply. Ask the user which scope they meant, offering the affected areas. Do not silently pick one, and do not change all of them on the assumption that wider is safer. When the request already names a scope, no question is raised and none should be invented.

`handshake` adds a third: **`duplicate_implementation`** means your plan creates something this project appears to already have, and lists the existing symbols to inspect. Read them and reuse or extend them if they fit. It is a recommendation, not a rejection — a new implementation is sometimes right, and when it is, say why rather than proceeding silently.

## Continuing work from a previous session

Your context window is temporary; this project's memory is not. At the **start** of a session, before reading source files, ask whether this task has been worked on before (CLI: `codeledger resume \"<task>\"`; MCP: `codeledger_get_resume` with the user's request as `task`). Selection is by relevance, so an unrelated previous task is never loaded — a `NO_RELEVANT_CHECKPOINT` answer means start fresh. When a checkpoint is returned, read `next_action` first, then `failed_attempts`: those are approaches already known not to work, and repeating them is the specific waste this exists to prevent. Everything in a resume package has been re-checked against the current source; anything that no longer holds is listed under `stale_items` and must not be trusted.

Before a session **ends**, or when your context is running low, record a checkpoint (MCP: `codeledger_get_session_state`, then `codeledger_record_checkpoint`). CodeLedger assembles what it observed — changes, files, symbols, verifications — but it cannot see your conversation, so you must supply `goal`, `current_state`, what was accomplished, what failed, what is unresolved, and the single `next_action` that would most help whoever continues. If your runtime reports context usage, pass `context_window` and `context_used`; if it does not, omit them and everything still works.

A checkpoint is a summary, and summaries rank below source code, the filesystem, tests, and recorded changes. Never let one override what the code currently says.

## Working alongside another agent

More than one agent may be editing this project. At the start of a turn, ask what changed while you were not looking (CLI: `codeledger since --agent <your name>`; MCP: `codeledger_get_changes_since` with your own agent name). It reports the files and symbols another agent changed since you last recorded anything, so you do not overwrite work you cannot see. If another agent is live, check for overlap before editing (MCP: `codeledger_check_conflicts` with your name and the files/symbols you intend to touch). A shared symbol is a stronger warning than a shared file: re-read that symbol before changing it.

Claim your own edits by calling `codeledger_refresh` (MCP) or `codeledger refresh --changed --agent <your name> --request \"<task>\"` after you finish. An agent refreshing on its own behalf is the only HIGH-confidence record of who changed what. The `watch` process observes the filesystem, which cannot show which process wrote a file, so everything it records is `unknown` at LOW confidence — never assume a LOW-confidence change was yours, and never treat one as evidence that nobody else is working.

## If a task did not work the first time

Before retrying a request that produced no visible result, ask what the previous attempt actually did (CLI: `codeledger progress \"<task>\"`; MCP: `codeledger_get_progress`). Do not re-read the repository to work it out. The answer is one of:

- `NO_EFFECT` — earlier attempts changed no symbol, only text or nothing. The edit is not reaching the code that runs. Confirm which file is actually imported and executed before editing again.
- `REPEATING` — the same symbols have been edited repeatedly and verification still fails. Editing them again is unlikely to help. Re-read the failure output, widen the search with `codeledger impact <symbol>`, or ask the user whether the request describes the real problem.
- `UNVERIFIED` — real symbols changed but nothing was verified. Record evidence with `codeledger verify-run project project TEST -- <command>`.
- `VERIFIED` — verification passed after the last attempt. Stop editing and report it.

A refresh reports `effect` for every attempt: `symbols-changed`, `text-only`, or `none`. Treat anything other than `symbols-changed` as a task that has not been done yet, whatever the edit appeared to do.

For automatic lifecycle tracking, run the agent through `codeledger run --agent <name> --request \"<task>\" -- <agent command>`, or run `codeledger watch --agent <name>` in a second terminal while the agent edits this project. MCP-capable agents may launch `codeledger mcp --root <project>`.
"""
        for name in ("AGENTS.md", "CLAUDE.md", "CODEX.md"):
            path = self.root / name
            if not path.exists() or "CodeLedger Protocol" in path.read_text(encoding="utf-8", errors="ignore"):
                path.write_text(text, encoding="utf-8")
        integration = self.root / ".ai" / "codeledger" / "agent-integration.md"
        integration.parent.mkdir(parents=True, exist_ok=True)
        integration.write_text("# CodeLedger Agent Integration\n\n## One-time Codex setup\n\nRun `codeledger setup-codex` from this project. It registers the local MCP server with Codex using the absolute project path. Start a new Codex session after setup if Codex was already running.\n\n## Persistent workflow\n\nRun `codeledger watch --agent codex` in a second terminal and leave it running while Codex works. The watcher records changed files and symbols without requiring `status` or `changes` after every task.\n\n## MCP tools\n\nCodex can call `codeledger_get_resume`, `codeledger_get_context`, `codeledger_find_symbol`, `codeledger_get_impact`, `codeledger_get_history`, `codeledger_get_recent_changes`, `codeledger_get_issues`, `codeledger_get_decisions`, `codeledger_record_change`, `codeledger_mark_verified`, `codeledger_get_session_state`, and `codeledger_record_checkpoint` during an ongoing conversation.\n\n## Continuing across sessions\n\nCall `codeledger_get_resume` at the start of a session and `codeledger_record_checkpoint` before it ends. The MCP server starts and ends its own session, so no setup is needed for this to work.\n", encoding="utf-8")

    def _stat_all(self, candidates: list[tuple[str, str]], metrics: DiscoveryMetrics):
        """Stat every candidate file, in parallel only where that actually helps.

        `stat` is the whole cost of discovery on a large tree, and its price
        varies by three orders of magnitude: about 2us on a local ext4 volume,
        about 1ms across the WSL /mnt/c boundary, where every call is a
        round-trip to the Windows filesystem driver. Those round-trips overlap
        almost perfectly, so a thread pool turns a second of waiting into a
        tenth of one — but on a fast volume the pool costs far more than the
        work, turning 1.4ms into 25ms.

        So the choice is measured rather than guessed: stat a sample serially,
        time it, and only spread the rest across threads if this filesystem is
        actually slow enough to pay for them. No filesystem detection, no
        configuration, and it adapts to volumes this code has never seen.
        """
        results: list[tuple[str, str, os.stat_result]] = []

        def record(path: str, rel: str):
            try:
                return (path, rel, os.stat(path, follow_symlinks=False))
            except (OSError, ValueError):
                metrics.permission_errors += 1
                return None

        metrics.files_stat = len(candidates)
        sample = candidates[:SLOW_FS_SAMPLE]
        started = time.perf_counter()
        results.extend(item for item in (record(path, rel) for path, rel in sample) if item)
        rest = candidates[len(sample):]
        if not rest:
            return results
        per_file = (time.perf_counter() - started) / max(1, len(sample))
        if per_file < SLOW_FS_SECONDS:
            results.extend(item for item in (record(path, rel) for path, rel in rest) if item)
            return results
        # A slow volume: overlap the waiting. Threads only, no processes and no
        # daemon — `os.stat` releases the GIL, which is the entire reason this
        # works and the reason nothing here needs to be shared or locked.
        metrics.stat_mode = "parallel"
        with ThreadPoolExecutor(max_workers=STAT_WORKERS) as pool:
            results.extend(item for item in pool.map(lambda pair: record(*pair), rest, chunksize=32) if item)
        return results

    def _discover(self, verbose: bool = False):
        """Discover source files without descending into ignored directories.

        The walk decides *which* files matter using only what `readdir` already
        returned; nothing is stat-ed until the set is known, so the expensive
        call is made once per surviving file and never for a pruned directory.
        """
        metrics = DiscoveryMetrics(); patterns = self.config.ignore_patterns(self.root)
        prefix = len(str(self.root)) + 1
        candidates: list[tuple[str, str]] = []
        stack = [self.root]
        while stack:
            directory = stack.pop(); metrics.directories_visited += 1
            try:
                entries = os.scandir(directory)
            except (OSError, PermissionError):
                metrics.permission_errors += 1
                continue
            try:
                for entry in entries:
                    rel = entry.path[prefix:].replace(os.sep, "/")
                    try:
                        if entry.is_symlink():
                            if not self.config.follow_symlinks:
                                if not os.path.exists(entry.path): metrics.broken_symlinks += 1
                                metrics.directories_skipped += 1
                                if verbose: print(f"Skipping symlink: {rel}")
                                continue
                        if entry.is_dir(follow_symlinks=self.config.follow_symlinks):
                            if self.config.is_ignored_rel(rel, entry.name, patterns):
                                metrics.directories_skipped += 1
                                if verbose: print(f"Skipping directory: {rel}/")
                            else:
                                stack.append(Path(entry.path))
                            continue
                        if self.config.is_ignored_rel(rel, entry.name, patterns):
                            metrics.files_ignored += 1
                            continue
                        if not self.config.is_source_file(entry.name):
                            metrics.files_skipped_type += 1
                            continue
                        candidates.append((entry.path, rel))
                    except (OSError, PermissionError):
                        metrics.permission_errors += 1
            finally:
                entries.close()
        for path, rel, stat in self._stat_all(candidates, metrics):
            if stat.st_size > self.config.max_file_size:
                metrics.files_skipped_large += 1
                metrics.large_files.append((rel, stat.st_size, stat.st_mtime_ns))
                if verbose: print(f"Skipping large file: {rel} ({stat.st_size} bytes)")
                continue
            metrics.files_discovered += 1; metrics.paths.append(rel)
            yield Path(path), rel, stat.st_size, stat.st_mtime_ns, metrics
        self._last_discovery_metrics = metrics

    def _files(self):
        for path, _rel, _size, _mtime_ns, _metrics in self._discover():
            yield path

    def _session_rows(self, statuses: tuple[str, ...] | None = None) -> list:
        sql = ("SELECT s.*, a.name AS agent FROM sessions s LEFT JOIN agents a ON a.id=s.agent_id"
               + (f" WHERE s.status IN ({','.join('?' * len(statuses))})" if statuses else "")
               + " ORDER BY s.id DESC")
        return self.db.execute(sql, statuses or ()).fetchall()

    def reconcile_sessions(self, now: datetime | None = None) -> dict:
        """Decide which sessions are still real, from evidence rather than hope.

        A session was only ever closed by the Ctrl+C handler in `watch`. Every
        other ending — closing WSL, closing the IDE, `kill -9`, a crash, power
        loss — left `status='active'` in the database permanently. That phantom
        then counted as a competing agent forever, so the watcher recorded every
        later edit as `unknown` and the conflict warnings fired against an agent
        that died days ago.

        Two independent signals are used, because neither is sufficient alone. A
        dead PID is strong evidence the session is gone, but PIDs are recycled,
        so a *live* PID proves little on its own; a stale heartbeat covers that
        case, and also covers sessions recorded on another machine where the PID
        means nothing here. Nothing is deleted — the row keeps its history and
        gains a status and a reason.
        """
        now = now or datetime.now(timezone.utc)
        idle_after = timedelta(seconds=self.config.session_idle_seconds)
        stale_after = timedelta(seconds=self.config.session_stale_seconds)
        host = socket.gethostname()
        transitions = []
        for row in self._session_rows(LIVE):
            seen_at = parse_time(row["last_activity_at"]) or parse_time(row["last_heartbeat_at"]) or parse_time(row["start_time"])
            age = now - seen_at if seen_at else None
            pid, same_host = row["pid"], (row["host"] or host) == host
            alive = process_alive(pid) if same_host else None
            if alive is False:
                status, reason = "crashed", f"process {pid} is no longer running on {host}"
            elif age is None:
                status, reason = "unknown", "the session has no usable timestamp, so liveness cannot be determined"
            elif age > stale_after:
                status, reason = "stale", f"no activity for {int(age.total_seconds())}s (limit {int(stale_after.total_seconds())}s)"
            elif age > idle_after:
                status, reason = "idle", f"no activity for {int(age.total_seconds())}s but within the stale limit"
            else:
                status, reason = "active", "recent activity"
            if status != row["status"]:
                self.db.execute("UPDATE sessions SET status=?,status_reason=?,end_time=COALESCE(end_time,?) WHERE session_id=?",
                                (status, reason, now.isoformat() if status in TERMINAL else None, row["session_id"]))
                transitions.append({"session_id": row["session_id"], "agent": row["agent"] or "unknown",
                                    "from": row["status"], "to": status, "reason": reason})
        if transitions: self.db.commit()
        return {"transitions": transitions, "reconciled": len(transitions)}

    def sessions(self, reconcile: bool = True) -> dict:
        """Every session grouped by what is actually true of it now."""
        if reconcile: self.reconcile_sessions()
        grouped: dict[str, list] = {}
        for row in self._session_rows():
            grouped.setdefault(row["status"], []).append({
                "session_id": row["session_id"], "agent": row["agent"] or "unknown", "pid": row["pid"],
                "host": row["host"], "started_at": row["start_time"],
                "last_activity_at": row["last_activity_at"] or row["last_heartbeat_at"] or row["start_time"],
                "ended_at": row["end_time"], "request": row["request"],
                "working_directory": row["working_directory"], "status_reason": row["status_reason"]})
        return {"by_status": grouped, "live": sum(len(grouped.get(name, [])) for name in LIVE),
                "counts": {name: len(rows) for name, rows in sorted(grouped.items())}}

    def active_agents(self, reconcile: bool = True) -> list[str]:
        """Agents genuinely working here — reconciled first, never assumed."""
        if reconcile: self.reconcile_sessions()
        return sorted({row["agent"] for row in self._session_rows(LIVE) if row["agent"]})

    def touch_session(self, session_id: str, heartbeat: bool = False, revive: bool = False) -> None:
        """Record that a session is still alive. Cheap enough to call per poll."""
        if not session_id: return
        column = "last_heartbeat_at" if heartbeat else "last_activity_at"
        # A heartbeat is also activity; recording both keeps a session that only
        # ever heartbeats from ageing into STALE while its process is healthy.
        cursor = self.db.execute(f"UPDATE sessions SET {column}=?, last_activity_at=? WHERE session_id=? AND status IN ({','.join('?' * len(LIVE))})",
                                 (NOW(), NOW(), session_id, *LIVE))
        # A long-lived server that goes quiet for longer than the stale limit —
        # a user away from the keyboard — is reconciled to STALE, and the update
        # above then matches nothing, leaving it dead for the rest of the
        # process's life. Work arriving on it is the strongest possible evidence
        # it is alive, so the caller that owns the process can say so.
        if revive and not cursor.rowcount:
            self.db.execute("UPDATE sessions SET status='active',status_reason=?,end_time=NULL,last_activity_at=?,last_heartbeat_at=? WHERE session_id=?",
                            ("activity resumed on a session that had been reconciled as inactive", NOW(), NOW(), session_id))
        self.db.commit()

    def _begin_immediate(self) -> None:
        """Acquire the write lock now, so subsequent reads see committed state.

        `busy_timeout` makes this wait for another agent rather than fail. If a
        transaction is somehow already open the existing one is kept — starting
        a nested transaction is an error, and the caller's work still commits.
        """
        try:
            self.db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "within a transaction" not in str(exc):
                raise

    def _inherit_session(self, agent: str, session: str, request: str, observed: bool):
        """Recover the task a nameless refresh belongs to, without naming an author.

        A bare `codeledger refresh --changed` arrives with no session and no
        request, and was recorded as `NOT RECORDED` even while a single live
        session in the same database held the request text. Losing that is a
        propagation failure — the request is the most valuable thing the ledger
        keeps, because it is the only record of *why* a change was made.

        Authorship is a different question and is deliberately not answered
        here. A live session proves work is underway; it does not prove who ran
        this command, and a developer typing `refresh` in a second terminal
        looks identical. So the session and its request are carried over, the
        agent is not, and the change reads: during this session, for this
        request, an edit nobody claimed.

        - an explicitly named agent or request always wins; the caller knows better
        - an observed edit inherits nothing: the watcher already has its own rule
        - two live sessions is ambiguous evidence, so nothing is taken
        """
        if observed or (agent and agent != "unknown"):
            return session, request, None
        # Reconcile first: a crashed session still says 'active' in the row, and
        # inheriting from a process that died last week would be worse than
        # recording nothing at all.
        self.reconcile_sessions()
        named = [row for row in self._session_rows(LIVE) if (row["agent"] or "unknown") != "unknown"]
        if len(named) != 1:
            return session, request, None
        row = named[0]
        return session or row["session_id"], request or (row["request"] or ""), row

    def _attribute(self, agent: str, observed: bool, inherited=None) -> tuple[str, dict]:
        """Decide who to credit for an edit, and how well that is actually known.

        The filesystem records that a file changed. It does not record which
        process changed it, and no amount of watching recovers that. So the two
        routes into the ledger carry genuinely different evidential weight, and
        the ledger stores which one it was rather than flattening both into a
        name that reads equally confident.
        """
        actor = agent or "unknown"
        if not observed:
            if actor == "unknown":
                reason = "A refresh was recorded without an agent name, so authorship is unknown."
                if inherited is not None:
                    # The task is recovered; the author still is not. Saying so
                    # in the same breath keeps the two from being confused.
                    reason += (f" It was recorded during {inherited['agent']}'s live session "
                               f"({inherited['session_id']}), and that session's request has been attached so the "
                               "change is not left without a reason — but a live session does not prove who ran "
                               "this command, so the edit is not credited to that agent.")
                return actor, {"source": "unattributed-refresh", "confidence": "UNKNOWN", "reason": reason}
            # The agent called refresh itself: it is reporting its own work.
            return actor, {"source": "explicit-agent-refresh", "confidence": "HIGH",
                           "reason": f"{actor} recorded this refresh on its own behalf."}
        # An observed edit is never credited to a name. `watch --agent codex`
        # says who started the watcher, not who wrote the file — a developer, an
        # editor or a formatter produces an identical filesystem event. Crediting
        # the only live agent was still an inference the evidence cannot carry,
        # so the live agents are reported as context and authorship stays unknown.
        live = [name for name in self.active_agents() if name != "unknown"]
        if live:
            return "unknown", {"source": "filesystem-watcher", "confidence": "LOW",
                               "reason": ("Edits were observed, not performed, while sessions were active for "
                                          f"{', '.join(sorted(set(live)))}. The filesystem cannot show which agent wrote a "
                                          "file, so authorship is recorded as unknown. Have each agent call "
                                          "`codeledger_refresh` itself to claim its own edits.")}
        return "unknown", {"source": "filesystem-watcher", "confidence": "LOW",
                           "reason": ("An edit was observed with no agent session active. It may have been made by a "
                                      "developer, an editor, or a tool, so authorship is recorded as unknown.")}

    def refresh(self, *args, **kwargs) -> dict[str, int | str | None | list[str] | dict]:
        """Index changed files as one transaction, releasing the lock on failure.

        A refresh writes many rows before committing, so an exception part-way
        through leaves an open write transaction. SQLite allows one writer, and
        `watch` and the MCP server are long-lived: without this rollback a single
        failed refresh would hold the write lock for the rest of the process's
        life, and every other agent's write would block until it timed out.
        """
        try:
            return self._refresh(*args, **kwargs)
        except BaseException:
            self.db.rollback()
            raise

    def _refresh(self, changed_only: bool = True, agent: str = "unknown", session: str = "", request: str = "", record: bool = True, analyze: bool = True, verbose: bool = False, include_git_status: bool = False, observed: bool = False, only: set[str] | None = None, settle_seconds: float = 0.0) -> dict[str, int | str | None | list[str] | dict]:
        """Index what changed.

        `settle_seconds` holds back files edited within the last few moments, so
        that whoever made the edit still has time to report it themselves. It
        exists because indexing is destructive to attribution: once a file
        matches the index, the agent's own refresh finds nothing left to record,
        and the edit is credited to `unknown` with no request attached — which
        also strips the request text `progress` needs to spot a repeated attempt.
        Only `watch` sets this; an agent reporting its own work must never wait.
        """
        pending = 0
        started = time.perf_counter(); statuses = git_status(self.root) if include_git_status and (self.root / ".git").exists() and analyze else {}
        session, request, inherited = self._inherit_session(agent, session, request, observed)
        actor, attribution = self._attribute(agent, observed, inherited)
        attribution_note = attribution["reason"] if attribution["confidence"] != "HIGH" else ""
        if session: self.touch_session(session)
        # Take the write lock before reading anything the indexing pass decides
        # on. A deferred transaction begins at the *first write*, so every read
        # before that saw a snapshot which another agent's commit had already
        # superseded: two agents refreshing together each re-indexed the same
        # edit and each recorded a change for it, inventing authorship for one
        # of them. Readers are never blocked by this — WAL keeps `context`,
        # `since` and `impact` fast while writers take turns.
        self._begin_immediate()
        seen, changed_paths, changed_symbols = set(), [], []
        added, modified, deleted, symbols = 0, 0, 0, 0
        # Captured before the walk: a file whose mtime is at or after this may
        # have been written during the scan itself, so it cannot be trusted.
        scan_started_ns = time.time_ns()
        discovery_started = time.perf_counter(); discovered = list(self._discover(verbose=verbose))
        # A targeted re-analysis looks at a named handful of files. It is a
        # partial view of the tree, so it must not also decide what was deleted.
        if only is not None: discovered = [item for item in discovered if item[1] in only]
        # `settle_seconds` leaves freshly-edited files alone so the agent that
        # made them can still claim its own work. Only `watch` sets it; see the
        # note on its parameter for why recording an edit early is destructive.
        if settle_seconds > 0:
            cutoff_ns = scan_started_ns - int(settle_seconds * 1_000_000_000)
            settled = [item for item in discovered if item[3] <= cutoff_ns]
            pending = len(discovered) - len(settled)
            discovered = settled
        discovery_seconds = time.perf_counter() - discovery_started
        metrics = discovered[-1][4] if discovered else self._last_discovery_metrics; hash_seconds = 0.0; parse_seconds = 0.0
        for rel, size, mtime_ns in metrics.large_files:
            self.db.execute("INSERT INTO files(path,language,size,mtime,mtime_ns,status,analysis_version) VALUES(?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime=excluded.mtime,mtime_ns=excluded.mtime_ns,status='skipped_large_file'", (rel, language(Path(rel)), size, mtime_ns / 1_000_000_000, mtime_ns, "skipped_large_file", "metadata"))
        for index, (path, rel, size, mtime_ns, _discovery_metrics) in enumerate(discovered, 1):
            if verbose and (index == 1 or index % 50 == 0 or index == len(discovered)):
                phase = "Indexing" if analyze else "Metadata indexing"
                print(f"{phase}: {index}/{len(discovered)}", flush=True)
            seen.add(rel)
            old = self.db.execute("SELECT * FROM files WHERE path=?", (rel,)).fetchone()
            # A file is up to date only if the analyser that produced its index
            # is still the one that would run now; otherwise installing (or
            # losing) grammars would leave stale, weaker analysis in place.
            current_version = version_for(path) if analyze else None
            fresh = bool(old) and old["status"] == "current" and old["analysis_version"] == current_version
            # Size and mtime are only proof for a file that has settled. A
            # filesystem timestamp is granular — a few milliseconds — so a
            # rewrite can share an mtime with the write before it, and if it also
            # preserves the size (a digit, a comparison, a boolean) then both
            # match what was indexed and the edit is invisible. It stays
            # invisible until something else touches the file. So anything
            # modified within the last moment is settled by reading it instead.
            # Steady state pays nothing: no file has a recent mtime, so nothing
            # is hashed. This is git's "racily clean" rule.
            racy = mtime_ns > scan_started_ns - RACE_WINDOW_NS
            if changed_only and fresh and not racy and old["size"] == size and old["mtime_ns"] == mtime_ns:
                continue
            hash_started = time.perf_counter(); raw = path.read_bytes(); file_hash = digest_bytes(raw); hash_seconds += time.perf_counter() - hash_started
            if changed_only and fresh and old["hash"] == file_hash:
                self.db.execute("UPDATE files SET size=?,mtime_ns=?,mtime=? WHERE path=?", (size, mtime_ns, mtime_ns / 1_000_000_000, rel)); continue
            if not analyze:
                if old:
                    self.db.execute("UPDATE files SET size=?,mtime_ns=?,mtime=?,hash=?,status='unindexed',git_status=? WHERE path=?", (size, mtime_ns, mtime_ns / 1_000_000_000, file_hash, statuses.get(rel, "current"), rel))
                else:
                    self.db.execute("INSERT INTO files(path,language,size,hash,mtime,mtime_ns,git_status,status,last_analyzed,analysis_version) VALUES(?,?,?,?,?,?,?,?,?,?)", (rel, language(path), size, file_hash, mtime_ns / 1_000_000_000, mtime_ns, statuses.get(rel, "current"), "unindexed", None, "metadata")); added += 1
                changed_paths.append(rel); continue
            parse_started = time.perf_counter(); text = raw.decode("utf-8", errors="replace"); parsed, edges, provider, coverage = analyze_source(path, text); parse_seconds += time.perf_counter() - parse_started; now = NOW()
            changed_paths.append(rel)
            if not old:
                cursor = self.db.execute("INSERT INTO files(path,language,size,hash,mtime,mtime_ns,git_status,status,last_analyzed,analysis_version,analysis_provider,coverage,last_modified_by,last_modified_session) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (rel, language(path), size, file_hash, mtime_ns / 1_000_000_000, mtime_ns, statuses.get(rel, "current"), "current", now, current_version, provider, coverage, actor, session)); added += 1; file_id = cursor.lastrowid
            else:
                self.db.execute("UPDATE files SET language=?,size=?,hash=?,mtime=?,mtime_ns=?,git_status=?,status='current',last_analyzed=?,analysis_version=?,analysis_provider=?,coverage=?,last_modified_by=?,last_modified_session=? WHERE path=?", (language(path), size, file_hash, mtime_ns / 1_000_000_000, mtime_ns, statuses.get(rel, "current"), now, current_version, provider, coverage, actor, session, rel)); modified += 1; file_id = old["id"]
            # Every prior symbol for this file, not only the live ones. Matching
            # on active rows alone meant a recreated symbol missed the revive
            # path and was inserted again, leaving the file with two rows for
            # one name — one permanently 'deleted' and still carrying the line
            # and signature it had when it was removed. `ORDER BY status='active'`
            # puts the live row last so it wins the mapping in a database that
            # already collected duplicates from that bug.
            old_symbols = {r["name"]: r for r in self.db.execute("SELECT * FROM symbols WHERE file_id=? ORDER BY status='active'", (file_id,))}
            current = set()
            for item in parsed:
                current.add(item.name); prior = old_symbols.get(item.name)
                revived = bool(prior) and prior["status"] != "active"
                if prior and prior["hash"] == item.hash and not revived:
                    # Same content, possibly shifted by an edit elsewhere in the
                    # file. Line numbers are facts and get refreshed; authorship
                    # and updated_at must not move, or every refresh would
                    # reassign credit for symbols nobody touched.
                    self.db.execute("UPDATE symbols SET kind=?,line_start=?,line_end=?,signature=?,status='active' WHERE id=?", (item.kind, item.start, item.end, item.signature, prior["id"]))
                    continue
                if prior:
                    # `deleted_at` is cleared here, not left behind: a symbol
                    # that exists again is current, and stale deletion metadata
                    # on a live row is the contradiction this fix removes.
                    self.db.execute("UPDATE symbols SET kind=?,line_start=?,line_end=?,signature=?,hash=?,updated_at=?,status='active',deleted_at=NULL,last_modified_by=?,last_modified_session=? WHERE id=?", (item.kind, item.start, item.end, item.signature, item.hash, now, actor, session, prior["id"]))
                    if revived: symbols += 1
                else:
                    self.db.execute("INSERT INTO symbols(name,qualified_name,kind,file_id,line_start,line_end,signature,hash,status,created_at,updated_at,last_modified_by,last_modified_session) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (item.name, item.name, item.kind, file_id, item.start, item.end, item.signature, item.hash, "active", now, now, actor, session)); symbols += 1
                changed_symbols.append(item.name)
            for name, prior in old_symbols.items():
                # Only a live symbol can be newly deleted. Without this an
                # already-retired symbol would be re-marked on every refresh and
                # reported as a fresh change forever.
                if prior["status"] != "active":
                    continue
                if name not in current:
                    self.db.execute("UPDATE symbols SET status='deleted',deleted_at=?,updated_at=?,last_modified_by=?,last_modified_session=? WHERE id=?", (now, now, actor, session, prior["id"])); deleted += 1
                    changed_symbols.append(name)
            self.db.execute("DELETE FROM dependencies WHERE source_file_id=? OR source_symbol_id IN (SELECT id FROM symbols WHERE file_id=?)", (file_id, file_id))
            symbol_ids = {row["name"]: row["id"] for row in self.db.execute("SELECT id,name FROM symbols WHERE file_id=? AND status='active'", (file_id,))}
            resolved: dict[str, int | None] = {}
            for source, target, kind in edges:
                # Module-level imports have no owning symbol. They are still
                # recorded (source_symbol_id NULL) so file-to-file edges survive;
                # dropping them left the dependency graph import-blind.
                if target not in resolved:
                    target_row = self.db.execute("SELECT id FROM symbols WHERE name=? AND status='active' LIMIT 1", (target,)).fetchone()
                    resolved[target] = target_row[0] if target_row else None
                self.db.execute("INSERT OR IGNORE INTO dependencies(source_file_id,source_symbol_id,target_name,target_symbol_id,kind) VALUES(?,?,?,?,?)", (file_id, symbol_ids.get(source), target, resolved[target], kind))
        # A deletion is an event, not a state to re-report. Without the status
        # check every refresh re-marked an already-deleted file and recorded it
        # again, so `watch` logged a change every poll forever for any file that
        # had ever been removed — and never idled down, because it always
        # believed something had just changed.
        # A targeted pass still retires the files it was asked about — a symbol
        # whose file was deleted is the stale record this exists to catch — but
        # it must not judge files it never looked at.
        candidates = (self.db.execute(f"SELECT id,path FROM files WHERE status!='deleted' AND path IN ({','.join('?' * len(only))})", tuple(only)).fetchall()
                      if only is not None else
                      self.db.execute("SELECT id,path FROM files WHERE status!='deleted'").fetchall()) if only != set() else []
        for row in candidates:
            if row["path"] not in seen and not (self.root / row["path"]).exists():
                self.db.execute("UPDATE files SET status='deleted',last_modified_by=?,last_modified_session=? WHERE id=?", (actor, session, row["id"])); deleted += 1; changed_paths.append(row["path"])
                # The file's symbols went with it. Leaving them active let
                # `impact` name a deleted file's symbols as live dependents.
                for symbol in self.db.execute("SELECT name FROM symbols WHERE file_id=? AND status='active'", (row["id"],)).fetchall():
                    changed_symbols.append(symbol["name"])
                self.db.execute("UPDATE symbols SET status='deleted',deleted_at=?,updated_at=?,last_modified_by=?,last_modified_session=? WHERE file_id=? AND status='active'", (NOW(), NOW(), actor, session, row["id"]))
        db_started = time.perf_counter()
        change_id = None
        # Did the edit actually do anything? A file rewritten with identical
        # content never reaches here, so "text-only" means the bytes moved but
        # no symbol did: formatting, comments, or an edit that missed.
        effect = "symbols-changed" if changed_symbols else "text-only" if changed_paths else "none"
        # Telling a comment from code needs a parse tree. The shallow provider
        # matches line patterns and hashes raw text, so for those files a
        # comment or a reindent reads as `symbols-changed`. Say so rather than
        # reporting the same certainty the parsed languages earn.
        shallow_changed = sorted({row["path"] for row in self.db.execute(
            f"SELECT path,coverage FROM files WHERE path IN ({','.join('?' * len(changed_paths))})", changed_paths)
            if (row["coverage"] or SHALLOW) != FULL}) if changed_paths else []
        effect_confidence = {"level": "LOW" if shallow_changed and effect == "symbols-changed" else "HIGH",
                             "shallow_files": shallow_changed}
        if shallow_changed and effect == "symbols-changed":
            effect_confidence["reason"] = (
                f"{', '.join(shallow_changed[:5])} are analysed by line patterns, which cannot separate comments and "
                "formatting from code, so this may not be a real code change. Install grammars for an exact answer: "
                "pip install 'code-ledger[languages]'.")
        # The index update and the change record are one fact and are committed
        # once. Committing the index first left a window where a crash produced
        # files marked changed with no change row to explain them, so `since`
        # and `progress` silently under-reported.
        if record and changed_paths:
            change_id = self.record_change(actor, session, request, f"Indexed {len(changed_paths)} changed file(s)", "unverified", changed_paths, sorted(set(changed_symbols)), added, modified, deleted, risk=self.analyze_prompt(request)["risk"] if request else "UNKNOWN", effect=effect, attribution=attribution, commit=False)
        self.db.commit(); db_seconds = time.perf_counter() - db_started; total = time.perf_counter() - started
        result = {"files_added": added, "files_modified": modified, "files_deleted": deleted, "symbols_changed": symbols, "change_id": change_id, "effect": effect, "effect_confidence": effect_confidence, "agent": actor, "attribution": attribution, "files": changed_paths, "symbols": sorted(set(changed_symbols)), "metrics": metrics.as_dict(), "scan": {
            # What the refresh actually had to look at. `files_checked` is the
            # honest cost: every source file is still stat-ed to prove it did
            # not change, because nothing cheaper can prove that.
            "files_checked": metrics.files_stat, "files_changed": len(changed_paths),
            "files_analyzed": added + modified, "directories_visited": metrics.directories_visited,
            "directories_pruned": metrics.directories_skipped, "stat_mode": metrics.stat_mode,
            "traversal": "targeted" if only is not None else "full",
            # Edited too recently to touch: still the author's to claim.
            "files_awaiting_claim": pending}, "timing": {"discovery_seconds": round(discovery_seconds, 4), "hashing_seconds": round(hash_seconds, 4), "parsing_seconds": round(parse_seconds, 4), "database_seconds": round(db_seconds, 4), "total_seconds": round(total, 4)}}
        if attribution_note:
            result["attribution_note"] = attribution_note
        if changed_paths:
            overlap = self.conflicts(actor, changed_paths, sorted(set(changed_symbols)))
            if overlap["status"] != "NONE": result["conflicts"] = overlap
        if request:
            result["scope"] = self.scope_check(request, changed_paths, changed_symbols)
        if verbose: print(f"Discovery: {result['timing']['discovery_seconds']}s | Hashing: {result['timing']['hashing_seconds']}s | Parsing: {result['timing']['parsing_seconds']}s | Database: {result['timing']['database_seconds']}s | Total: {result['timing']['total_seconds']}s")
        return result

    def status(self) -> dict:
        queries = {"files": "SELECT count(*) FROM files WHERE status IN ('current','unindexed')", "symbols": "SELECT count(*) FROM symbols WHERE status='active'", "deleted_symbols": "SELECT count(*) FROM symbols WHERE status='deleted'", "changes": "SELECT count(*) FROM changes", "issues": "SELECT count(*) FROM issues WHERE status='OPEN'"}
        result = {key: self.db.execute(sql).fetchone()[0] for key, sql in queries.items()}
        result.update(stale_files=self.db.execute("SELECT count(*) FROM files WHERE status='stale' OR hash IS NULL").fetchone()[0], project=self.config.project_name, git_commit=head(self.root))
        result["analysis"] = self.coverage_report()
        # Reconcile before reporting. Listing a session as active because the
        # row still says so is how a dead agent stayed "working" indefinitely.
        sessions = self.sessions()
        result["sessions"] = sessions["counts"]
        result["active_agents"] = self.active_agents(reconcile=False)
        result["stale_sessions"] = [item["session_id"] for name in ("stale", "crashed") for item in sessions["by_status"].get(name, [])]
        return result

    def doctor(self, check_mcp: bool = True) -> dict:
        """One command that answers "is this ledger telling me the truth?"

        Every check reports what it found rather than a bare OK, and anything
        that cannot be determined says so instead of passing quietly.
        """
        checks: dict[str, str] = {}
        pragmas = {name: self.db.execute(f"PRAGMA {name}").fetchone()[0] for name in ("journal_mode", "foreign_keys", "busy_timeout")}
        checks["database"] = "OK"
        checks["wal"] = "OK" if str(pragmas["journal_mode"]).lower() == "wal" else f"NOT WAL ({pragmas['journal_mode']}) — concurrent agents may block"
        checks["foreign_keys"] = "OK" if pragmas["foreign_keys"] else "OFF — orphaned rows are possible"
        tables = {row["name"] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"files", "symbols", "changes", "sessions", "agents", "verifications", "dependencies"}
        checks["schema"] = "OK" if required <= tables else f"MISSING TABLES: {', '.join(sorted(required - tables))}"
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(sessions)")}
        checks["migrations"] = "OK" if {"pid", "last_activity_at", "status_reason"} <= columns else "INCOMPLETE — run any command to apply pending migrations"

        reconciled = self.reconcile_sessions()
        sessions = self.sessions(reconcile=False)
        dead = [item for name in ("stale", "crashed") for item in sessions["by_status"].get(name, [])]
        checks["sessions"] = f"{sessions['live']} live" + (f", {len(dead)} stale/crashed" if dead else "")
        status = self.status()
        checks["file_index"] = "OK" if not status["stale_files"] else f"{status['stale_files']} file(s) never hashed — run `codeledger refresh --changed`"
        orphans = self.db.execute("SELECT count(*) FROM symbols WHERE file_id NOT IN (SELECT id FROM files)").fetchone()[0]
        checks["symbol_index"] = "OK" if not orphans else f"{orphans} symbol(s) reference a missing file"
        checks["git"] = "OK" if (self.root / ".git").exists() else "no git repository — commit metadata is unavailable"
        analysis = status["analysis"]
        checks["analysis_coverage"] = "OK" if not analysis["shallow_languages"] else f"shallow: {', '.join(analysis['shallow_languages'])} — {analysis['hint']}"
        protocols = [name for name in ("CLAUDE.md", "AGENTS.md", "CODEX.md") if (self.root / name).exists()]
        checks["agent_protocols"] = ", ".join(protocols) if protocols else "MISSING — run `codeledger init` to write the agent protocol"
        checks["config"] = "OK" if (self.root / ".ai" / "codeledger" / "config.json").exists() else "using defaults (no config.json)"
        checks["storage_ignored"] = "OK" if self.config.is_ignored(Path(".ai/codeledger"), self.root) else "WARNING — CodeLedger is indexing its own database"

        # A healthy ledger no agent can reach is the failure this exists to
        # catch: every check above passed while the registered MCP command could
        # not be launched at all.
        mcp = self.mcp_selftest() if check_mcp else None
        if mcp:
            checks["mcp_server"] = {
                "OK": f"OK — handshake succeeded, {mcp.get('tools_exposed')} tools, bound to {mcp.get('reported_root')}",
                "OK_WITH_WARNINGS": f"OK (with warnings) — {mcp['detail']}",
            }.get(mcp["status"], f"{mcp['status']} — {mcp['detail']}")

        actions = []
        if dead: actions.append("codeledger session reconcile")
        if status["stale_files"]: actions.append("codeledger refresh --changed")
        if analysis["shallow_languages"]: actions.append(analysis["hint"])
        if not protocols: actions.append("codeledger init")
        if mcp and mcp["status"] not in ("OK", "OK_WITH_WARNINGS"):
            actions.append("codeledger setup-agent <agent>   # re-register the MCP server with a launchable command")
        result = {"checks": checks, "reconciled_now": reconciled["transitions"], "stale_sessions": dead,
                  "active_agents": self.active_agents(reconcile=False), "recommended_actions": actions or ["none"]}
        if mcp: result["mcp"] = mcp
        return result

    def mcp_selftest(self, timeout: float = 30.0) -> dict:
        """Launch the registered MCP command and complete a real handshake.

        Checking that a file exists proves nothing: the reported failure was a
        command that existed conceptually and could not be launched by the
        process that needed it. So this runs the exact argv an agent would run,
        from a *different* working directory, and reads the protocol back.

        Running from elsewhere is deliberate — it is what proves `--root` is
        doing the binding. If the root argument were wrong or missing, the
        server would resolve to the temporary directory and answer
        NOT_INITIALISED, which shows up here as ROOT_MISMATCH rather than as a
        healthy server pointed at nothing.

        What this cannot prove is that *your* agent can launch it: the agent's
        own environment is not visible from here, and the result says so.
        """
        from .mcp import TOOLS
        command = self.mcp_command()
        executable = command[0]
        report = {"command": command, "executable": executable, "expected_root": str(self.root),
                  "agent_environment_verified": False,
                  "note": ("Verified by launching the server from this process. An agent runs it from its own "
                           "environment, which cannot be inspected from here, so a pass does not guarantee the "
                           "agent will succeed — only that the command itself is launchable and correct.")}

        def fail(status, detail, **extra):
            return {**report, "status": status, "detail": detail, **extra}

        if not Path(executable).exists():
            return fail("LAUNCH_FAILED", f"{executable} does not exist. Reinstall CodeLedger, or re-run `codeledger setup-agent <agent>` after moving the virtualenv.")
        # A neutral directory: also proves the server creates nothing where it
        # happens to be started.
        workdir = tempfile.gettempdir()
        # Everything before `mcp` is how CodeLedger is invoked; swapping the
        # subcommand for --version proves the executable runs and is CodeLedger,
        # separately from whether the MCP server works.
        invocation = command[:command.index("mcp")] if "mcp" in command else [executable]
        version_argv = [*invocation, "--version"]
        try:
            version = subprocess.run(version_argv, capture_output=True, text=True, timeout=timeout, cwd=workdir)
        except (OSError, subprocess.SubprocessError) as exc:
            return fail("LAUNCH_FAILED", f"{' '.join(version_argv)} could not be executed: {exc}")
        if version.returncode != 0:
            return fail("LAUNCH_FAILED", f"{' '.join(version_argv)} exited {version.returncode}: {(version.stderr or '').strip()[:300]}")
        report["version"] = (version.stdout or version.stderr).strip()

        handshake = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"clientInfo": {"name": "codeledger-doctor"}}})
        listing = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        try:
            completed = subprocess.run([*command, "--agent", "codeledger-doctor"],
                                       input=f"{handshake}\n{listing}\n", capture_output=True, text=True,
                                       timeout=timeout, cwd=workdir)
        except subprocess.TimeoutExpired:
            return fail("HANDSHAKE_FAILED", f"the MCP server did not respond within {timeout:g}s")
        except (OSError, subprocess.SubprocessError) as exc:
            return fail("LAUNCH_FAILED", f"the MCP server could not be started: {exc}")

        stderr = (completed.stderr or "").strip()
        report["stderr"] = stderr[:500]
        replies = {}
        for line in (completed.stdout or "").splitlines():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") is not None:
                replies[message["id"]] = message
        if 1 not in replies or "result" not in replies.get(1, {}):
            detail = f"no initialize response; the process exited {completed.returncode}"
            if stderr: detail += f" with stderr: {stderr[:300]}"
            return fail("HANDSHAKE_FAILED", detail)

        info = replies[1]["result"].get("serverInfo") or {}
        report["protocol_version"] = replies[1]["result"].get("protocolVersion")
        report["reported_root"] = info.get("root")
        report["server_status"] = info.get("status")
        if info.get("status") == "NOT_INITIALISED" or (info.get("root") and Path(info["root"]) != self.root):
            return fail("ROOT_MISMATCH",
                        f"the server bound to {info.get('root')!r}, not {str(self.root)!r}. The registered command's "
                        "--root is wrong or missing, so an agent would be querying a different project.")

        exposed = {tool.get("name") for tool in (replies.get(2, {}).get("result", {}).get("tools") or [])}
        report["tools_exposed"] = len(exposed)
        missing = sorted({name for name, _ in TOOLS} - exposed)
        if missing:
            return fail("TOOL_SURFACE_MISMATCH",
                        f"{len(missing)} expected tool(s) absent, starting with {', '.join(missing[:5])}. "
                        "The launched executable is probably a different CodeLedger version than this one.",
                        missing_tools=missing[:10])
        # stderr is reported either way; a server that answered correctly while
        # printing to stderr is working but worth looking at.
        if stderr:
            return {**report, "status": "OK_WITH_WARNINGS",
                    "detail": f"handshake succeeded, but the server wrote to stderr: {stderr[:300]}"}
        return {**report, "status": "OK",
                "detail": f"launched {executable}, completed the MCP handshake, and exposed {len(exposed)} tools bound to {self.root}."}

    def coverage_report(self) -> dict:
        """How much of this project is actually analysed, by language.

        Surfaced so a developer learns their impact analysis is shallow from
        `status`, rather than from being wrong.
        """
        by_language: dict[str, dict] = {}
        for row in self.db.execute("SELECT language,coverage,count(*) AS files FROM files WHERE status='current' GROUP BY language,coverage"):
            entry = by_language.setdefault(row["language"] or "unknown", {"full": 0, "shallow": 0})
            entry["shallow" if (row["coverage"] or SHALLOW) != FULL else "full"] += row["files"]
        caps = capabilities()
        shallow_languages = sorted(name for name, counts in by_language.items() if counts["shallow"])
        return {"tree_sitter_installed": caps["tree_sitter_installed"], "files_by_language": by_language,
                "shallow_languages": shallow_languages,
                "hint": caps["install_hint"] if shallow_languages else None}

    def import_git(self, limit: int = 200) -> dict:
        imported = files = 0
        for commit in commits(self.root, limit):
            self.db.execute("INSERT OR IGNORE INTO git_commits(commit_hash,parent_hash,author,timestamp,subject) VALUES(?,?,?,?,?)", (commit["commit_hash"], commit["parent_hash"], commit["author"], commit["timestamp"], commit["subject"]))
            for item in commit_files(self.root, commit["commit_hash"]):
                self.db.execute("INSERT OR IGNORE INTO git_commit_files(commit_hash,path,status) VALUES(?,?,?)", (commit["commit_hash"], item["path"], item["status"])); files += 1
            imported += 1
        self.db.commit(); return {"commits_imported": imported, "commit_files_imported": files}

    def _symbol_verifications(self, names) -> dict[str, tuple]:
        """Latest verification per symbol name, in one query.

        Deliberately not a correlated subquery on `lookup`: `verifications` has
        no index on (subject_type, subject_id), so a per-row lookup made
        `handshake` five times slower — `search_symbols` calls `lookup` once per
        salient word, and each call would rescan the table for every row. One
        bounded query per lookup, skipped entirely when nothing has ever been
        verified, keeps the common case free.
        """
        if not names: return {}
        if not self.db.execute("SELECT 1 FROM verifications WHERE subject_type='symbol' LIMIT 1").fetchone():
            return {}
        names = list(names)
        latest: dict[str, tuple] = {}
        for row in self.db.execute(
                f"SELECT subject_id,recorded_at,result FROM verifications WHERE subject_type='symbol' "
                f"AND subject_id IN ({','.join('?' * len(names))}) ORDER BY recorded_at, id", tuple(names)):
            latest[row["subject_id"]] = (row["recorded_at"], row["result"])   # ordered, so the last wins
        return latest

    @staticmethod
    def _verification_applicability(verified_at, verified_result, code_changed_at, alive: bool) -> dict:
        """Does this verification still describe the code that exists now?

        Evidence is about a code state, not about a name. `symbols.updated_at`
        moves only when a symbol's content hash changes — reformatting and
        comment edits leave it alone — so it is exactly the line between
        "verified and untouched since" and "verified, then rewritten". Comparing
        the two timestamps needs no new column and no new subsystem.

        Nothing is deleted and nothing is invented: the recorded result is still
        reported, under a status that says what it is now worth.
        """
        if not verified_result:
            return {"status": "UNKNOWN", "applicability": "NONE",
                    "reason": "No verification has been recorded for this symbol."}
        if not alive:
            return {"status": "UNKNOWN", "applicability": "SUPERSEDED", "result_recorded": verified_result,
                    "verified_at": verified_at,
                    "reason": "The symbol has been deleted since it was verified."}
        if not verified_at:
            # A `last_verified` value with no dated row behind it — written by a
            # release before verifications were checked for applicability.
            return {"status": "UNKNOWN", "applicability": "UNVERIFIABLE", "result_recorded": verified_result,
                    "reason": "The verification predates applicability tracking, so it cannot be dated against the code."}
        if code_changed_at and code_changed_at > verified_at:
            return {"status": "UNKNOWN", "applicability": "SUPERSEDED", "result_recorded": verified_result,
                    "verified_at": verified_at, "code_changed_at": code_changed_at,
                    "reason": (f"The symbol changed at {code_changed_at} after being verified at {verified_at}. "
                               "The recorded result describes code that no longer exists.")}
        return {"status": verified_result, "applicability": "CURRENT", "result_recorded": verified_result,
                "verified_at": verified_at, "code_changed_at": code_changed_at,
                "reason": "The symbol has not changed since it was verified."}

    @staticmethod
    def _present_symbol(row, verification=None) -> dict:
        """One representation rule for a symbol, applied wherever one is shown.

        A symbol that no longer exists has no current line and no current
        signature, and presenting the ones it had when it was removed is how an
        agent was told a function is at line 42 of a file that no longer
        contains it. The record is not destroyed — history is the product — it
        moves under `historical`, where nothing can mistake it for the present.
        """
        record = dict(row)
        alive = record.get("status") == "active"
        # Verification travels with the symbol, because that is where an agent
        # reads it. A stale PASSED is worse than no answer: it is the one field
        # that could talk an agent out of running the test that would have
        # caught the regression.
        verified_at, verified_result = verification or (None, None)
        verification = Ledger._verification_applicability(
            verified_at, verified_result or record.get("last_verified"), record.get("updated_at"), alive)
        if verification["applicability"] != "NONE":
            record["verification"] = verification
        # The bare column keeps its meaning only while the evidence still holds.
        record["last_verified"] = verification["result_recorded"] if verification["applicability"] == "CURRENT" else None
        if alive:
            return record
        record["historical"] = {"line_start": record.get("line_start"), "line_end": record.get("line_end"),
                                "signature": record.get("signature"), "deleted_at": record.get("deleted_at")}
        record["line_start"] = record["line_end"] = record["signature"] = None
        return record

    def lookup(self, query: str, limit: int = 200) -> list[dict]:
        # `%` and `_` in a user query are literal text, not wildcards, and the
        # result set is bounded so a one-character query cannot fan out.
        pattern = f"%{query.translate(LIKE_ESCAPE)}%"
        rows = self.db.execute("SELECT s.*,f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.name LIKE ? ESCAPE '\\' OR s.qualified_name LIKE ? ESCAPE '\\' ORDER BY s.status='active' DESC,s.name LIMIT ?", (pattern, pattern, limit)).fetchall()
        verified = self._symbol_verifications({row["name"] for row in rows})
        return [self._present_symbol(r, verified.get(r["name"])) for r in rows]

    def history(self, query: str, limit: int = 200) -> list[dict]:
        pattern = f"%{query.translate(LIKE_ESCAPE)}%"
        rows = self.db.execute("SELECT c.* FROM changes c WHERE c.summary LIKE ? ESCAPE '\\' OR c.user_request LIKE ? ESCAPE '\\' ORDER BY c.id DESC LIMIT ?", (pattern, pattern, limit)).fetchall()
        return [dict(r) for r in rows]

    def _scan_for_names(self, names: list[str]) -> set[str]:
        found = set()
        for path in self._files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(re.search(r"\b" + re.escape(name) + r"\b", text) for name in names):
                found.add(path.relative_to(self.root).as_posix())
        return found

    def impact(self, query: str, scan: bool = False, limit: int = 50, fallback: bool = True) -> dict:
        """Find dependents of a symbol from the index.

        The dependency graph records who calls, uses, and imports each name, so
        the default path is a bounded set of indexed queries. Language coverage
        is uneven — Python is parsed with the AST, other languages are matched
        conservatively — so when the index reports no dependents at all the
        result falls back to reading the working tree. "No evidence" must never
        be presented as "no impact"; that is the failure mode that lets an agent
        change a symbol believing nothing depends on it. Pass ``fallback=False``
        to keep the query strictly indexed.
        """
        found = self.lookup(query, limit=limit)
        if not found:
            return {"query": query, "symbols": [], "historical_symbols": [], "dependencies": [], "referencing_files": [], "risk": "UNKNOWN", "evidence": "No indexed symbol matched this query. Run `codeledger refresh --changed`, or pass --scan to read the working tree.", "source": "index"}
        # A deleted symbol is not a live dependency target, and its file is not
        # a live defining file. Its edges are still queried below, because
        # "what still references this thing that no longer exists?" is one of
        # the more useful questions the graph can answer.
        matches = [row for row in found if row["status"] == "active"]
        historical = [row for row in found if row["status"] != "active"]
        names = sorted({row["name"] for row in found}); ids = [row["id"] for row in found]
        name_slots = ",".join("?" * len(names)); id_slots = ",".join("?" * len(ids))
        dependency_rows = [dict(row) for row in self.db.execute(
            f"SELECT d.*,s.name AS source_name,f.path AS source_path FROM dependencies d "
            f"LEFT JOIN symbols s ON s.id=d.source_symbol_id LEFT JOIN files f ON f.id=d.source_file_id "
            f"WHERE (d.target_symbol_id IN ({id_slots}) OR d.target_name IN ({name_slots})) AND f.status!='deleted'",
            (*ids, *names))]
        refs = {row["source_path"] for row in dependency_rows if row["source_path"]}
        defining = {row["path"] for row in matches}
        # Coverage decides whether the index can be trusted, rather than
        # inferring it from an empty result. A shallowly-analysed defining file
        # means the edges for this symbol were never fully recorded, so an
        # empty answer proves nothing.
        shallow = [row["path"] for row in self.db.execute(
            f"SELECT path,coverage FROM files WHERE path IN ({','.join('?' * len(matches))})",
            [row["path"] for row in matches]) if (row["coverage"] or SHALLOW) != FULL] if matches else []
        used_scan = scan or (fallback and (shallow or not refs))
        if used_scan:
            refs |= self._scan_for_names(names)
        refs -= defining
        blast = refs | defining
        source = "filesystem scan" if scan else "index + fallback scan" if used_scan else "index"
        evidence = ""
        if used_scan and not scan:
            evidence = (f"Shallow analysis coverage for {', '.join(sorted(set(shallow)))}; the working tree was read directly. Install grammars for full coverage: pip install 'code-ledger[languages]'."
                        if shallow else "The dependency index reported no dependents, so the working tree was read directly.")
        if historical and not matches:
            evidence = (evidence + " " if evidence else "") + (
                f"{names[0]} is recorded as deleted, so it has no current definition. Anything listed as referencing "
                "it may now be pointing at something that no longer exists.")
        return {"query": query, "symbols": matches, "historical_symbols": historical, "dependencies": dependency_rows, "referencing_files": sorted(refs), "defining_files": sorted(defining), "risk": "HIGH" if len(blast) > 10 else "MEDIUM" if len(blast) > 3 else "LOW", "source": source, "coverage": "shallow" if shallow else "full", "evidence": evidence}

    def search_symbols(self, query: str, limit: int = 200) -> list[dict]:
        """Find symbols relevant to a natural-language request.

        `lookup` matches the query as a single substring, which only ever hits
        when the user typed a bare symbol name. Real requests are sentences
        ("Fix the login timeout"), so the whole pre-change layer — context,
        plan, scope — saw nothing and fell back to UNKNOWN. Search the salient
        words as well, ranked by how many of them a symbol matches.
        """
        found: dict[int, list] = {}
        for row in self.lookup(query, limit=limit):
            found[row["id"]] = [row, 2]                       # whole-phrase hit is the strongest signal
        words = [word for word in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", query) if word.lower() not in STOPWORDS]
        for word in dict.fromkeys(words):
            for row in self.lookup(word, limit=limit):
                entry = found.get(row["id"])
                if entry: entry[1] += 1
                else: found[row["id"]] = [row, 1]
        ranked = sorted(found.values(), key=lambda item: (-item[1], item[0]["status"] != "active", item[0]["name"]))
        return [row for row, _score in ranked][:limit]

    def stale_records(self, paths: list[str]) -> list[dict]:
        """Which of these indexed files no longer match what is on disk?

        Cheap: one stat per file, no reading and no parsing. The ledger is a
        cache of the source, and the source always wins — reporting a symbol
        from a file that has since been edited is how an agent is told a
        function exists that somebody already deleted.
        """
        stale = []
        for row in self.db.execute(f"SELECT path,size,mtime_ns FROM files WHERE path IN ({','.join('?' * len(paths))})", paths) if paths else []:
            full = self.root / row["path"]
            try:
                stat = full.stat()
            except OSError:
                stale.append({"path": row["path"], "reason": "the file no longer exists"}); continue
            if stat.st_size != row["size"] or stat.st_mtime_ns != row["mtime_ns"]:
                stale.append({"path": row["path"], "reason": "the file changed since it was indexed"})
        return stale

    def context(self, query: str) -> dict:
        matches = self.search_symbols(query); paths = sorted({r["path"] for r in matches})
        # Never answer from memory that the filesystem has already contradicted.
        # Re-analysing only the handful of files behind this answer keeps the
        # query cheap while making it truthful.
        stale = self.stale_records(paths)
        if stale:
            self.refresh(changed_only=True, only={item["path"] for item in stale}, record=False)
            matches = self.search_symbols(query); paths = sorted({r["path"] for r in matches})
        recent = [dict(r) for r in self.db.execute("SELECT id,timestamp,agent,summary,risk FROM changes ORDER BY id DESC LIMIT 5")]
        issues = [dict(r) for r in self.db.execute("SELECT key,title,severity FROM issues WHERE status='OPEN' ORDER BY updated_at DESC LIMIT 10")]
        decisions = [dict(r) for r in self.db.execute("SELECT key,title,rationale FROM decisions WHERE status='ACTIVE' ORDER BY created_at DESC LIMIT 10")]
        features = [dict(r) for r in self.db.execute("SELECT name,description,status,last_verified FROM features WHERE name LIKE ? ESCAPE '\\' ORDER BY name LIMIT 10", (f"%{query.translate(LIKE_ESCAPE)}%",))]
        # What this answer cost, in the terms that matter: files the agent did
        # not have to open. The whole point of the ledger is the third number.
        total = self.db.execute("SELECT count(*) FROM files WHERE status IN ('current','unindexed')").fetchone()[0]
        efficiency = {"files_relevant": len(paths), "files_in_repository": total,
                      "files_avoided": max(0, total - len(paths)), "symbols_returned": len(matches[:20]),
                      "changes_returned": len(recent), "full_scan_required": not bool(matches or features),
                      "stale_records_reanalyzed": stale}
        # The matches are already computed; handing them over keeps the scope
        # analysis from repeating the symbol search on every context call.
        return {"query": query, "task_analysis": self.analyze_prompt(query, symbols=matches), "features": features, "symbols": matches[:20], "files": paths[:20], "recent_changes": recent, "known_issues": issues, "decisions": decisions, "scan_required": not bool(matches or features), "efficiency": efficiency}

    def _area_for_path(self, rel: str) -> str:
        """The part of the system a file belongs to, for reporting blast radius.

        Naming the directory would collapse `pages/Landing` and `pages/Payment`
        into one "pages", which is exactly the distinction a user needs when
        deciding how widely a shared change should apply. Under a container
        directory the file is the area; elsewhere the directory is.
        """
        parts = Path(rel).parts
        if len(parts) >= 2 and parts[-2].lower() in CONTAINER_DIRS:
            return Path(rel).stem
        if len(parts) >= 2:
            return parts[-2]
        return Path(rel).stem

    def _areas(self, paths) -> list[str]:
        return sorted({self._area_for_path(path) for path in paths})

    def _shared_signals(self, symbol: dict, areas: list[str]) -> tuple[bool, list[str]]:
        """Is this symbol shared on purpose, rather than merely used twice?

        Three independent signals, because none is sufficient alone: where it
        lives, what it is called, and how widely it is actually depended on.
        Path and name are conventions and can lie; the dependency count is
        measured. A symbol is called shared when the measurement agrees with at
        least one convention, or when the measurement alone is emphatic.
        """
        reasons = []
        lowered_path = (symbol.get("path") or "").lower()
        if any(part in SHARED_PATH_HINTS for part in Path(lowered_path).parts):
            reasons.append(f"defined under a shared location ({Path(lowered_path).parent.as_posix()})")
        name = (symbol.get("name") or "").lower()
        if any(hint in name for hint in SHARED_NAME_HINTS):
            reasons.append(f"named like shared infrastructure ({symbol.get('name')})")
        if len(areas) >= AMBIGUITY_LONE_AREAS:
            reasons.append(f"depended on from {len(areas)} distinct areas")
        return bool(reasons), reasons

    def shared_dependencies(self, symbols: list[dict], limit: int = 5) -> dict:
        """Which of these symbols are shared, and what would a change to them reach?

        Uses the indexed dependency graph only (`fallback=False`). The scanning
        fallback in `impact` reads the whole working tree, which is the right
        trade when an agent has explicitly asked about one symbol and the wrong
        one on every planning call. The cost of that choice is that absence of
        evidence gets weaker here, which is why coverage travels with the answer
        instead of being dropped.
        """
        entries, reached, defining = [], set(), []
        for symbol in symbols[:limit]:
            name, path = symbol.get("name"), symbol.get("path")
            if not name: continue
            defining.append(path)
            referencing = [item for item in self.impact(name, fallback=False)["referencing_files"] if item != path]
            if not referencing: continue
            areas = self._areas(referencing)
            shared, reasons = self._shared_signals(symbol, areas)
            reached.update(referencing)
            entries.append({"symbol": name, "defined_in": path, "shared": shared, "why_shared": reasons,
                            "dependent_files": referencing, "areas": areas,
                            "dependent_count": len(referencing), "area_count": len(areas)})
        # Coverage decides how much an empty answer is worth. A shallowly parsed
        # file had its edges recorded conservatively, so "nothing depends on
        # this" from such a file is not a finding — it is a gap.
        shallow = [row["path"] for row in self.db.execute(
            f"SELECT path,coverage FROM files WHERE path IN ({','.join('?' * len(defining))})", defining)
            if (row["coverage"] or SHALLOW) != FULL] if defining else []
        confidence = "UNKNOWN" if not symbols else "LOW" if shallow else "HIGH"
        caveat = None
        if shallow:
            caveat = (f"{len(shallow)} of the defining file(s) are analysed shallowly, so the dependency graph for "
                      f"them is incomplete: {', '.join(sorted(shallow)[:5])}. Treat an empty or small dependent list "
                      f"as unproven rather than as evidence that a change is contained. "
                      f"{capabilities()['install_hint']}")
        elif not symbols:
            caveat = "No indexed symbol matched this request, so no dependency evidence exists either way."
        entries.sort(key=lambda item: (-item["area_count"], -item["dependent_count"], item["symbol"]))
        return {"shared_dependencies": entries,
                "blast_radius": {"files": sorted(reached), "areas": self._areas(reached),
                                 "file_count": len(reached), "area_count": len(self._areas(reached)),
                                 "confidence": confidence},
                "coverage_caveat": caveat}

    def _scope_ambiguity(self, analysis: dict, shared: dict) -> dict | None:
        """Does this request fail to say how widely it should apply?

        Ambiguity is not "more than one file changed" — that is most work. It is
        a request that touches something deliberately shared, spans parts of the
        system the user did not mention, and never says which of them it meant.
        Any one of those alone is ordinary; together they mean the agent is about
        to pick a scope on the user's behalf and be confidently wrong.

        A request that names its own scope is never ambiguous, however wide it
        reaches: the user already answered the question.
        """
        entries = shared["shared_dependencies"]
        if not entries: return None
        radius = shared["blast_radius"]
        areas = radius["areas"]
        if len(areas) < AMBIGUITY_MIN_AREAS: return None
        # The user naming an affected area, or any path, settles it.
        text = analysis["normalized"].lower()
        named = [area for area in areas if re.search(r"\b" + re.escape(area.lower()) + r"\b", text)]
        if named or analysis["paths"]:
            return None
        explicitly_shared = [item for item in entries if item["shared"]]
        # Two areas alone is weak evidence — plenty of local helpers are used
        # twice. It needs a second signal: something shared by design, or a
        # radius wide enough to speak for itself.
        if not (explicitly_shared or len(areas) >= AMBIGUITY_LONE_AREAS or radius["file_count"] >= AMBIGUITY_LONE_FILES):
            return None
        # Adding to a shared module does not change what its existing dependents
        # already do, so there is usually no scope to settle: "add a helper to
        # drawerState" needs no interrogation. Only a genuinely wide radius
        # overrides that, since a broad addition can still be a broad decision.
        if analysis["intent"] == "feature" and radius["file_count"] < AMBIGUITY_LONE_FILES:
            return None
        subject = explicitly_shared[0] if explicitly_shared else entries[0]
        reasons = list(subject["why_shared"]) or [f"used across {len(areas)} areas"]
        return {
            "status": "SCOPE_AMBIGUOUS", "subject": subject["symbol"], "defined_in": subject["defined_in"],
            "affected_areas": areas, "affected_files": radius["files"],
            "evidence": reasons, "confidence": radius["confidence"],
            "question": (f"'{subject['symbol']}' is shared: it affects {', '.join(areas)}. "
                         f"The request does not say which of those it should apply to."),
            # Never nominate one area as the default: the alphabetically first
            # affected area is not a guess worth putting in the user's mouth.
            "options": [f"one area only — ask which of: {', '.join(areas)}", "all affected areas",
                        "a specific subset the user names"],
            "guidance": ("Ask the user which scope is intended before editing. Do not choose one silently, and do "
                         "not change every area on the assumption that wider is safer."),
        }

    def analyze_prompt(self, prompt: str, scope: bool = True, symbols: list[dict] | None = None) -> dict:
        text = " ".join(prompt.strip().split()); lower = text.lower()
        first = re.match(r"(?:please\s+)?([a-z]+)", lower)
        verb = first.group(1) if first else "investigate"
        intent_map = {"add":"feature", "create":"feature", "implement":"feature", "build":"feature", "fix":"bugfix", "debug":"bugfix", "diagnose":"investigation", "resolve":"bugfix", "refactor":"refactor", "clean":"refactor", "remove":"removal", "delete":"removal", "update":"change", "change":"change", "improve":"improvement", "investigate":"investigation", "find":"investigation", "recover":"recovery", "restore":"recovery", "prepare":"deployment", "deploy":"deployment", "revert":"recovery"}
        intent = intent_map.get(verb, "change")
        constraints = re.findall(r"(?:only|must|should|without|don't|do not|never|keep|preserve)\s+[^.;]+", text, flags=re.I)
        paths = sorted(set(re.findall(r"(?:[\w.-]+/)+[\w.-]+", text)))
        acceptance = [sentence.strip() for sentence in re.split(r"[.!?]", text) if re.search(r"\b(?:must|should|expect|so that|when|after|add tests?|pass tests?)\b", sentence, re.I)]
        areas = []
        for keyword, area in (("auth", "authentication"), ("login", "authentication"), ("user", "users"), ("admin", "administration"), ("payment", "payments"), ("ticket", "ticketing"), ("database", "database"), ("migration", "database"), ("notification", "notifications"), ("test", "testing"), ("security", "security")):
            if keyword in lower and area not in areas: areas.append(area)
        data_sources = [source for source in ("window.storage", "localStorage", "indexedDB", "database", "supabase", "git", "browser storage", "runtime memory") if source.lower() in lower]
        preservation = [phrase.strip() for phrase in re.findall(r"(?:preserve|keep|do not change|don't change|without changing|leave)\s+[^.;]+", text, flags=re.I)]
        risk = "HIGH" if any(word in lower for word in ("delete", "remove", "migration", "payment", "auth", "security", "permission", "deploy", "production")) else "MEDIUM" if intent in {"bugfix", "refactor", "recovery", "deployment"} else "LOW"
        questions = []
        if len(text.split()) < 4: questions.append("Clarify the expected behavior and acceptance criteria.")
        if intent == "feature" and not acceptance: questions.append("Confirm how success should be verified and which existing UI/API should be extended.")
        if intent == "recovery" and not data_sources: questions.append("Which source of truth should be used: project files, Git history, database, or browser/runtime storage?")
        if data_sources and intent == "recovery": questions.append("Is the requested runtime data backed up or exported, or should the system create a new persistence path?")
        result = {"original": prompt, "normalized": text, "intent": intent, "verb": verb, "areas": areas, "paths": paths, "constraints": constraints, "preservation_constraints": preservation, "data_sources": data_sources, "acceptance_criteria": acceptance, "risk": risk, "clarifying_questions": questions}
        # Off by default for callers that only want the text reading of a
        # request — `record_change` derives `risk` on every refresh and must not
        # pay for dependency queries to do it.
        if scope:
            matched = symbols if symbols is not None else self.search_symbols(prompt)
            shared = self.shared_dependencies(matched)
            result.update(shared_dependencies=shared["shared_dependencies"], blast_radius=shared["blast_radius"],
                          coverage_caveat=shared["coverage_caveat"])
            ambiguity = self._scope_ambiguity(result, shared)
            result["scope_ambiguity"] = ambiguity
            if ambiguity:
                questions.append(ambiguity["question"] + " Ask which scope is intended before editing.")
        return result

    def _symbol_files(self, names: set[str]) -> dict[str, set[str]]:
        """Which indexed file(s) define each of these symbol names."""
        if not names: return {}
        owners: dict[str, set[str]] = {}
        for row in self.db.execute(
                f"SELECT s.name,f.path FROM symbols s JOIN files f ON f.id=s.file_id "
                f"WHERE s.name IN ({','.join('?' * len(names))})", tuple(names)):
            owners.setdefault(row["name"], set()).add(row["path"])
        return owners

    def scope_check(self, request: str, changed_files: list[str], changed_symbols: list[str] | None = None,
                    plan_files: list[str] | None = None, plan_symbols: list[str] | None = None) -> dict:
        """Did this change stay inside the task, or reach somewhere unrelated?

        The question is about *files*. A symbol is not a scope violation because
        the request did not happen to say its name — most symbols an edit touches
        are implementation details of a file that is legitimately in scope, and
        flagging them made the guard fire on ordinary work until it meant
        nothing. So a symbol is unexpected only when the file it lives in is,
        which is the only version of the question the evidence can answer.

        `plan_files`/`plan_symbols` let a caller widen the boundary with what it
        said it would do. Missing evidence is still never SAFE.
        """
        changed_files = sorted(set(changed_files)); changed_symbols = sorted(set(changed_symbols or []))
        plan_files = sorted(set(plan_files or [])); plan_symbols = set(plan_symbols or [])
        if not changed_files:
            return {"status": "NO_CHANGES", "request": request, "allowed_files": [], "unexpected_files": [], "unexpected_symbols": []}
        context = self.context(request); allowed, evidence = self._task_boundary(context, request, plan_files)
        if not allowed:
            return {"status": "UNKNOWN", "request": request, "reason": "Insufficient task-specific context to define a safe boundary.", "allowed_files": [], "unexpected_files": [], "unexpected_symbols": changed_symbols, "boundary_evidence": []}
        # A new file is tolerated only in a directory that already holds a
        # task-relevant file. Matching on prefixes instead let one hit under
        # `src/` mark the whole subtree SAFE, which made the guard vacuous.
        allowed_dirs = {str(Path(path).parent).replace("\\", "/") for path in allowed}; allowed_dirs.discard(".")
        unexpected = [path for path in changed_files if path not in allowed and str(Path(path).parent).replace("\\", "/") not in allowed_dirs]
        # Every changed symbol arrived with one of the changed files. If none of
        # those files is out of bounds there is nothing for a symbol to violate,
        # so only symbols that actually live in an unexpected file are reported.
        unexpected_set = set(unexpected)
        unexpected_symbols = []
        if unexpected_set:
            owners = self._symbol_files(set(changed_symbols))
            for name in changed_symbols:
                if name in plan_symbols:
                    continue
                homes = owners.get(name)
                # Unindexed name (just deleted, or created since the last index):
                # attributable only to the files in this change, so it counts as
                # unexpected exactly when every changed file it could have come
                # from is unexpected.
                if homes is None:
                    if set(changed_files) <= unexpected_set:
                        unexpected_symbols.append(name)
                elif homes <= unexpected_set:
                    unexpected_symbols.append(name)
        status = "WARNING" if unexpected else "SAFE"
        return {"status": status, "request": request, "allowed_files": sorted(allowed), "unexpected_files": unexpected,
                "unexpected_symbols": unexpected_symbols, "boundary_evidence": evidence,
                "changed_symbols_in_scope": [name for name in changed_symbols if name not in unexpected_symbols],
                "reason": "Review the diff for unrelated changes." if status == "WARNING" else "Changed files are within the known task boundary."}

    def _task_boundary(self, context: dict, request: str = "", plan_files: list[str] | None = None) -> tuple[set[str], list[str]]:
        """Files a task may legitimately touch, with the evidence behind them.

        Indexed symbol matches are the strong signal. Paths written into the
        request are equally strong and are always honoured. So are the files
        that *depend on* those symbols: a request to change `PeriodNav` lands
        legitimately in whatever imports it, and the dependency graph already
        knows which files those are. That is structural evidence rather than
        word matching, and it is what stops a correct edit to a caller being
        reported as out of scope.

        Only when none of that exists does this fall back to matching request
        keywords against file paths, which is weak but still better than
        refusing to judge at all.
        """
        analysis = context["task_analysis"]; evidence = []
        # Not `context["files"]`: that is capped at 20 for presentation, and
        # reusing it silently clipped the safety boundary to the same number.
        matches = self.search_symbols(request) if request else context["symbols"]
        allowed = {row["path"] for row in matches}
        if allowed:
            evidence.append("indexed symbols matching the request")
        names = {row["name"] for row in matches}
        if names:
            dependents = {row["path"] for row in self.db.execute(
                f"SELECT DISTINCT f.path FROM dependencies d JOIN files f ON f.id=d.source_file_id "
                f"WHERE d.target_name IN ({','.join('?' * len(names))}) AND f.status!='deleted'", tuple(names))}
            if dependents - allowed:
                allowed |= dependents; evidence.append("files that depend on those symbols")
        indexed = [row["path"] for row in self.db.execute("SELECT path FROM files WHERE status!='deleted'")]
        named = {path for token in analysis["paths"] for path in indexed if token.strip("/") in path}
        if named:
            allowed |= named; evidence.append("paths named in the request")
        if plan_files:
            allowed |= set(plan_files); evidence.append("files named in the implementation plan")
        if not allowed:
            words = {word for word in re.findall(r"[a-z][a-z0-9]{3,}", analysis["normalized"].lower()) if word not in STOPWORDS}
            keyword_matches = {path for path in indexed if any(word in path.lower() for word in words)}
            if keyword_matches:
                allowed |= keyword_matches; evidence.append("request keywords matched against file paths (weak evidence)")
        return allowed, evidence

    def plan(self, request: str) -> dict:
        context = self.context(request); symbols = context["symbols"][:20]; impact_files = set(context["files"])
        for symbol in symbols:
            impact_files.update(row["path"] for row in self.lookup(symbol["name"]))
        analysis = context["task_analysis"]
        # `lookup` above returns where each symbol is *defined*. What a change to
        # it would reach is a different question, and the dependency graph has
        # always been able to answer it — it simply was not asked here, so a plan
        # for a shared symbol listed one file while the ledger knew of six.
        radius = analysis.get("blast_radius") or {"files": [], "areas": [], "file_count": 0, "area_count": 0, "confidence": "UNKNOWN"}
        shared = analysis.get("shared_dependencies") or []
        impact_files.update(radius["files"])
        risk = "UNKNOWN" if context["scan_required"] else "HIGH" if len(impact_files) > 10 else "MEDIUM" if len(impact_files) > 3 else "LOW"
        if shared and radius["area_count"] >= AMBIGUITY_MIN_AREAS and risk in ("LOW", "MEDIUM"):
            risk = "HIGH" if radius["area_count"] >= AMBIGUITY_LONE_AREAS else "MEDIUM"
        if not symbols:
            recommendation = "Use targeted discovery; CodeLedger has no exact symbol match yet."
        elif shared:
            names = ", ".join(item["symbol"] for item in shared[:3])
            recommendation = (f"{names} {'are' if len(shared) > 1 else 'is'} shared: a change reaches {radius['file_count']} file(s) across "
                              f"{', '.join(radius['areas'][:6])}. Read those before editing, and extend the existing "
                              f"implementation rather than adding a parallel one.")
        else:
            recommendation = "Inspect the existing implementation before adding new code."
        return {"request": request, "task_analysis": analysis, "existing_files": sorted(impact_files), "relevant_symbols": symbols,
                "shared_dependencies": shared, "blast_radius": radius, "coverage_caveat": analysis.get("coverage_caveat"),
                "scope_ambiguity": analysis.get("scope_ambiguity"),
                "recent_changes": context["recent_changes"], "known_issues": context["known_issues"], "decisions": context["decisions"], "risk": "HIGH" if analysis["risk"] == "HIGH" else risk, "recommendation": recommendation, "full_scan_required": context["scan_required"], "suggested_tests": self.suggest_tests(sorted(impact_files), [symbol["name"] for symbol in symbols])}

    def since(self, marker: str = "", agent: str = "", limit: int = 50) -> dict:
        """What has changed since a point in time, and who did it.

        The handoff query for more than one agent on a repository. With no
        marker and an agent name it means "since that agent last recorded
        anything", which is what an agent needs at the start of a turn to find
        out what the other one did while it was not looking.
        """
        cutoff_id = cutoff_time = None
        if marker.isdigit():
            cutoff_id = int(marker); resolved = {"type": "change_id", "value": cutoff_id}
        elif marker.startswith("session-"):
            row = self.db.execute("SELECT start_time FROM sessions WHERE session_id=?", (marker,)).fetchone()
            cutoff_time = row["start_time"] if row else None
            resolved = {"type": "session", "value": marker, "start_time": cutoff_time}
        elif marker:
            cutoff_time = marker; resolved = {"type": "timestamp", "value": marker}
        elif agent:
            row = self.db.execute("SELECT timestamp FROM changes WHERE agent=? ORDER BY id DESC LIMIT 1", (agent,)).fetchone()
            cutoff_time = row["timestamp"] if row else None
            resolved = {"type": "last change by agent", "value": agent, "timestamp": cutoff_time or "NOT RECORDED"}
        else:
            resolved = {"type": "most recent", "value": limit}

        if cutoff_id is not None:
            rows = self.db.execute("SELECT * FROM changes WHERE id>? ORDER BY id DESC LIMIT ?", (cutoff_id, limit)).fetchall()
        elif cutoff_time:
            rows = self.db.execute("SELECT * FROM changes WHERE timestamp>? ORDER BY id DESC LIMIT ?", (cutoff_time, limit)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM changes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

        changes, files, symbols, agents = [], set(), set(), set()
        for row in rows:
            row_files = [item["path"] for item in self.db.execute("SELECT path FROM change_files WHERE change_id=?", (row["id"],))]
            row_symbols = [item["name"] for item in self.db.execute("SELECT name FROM change_symbols WHERE change_id=?", (row["id"],))]
            files.update(row_files); symbols.update(row_symbols); agents.add(row["agent"] or "unknown")
            # The aggregates above keep the complete set; only the per-change
            # view is bounded, and it carries its own totals so the reader can
            # see exactly how much was left out.
            changes.append({"change_id": row["id"], "timestamp": row["timestamp"], "agent": row["agent"] or "unknown",
                            "request": row["user_request"] or "NOT RECORDED", "effect": row["effect"] or "unknown",
                            "risk": row["risk"],
                            "files": row_files[:CHANGE_LIST_LIMIT], "files_total": len(row_files),
                            "files_truncated": len(row_files) > CHANGE_LIST_LIMIT,
                            "symbols": row_symbols[:CHANGE_LIST_LIMIT], "symbols_total": len(row_symbols),
                            "symbols_truncated": len(row_symbols) > CHANGE_LIST_LIMIT})
        by_others = [item for item in changes if agent and item["agent"] != agent]
        summary = "Nothing has been recorded since that point."
        if changes:
            who = ", ".join(sorted(agents))
            summary = (f"{len(changes)} change(s) by {who}: {len(files)} file(s), {len(symbols)} symbol(s)."
                       + (f" {len(by_others)} {'was' if len(by_others) == 1 else 'were'} made by another agent." if by_others else ""))
        result = {"since": resolved, "changes": changes, "files_changed": sorted(files),
                  "symbols_changed": sorted(symbols), "agents": sorted(agents),
                  "changes_by_other_agents": len(by_others), "active_agents": self.active_agents(), "summary": summary}
        if agent and (files or symbols):
            overlap = self.conflicts(agent, sorted(files), sorted(symbols))
            if overlap["status"] != "NONE": result["conflicts"] = overlap
        return result

    def conflicts(self, agent: str, files: list[str], symbols: list[str] | None = None, window_seconds: int = 1800) -> dict:
        """Is another live agent working on the same code right now?

        Two agents editing the same repository is the case CodeLedger exists to
        make safe, and the dangerous overlap is not the directory — it is the
        symbol. Touching the same file can be coincidence; rewriting the same
        function is one agent about to undo the other. Only agents with a live
        session count, so a crashed session cannot raise a conflict forever.
        """
        symbols = symbols or []
        live = [name for name in self.active_agents() if name not in ("unknown", agent)]
        if not live or not (files or symbols):
            return {"status": "NONE", "conflicts": [], "message": "No other agent holds a live session."}
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
        slots = ",".join("?" * len(live))
        found = []
        for row in self.db.execute(f"SELECT * FROM changes WHERE agent IN ({slots}) AND timestamp>? ORDER BY id DESC LIMIT 50", (*live, cutoff)):
            their_files = {item["path"] for item in self.db.execute("SELECT path FROM change_files WHERE change_id=?", (row["id"],))}
            their_symbols = {item["name"] for item in self.db.execute("SELECT name FROM change_symbols WHERE change_id=?", (row["id"],))}
            shared_symbols, shared_files = sorted(their_symbols & set(symbols)), sorted(their_files & set(files))
            if not shared_symbols and not shared_files:
                continue
            found.append({"severity": "HIGH" if shared_symbols else "MEDIUM", "agent": row["agent"],
                          "session": row["session_id"] or "unrecorded", "change_id": row["id"], "at": row["timestamp"],
                          "request": row["user_request"] or "NOT RECORDED",
                          "shared_symbols": shared_symbols, "shared_files": shared_files,
                          "attribution_confidence": row["attribution_confidence"] or "UNKNOWN"})
        if not found:
            return {"status": "NONE", "conflicts": [], "active_agents": live,
                    "message": f"{', '.join(live)} also active, but not on the same files or symbols."}
        worst = "HIGH" if any(item["severity"] == "HIGH" for item in found) else "MEDIUM"
        overlapping = sorted({name for item in found for name in item["shared_symbols"]})
        message = (f"POTENTIAL CONFLICT: {', '.join(sorted({item['agent'] for item in found}))} also changed "
                   + (f"the same symbol(s): {', '.join(overlapping)}." if overlapping
                      else f"the same file(s): {', '.join(sorted({name for item in found for name in item['shared_files']}))}.")
                   + " Re-read those before editing so the two agents do not undo each other.")
        return {"status": worst, "conflicts": found, "active_agents": live, "message": message}

    def _salient(self, text: str) -> set[str]:
        return {word.lower() for word in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", text or "") if word.lower() not in STOPWORDS}

    def _relevance(self, wanted: set[str], other: set[str]) -> tuple[float, list[str]]:
        """Do two pieces of text describe the same task? 0.0 means no.

        One shared word is not the same task: "fix the login timeout" and
        "change the login button colour" would otherwise count as the same work.
        A missed match is honest — it merely reports nothing found; a false one
        tells an agent about work it never did. Both repeat detection and
        checkpoint selection ask this question, and must answer it the same way.
        """
        overlap = wanted & other
        smaller = min(len(wanted), len(other)) if wanted and other else 0
        score = len(overlap) / smaller if smaller else 0.0
        if score < 0.5 or (len(overlap) < 2 and smaller > 1):
            return 0.0, sorted(overlap)
        return score, sorted(overlap)

    def progress(self, request: str, limit: int = 20) -> dict:
        """Has work on this request actually achieved anything yet?

        Agents repeat themselves. They edit, the behaviour does not change, and
        with no memory of the previous attempt they try the same thing again —
        re-reading the repository each time and spending tokens to rediscover
        what already failed. The ledger already knows: which attempts changed a
        symbol rather than only text, which symbols keep being rewritten, and
        whether verification ever passed afterwards.
        """
        wanted = self._salient(request)
        attempts = []
        for row in self.db.execute("SELECT * FROM changes WHERE user_request IS NOT NULL AND user_request!='' ORDER BY id DESC LIMIT 200"):
            score, overlap = self._relevance(wanted, self._salient(row["user_request"]))
            if not score:
                continue
            symbols = [item["name"] for item in self.db.execute("SELECT name FROM change_symbols WHERE change_id=?", (row["id"],))]
            files = [item["path"] for item in self.db.execute("SELECT path FROM change_files WHERE change_id=?", (row["id"],))]
            attempts.append({"change_id": row["id"], "timestamp": row["timestamp"], "agent": row["agent"] or "unknown",
                             "request": row["user_request"], "effect": row["effect"] or "unknown",
                             "matched_terms": overlap, "match_score": round(score, 2), "files": files, "symbols": symbols})
            if len(attempts) >= limit:
                break
        attempts.reverse()
        if not attempts:
            return {"request": request, "status": "NO_PRIOR_ATTEMPTS", "attempts": [], "attempt_count": 0,
                    "repeated_symbols": [], "verifications": [],
                    "guidance": "No recorded attempt matches this request. Proceed, then run `codeledger refresh --changed --request \"...\"` so the next attempt can see this one."}

        counts: dict[str, int] = {}
        for attempt in attempts:
            for name in set(attempt["symbols"]):
                counts[name] = counts.get(name, 0) + 1
        repeated = sorted(name for name, count in counts.items() if count > 1)
        subjects = sorted({name for attempt in attempts for name in attempt["symbols"]})
        latest = attempts[-1]["timestamp"]
        verifications = [dict(row) for row in self.db.execute(
            "SELECT subject_type,subject_id,kind,result,recorded_at FROM verifications ORDER BY recorded_at DESC LIMIT 20")]
        after = [v for v in verifications if v["recorded_at"] >= latest]
        passed_after = [v for v in after if v["result"] in {"PASSED", "WORKING"}]
        failed_after = [v for v in after if v["result"] in {"FAILED", "BROKEN", "ERROR"}]
        ineffective = [a for a in attempts if a["effect"] in {"text-only", "none"}]

        if passed_after and not failed_after:
            status = "VERIFIED"
            guidance = "Verification passed after the most recent attempt. The change took effect; stop editing and report it."
        elif len(attempts) >= 3 and repeated and failed_after:
            status = "REPEATING"
            guidance = (f"{len(attempts)} attempts have edited {', '.join(repeated[:5])} and verification still fails. "
                        "Editing the same symbol again is unlikely to help. Re-read the failure output, widen the search with "
                        "`codeledger impact <symbol>`, or ask the user whether the request describes the real problem.")
        elif len(ineffective) >= 2:
            status = "NO_EFFECT"
            guidance = (f"{len(ineffective)} of {len(attempts)} attempts changed no symbol — only text, or nothing at all. "
                        "The edits are not reaching the code that runs. Confirm you are editing the file that is actually "
                        "imported and executed before trying again.")
        elif len(attempts) >= 2 and not any(v["result"] in {"PASSED", "WORKING"} for v in verifications):
            status = "UNVERIFIED"
            guidance = ("Attempts are changing real symbols but nothing has been verified. Record evidence with "
                        "`codeledger verify-run project project TEST -- <your test command>` so a repeat can be detected.")
        else:
            status = "PROGRESSING"
            guidance = "The most recent attempt changed real symbols. Verify it before making further edits."
        return {"request": request, "status": status, "attempt_count": len(attempts), "attempts": attempts,
                "repeated_symbols": repeated, "subjects": subjects, "verifications": after or verifications[:3],
                "ineffective_attempts": len(ineffective), "guidance": guidance}

    def _reusable_implementation(self, symbols: list[dict], limit: int = 8) -> list[dict]:
        """What the matched symbols are built out of, one hop forward.

        `impact` walks the dependency graph backwards to find dependents. The
        same edges read forwards answer a different question: an existing flow is
        rarely one symbol, and recommending only the entry point tells an agent
        nothing about the shared drawer and shared state underneath it. One hop
        is deliberate — two would drag in most of the project.
        """
        found: dict[str, dict] = {}
        for symbol in symbols[:5]:
            found.setdefault(symbol["name"], {"symbol": symbol["name"], "path": symbol["path"], "role": "matches the request"})
        paths = [symbol["path"] for symbol in symbols[:5]]
        if not paths: return []
        for row in self.db.execute(
                f"SELECT DISTINCT d.target_name FROM dependencies d JOIN files f ON f.id=d.source_file_id "
                f"WHERE f.path IN ({','.join('?' * len(paths))}) AND d.kind IN ('uses','calls','imports')", paths):
            name = row["target_name"]
            if name in found: continue
            target = self.db.execute(
                "SELECT s.name,f.path FROM symbols s JOIN files f ON f.id=s.file_id "
                "WHERE s.name=? AND s.status='active' LIMIT 1", (name,)).fetchone()
            # Only names that resolve to something this project actually defines.
            # Everything else is a library or a module path, not a flow to reuse.
            if target:
                found[name] = {"symbol": target["name"], "path": target["path"], "role": "used by the existing implementation"}
        return list(found.values())[:limit]

    def _proposed_new_symbols(self, plan_text: str) -> list[str]:
        """Names the plan says it will create that this project does not have."""
        candidates = set(re.findall(r"\b(?:creat\w+|add\w*|new|build\w*|implement\w*|introduc\w+|writ\w+)\s+"
                                    r"(?:a\s+|an\s+|the\s+)?(?:new\s+)?([A-Z][A-Za-z0-9_]{2,})", plan_text))
        candidates.update(re.findall(r"\b([A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+)\b", plan_text))
        proposed = []
        for name in sorted(candidates):
            if not self.db.execute("SELECT 1 FROM symbols WHERE name=? AND status='active' LIMIT 1", (name,)).fetchone():
                proposed.append(name)
        return proposed

    def _plan_paths(self, plan_text: str) -> list[str]:
        """Paths the plan names explicitly. Same shape `analyze_prompt` reads."""
        return sorted(set(re.findall(r"(?:[\w.-]+/)+[\w.-]+", plan_text)))

    def _plan_scope_violations(self, analysis: dict, matched: list[dict], plan_paths: list[str]) -> list[dict]:
        """Paths the plan names that the request's own scope does not cover.

        Only ever fires on a path the plan actually named and got wrong. A plan
        that names no paths is not punished for its silence — the alternative
        warns on most ordinary plans, which is how a safety signal becomes noise.
        """
        if not plan_paths: return []
        requested = analysis["paths"]
        if requested:
            # The user named the scope. Nothing outranks that.
            outside = [path for path in plan_paths
                       if not any(token.strip("/") in path or path in token for token in requested)]
            return [{"path": path, "reason": f"the request limits this task to {', '.join(requested)}",
                     "evidence": "explicit user scope"} for path in outside]
        relevant = {row["path"] for row in matched} | set((analysis.get("blast_radius") or {}).get("files") or [])
        if not relevant: return []
        # A new file beside a relevant one is ordinary; a new file in an
        # unrelated part of the tree is the thing worth asking about.
        relevant_dirs = {str(Path(path).parent).replace("\\", "/") for path in relevant}; relevant_dirs.discard(".")
        return [{"path": path, "reason": "outside the files CodeLedger finds relevant to this request",
                 "evidence": "indexed relevance and blast radius"}
                for path in plan_paths
                if path not in relevant and str(Path(path).parent).replace("\\", "/") not in relevant_dirs]

    def handshake(self, request: str, ai_plan: str = "") -> dict:
        matched = self.search_symbols(request)
        analysis = self.analyze_prompt(request, symbols=matched); plan_text = " ".join(ai_plan.split()); lower = plan_text.lower()
        if not plan_text:
            return {"status": "AWAITING_AI_PLAN", "request": request, "task_analysis": analysis, "message": "Submit the AI's proposed files, changes, and tests before editing."}
        required = set(re.findall(r"[a-z][a-z0-9_-]{3,}", analysis["normalized"].lower()))
        mentioned = set(re.findall(r"[a-z][a-z0-9_-]{3,}", lower))
        missing_constraints = [constraint for constraint in analysis["preservation_constraints"] if not any(word in lower for word in re.findall(r"[a-z][a-z0-9_-]{3,}", constraint.lower()))]
        relevant = [area for area in analysis["areas"] if area not in lower]
        plan_paths = self._plan_paths(plan_text)
        scope_violations = self._plan_scope_violations(analysis, matched, plan_paths)
        # Building a second implementation of something the project already has
        # is the most expensive mistake an agent makes here, and nothing was
        # looking for it: a plan to create a parallel component with its own
        # animation and its own state read as perfectly ALIGNED.
        proposed = self._proposed_new_symbols(plan_text)
        reusable = self._reusable_implementation(matched) if proposed else []
        duplicate = None
        if proposed and reusable:
            duplicate = {
                "status": "POSSIBLE_DUPLICATE", "proposed_new": proposed[:5],
                "existing_implementation": reusable,
                "message": (f"The plan creates {', '.join(proposed[:3])}, and this project already has an "
                            f"implementation the request matches: {', '.join(item['symbol'] for item in reusable[:5])}."),
                "guidance": ("Inspect those before writing a parallel version, and extend or reuse them if they fit. "
                             "This is a recommendation, not a rejection — a genuinely new implementation is sometimes "
                             "correct, and if it is, say why."),
            }
        # What this handshake was actually able to check. Word overlap is not on
        # the list: "fix payment calculation" and "change payment animation"
        # share a word and are different tasks, so a shared term is reported
        # below as a signal and never counted as a reason to believe anything.
        #
        # A feature request is the one case where finding no existing symbol is
        # itself the finding — there is nothing to reuse — rather than an
        # absence of evidence, which is what `duplicate_implementation` reports.
        checkable = bool(matched or analysis["paths"] or analysis["preservation_constraints"]
                         or duplicate or analysis["intent"] == "feature")
        if not checkable:
            return {"status": "INSUFFICIENT_EVIDENCE", "request": request, "task_analysis": analysis, "ai_plan": ai_plan,
                    "missing_preservation_constraints": missing_constraints, "unmentioned_areas": relevant,
                    "matched_terms": sorted(required & mentioned), "duplicate_implementation": None,
                    "scope_violations": scope_violations, "plan_paths": plan_paths,
                    "scope_ambiguity": analysis.get("scope_ambiguity"),
                    "message": ("No indexed symbol, named path or stated constraint connects this request to this "
                                "project, so there is no evidence to align the plan against. This is not approval. "
                                "Index the relevant code (`codeledger refresh --changed`), or proceed knowing "
                                "CodeLedger could not check this plan."),
                    "guidance": "Treat this as unverified rather than as agreement."}
        status = "WARNING" if missing_constraints or duplicate or scope_violations else "ALIGNED"
        message = "The AI plan covers the known task requirements."
        if status == "WARNING":
            if scope_violations:
                message = (f"The plan changes {', '.join(item['path'] for item in scope_violations[:3])}, which is "
                           f"{scope_violations[0]['reason']}. Revise the plan, or say why the wider scope is needed.")
            elif duplicate:
                message = duplicate["message"] + " Consider reuse before editing."
            else:
                message = "Revise the plan before editing."
        return {"status": status, "request": request, "task_analysis": analysis, "ai_plan": ai_plan, "missing_preservation_constraints": missing_constraints, "unmentioned_areas": relevant, "matched_terms": sorted(required & mentioned), "duplicate_implementation": duplicate, "scope_violations": scope_violations, "plan_paths": plan_paths, "scope_ambiguity": analysis.get("scope_ambiguity"), "message": message}

    def suggest_tests(self, changed_files: list[str] | None = None, symbols: list[str] | None = None) -> list[str]:
        changed_files, symbols = changed_files or [], symbols or []
        candidates = []
        for path in self._files():
            rel = path.relative_to(self.root).as_posix(); low = rel.lower()
            if "test" not in low and "spec" not in low: continue
            if not changed_files and not symbols: candidates.append(rel); continue
            source = path.read_text(encoding="utf-8", errors="ignore")
            if any(Path(item).stem.lower() in low or Path(item).stem.lower() in source.lower() for item in changed_files) or any(re.search(r"\b" + re.escape(symbol) + r"\b", source) for symbol in symbols): candidates.append(rel)
        return sorted(set(candidates))

    def start_session(self, agent: str, request: str = "", session_id: str | None = None, owns_process: bool = False,
                      provider: str = "", model: str = "", model_version: str = "") -> dict:
        session_id = session_id or f"session-{uuid.uuid4().hex[:10]}"
        now = NOW()
        # The adapter seam exists so an unrecognised agent is recorded as a
        # generic provider rather than inventing a vendor for it.
        identified = identify(agent); agent = identified.name
        # Which model is behind the agent is a separate question from which
        # agent it is, and one only the runtime can answer. Unanswered is
        # recorded as UNKNOWN rather than deduced from the agent's name.
        runtime = resolve_model(agent, provider, model, model_version)
        self.db.execute("INSERT OR IGNORE INTO agents(name,provider,created_at) VALUES(?,?,?)", (agent, identified.provider, now))
        agent_id = self.db.execute("SELECT id FROM agents WHERE name=?", (agent,)).fetchone()[0]
        # A PID is only evidence when the process that recorded it stays alive
        # for the session's lifetime — `watch` and `run` do. A bare
        # `session start` is bookkeeping: the CLI exits immediately, so storing
        # its PID would make every such session look crashed within seconds.
        # Those sessions are judged by heartbeat and timeout instead.
        pid = os.getpid() if owns_process else None
        self.db.execute("INSERT INTO sessions(session_id,agent_id,working_directory,start_time,request,pid,host,last_activity_at,last_heartbeat_at,status,provider,model,model_version) VALUES(?,?,?,?,?,?,?,?,?,'active',?,?,?)",
                        (session_id, agent_id, str(self.root), now, request, pid, socket.gethostname(), now, now,
                         runtime["provider"], runtime["model"], runtime["model_version"]))
        self.db.commit()
        return {"session_id": session_id, "agent": agent, "request": request, "status": "active", "start_time": now,
                "pid": pid, "host": socket.gethostname(), **runtime}

    def end_session(self, session_id: str, result: str = "completed") -> dict:
        """Close a session. Safe to call twice, and on a session already gone.

        Cleanup runs from a signal handler and from the normal exit path, so it
        must be idempotent: a session that reconciliation already marked crashed
        or stale keeps that more informative verdict rather than being rewritten
        to a tidy 'ended' that hides how it really finished.
        """
        now = NOW()
        self.db.execute("UPDATE sessions SET end_time=COALESCE(end_time,?),result=COALESCE(result,?),status='ended',status_reason=? "
                        "WHERE session_id=? AND status IN (?,?)", (now, result, f"ended cleanly ({result})", session_id, *LIVE))
        self.db.commit()
        row = self.db.execute("SELECT s.*,a.name AS agent FROM sessions s LEFT JOIN agents a ON a.id=s.agent_id WHERE s.session_id=?", (session_id,)).fetchone()
        return dict(row) if row else {"session_id": session_id, "status": "not_found"}

    def record_change(self, agent: str, session: str, request: str, summary: str, result: str = "unverified", files: list[str] | None = None, symbols: list[str] | None = None, added: int = 0, modified: int = 0, deleted: int = 0, risk: str | None = None, effect: str | None = None, attribution: dict | None = None, commit: bool = True) -> int:
        files, symbols = files or [], symbols or []
        # `risk` is derived from the recorded request, never invented: with no
        # request there is no evidence, so it stays UNKNOWN.
        risk = (risk or (self.analyze_prompt(request, scope=False)["risk"] if request else "UNKNOWN")).upper()
        effect = effect or ("symbols-changed" if symbols else "text-only" if files else "none")
        # A change recorded by hand carries no evidence about who made it beyond
        # the name supplied, which is exactly what MEDIUM means here.
        attribution = attribution or {"source": "manual-record", "confidence": "MEDIUM" if agent and agent != "unknown" else "UNKNOWN",
                                      "reason": "Recorded by hand; the ledger did not observe the edit."}
        cur = self.db.execute("INSERT INTO changes(timestamp,agent,session_id,user_request,summary,risk,result,effect,git_commit,files_added,files_modified,files_deleted,symbols_modified,attribution_source,attribution_confidence,attribution_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (NOW(), agent, session, request, summary, risk, result, effect, head(self.root), added, modified, deleted, len(symbols), attribution["source"], attribution["confidence"], attribution["reason"]))
        change_id = cur.lastrowid
        for path in files:
            file_row = self.db.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
            self.db.execute("INSERT OR IGNORE INTO change_files(change_id,file_id,path,status) VALUES(?,?,?,?)", (change_id, file_row[0] if file_row else None, path, "changed"))
        for name in symbols:
            symbol_row = self.db.execute("SELECT id FROM symbols WHERE name=? ORDER BY status='active' DESC LIMIT 1", (name,)).fetchone()
            self.db.execute("INSERT OR IGNORE INTO change_symbols(change_id,symbol_id,name,status) VALUES(?,?,?,?)", (change_id, symbol_row[0] if symbol_row else None, name, "changed"))
        if commit: self.db.commit()
        return change_id

    def verification_state(self, subject_type: str, subject_id: str) -> dict:
        """Is the newest verification for this subject still worth anything?

        Answered from what is already recorded: the verification's timestamp
        against the moment the code it covers last changed. No new table, and no
        result is ever invented — a superseded PASSED becomes UNKNOWN with the
        original result still visible under `result_recorded`.

        Scope honesty differs by subject. A symbol is precise: its own hash
        moved or it did not. `project` and `feature` verifications cannot be
        scoped to the code they actually exercised, so *any* later symbol change
        supersedes them. That is deliberately conservative — it errs toward
        UNKNOWN, never toward a PASSED the evidence cannot support.
        """
        latest = self.db.execute(
            "SELECT * FROM verifications WHERE subject_type=? AND subject_id=? ORDER BY recorded_at DESC, id DESC LIMIT 1",
            (subject_type, subject_id)).fetchone()
        history = self.db.execute(
            "SELECT count(*) FROM verifications WHERE subject_type=? AND subject_id=?",
            (subject_type, subject_id)).fetchone()[0]
        if latest is None:
            return {"subject_type": subject_type, "subject_id": subject_id, "status": "UNKNOWN",
                    "applicability": "NONE", "history_count": 0,
                    "reason": "Nothing has been verified for this subject."}
        alive, changed_at = True, None
        if subject_type == "symbol":
            row = self.db.execute("SELECT status,updated_at FROM symbols WHERE name=? ORDER BY status='active' DESC, updated_at DESC LIMIT 1",
                                  (subject_id,)).fetchone()
            alive = bool(row) and row["status"] == "active"
            changed_at = row["updated_at"] if row else None
        elif subject_type == "file":
            row = self.db.execute("SELECT status,last_analyzed FROM files WHERE path=?", (subject_id,)).fetchone()
            alive = bool(row) and row["status"] != "deleted"
            changed_at = row["last_analyzed"] if row else None
        else:
            # Unscopable subjects: the newest code change anywhere supersedes.
            row = self.db.execute("SELECT MAX(updated_at) AS moved FROM symbols WHERE status='active'").fetchone()
            changed_at = row["moved"] if row else None
        state = self._verification_applicability(latest["recorded_at"], latest["result"], changed_at, alive)
        if subject_type not in ("symbol", "file") and state["applicability"] == "SUPERSEDED":
            state["reason"] = (f"Code changed at {changed_at} after this was verified at {latest['recorded_at']}. "
                               f"A {subject_type}-level result cannot be scoped to the code it exercised, so any "
                               "later change supersedes it.")
        return {"subject_type": subject_type, "subject_id": subject_id, "kind": latest["kind"],
                "history_count": history, **state}

    def verify(self, subject_type: str, subject_id: str, kind: str, result: str, evidence: str = "") -> dict:
        now = NOW()
        self.db.execute("INSERT INTO verifications(subject_type,subject_id,kind,result,evidence,recorded_at) VALUES(?,?,?,?,?,?)", (subject_type, subject_id, kind, result.upper(), evidence, now))
        if subject_type == "symbol":
            self.db.execute("UPDATE symbols SET last_verified=? WHERE name=? AND status='active'", (result.upper(), subject_id))
        elif subject_type == "feature":
            self.db.execute("UPDATE features SET last_verified=?,status=? WHERE name=?", (now, result.upper() if result.upper() in {"WORKING", "BROKEN", "PARTIAL"} else "UNVERIFIED", subject_id))
        self.db.commit(); record = {"subject_type": subject_type, "subject_id": subject_id, "kind": kind, "result": result.upper(), "evidence": evidence, "recorded_at": now}
        record["regressions"] = self.regressions(subject_type, subject_id)
        # What this result is worth *now*, computed the same way every reader
        # computes it. Recorded and current are different questions.
        record["state"] = self.verification_state(subject_type, subject_id)
        return record

    def verify_command(self, subject_type: str, subject_id: str, kind: str, command: list[str], evidence: str = "") -> dict:
        if not command:
            raise ValueError("A verification command is required")
        completed = subprocess.run(command, cwd=self.root, text=True, capture_output=True)
        output = (completed.stdout + "\n" + completed.stderr).strip()[-4000:]
        result = "PASSED" if completed.returncode == 0 else "FAILED"
        return self.verify(subject_type, subject_id, kind, result, evidence or output)

    def regressions(self, subject_type: str | None = None, subject_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM verifications"; args = []
        clauses = []
        if subject_type: clauses.append("subject_type=?"); args.append(subject_type)
        if subject_id: clauses.append("subject_id=?"); args.append(subject_id)
        if clauses: query += " WHERE " + " AND ".join(clauses)
        rows = [dict(row) for row in self.db.execute(query + " ORDER BY recorded_at", args)]
        grouped = {}
        for row in rows: grouped.setdefault((row["subject_type"], row["subject_id"], row["kind"]), []).append(row)
        result = []
        for key, history in grouped.items():
            for index, current in enumerate(history):
                if current["result"] in {"FAILED", "BROKEN", "ERROR"} and any(previous["result"] in {"PASSED", "WORKING"} for previous in history[:index]):
                    result.append({"subject_type": key[0], "subject_id": key[1], "kind": key[2], "status": "REGRESSION", "current": current, "previous_working": next(previous for previous in reversed(history[:index]) if previous["result"] in {"PASSED", "WORKING"})})
        return result

    def upsert_issue(self, key: str, title: str, description: str = "", severity: str = "MEDIUM", status: str = "OPEN") -> dict:
        now = NOW()
        self.db.execute("INSERT INTO issues(key,title,description,status,severity,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET title=excluded.title,description=excluded.description,status=excluded.status,severity=excluded.severity,updated_at=excluded.updated_at", (key, title, description, status.upper(), severity.upper(), now, now))
        self.db.commit(); return dict(self.db.execute("SELECT * FROM issues WHERE key=?", (key,)).fetchone())

    def upsert_decision(self, key: str, title: str, rationale: str = "", status: str = "ACTIVE") -> dict:
        now = NOW()
        self.db.execute("INSERT INTO decisions(key,title,rationale,status,created_at) VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET title=excluded.title,rationale=excluded.rationale,status=excluded.status", (key, title, rationale, status.upper(), now))
        self.db.commit(); return dict(self.db.execute("SELECT * FROM decisions WHERE key=?", (key,)).fetchone())

    def upsert_feature(self, name: str, description: str = "", status: str = "UNKNOWN") -> dict:
        now = NOW(); self.db.execute("INSERT INTO features(name,description,status,last_changed) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET description=excluded.description,status=excluded.status,last_changed=excluded.last_changed", (name, description, status.upper(), now)); self.db.commit(); return dict(self.db.execute("SELECT * FROM features WHERE name=?", (name,)).fetchone())

    def features(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM features ORDER BY name")]

    def infer_features(self) -> list[dict]:
        """Infer conservative feature groups from paths; never overwrite explicit descriptions."""
        groups = {}
        labels = {"auth":"Authentication", "login":"Authentication", "admin":"Administration", "user":"Users", "payment":"Payments", "ticket":"Ticketing", "notification":"Notifications", "message":"Messaging", "report":"Reporting", "dashboard":"Dashboard", "dtr":"Time Records"}
        for path, *_ in self._discover():
            rel = path.relative_to(self.root).as_posix(); low = rel.lower(); label = next((name for key, name in labels.items() if key in low), None)
            if not label:
                parts = Path(rel).parts
                label = parts[1].replace("-", " ").replace("_", " ").title() if len(parts) > 2 and parts[0] in {"src", "app", "pages", "components"} else None
            if label: groups.setdefault(label, set()).add(rel)
        now = NOW(); result = []
        for name, paths in sorted(groups.items()):
            self.db.execute("INSERT INTO features(name,description,status,last_changed) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET last_changed=excluded.last_changed", (name, f"Inferred from {len(paths)} related source paths", "UNKNOWN", now))
            result.append({"name": name, "files": sorted(paths), "status": "UNKNOWN", "evidence": "path and naming analysis"})
        self.db.commit(); return result

    def mcp_command(self) -> list[str]:
        """How this project's MCP server should be launched. One source of truth.

        Every agent registration and the doctor self-test read this, so the path
        logic exists once rather than once per vendor.
        """
        return mcp_launch_command(self.root)

    def agent_config(self, agent: str) -> dict:
        executable = {"codex": "codex", "claude-code": "claude", "gemini": "gemini", "aider": "aider", "cursor": "cursor"}.get(agent, agent)
        launch = self.mcp_command()
        # Quoted for copy/paste into a shell: the interpreter path, the project
        # root, or both may contain spaces.
        printable = " ".join(f'"{part}"' if " " in part else part for part in launch)
        return {"agent": agent, "command": f"{executable} mcp add codeledger -- {printable}",
                "launch_command": launch, "server": "codeledger", "transport": "stdio", "root": str(self.root),
                "note": "Agent CLI syntax may vary by version; use this command as the setup template and verify its output."}

    def why(self, query: str) -> dict:
        symbols = self.lookup(query)
        changes = self.history(query)
        # A symbol's own name rarely appears in a change summary, so text search
        # alone answered "why does this exist?" with nothing. Walk the recorded
        # symbol->change links instead, which is what the ledger is for.
        linked, attribution = [], []
        for symbol in symbols:
            attribution.append({"symbol": symbol["name"], "file": symbol["path"], "status": symbol["status"], "last_modified_by": symbol["last_modified_by"] or "unknown", "last_modified_session": symbol["last_modified_session"] or "NOT RECORDED", "updated_at": symbol["updated_at"]})
            linked.extend(dict(row) for row in self.db.execute(
                "SELECT c.*,cs.name AS symbol FROM change_symbols cs JOIN changes c ON c.id=cs.change_id WHERE cs.symbol_id=? OR cs.name=? ORDER BY c.id DESC LIMIT 20",
                (symbol["id"], symbol["name"])))
        seen, recorded = set(), []
        for row in linked:
            if row["id"] not in seen: seen.add(row["id"]); recorded.append(row)
        requests = [row["user_request"] for row in recorded if row["user_request"]]
        answer = "No recorded reason found. Current source and Git evidence should be inspected; attribution is UNKNOWN."
        if requests: answer = f"Last recorded request touching this symbol: {requests[0]}"
        elif recorded or changes: answer = "Changes are recorded, but no originating request was captured."
        return {"query": query, "symbols": symbols, "attribution": attribution, "changes": changes, "recorded_changes": recorded, "answer": answer}

    # ------------------------------------------------------------------
    # Context continuity: turning a session's working memory into something
    # the next session can read without the conversation or the repository.
    # ------------------------------------------------------------------

    def _estimate_tokens(self, value) -> int:
        """A deliberately crude size estimate, never presented as a measurement."""
        if value is None: return 0
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        return len(text) // CHARS_PER_TOKEN

    def _session_row(self, session_id: str = ""):
        if session_id:
            return self.db.execute("SELECT s.*,a.name AS agent FROM sessions s LEFT JOIN agents a ON a.id=s.agent_id WHERE s.session_id=?", (session_id,)).fetchone()
        live = self._session_rows(LIVE)
        return live[0] if live else (self._session_rows() or [None])[0]

    def context_usage(self, context_window: int | None = None, context_used: int | None = None) -> dict:
        """What the runtime said about its own context, and what follows from it.

        Most runtimes expose nothing here, so UNKNOWN is the normal answer and
        must stay usable: a checkpoint is worth taking at the end of a session
        whatever the numbers say, and this never reports a percentage it was not
        given. CodeLedger only ever recommends — it does not interrupt an agent.
        """
        threshold = self.config.checkpoint_threshold_pct
        if not context_window or not context_used or context_window <= 0:
            return {"context_window": UNKNOWN, "context_used": UNKNOWN, "context_remaining": UNKNOWN,
                    "context_used_pct": UNKNOWN, "threshold_pct": threshold,
                    "checkpoint_recommended": False, "recommendation": "UNKNOWN_CONTEXT",
                    "message": ("This runtime did not report its context usage, so no threshold can be applied. "
                                "Record a checkpoint before the session ends, or whenever the task reaches a "
                                "state another session would need to continue from.")}
        pct = round(context_used / context_window * 100, 1)
        recommended = pct >= threshold
        return {"context_window": context_window, "context_used": context_used,
                "context_remaining": max(0, context_window - context_used), "context_used_pct": pct,
                "threshold_pct": threshold, "checkpoint_recommended": recommended,
                "recommendation": "CHECKPOINT_RECOMMENDED" if recommended else "NOT_YET",
                "message": (f"Estimated context usage {pct}% is at or past the {threshold}% threshold. "
                            "Record a CodeLedger checkpoint before continuing." if recommended
                            else f"Estimated context usage {pct}%, below the {threshold}% threshold.")}

    def session_state(self, session_id: str = "", request: str = "", context_window: int | None = None,
                      context_used: int | None = None) -> dict:
        """Everything CodeLedger already knows about this session, ready to checkpoint.

        This is the mechanical half of a checkpoint and nothing more. CodeLedger
        never sees the conversation, so it cannot write the goal, the rationale
        behind a decision, or what was tried and abandoned — an agent supplies
        those to `record_checkpoint`. Inventing them here would produce a
        confident summary with no evidence under it, which is the failure this
        project exists to avoid.
        """
        row = self._session_row(session_id)
        if row is None:
            return {"status": "NO_SESSION", "session_id": session_id or UNKNOWN,
                    "guidance": "No session has been recorded for this project yet. Start one with `codeledger session start --agent <name>`, or call this from an MCP session.",
                    "context": self.context_usage(context_window, context_used)}
        session_id = row["session_id"]
        task = request or row["request"] or ""
        changes = [dict(item) for item in self.db.execute(
            "SELECT id,timestamp,agent,user_request,summary,effect,risk,result FROM changes WHERE session_id=? ORDER BY id DESC LIMIT 50", (session_id,))]
        files, symbols = set(), set()
        for change in changes:
            files.update(item["path"] for item in self.db.execute("SELECT path FROM change_files WHERE change_id=?", (change["id"],)))
            symbols.update(item["name"] for item in self.db.execute("SELECT name FROM change_symbols WHERE change_id=?", (change["id"],)))
        verifications = [dict(item) for item in self.db.execute(
            "SELECT id,subject_type,subject_id,kind,result,recorded_at FROM verifications WHERE recorded_at>=? ORDER BY recorded_at DESC LIMIT 20", (row["start_time"],))]
        existing = [dict(item) for item in self.db.execute(
            "SELECT id,created_at,goal,status,source FROM checkpoints WHERE session_id=? ORDER BY id DESC", (session_id,))]
        return {
            "status": "READY", "session_id": session_id, "agent": row["agent"] or "unknown",
            "provider": row["provider"] or UNKNOWN, "model": row["model"] or UNKNOWN,
            "model_version": row["model_version"] or UNKNOWN,
            "session_status": row["status"], "started_at": row["start_time"], "request": task or UNKNOWN,
            "git_commit": head(self.root),
            "changes": changes, "files_touched": sorted(files), "symbols_touched": sorted(symbols),
            "verifications": verifications,
            "open_issues": [dict(item) for item in self.db.execute("SELECT key,title,severity FROM issues WHERE status='OPEN' ORDER BY updated_at DESC LIMIT 10")],
            "active_decisions": [dict(item) for item in self.db.execute("SELECT key,title FROM decisions WHERE status='ACTIVE' ORDER BY created_at DESC LIMIT 10")],
            "progress": self.progress(task) if task else None,
            "context": self.context_usage(context_window, context_used),
            "existing_checkpoints": existing,
            "supply_to_checkpoint": ["goal", "summary", "current_state", "accomplished", "unresolved",
                                     "failed_attempts", "next_action"],
            "guidance": ("CodeLedger has assembled what it recorded. It cannot see the conversation, so the goal, "
                         "what was learned, what was abandoned and what to do next must come from you. Pass them to "
                         "`codeledger_record_checkpoint` with the file and symbol lists above."),
        }

    def _link_item(self, kind: str, value: str) -> tuple[str | None, str | None, str | None]:
        """Point a checkpoint entry at an existing row wherever one exists.

        A checkpoint that copies a decision's text becomes a second, silently
        diverging record of it. A reference cannot diverge: it either resolves
        to the current row or is reported as stale.
        """
        text = (value or "").strip()
        if not text: return None, None, None
        if kind == "file": return "file", text, None
        if kind == "symbol": return "symbol", text, None
        if kind == "decision" and self.db.execute("SELECT 1 FROM decisions WHERE key=?", (text,)).fetchone():
            return "decision", text, None
        if kind == "issue" and self.db.execute("SELECT 1 FROM issues WHERE key=?", (text,)).fetchone():
            return "issue", text, None
        if kind == "verification" and text.isdigit() and self.db.execute("SELECT 1 FROM verifications WHERE id=?", (int(text),)).fetchone():
            return "verification", text, None
        if text.isdigit() and self.db.execute("SELECT 1 FROM changes WHERE id=?", (int(text),)).fetchone():
            return "change", text, None
        return None, None, text

    def record_checkpoint(self, session_id: str = "", goal: str = "", summary: str = "", current_state: str = "",
                          next_action: str = "", accomplished: list[str] | None = None,
                          unresolved: list[str] | None = None, failed_attempts: list[str] | None = None,
                          questions: list[str] | None = None, decisions: list[str] | None = None,
                          issues: list[str] | None = None, verifications: list[str] | None = None,
                          files: list[str] | None = None, symbols: list[str] | None = None,
                          agent: str = "", provider: str = "", model: str = "", model_version: str = "",
                          context_window: int | None = None, context_used: int | None = None,
                          source: str = "agent") -> dict:
        """Store one compact, durable summary of where a task has got to."""
        goal = (goal or "").strip()
        if not goal:
            # Selection is by goal. A checkpoint without one can never be matched
            # to a later task, so it would silently never be retrieved.
            raise ValueError("A checkpoint needs a goal: it is what a later session matches its task against.")
        row = self._session_row(session_id)
        session_id = session_id or (row["session_id"] if row else f"session-{uuid.uuid4().hex[:10]}")
        agent = agent or (row["agent"] if row else "") or "unknown"
        runtime = resolve_model(agent, provider or (row["provider"] if row else ""),
                                model or (row["model"] if row else ""),
                                model_version or (row["model_version"] if row else ""))
        confidence = {"agent": "HIGH", "cli": "MEDIUM"}.get(source, "LOW")
        now = NOW()
        self._begin_immediate()
        try:
            cursor = self.db.execute(
                "INSERT INTO checkpoints(session_id,created_at,agent,provider,model,model_version,goal,summary,current_state,next_action,git_commit,context_window,context_used,source,confidence,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN')",
                (session_id, now, agent, runtime["provider"], runtime["model"], runtime["model_version"], goal,
                 summary, current_state, next_action, head(self.root), context_window, context_used, source, confidence))
            checkpoint_id = cursor.lastrowid
            for kind, values in (("accomplished", accomplished), ("unresolved", unresolved),
                                 ("failed_attempt", failed_attempts), ("question", questions),
                                 ("decision", decisions), ("issue", issues), ("verification", verifications),
                                 ("file", files), ("symbol", symbols)):
                for ordinal, value in enumerate(values or []):
                    ref_type, ref_id, text = self._link_item(kind, value)
                    if not (ref_type or text): continue
                    self.db.execute("INSERT INTO checkpoint_items(checkpoint_id,kind,ref_type,ref_id,text,ordinal) VALUES(?,?,?,?,?,?)",
                                    (checkpoint_id, kind, ref_type, ref_id, text, ordinal))
            # Only the newest checkpoint for a session is OPEN. The older ones
            # are kept and marked, never deleted: history is the product.
            self.db.execute("UPDATE checkpoints SET status='SUPERSEDED',superseded_by=? WHERE session_id=? AND id!=? AND status='OPEN'",
                            (checkpoint_id, session_id, checkpoint_id))
            self.db.commit()
        except Exception:
            self.db.rollback(); raise
        return self.checkpoint(checkpoint_id)

    def _checkpoint_items(self, checkpoint_id: int) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT kind,ref_type,ref_id,text,ordinal FROM checkpoint_items WHERE checkpoint_id=? ORDER BY kind,ordinal", (checkpoint_id,))]

    def checkpoint(self, checkpoint_id: int) -> dict:
        row = self.db.execute("SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,)).fetchone()
        if row is None: return {"status": "NOT_FOUND", "checkpoint_id": checkpoint_id}
        record = dict(row)
        grouped: dict[str, list] = {}
        for item in self._checkpoint_items(checkpoint_id):
            grouped.setdefault(item["kind"], []).append(item["text"] if item["text"] is not None else item["ref_id"])
        record["items"] = grouped
        record["estimated_tokens"] = self._estimate_tokens(record)
        return record

    def checkpoints(self, status: str = "", limit: int = 20) -> list[dict]:
        sql = "SELECT id,session_id,created_at,agent,provider,model,goal,status,source,confidence FROM checkpoints"
        args: list = []
        if status: sql += " WHERE status=?"; args.append(status.upper())
        return [dict(row) for row in self.db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))]

    def _validate_checkpoint(self, row) -> tuple[dict, list[dict]]:
        """Check a checkpoint against the source before believing any of it.

        The ordering CodeLedger already commits to is source code, then the
        filesystem and Git, then tests, then structured memory, then summaries.
        A checkpoint is the last of those, so every claim it makes about a file
        or a symbol is re-checked here and dropped from the body if the source no
        longer supports it. Nothing is deleted — a stale entry is reported as
        stale, which is itself useful: it says what changed underneath the work.
        """
        items = self._checkpoint_items(row["id"])
        paths = {item["ref_id"] for item in items if item["kind"] == "file" and item["ref_id"]}
        symbol_names = [item["ref_id"] for item in items if item["kind"] == "symbol" and item["ref_id"]]
        symbol_rows: dict[str, dict] = {}
        for name in symbol_names:
            found = self.db.execute("SELECT s.status,f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.name=? ORDER BY s.status='active' DESC LIMIT 1", (name,)).fetchone()
            if found:
                symbol_rows[name] = dict(found); paths.add(found["path"])
        unreliable = {item["path"]: item["reason"] for item in self.stale_records(sorted(paths))}
        fresh: dict[str, list] = {}
        stale: list[dict] = []
        for item in items:
            kind, ref_id = item["kind"], item["ref_id"]
            value = item["text"] if item["text"] is not None else ref_id
            reason = None
            if kind == "file" and ref_id in unreliable:
                reason = unreliable[ref_id]
            elif kind == "symbol":
                found = symbol_rows.get(ref_id)
                if not found:
                    reason = "the symbol is no longer in the index"
                elif found["status"] != "active":
                    reason = "the symbol has been deleted since the checkpoint was written"
                elif found["path"] in unreliable:
                    reason = f"its file is not what was indexed: {unreliable[found['path']]}"
            elif kind == "issue" and ref_id:
                issue = self.db.execute("SELECT status FROM issues WHERE key=?", (ref_id,)).fetchone()
                if issue and issue["status"] != "OPEN":
                    reason = f"the issue is now {issue['status']}"
            elif kind == "decision" and ref_id:
                decision = self.db.execute("SELECT status FROM decisions WHERE key=?", (ref_id,)).fetchone()
                if decision and decision["status"] != "ACTIVE":
                    reason = f"the decision is now {decision['status']}"
            if reason:
                stale.append({"kind": kind, "value": value, "reason": reason})
            else:
                fresh.setdefault(kind, []).append(value)
        return fresh, stale

    def resume(self, task: str = "", limit: int = 20) -> dict:
        """The smallest package that lets a new session continue previous work.

        Selection is by task, not by recency: loading the newest checkpoint when
        the user has moved on to something else is how an agent gets confidently
        pointed at the wrong part of the repository. With no relevant checkpoint
        this says so and lists what exists, rather than promoting an unrelated
        one to fill the space.
        """
        candidates = self.db.execute("SELECT * FROM checkpoints WHERE status='OPEN' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        total_files = self.db.execute("SELECT count(*) FROM files WHERE status IN ('current','unindexed')").fetchone()[0]
        if not candidates:
            return self._resume_without_checkpoint(task, total_files)
        selected, score, matched, basis = None, 0.0, [], ""
        # A request made entirely of common words ("fix the code") leaves nothing
        # distinctive to match on. Scoring it would compare two empty sets and
        # reject every checkpoint, so relevance is declared unassessable and the
        # basis says so — an agent can see it was not actually matched.
        if task and not self._salient(task):
            selected, basis = candidates[0], "most recent open checkpoint; the task had no distinctive terms, so relevance could not be assessed"
        elif task:
            ranked = []
            for row in candidates:
                found, overlap = self._relevance(self._salient(task), self._salient(f"{row['goal']} {row['summary'] or ''}"))
                if found: ranked.append((found, row["id"], row, overlap))
            ranked.sort(key=lambda item: (-item[0], -item[1]))
            if ranked:
                score, _, selected, matched = ranked[0]
                basis = "matched against the task you supplied"
        else:
            selected, basis = candidates[0], "most recent open checkpoint; no task was supplied, so relevance was not assessed"
        if selected is None:
            return {"status": "NO_RELEVANT_CHECKPOINT", "task": task,
                    "open_checkpoints": [{"checkpoint_id": row["id"], "goal": row["goal"], "created_at": row["created_at"],
                                          "agent": row["agent"], "model": row["model"] or UNKNOWN} for row in candidates],
                    "guidance": ("No recorded checkpoint describes this task. The open ones above are listed so you can "
                                 "pick one deliberately; none was loaded, because unrelated previous work is worse than "
                                 "no context. Proceed with `codeledger_get_plan` for a fresh task.")}
        fresh, stale = self._validate_checkpoint(selected)
        moved = self.since(marker=selected["created_at"], limit=20)
        commit = head(self.root)
        package = {
            "status": "RESUME", "task": task or UNKNOWN,
            "checkpoint_id": selected["id"], "created_at": selected["created_at"],
            "selection": {"basis": basis, "match_score": round(score, 2), "matched_terms": matched,
                          "open_checkpoints_considered": len(candidates)},
            "recorded_by": {"agent": selected["agent"] or "unknown", "provider": selected["provider"] or UNKNOWN,
                            "model": selected["model"] or UNKNOWN, "model_version": selected["model_version"] or UNKNOWN,
                            "session_id": selected["session_id"], "confidence": selected["confidence"],
                            "source": selected["source"]},
            "goal": selected["goal"], "summary": selected["summary"] or "", "current_state": selected["current_state"] or "",
            "accomplished": fresh.get("accomplished", []), "unresolved": fresh.get("unresolved", []),
            "failed_attempts": fresh.get("failed_attempt", []), "open_questions": fresh.get("question", []),
            "important_files": fresh.get("file", []), "important_symbols": fresh.get("symbol", []),
            "decisions": [dict(item) for item in self.db.execute(
                "SELECT key,title,rationale FROM decisions WHERE status='ACTIVE' AND key IN ({})".format(",".join("?" * len(fresh.get("decision", [])))),
                fresh.get("decision", []))] if fresh.get("decision") else [],
            "known_issues": [dict(item) for item in self.db.execute(
                "SELECT key,title,severity FROM issues WHERE status='OPEN' AND key IN ({})".format(",".join("?" * len(fresh.get("issue", [])))),
                fresh.get("issue", []))] if fresh.get("issue") else [],
            "verification": fresh.get("verification", []),
            "next_action": selected["next_action"] or "NOT RECORDED",
            "stale_items": stale,
            "changed_since_checkpoint": {"summary": moved["summary"], "files": moved["files_changed"][:20],
                                         "symbols": moved["symbols_changed"][:20], "agents": moved["agents"]},
            "git": {"checkpoint_commit": selected["git_commit"] or UNKNOWN, "current_commit": commit or UNKNOWN,
                    "moved": bool(selected["git_commit"] and commit and selected["git_commit"] != commit)},
        }
        relevant = len(package["important_files"])
        package["efficiency"] = {
            "files_relevant": relevant, "files_in_repository": total_files,
            "files_avoided": max(0, total_files - relevant),
            "full_repository_scan_required": False,
            "estimated_checkpoint_tokens": self._estimate_tokens({k: package[k] for k in ("goal", "summary", "current_state", "accomplished", "unresolved", "failed_attempts", "next_action")}),
            "estimated_history_tokens": self._estimate_tokens(package["changed_since_checkpoint"]),
            "estimated_total_tokens": self._estimate_tokens(package),
            "note": "Token figures are estimates at roughly four characters per token, not measurements.",
        }
        package["guidance"] = (
            "This is recorded engineering memory, not current truth. Anything it says about a file or symbol was "
            "re-checked against the source just now, and whatever no longer holds is in `stale_items` rather than "
            "above. Read `next_action` first; re-read the listed files before editing them.")
        if stale:
            package["guidance"] += f" {len(stale)} recorded item(s) no longer match the source and were excluded."
        return package

    def _resume_without_checkpoint(self, task: str, total_files: int) -> dict:
        """Is there recent unfinished work, in a project that predates checkpoints?

        Derived rather than recorded, and labelled as such: unverified changes
        are evidence that something was left in the middle, not a statement that
        it was. Projects upgrading to this version have no checkpoints at all,
        and answering "nothing here" would be wrong.
        """
        recent = self.since(limit=10)
        open_issues = self.db.execute("SELECT count(*) FROM issues WHERE status='OPEN'").fetchone()[0]
        if task:
            # With a task in hand, orient around *its* code rather than around
            # whatever happened to change last. This is the difference between
            # a summary and a dump: the same query the rest of the pre-change
            # layer uses, bounded, instead of the repository's recent history.
            relevant = self.search_symbols(task)
            relevant_files = sorted({row["path"] for row in relevant})
            relevant_symbols = [row["name"] for row in relevant[:10]]
        else:
            relevant_files, relevant_symbols = [], []
        summary = {"project": self.config.project_name, "recent_changes": len(recent["changes"]),
                   "relevant_files": len(relevant_files), "relevant_symbols": len(relevant_symbols),
                   "open_issues": open_issues, "checkpoint": "NONE"}
        unverified = [item for item in recent["changes"] if item["effect"] != "none"]
        if not unverified:
            return {"status": "NO_CHECKPOINTS", "task": task or UNKNOWN, "recent_work": [], "summary": summary,
                    "relevant_files": relevant_files[:10], "relevant_symbols": relevant_symbols,
                    "guidance": "No checkpoint has been recorded for this project and nothing recent looks unfinished. Start with `codeledger_get_plan`."}
        return {"status": "NO_CHECKPOINTS_RECENT_WORK_FOUND", "task": task or UNKNOWN, "summary": summary,
                "recent_work": unverified[:5],
                "relevant_files": relevant_files[:10], "relevant_symbols": relevant_symbols,
                "files": recent["files_changed"][:20],
                "symbols": recent["symbols_changed"][:20], "agents": recent["agents"],
                "efficiency": {"files_relevant": len(relevant_files) or len(recent["files_changed"]),
                               "files_in_repository": total_files,
                               "files_avoided": max(0, total_files - len(recent["files_changed"])),
                               "full_repository_scan_required": False,
                               "estimated_tokens": self._estimate_tokens(summary) + self._estimate_tokens(unverified[:5])},
                "guidance": ("No checkpoint exists, so this is inferred from recorded changes rather than recorded "
                             "intent: it shows what was changed recently, not what anybody meant to do. Lists are "
                             "bounded and report their totals; use `codeledger since` for the full record. Record a "
                             "checkpoint at the end of this session so the next one does not have to guess.")}

    def _mechanical_checkpoint(self, session_id: str, reason: str) -> dict | None:
        """A last-resort checkpoint written when a session ends without one.

        It is honest about being thin: nobody was asked what the work meant, so
        it carries LOW confidence, no rationale and no next action. Its value is
        that the files and symbols a session touched are not lost entirely.
        """
        if not session_id: return None
        if self.db.execute("SELECT 1 FROM checkpoints WHERE session_id=? AND source='agent'", (session_id,)).fetchone():
            return None
        state = self.session_state(session_id)
        if state.get("status") != "READY" or not state["changes"]:
            return None
        # A session started over MCP carries no request of its own — the agent
        # was never asked for one. The changes it recorded do carry the request
        # they were made for, and that is the only description of this work that
        # exists, so a checkpoint that ignored it would be unfindable by task.
        requests = [item["user_request"] for item in state["changes"] if item["user_request"]]
        goal = state["request"] if state["request"] != UNKNOWN else (requests[0] if requests else f"Unlabelled work in {session_id}")
        return self.record_checkpoint(
            session_id=session_id, goal=goal,
            summary=f"Recorded automatically when the session ended ({reason}). No agent summary was supplied.",
            current_state=f"{len(state['changes'])} change(s) recorded; {len(state['symbols_touched'])} symbol(s) touched.",
            next_action="NOT RECORDED — no agent summary was captured before the session ended.",
            files=state["files_touched"], symbols=state["symbols_touched"],
            agent=state["agent"], provider=state["provider"], model=state["model"],
            model_version=state["model_version"], source="mechanical")

    def export(self) -> list[str]:
        target = self.root / ".ai" / "codeledger" / "exports"; target.mkdir(parents=True, exist_ok=True)
        data = self.status(); project = target / "project.md"; project.write_text("# CodeLedger Project Memory\n\n" + "\n".join(f"- {key}: {value}" for key, value in data.items()) + "\n", encoding="utf-8")
        changes = self.db.execute("SELECT id,timestamp,agent,summary FROM changes ORDER BY id DESC LIMIT 50").fetchall(); recent = target / "recent-changes.md"
        recent.write_text("# Recent Changes\n\n" + "\n".join(f"- #{row['id']} {row['timestamp']} — {row['agent'] or 'UNKNOWN'} — {row['summary'] or 'NOT RECORDED'}" for row in changes) + "\n", encoding="utf-8")
        return [str(project), str(recent)]
