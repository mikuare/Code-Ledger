from __future__ import annotations

import re
import uuid
import os
import time
import subprocess
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from .config import Config
from .db import connect
from .git import commit_files, commits, head, status as git_status
from .parser import digest, digest_bytes, language
from .providers import FULL, SHALLOW, analyze as analyze_source, capabilities, version_for

LIKE_ESCAPE = str.maketrans({"\\": r"\\", "%": r"\%", "_": r"\_"})
# Words too generic to define a task boundary from a file path.
STOPWORDS = {"add", "also", "and", "any", "app", "back", "been", "code", "create", "change", "changes", "current", "delete", "each", "file", "files", "fix", "from", "have", "into", "make", "more", "must", "need", "new", "not", "only", "page", "please", "remove", "should", "some", "that", "the", "them", "then", "there", "this", "update", "use", "using", "when", "where", "which", "with", "without", "work"}

NOW = lambda: datetime.now(timezone.utc).isoformat()

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
    paths: list[str] = field(default_factory=list)
    large_files: list[tuple[str, int, int]] = field(default_factory=list)

    def as_dict(self):
        return {"directories_visited": self.directories_visited, "directories_skipped": self.directories_skipped, "files_discovered": self.files_discovered, "files_ignored": self.files_ignored, "files_skipped_large": self.files_skipped_large, "files_skipped_type": self.files_skipped_type, "permission_errors": self.permission_errors, "broken_symlinks": self.broken_symlinks}

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

For automatic lifecycle tracking, run the agent through `codeledger run --agent <name> --request \"<task>\" -- <agent command>`, or run `codeledger watch --agent <name>` in a second terminal while the agent edits this project. MCP-capable agents may launch `codeledger mcp --root <project>`.
"""
        for name in ("AGENTS.md", "CLAUDE.md", "CODEX.md"):
            path = self.root / name
            if not path.exists() or "CodeLedger Protocol" in path.read_text(encoding="utf-8", errors="ignore"):
                path.write_text(text, encoding="utf-8")
        integration = self.root / ".ai" / "codeledger" / "agent-integration.md"
        integration.parent.mkdir(parents=True, exist_ok=True)
        integration.write_text("# CodeLedger Agent Integration\n\n## One-time Codex setup\n\nRun `codeledger setup-codex` from this project. It registers the local MCP server with Codex using the absolute project path. Start a new Codex session after setup if Codex was already running.\n\n## Persistent workflow\n\nRun `codeledger watch --agent codex` in a second terminal and leave it running while Codex works. The watcher records changed files and symbols without requiring `status` or `changes` after every task.\n\n## MCP tools\n\nCodex can call `codeledger_get_context`, `codeledger_find_symbol`, `codeledger_get_impact`, `codeledger_get_history`, `codeledger_get_recent_changes`, `codeledger_get_issues`, `codeledger_get_decisions`, `codeledger_record_change`, and `codeledger_mark_verified` during an ongoing conversation.\n", encoding="utf-8")

    def _discover(self, verbose: bool = False):
        """Discover source files without descending into ignored directories."""
        metrics = DiscoveryMetrics(); patterns = self.config.ignore_patterns(self.root)
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
                    rel = Path(entry.path).relative_to(self.root).as_posix()
                    rel_path = Path(rel)
                    try:
                        if entry.is_symlink():
                            if not self.config.follow_symlinks:
                                if not os.path.exists(entry.path): metrics.broken_symlinks += 1
                                metrics.directories_skipped += 1
                                if verbose: print(f"Skipping symlink: {rel}")
                                continue
                        if entry.is_dir(follow_symlinks=self.config.follow_symlinks):
                            if self.config.is_ignored(rel_path, self.root, patterns):
                                metrics.directories_skipped += 1
                                if verbose: print(f"Skipping directory: {rel}/")
                            else:
                                stack.append(Path(entry.path))
                            continue
                        if self.config.is_ignored(rel_path, self.root, patterns):
                            metrics.files_ignored += 1
                            continue
                        if not self.config.is_source_file(entry.name):
                            metrics.files_skipped_type += 1
                            continue
                        stat = entry.stat(follow_symlinks=False)
                        if stat.st_size > self.config.max_file_size:
                            metrics.files_skipped_large += 1
                            metrics.large_files.append((rel, stat.st_size, stat.st_mtime_ns))
                            if verbose: print(f"Skipping large file: {rel} ({stat.st_size} bytes)")
                            continue
                        metrics.files_discovered += 1; metrics.paths.append(rel)
                        yield Path(entry.path), rel, stat.st_size, stat.st_mtime_ns, metrics
                    except (OSError, PermissionError):
                        metrics.permission_errors += 1
            finally:
                entries.close()
        self._last_discovery_metrics = metrics

    def _files(self):
        for path, _rel, _size, _mtime_ns, _metrics in self._discover():
            yield path

    def refresh(self, changed_only: bool = True, agent: str = "unknown", session: str = "", request: str = "", record: bool = True, analyze: bool = True, verbose: bool = False, include_git_status: bool = False) -> dict[str, int | str | None | list[str] | dict]:
        started = time.perf_counter(); statuses = git_status(self.root) if include_git_status and (self.root / ".git").exists() and analyze else {}
        actor = agent or "unknown"   # attribution is recorded as given; it is never guessed
        seen, changed_paths, changed_symbols = set(), [], []
        added, modified, deleted, symbols = 0, 0, 0, 0
        discovery_started = time.perf_counter(); discovered = list(self._discover(verbose=verbose)); discovery_seconds = time.perf_counter() - discovery_started
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
            if changed_only and fresh and old["size"] == size and old["mtime_ns"] == mtime_ns:
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
            old_symbols = {r["name"]: r for r in self.db.execute("SELECT * FROM symbols WHERE file_id=? AND status='active'", (file_id,))}
            current = set()
            for item in parsed:
                current.add(item.name); prior = old_symbols.get(item.name)
                if prior and prior["hash"] == item.hash:
                    # Same content, possibly shifted by an edit elsewhere in the
                    # file. Line numbers are facts and get refreshed; authorship
                    # and updated_at must not move, or every refresh would
                    # reassign credit for symbols nobody touched.
                    self.db.execute("UPDATE symbols SET kind=?,line_start=?,line_end=?,signature=?,status='active' WHERE id=?", (item.kind, item.start, item.end, item.signature, prior["id"]))
                    continue
                if prior:
                    self.db.execute("UPDATE symbols SET kind=?,line_start=?,line_end=?,signature=?,hash=?,updated_at=?,status='active',last_modified_by=?,last_modified_session=? WHERE id=?", (item.kind, item.start, item.end, item.signature, item.hash, now, actor, session, prior["id"]))
                else:
                    self.db.execute("INSERT INTO symbols(name,qualified_name,kind,file_id,line_start,line_end,signature,hash,status,created_at,updated_at,last_modified_by,last_modified_session) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (item.name, item.name, item.kind, file_id, item.start, item.end, item.signature, item.hash, "active", now, now, actor, session)); symbols += 1
                changed_symbols.append(item.name)
            for name, prior in old_symbols.items():
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
        for row in self.db.execute("SELECT id,path FROM files WHERE status!='deleted'").fetchall():
            if row["path"] not in seen and not (self.root / row["path"]).exists():
                self.db.execute("UPDATE files SET status='deleted',last_modified_by=?,last_modified_session=? WHERE id=?", (actor, session, row["id"])); deleted += 1; changed_paths.append(row["path"])
                # The file's symbols went with it. Leaving them active let
                # `impact` name a deleted file's symbols as live dependents.
                for symbol in self.db.execute("SELECT name FROM symbols WHERE file_id=? AND status='active'", (row["id"],)).fetchall():
                    changed_symbols.append(symbol["name"])
                self.db.execute("UPDATE symbols SET status='deleted',deleted_at=?,updated_at=?,last_modified_by=?,last_modified_session=? WHERE file_id=? AND status='active'", (NOW(), NOW(), actor, session, row["id"]))
        db_started = time.perf_counter(); self.db.commit(); db_seconds = time.perf_counter() - db_started; total = time.perf_counter() - started
        change_id = None
        if record and changed_paths:
            change_id = self.record_change(agent, session, request, f"Indexed {len(changed_paths)} changed file(s)", "unverified", changed_paths, sorted(set(changed_symbols)), added, modified, deleted, risk=self.analyze_prompt(request)["risk"] if request else "UNKNOWN")
        result = {"files_added": added, "files_modified": modified, "files_deleted": deleted, "symbols_changed": symbols, "change_id": change_id, "files": changed_paths, "symbols": sorted(set(changed_symbols)), "metrics": metrics.as_dict(), "timing": {"discovery_seconds": round(discovery_seconds, 4), "hashing_seconds": round(hash_seconds, 4), "parsing_seconds": round(parse_seconds, 4), "database_seconds": round(db_seconds, 4), "total_seconds": round(total, 4)}}
        if request:
            result["scope"] = self.scope_check(request, changed_paths, changed_symbols)
        if verbose: print(f"Discovery: {result['timing']['discovery_seconds']}s | Hashing: {result['timing']['hashing_seconds']}s | Parsing: {result['timing']['parsing_seconds']}s | Database: {result['timing']['database_seconds']}s | Total: {result['timing']['total_seconds']}s")
        return result

    def status(self) -> dict:
        queries = {"files": "SELECT count(*) FROM files WHERE status IN ('current','unindexed')", "symbols": "SELECT count(*) FROM symbols WHERE status='active'", "deleted_symbols": "SELECT count(*) FROM symbols WHERE status='deleted'", "changes": "SELECT count(*) FROM changes", "issues": "SELECT count(*) FROM issues WHERE status='OPEN'"}
        result = {key: self.db.execute(sql).fetchone()[0] for key, sql in queries.items()}
        result.update(stale_files=self.db.execute("SELECT count(*) FROM files WHERE status='stale' OR hash IS NULL").fetchone()[0], project=self.config.project_name, git_commit=head(self.root))
        result["analysis"] = self.coverage_report()
        return result

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

    def lookup(self, query: str, limit: int = 200) -> list[dict]:
        # `%` and `_` in a user query are literal text, not wildcards, and the
        # result set is bounded so a one-character query cannot fan out.
        pattern = f"%{query.translate(LIKE_ESCAPE)}%"
        rows = self.db.execute("SELECT s.*,f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.name LIKE ? ESCAPE '\\' OR s.qualified_name LIKE ? ESCAPE '\\' ORDER BY s.status='active' DESC,s.name LIMIT ?", (pattern, pattern, limit)).fetchall()
        return [dict(r) for r in rows]

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
        matches = self.lookup(query, limit=limit)
        if not matches:
            return {"query": query, "symbols": [], "dependencies": [], "referencing_files": [], "risk": "UNKNOWN", "evidence": "No indexed symbol matched this query. Run `codeledger refresh --changed`, or pass --scan to read the working tree.", "source": "index"}
        names = sorted({row["name"] for row in matches}); ids = [row["id"] for row in matches]
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
            [row["path"] for row in matches]) if (row["coverage"] or SHALLOW) != FULL]
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
        return {"query": query, "symbols": matches, "dependencies": dependency_rows, "referencing_files": sorted(refs), "defining_files": sorted(defining), "risk": "HIGH" if len(blast) > 10 else "MEDIUM" if len(blast) > 3 else "LOW", "source": source, "coverage": "shallow" if shallow else "full", "evidence": evidence}

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

    def context(self, query: str) -> dict:
        matches = self.search_symbols(query); paths = sorted({r["path"] for r in matches})
        recent = [dict(r) for r in self.db.execute("SELECT id,timestamp,agent,summary,risk FROM changes ORDER BY id DESC LIMIT 5")]
        issues = [dict(r) for r in self.db.execute("SELECT key,title,severity FROM issues WHERE status='OPEN' ORDER BY updated_at DESC LIMIT 10")]
        decisions = [dict(r) for r in self.db.execute("SELECT key,title,rationale FROM decisions WHERE status='ACTIVE' ORDER BY created_at DESC LIMIT 10")]
        features = [dict(r) for r in self.db.execute("SELECT name,description,status,last_verified FROM features WHERE name LIKE ? ESCAPE '\\' ORDER BY name LIMIT 10", (f"%{query.translate(LIKE_ESCAPE)}%",))]
        return {"query": query, "task_analysis": self.analyze_prompt(query), "features": features, "symbols": matches[:20], "files": paths[:20], "recent_changes": recent, "known_issues": issues, "decisions": decisions, "scan_required": not bool(matches or features)}

    def analyze_prompt(self, prompt: str) -> dict:
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
        return {"original": prompt, "normalized": text, "intent": intent, "verb": verb, "areas": areas, "paths": paths, "constraints": constraints, "preservation_constraints": preservation, "data_sources": data_sources, "acceptance_criteria": acceptance, "risk": risk, "clarifying_questions": questions}

    def scope_check(self, request: str, changed_files: list[str], changed_symbols: list[str] | None = None) -> dict:
        """Conservative post-change scope check; missing evidence is never SAFE."""
        changed_files = sorted(set(changed_files)); changed_symbols = sorted(set(changed_symbols or []))
        if not changed_files:
            return {"status": "NO_CHANGES", "request": request, "allowed_files": [], "unexpected_files": [], "unexpected_symbols": []}
        context = self.context(request); allowed, evidence = self._task_boundary(context)
        if not allowed:
            return {"status": "UNKNOWN", "request": request, "reason": "Insufficient task-specific context to define a safe boundary.", "allowed_files": [], "unexpected_files": [], "unexpected_symbols": changed_symbols, "boundary_evidence": []}
        # A new file is tolerated only in a directory that already holds a
        # task-relevant file. Matching on prefixes instead let one hit under
        # `src/` mark the whole subtree SAFE, which made the guard vacuous.
        allowed_dirs = {str(Path(path).parent).replace("\\", "/") for path in allowed}; allowed_dirs.discard(".")
        unexpected = [path for path in changed_files if path not in allowed and str(Path(path).parent).replace("\\", "/") not in allowed_dirs]
        relevant_names = {row["name"] for row in context["symbols"]}
        unexpected_symbols = [name for name in changed_symbols if name not in relevant_names]
        status = "WARNING" if unexpected or unexpected_symbols else "SAFE"
        return {"status": status, "request": request, "allowed_files": sorted(allowed), "unexpected_files": unexpected, "unexpected_symbols": unexpected_symbols, "boundary_evidence": evidence, "reason": "Review the diff for unrelated changes." if status == "WARNING" else "Changed files are within the known task boundary."}

    def _task_boundary(self, context: dict) -> tuple[set[str], list[str]]:
        """Files a task may legitimately touch, with the evidence behind them.

        Indexed symbol matches are the strong signal. Paths written into the
        request are equally strong and are always honoured. Only when neither
        exists does this fall back to matching request keywords against file
        paths, which is weak but still better than refusing to judge at all —
        the guard was returning UNKNOWN on any request whose wording did not
        happen to match an indexed symbol name.
        """
        analysis = context["task_analysis"]; allowed = set(context["files"]); evidence = []
        if allowed:
            evidence.append("indexed symbols matching the request")
        indexed = [row["path"] for row in self.db.execute("SELECT path FROM files WHERE status!='deleted'")]
        named = {path for token in analysis["paths"] for path in indexed if token.strip("/") in path}
        if named:
            allowed |= named; evidence.append("paths named in the request")
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
        risk = "UNKNOWN" if context["scan_required"] else "HIGH" if len(impact_files) > 10 else "MEDIUM" if len(impact_files) > 3 else "LOW"
        recommendation = "Inspect the existing implementation before adding new code." if symbols else "Use targeted discovery; CodeLedger has no exact symbol match yet."
        analysis = context["task_analysis"]
        return {"request": request, "task_analysis": analysis, "existing_files": sorted(impact_files), "relevant_symbols": symbols, "recent_changes": context["recent_changes"], "known_issues": context["known_issues"], "decisions": context["decisions"], "risk": "HIGH" if analysis["risk"] == "HIGH" else risk, "recommendation": recommendation, "full_scan_required": context["scan_required"], "suggested_tests": self.suggest_tests(sorted(impact_files), [symbol["name"] for symbol in symbols])}

    def handshake(self, request: str, ai_plan: str = "") -> dict:
        analysis = self.analyze_prompt(request); plan_text = " ".join(ai_plan.split()); lower = plan_text.lower()
        if not plan_text:
            return {"status": "AWAITING_AI_PLAN", "request": request, "task_analysis": analysis, "message": "Submit the AI's proposed files, changes, and tests before editing."}
        required = set(re.findall(r"[a-z][a-z0-9_-]{3,}", analysis["normalized"].lower()))
        mentioned = set(re.findall(r"[a-z][a-z0-9_-]{3,}", lower))
        missing_constraints = [constraint for constraint in analysis["preservation_constraints"] if not any(word in lower for word in re.findall(r"[a-z][a-z0-9_-]{3,}", constraint.lower()))]
        relevant = [area for area in analysis["areas"] if area not in lower]
        status = "WARNING" if missing_constraints or (relevant and len(relevant) > 1) else "ALIGNED"
        return {"status": status, "request": request, "task_analysis": analysis, "ai_plan": ai_plan, "missing_preservation_constraints": missing_constraints, "unmentioned_areas": relevant, "matched_terms": sorted(required & mentioned), "message": "Revise the plan before editing." if status == "WARNING" else "The AI plan covers the known task requirements."}

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

    def start_session(self, agent: str, request: str = "", session_id: str | None = None) -> dict:
        session_id = session_id or f"session-{uuid.uuid4().hex[:10]}"
        now = NOW()
        self.db.execute("INSERT OR IGNORE INTO agents(name,provider,created_at) VALUES(?,?,?)", (agent, agent, now))
        agent_id = self.db.execute("SELECT id FROM agents WHERE name=?", (agent,)).fetchone()[0]
        self.db.execute("INSERT INTO sessions(session_id,agent_id,working_directory,start_time,request) VALUES(?,?,?,?,?)", (session_id, agent_id, str(self.root), now, request))
        self.db.commit()
        return {"session_id": session_id, "agent": agent, "request": request, "status": "active", "start_time": now}

    def end_session(self, session_id: str, result: str = "completed") -> dict:
        now = NOW(); self.db.execute("UPDATE sessions SET end_time=?,result=?,status='completed' WHERE session_id=?", (now, result, session_id)); self.db.commit()
        row = self.db.execute("SELECT s.*,a.name AS agent FROM sessions s LEFT JOIN agents a ON a.id=s.agent_id WHERE s.session_id=?", (session_id,)).fetchone()
        return dict(row) if row else {"session_id": session_id, "status": "not_found"}

    def record_change(self, agent: str, session: str, request: str, summary: str, result: str = "unverified", files: list[str] | None = None, symbols: list[str] | None = None, added: int = 0, modified: int = 0, deleted: int = 0, risk: str | None = None) -> int:
        files, symbols = files or [], symbols or []
        # `risk` is derived from the recorded request, never invented: with no
        # request there is no evidence, so it stays UNKNOWN.
        risk = (risk or (self.analyze_prompt(request)["risk"] if request else "UNKNOWN")).upper()
        cur = self.db.execute("INSERT INTO changes(timestamp,agent,session_id,user_request,summary,risk,result,git_commit,files_added,files_modified,files_deleted,symbols_modified) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (NOW(), agent, session, request, summary, risk, result, head(self.root), added, modified, deleted, len(symbols)))
        change_id = cur.lastrowid
        for path in files:
            file_row = self.db.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
            self.db.execute("INSERT OR IGNORE INTO change_files(change_id,file_id,path,status) VALUES(?,?,?,?)", (change_id, file_row[0] if file_row else None, path, "changed"))
        for name in symbols:
            symbol_row = self.db.execute("SELECT id FROM symbols WHERE name=? ORDER BY status='active' DESC LIMIT 1", (name,)).fetchone()
            self.db.execute("INSERT OR IGNORE INTO change_symbols(change_id,symbol_id,name,status) VALUES(?,?,?,?)", (change_id, symbol_row[0] if symbol_row else None, name, "changed"))
        self.db.commit(); return change_id

    def verify(self, subject_type: str, subject_id: str, kind: str, result: str, evidence: str = "") -> dict:
        now = NOW()
        self.db.execute("INSERT INTO verifications(subject_type,subject_id,kind,result,evidence,recorded_at) VALUES(?,?,?,?,?,?)", (subject_type, subject_id, kind, result.upper(), evidence, now))
        if subject_type == "symbol":
            self.db.execute("UPDATE symbols SET last_verified=? WHERE name=? AND status='active'", (result.upper(), subject_id))
        elif subject_type == "feature":
            self.db.execute("UPDATE features SET last_verified=?,status=? WHERE name=?", (now, result.upper() if result.upper() in {"WORKING", "BROKEN", "PARTIAL"} else "UNVERIFIED", subject_id))
        self.db.commit(); record = {"subject_type": subject_type, "subject_id": subject_id, "kind": kind, "result": result.upper(), "evidence": evidence, "recorded_at": now}; record["regressions"] = self.regressions(subject_type, subject_id); return record

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

    def agent_config(self, agent: str) -> dict:
        executable = {"codex": "codex", "claude-code": "claude", "gemini": "gemini", "aider": "aider", "cursor": "cursor"}.get(agent, agent)
        command = f"{executable} mcp add codeledger -- codeledger mcp --root \"{self.root}\""
        return {"agent": agent, "command": command, "server": "codeledger", "transport": "stdio", "root": str(self.root), "note": "Agent CLI syntax may vary by version; use this command as the setup template and verify its output."}

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

    def export(self) -> list[str]:
        target = self.root / ".ai" / "codeledger" / "exports"; target.mkdir(parents=True, exist_ok=True)
        data = self.status(); project = target / "project.md"; project.write_text("# CodeLedger Project Memory\n\n" + "\n".join(f"- {key}: {value}" for key, value in data.items()) + "\n", encoding="utf-8")
        changes = self.db.execute("SELECT id,timestamp,agent,summary FROM changes ORDER BY id DESC LIMIT 50").fetchall(); recent = target / "recent-changes.md"
        recent.write_text("# Recent Changes\n\n" + "\n".join(f"- #{row['id']} {row['timestamp']} — {row['agent'] or 'UNKNOWN'} — {row['summary'] or 'NOT RECORDED'}" for row in changes) + "\n", encoding="utf-8")
        return [str(project), str(recent)]
