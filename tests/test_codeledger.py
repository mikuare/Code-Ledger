import json
import sqlite3
import tempfile
import unittest
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from codeledger.cli import build_parser
from codeledger.core import Ledger, process_alive
from codeledger.db import SCHEMA
from codeledger.providers import capabilities, provider_for

def dead_pid() -> int:
    """A PID that is certainly not running, for simulating a killed process."""
    for candidate in range(999_000, 1_000_000):
        if process_alive(candidate) is False:
            return candidate
    raise unittest.SkipTest("no free PID available to simulate a dead process")



class SessionLifecycleTests(unittest.TestCase):
    """A session that dies without cleaning up must not stay active forever."""

    def test_a_session_whose_process_is_gone_is_reconciled_as_crashed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"a.py").write_text("def alpha():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            session=ledger.start_session("codex", "work that never finished")
            # Simulate a process killed outright: the row still says active, but
            # the PID it recorded belongs to nothing. `kill -9`, closing WSL and
            # power loss all leave exactly this state behind.
            ledger.db.execute("UPDATE sessions SET pid=? WHERE session_id=?", (dead_pid(), session["session_id"]))
            ledger.db.commit()
            self.assertEqual([row["status"] for row in ledger._session_rows()], ["active"])

            report=ledger.reconcile_sessions()
            self.assertEqual(report["transitions"][0]["to"], "crashed")
            self.assertEqual(ledger.active_agents(), [])
            self.assertIn("no longer running", report["transitions"][0]["reason"])

    def test_a_silent_session_ages_through_idle_into_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"a.py").write_text("def alpha():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            session=ledger.start_session("codex", "thinking")
            # No PID evidence — the session was recorded on another machine, so
            # liveness rests on the heartbeat alone.
            ledger.db.execute("UPDATE sessions SET pid=NULL, host='some-other-host' WHERE session_id=?", (session["session_id"],))
            ledger.db.commit()
            now=datetime.now(timezone.utc)

            # An agent thinking for ten minutes is still working, not dead.
            ledger.reconcile_sessions(now=now + timedelta(seconds=600))
            self.assertEqual(ledger.active_agents(reconcile=False), ["codex"])

            ledger.reconcile_sessions(now=now + timedelta(seconds=1000))
            self.assertEqual([row["status"] for row in ledger._session_rows()], ["idle"])
            self.assertEqual(ledger.active_agents(reconcile=False), ["codex"], "idle agents are still live")

            ledger.reconcile_sessions(now=now + timedelta(seconds=4000))
            self.assertEqual([row["status"] for row in ledger._session_rows()], ["stale"])
            self.assertEqual(ledger.active_agents(reconcile=False), [])

    def test_a_live_pid_with_an_ancient_heartbeat_is_still_stale(self):
        """PIDs are recycled, so a live PID alone must not keep a session alive."""
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"a.py").write_text("def alpha():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            ledger.start_session("codex", "work")   # records this test's own live PID
            ledger.reconcile_sessions(now=datetime.now(timezone.utc) + timedelta(seconds=99999))
            self.assertEqual([row["status"] for row in ledger._session_rows()], ["stale"])

    def test_ending_a_session_never_overwrites_how_it_really_finished(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"a.py").write_text("def alpha():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            session=ledger.start_session("codex", "work")
            ledger.db.execute("UPDATE sessions SET pid=? WHERE session_id=?", (dead_pid(), session["session_id"]))
            ledger.db.commit(); ledger.reconcile_sessions()
            # A late cleanup must not relabel a crash as a tidy exit.
            ledger.end_session(session["session_id"], "watch stopped")
            self.assertEqual(ledger._session_rows()[0]["status"], "crashed")


class ConcurrencyAndRecoveryTests(unittest.TestCase):
    """Two agents and a watcher share one database; nothing may be lost."""

    def test_status_survives_a_shutdown_that_killed_every_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"a.py").write_text("def alpha():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            ledger.start_session("codex", "work"); ledger.start_session("claude-code", "work")
            ledger.db.execute("UPDATE sessions SET pid=? WHERE status='active'", (dead_pid(),)); ledger.db.commit()

            restarted=Ledger(root)          # a fresh process after the shutdown
            self.assertEqual(restarted.status()["active_agents"], [])
            self.assertEqual(restarted.status()["sessions"], {"crashed": 2})
            # History is kept, never deleted.
            self.assertEqual(restarted.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 2)

    def test_doctor_reports_stale_sessions_and_what_to_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"a.py").write_text("def alpha():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            ledger.start_session("codex", "work")
            ledger.db.execute("UPDATE sessions SET pid=? WHERE status='active'", (dead_pid(),)); ledger.db.commit()
            report=ledger.doctor()
            self.assertEqual(report["checks"]["wal"], "OK")
            self.assertEqual(report["checks"]["foreign_keys"], "OK")
            self.assertEqual(report["checks"]["storage_ignored"], "OK")
            self.assertIn("1 stale/crashed", report["checks"]["sessions"])
            self.assertIn("codeledger session reconcile", report["recommended_actions"])


class MigrationTests(unittest.TestCase):
    def test_a_database_predating_session_tracking_upgrades_and_keeps_its_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"a.py").write_text("def alpha():\n    return 1\n")
            path=root/".ai"/"codeledger"/"codeledger.db"; path.parent.mkdir(parents=True)
            legacy=sqlite3.connect(path)
            legacy.executescript(SCHEMA)          # the shape before pid/attribution columns existed
            legacy.execute("INSERT INTO agents(name,provider,created_at) VALUES('codex','openai','2020-01-01T00:00:00+00:00')")
            legacy.execute("INSERT INTO sessions(session_id,agent_id,working_directory,start_time,request,status) "
                           "VALUES('session-legacy',1,?,'2020-01-01T00:00:00+00:00','old work','active')", (str(root),))
            legacy.execute("INSERT INTO changes(timestamp,agent,session_id,user_request,summary,result) "
                           "VALUES('2020-01-01T00:00:00+00:00','codex','session-legacy','old work','Indexed 1 file','unverified')")
            legacy.commit(); legacy.close()
            self.assertNotIn("pid", {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(sessions)")})

            ledger=Ledger(root)                    # connecting applies the migrations
            columns={row["name"] for row in ledger.db.execute("PRAGMA table_info(sessions)")}
            self.assertLessEqual({"pid", "host", "last_activity_at", "last_heartbeat_at", "status_reason"}, columns)
            # History is never destroyed by an upgrade.
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM changes").fetchone()[0], 1)
            self.assertEqual(ledger.db.execute("SELECT user_request FROM changes").fetchone()[0], "old work")

            # A legacy session has no PID, so it is judged on age alone — and a
            # session from 2020 is certainly not still running.
            ledger.reconcile_sessions()
            self.assertEqual(ledger._session_rows()[0]["status"], "stale")
            self.assertEqual(ledger.active_agents(), [])

    def test_a_config_with_unknown_keys_falls_back_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"a.py").write_text("def alpha():\n    return 1\n")
            path=root/".ai"/"codeledger"/"config.json"; path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"project_name": "p", "root": str(root), "ignores": [],
                                        "from_a_newer_version": {"unknown": True}}))
            self.assertEqual(Ledger(root).config.project_name, "p")


class CodeLedgerTests(unittest.TestCase):
    def test_incremental_symbols_and_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"service.py"; source.write_text("def authenticateUser():\n    return True\n", encoding="utf-8")
            ledger=Ledger(root); ledger.init(); self.assertEqual(ledger.lookup("authenticateUser")[0]["status"], "active")
            refresh = ledger.refresh(); self.assertEqual(refresh["files_added"], 0); self.assertEqual(refresh["symbols_changed"], 0); self.assertIsNone(refresh["change_id"])
            source.write_text("def login():\n    return True\n", encoding="utf-8"); ledger.refresh()
            deleted=ledger.lookup("authenticateUser"); self.assertTrue(deleted and deleted[0]["status"] == "deleted")

    def test_session_and_automatic_change_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"main.py"; source.write_text("def run():\n pass\n", encoding="utf-8")
            ledger=Ledger(root); ledger.init(); session=ledger.start_session("codex", "update run")
            source.write_text("def run():\n return 1\n", encoding="utf-8")
            result=ledger.refresh(agent="codex", session=session["session_id"], request="update run")
            self.assertIsNotNone(result["change_id"]); self.assertEqual(ledger.history("update run")[0]["agent"], "codex")
            closed=ledger.end_session(session["session_id"])
            self.assertEqual(closed["status"], "ended"); self.assertEqual(closed["result"], "completed")
            # Closing runs from a signal handler and from the exit path, so it
            # must survive being called twice without rewriting the outcome.
            self.assertEqual(ledger.end_session(session["session_id"], "second call")["result"], "completed")

    def test_context_is_structured(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"main.py").write_text("def run():\n pass\n", encoding="utf-8")
            ledger=Ledger(root); ledger.init(); context=ledger.context("run")
            self.assertFalse(context["scan_required"]); self.assertEqual(context["files"], ["main.py"])

    def test_scandir_discovery_prunes_ignored_dirs_and_filters_files(self):
        with tempfile.TemporaryDirectory(prefix="HD anti gravity ") as directory:
            root=Path(directory); (root/"src").mkdir(); (root/"node_modules"/"nested").mkdir(parents=True); (root/".next"/"nested").mkdir(parents=True)
            (root/"src"/"main.py").write_text("def run():\n pass\n")
            (root/"node_modules"/"nested"/"ignored.py").write_text("def ignored():\n pass\n")
            (root/".next"/"nested"/"ignored.py").write_text("def ignored2():\n pass\n")
            (root/"image.png").write_bytes(b"not source")
            ledger=Ledger(root); result=ledger.init()
            self.assertEqual(result["metrics"]["files_discovered"], 1)
            self.assertGreaterEqual(result["metrics"]["directories_skipped"], 2)
            self.assertGreaterEqual(result["metrics"]["files_skipped_type"], 1)
            self.assertEqual(ledger.lookup("ignored").__len__(), 0)

    def test_large_file_and_quick_init(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"large.py").write_bytes(b"x" * 2_000_001); (root/"main.py").write_text("def run():\n pass\n")
            ledger=Ledger(root); result=ledger.init(quick=True)
            self.assertEqual(result["quick"], True); self.assertEqual(result["metrics"]["files_skipped_large"], 1)
            self.assertEqual(ledger.lookup("run"), [])
            refreshed=ledger.refresh(changed_only=True)
            self.assertEqual(ledger.lookup("run")[0]["status"], "active")
            self.assertEqual(refreshed["files_modified"], 1)

    def test_symlink_and_broken_symlink_are_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); target=root/"target.py"; target.write_text("def target():\n pass\n")
            try:
                (root/"link.py").symlink_to(target); (root/"broken.py").symlink_to(root/"missing.py")
            except OSError:
                self.skipTest("symlinks unavailable")
            ledger=Ledger(root); result=ledger.init()
            self.assertEqual(ledger.lookup("target").__len__(), 1); self.assertGreaterEqual(result["metrics"]["broken_symlinks"], 1)

    def test_scope_guard_safe_warning_and_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"src").mkdir(); (root/"src"/"auth.py").write_text("def authenticate():\n pass\n")
            ledger=Ledger(root); ledger.init()
            safe=ledger.scope_check("authenticate", ["src/auth.py"], ["authenticate"])
            warning=ledger.scope_check("authenticate", ["README.md"], ["unrelated"])
            unknown=ledger.scope_check("something not indexed", ["src/auth.py"], ["authenticate"])
            self.assertEqual(safe["status"], "SAFE"); self.assertEqual(warning["status"], "WARNING"); self.assertEqual(unknown["status"], "UNKNOWN")

    def test_plan_verification_and_regression(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"auth.py").write_text("def authenticate():\n pass\n")
            ledger=Ledger(root); ledger.init(); plan=ledger.plan("authenticate")
            self.assertEqual(plan["risk"], "HIGH"); self.assertIn("auth.py", plan["existing_files"])
            ledger.verify("project", "project", "TYPECHECK", "PASSED", "initial pass")
            result=ledger.verify("project", "project", "TYPECHECK", "FAILED", "later failure")
            self.assertEqual(result["regressions"][0]["status"], "REGRESSION")

    def test_prompt_analysis_extracts_constraints_risk_and_questions(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=Ledger(Path(directory)); analysis=ledger.analyze_prompt("Add a secure admin login migration, preserve existing permissions, and add tests")
            self.assertEqual(analysis["intent"], "feature"); self.assertIn("administration", analysis["areas"]); self.assertEqual(analysis["risk"], "HIGH")
            self.assertTrue(analysis["constraints"]); self.assertTrue(analysis["acceptance_criteria"])

    def test_handshake_and_feature_and_test_suggestions(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"auth.py").write_text("def authenticate():\n pass\n"); (root/"test_auth.py").write_text("def test_authenticate():\n pass\n")
            ledger=Ledger(root); ledger.init(); ledger.upsert_feature("Authentication", "User login", "WORKING")
            aligned=ledger.handshake("Fix authentication and preserve permissions", "Update authentication in auth.py, preserve permissions, and run test_auth.py")
            warning=ledger.handshake("Fix authentication and preserve permissions", "Update auth.py")
            self.assertEqual(aligned["status"], "ALIGNED"); self.assertEqual(warning["status"], "WARNING"); self.assertIn("test_auth.py", ledger.suggest_tests(["auth.py"], ["authenticate"]))

    def test_inferred_features_and_agent_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"src"/"auth").mkdir(parents=True); (root/"src"/"auth"/"service.ts").write_text("export const authenticate = async () => true;\ninterface Session { id: string }\n")
            ledger=Ledger(root); ledger.init(); names={item["name"] for item in ledger.infer_features()}
            self.assertIn("Authentication", names); self.assertEqual(ledger.lookup("authenticate")[0]["kind"], "function"); self.assertIn("codeledger mcp", ledger.agent_config("codex")["command"])

    def test_edit_in_undecodable_bytes_is_still_detected(self):
        """Two files differing only in undecodable bytes must not share a hash."""
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"data.py"
            source.write_bytes(b"# \xff\ndef run():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            os.utime(source, (0, 0))  # force the mtime short-circuit to miss
            source.write_bytes(b"# \xfe\ndef run():\n    return 1\n")
            result=ledger.refresh(changed_only=True)
            self.assertEqual(result["files_modified"], 1); self.assertIn("data.py", result["files"])

    def test_change_records_risk_from_the_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"auth.py"; source.write_text("def authenticate():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            source.write_text("def authenticate():\n    return 2\n")
            ledger.refresh(agent="codex", request="Fix the authentication timeout")
            self.assertEqual(ledger.db.execute("SELECT risk FROM changes ORDER BY id DESC").fetchone()["risk"], "HIGH")
            # No request means no evidence, so risk must not be invented.
            self.assertEqual(ledger.record_change("codex", "", "", "manual entry") and ledger.db.execute("SELECT risk FROM changes ORDER BY id DESC").fetchone()["risk"], "UNKNOWN")

    def test_impact_resolves_dependents_from_the_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"helpers.py").write_text("def helper():\n    return 1\n")
            (root/"app.py").write_text("from helpers import helper\n\ndef run():\n    return helper()\n")
            ledger=Ledger(root); ledger.init(); result=ledger.impact("helper")
            self.assertEqual(result["source"], "index")
            self.assertIn("app.py", result["referencing_files"]); self.assertIn("helpers.py", result["defining_files"])
            missing=ledger.impact("nothingMatchesThis")
            self.assertEqual(missing["risk"], "UNKNOWN"); self.assertEqual(missing["referencing_files"], [])

    def test_module_level_imports_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"app.py").write_text("import helpers\n\ndef run():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            row=ledger.db.execute("SELECT * FROM dependencies WHERE target_name='helpers' AND kind='imports'").fetchone()
            self.assertIsNotNone(row); self.assertIsNotNone(row["source_file_id"])

    def test_impact_finds_a_react_component_that_uses_a_hook(self):
        """ES named imports must be visible: `import { x } from` binds no AST."""
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"src"/"hooks").mkdir(parents=True); (root/"src"/"components").mkdir(parents=True)
            (root/"src"/"hooks"/"useAuth.ts").write_text("export const useAuth = () => {\n  return { user: null };\n};\n")
            (root/"src"/"components"/"UserList.tsx").write_text(
                "import React from 'react';\nimport { useAuth } from '../hooks/useAuth';\nimport './UserList.css';\n\n"
                "export const UserList = () => {\n  const { user } = useAuth();\n  return <div>{user}</div>;\n};\n")
            ledger=Ledger(root); ledger.init(); result=ledger.impact("useAuth")
            self.assertEqual(result["referencing_files"], ["src/components/UserList.tsx"])
            # A real parse tree records this as `calls`; the regex provider can
            # only tell that the name is used. Either is a dependency edge.
            kinds={row["kind"] for row in result["dependencies"] if row["target_name"] == "useAuth"}
            self.assertTrue(kinds & {"calls", "uses"}, f"no dependency edge recorded, only {kinds}")
            # Without grammars a .tsx file is shallow, and the honest answer is
            # to verify against the working tree rather than trust the index.
            expected = "index" if capabilities()["tree_sitter_installed"] else "index + fallback scan"
            self.assertEqual(result["source"], expected)

    def test_impact_falls_back_instead_of_claiming_no_dependents(self):
        """An empty index must never be reported as an absence of impact."""
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"helpers.py").write_text("def helper():\n    return 1\n")
            (root/"report.sql").write_text("-- calls helper() nightly\nSELECT 1;\n")
            ledger=Ledger(root); ledger.init()
            result=ledger.impact("helper")
            self.assertEqual(result["source"], "index + fallback scan")
            self.assertIn("report.sql", result["referencing_files"])
            strict=ledger.impact("helper", fallback=False)
            self.assertEqual(strict["source"], "index"); self.assertEqual(strict["referencing_files"], [])

    TSX_SOURCE = ("import { useAuth } from '../hooks/useAuth';\n\n"
                  "export const formatName = (u) => {\n  return u.name.trim();\n};\n\n"
                  "export const UserList = () => {\n  const { user } = useAuth();\n"
                  "  const label = formatName(user);\n  return <div>{label}</div>;\n};\n")

    def test_symbol_level_call_edges_inside_one_file(self):
        """JS/TS coverage must reach beyond imports to calls within a file."""
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"src").mkdir(); (root/"src"/"users.tsx").write_text(self.TSX_SOURCE)
            ledger=Ledger(root); ledger.init()
            edges={(row["src"], row["target_name"]) for row in ledger.db.execute(
                "SELECT s.name AS src, d.target_name FROM dependencies d JOIN symbols s ON s.id=d.source_symbol_id WHERE d.kind='calls'")}
            self.assertIn(("UserList", "formatName"), edges)
            self.assertIn(("UserList", "useAuth"), edges)

    def test_attribution_credits_only_the_symbol_that_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"src").mkdir(); source=root/"src"/"users.tsx"
            source.write_text(self.TSX_SOURCE)
            ledger=Ledger(root); ledger.init()
            source.write_text(self.TSX_SOURCE.replace("u.name.trim()", "u.name.trim().toUpperCase()"))
            result=ledger.refresh(changed_only=True, agent="claude-code", session="sess-42", request="Uppercase the name")
            attribution={row["name"]: (row["last_modified_by"], row["last_modified_session"]) for row in ledger.db.execute("SELECT name,last_modified_by,last_modified_session FROM symbols")}
            self.assertEqual(attribution["formatName"], ("claude-code", "sess-42"))
            self.assertEqual(attribution["UserList"], ("unknown", ""))   # untouched: credit must not move
            self.assertEqual(result["symbols"], ["formatName"])          # and it is not reported as changed

    def test_why_links_a_symbol_to_the_request_that_changed_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"auth.py"; source.write_text("def authenticate():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            source.write_text("def authenticate():\n    return 2\n")
            ledger.refresh(changed_only=True, agent="codex", session="s-1", request="Fix the login timeout")
            answer=ledger.why("authenticate")
            self.assertIn("Fix the login timeout", answer["answer"])
            self.assertEqual(answer["attribution"][0]["last_modified_by"], "codex")
            self.assertEqual(answer["attribution"][0]["last_modified_session"], "s-1")
            self.assertEqual(answer["recorded_changes"][0]["agent"], "codex")

    def test_refresh_reports_whether_an_edit_changed_any_symbol(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"billing.py"
            source.write_text("def total(items):\n    return sum(items)\n")
            ledger=Ledger(root); ledger.init()
            self.assertEqual(ledger.refresh(changed_only=True)["effect"], "none")
            source.write_text("# a comment only\ndef total(items):\n    return sum(items)\n")
            self.assertEqual(ledger.refresh(changed_only=True)["effect"], "text-only")
            source.write_text("# a comment only\ndef total(items):\n    return round(sum(items), 2)\n")
            self.assertEqual(ledger.refresh(changed_only=True)["effect"], "symbols-changed")

    def test_progress_detects_ineffective_and_repeating_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"billing.py"
            source.write_text("def total(items):\n    return sum(items)\n")
            ledger=Ledger(root); ledger.init()
            request="Fix the total calculation rounding"
            self.assertEqual(ledger.progress(request)["status"], "NO_PRIOR_ATTEMPTS")

            for comment in ("# try one", "# try two"):                      # edits that touch no symbol
                source.write_text(f"{comment}\ndef total(items):\n    return sum(items)\n")
                ledger.refresh(changed_only=True, agent="codex", request=request)
            no_effect=ledger.progress(request)
            self.assertEqual(no_effect["status"], "NO_EFFECT"); self.assertEqual(no_effect["ineffective_attempts"], 2)

            for value in ("round(sum(items))", "round(sum(items), 2)", "round(float(sum(items)), 2)"):
                source.write_text(f"def total(items):\n    return {value}\n")
                ledger.refresh(changed_only=True, agent="codex", request=request)
                ledger.verify("project", "project", "TEST", "FAILED", "still failing")
            repeating=ledger.progress(request)
            self.assertEqual(repeating["status"], "REPEATING")
            self.assertIn("total", repeating["repeated_symbols"])

            source.write_text("def total(items):\n    return round(sum(float(i) for i in items), 2)\n")
            ledger.refresh(changed_only=True, agent="codex", request=request)
            ledger.verify("project", "project", "TEST", "PASSED", "suite green")
            self.assertEqual(ledger.progress(request)["status"], "VERIFIED")

    def test_progress_does_not_confuse_tasks_sharing_one_word(self):
        """A shared keyword is not a repeated attempt."""
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"auth.py"
            source.write_text("def login(u):\n    return u\n")
            ledger=Ledger(root); ledger.init()
            for index, other in enumerate(["Change the login button colour to blue",
                                           "Add login analytics events"], 1):
                source.write_text(f"def login(u):\n    return u  # {index}\n")
                ledger.refresh(changed_only=True, agent="codex", request=other)
                ledger.verify("project", "project", "TEST", "FAILED", "x")
            result=ledger.progress("Fix the login timeout bug")
            self.assertEqual(result["attempt_count"], 0, f"unrelated tasks matched: {result['attempts']}")
            self.assertEqual(result["status"], "NO_PRIOR_ATTEMPTS")
            # the same request reworded slightly must still match
            source.write_text("def login(u):\n    return u  # real\n")
            ledger.refresh(changed_only=True, agent="codex", request="Fix the login timeout bug")
            self.assertEqual(ledger.progress("Fix the login timeout bug urgently")["attempt_count"], 1)

    def test_progress_ignores_an_unrelated_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"billing.py"
            source.write_text("def total(items):\n    return sum(items)\n")
            ledger=Ledger(root); ledger.init()
            source.write_text("def total(items):\n    return sum(items) + 1\n")
            ledger.refresh(changed_only=True, agent="codex", request="Fix the total calculation rounding")
            self.assertEqual(ledger.progress("Add dark mode to the settings page")["status"], "NO_PRIOR_ATTEMPTS")

    def test_an_unknown_agent_is_recorded_as_a_generic_provider(self):
        """Documented behaviour that the adapter seam never actually applied."""
        with tempfile.TemporaryDirectory() as directory:
            ledger=Ledger(Path(directory))
            ledger.start_session("codex"); ledger.start_session("SomeRandomBot")
            providers={row["name"]: row["provider"] for row in ledger.db.execute("SELECT name,provider FROM agents")}
            self.assertEqual(providers["codex"], "codex")
            self.assertEqual(providers["somerandombot"], "generic")   # normalised, not invented

    def test_a_deleted_file_is_reported_once_not_every_refresh(self):
        """`watch` polls continuously; a deletion must not re-fire each poll."""
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); temp=root/"scratch.py"
            (root/"keep.py").write_text("def keep():\n    return 1\n")
            temp.write_text("def temporary():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            temp.unlink()
            first=ledger.refresh(changed_only=True)
            self.assertIn("scratch.py", first["files"]); self.assertEqual(first["files_deleted"], 1)
            for poll in range(3):
                again=ledger.refresh(changed_only=True)
                self.assertEqual(again["files"], [], f"poll {poll} re-reported the deletion")
                self.assertIsNone(again["change_id"], f"poll {poll} recorded a phantom change")
            self.assertEqual(ledger.lookup("temporary")[0]["status"], "deleted")   # still queryable history

    def test_a_sentence_request_finds_its_symbols(self):
        """Real requests are sentences, not bare symbol names.

        A substring match on the whole phrase never hits, which left context,
        plan and scope blind for every realistic request.
        """
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"src"/"auth").mkdir(parents=True); (root/"src"/"payments").mkdir()
            (root/"src"/"auth"/"session.py").write_text("def login(token):\n    return bool(token)\n")
            (root/"src"/"payments"/"charge.py").write_text("def charge(amount):\n    return amount\n")
            ledger=Ledger(root); ledger.init()
            self.assertEqual(ledger.lookup("Fix the login timeout"), [])       # the old behaviour
            context=ledger.context("Fix the login timeout")
            self.assertEqual(context["files"], ["src/auth/session.py"])
            self.assertFalse(context["scan_required"])
            self.assertEqual(ledger.scope_check("Fix the login timeout", ["src/auth/session.py"], [])["status"], "SAFE")
            unrelated=ledger.scope_check("Fix the login timeout", ["src/payments/charge.py"], [])
            self.assertEqual(unrelated["status"], "WARNING")
            self.assertIn("src/payments/charge.py", unrelated["unexpected_files"])

    def test_plan_suggests_tests_covering_the_affected_symbol(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"src").mkdir(); (root/"tests").mkdir()
            (root/"src"/"auth.py").write_text("def authenticateUser(t):\n    return bool(t)\n")
            (root/"tests"/"test_auth.py").write_text("def test_authenticateUser():\n    assert True\n")
            ledger=Ledger(root); ledger.init()
            self.assertIn("tests/test_auth.py", ledger.plan("Fix authenticateUser token expiry")["suggested_tests"])

    def test_scope_boundary_falls_back_to_path_keywords(self):
        """A request naming no indexed symbol should still get a boundary."""
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"src"/"admin").mkdir(parents=True); (root/"src"/"billing").mkdir(parents=True)
            (root/"src"/"admin"/"users.tsx").write_text("export const UserList = () => null;\n")
            (root/"src"/"billing"/"charge.ts").write_text("export const charge = () => null;\n")
            ledger=Ledger(root); ledger.init()
            inside=ledger.scope_check("Add activity tracking to the admin area", ["src/admin/users.tsx"], [])
            self.assertEqual(inside["status"], "SAFE")
            self.assertIn("request keywords matched against file paths (weak evidence)", inside["boundary_evidence"])
            outside=ledger.scope_check("Add activity tracking to the admin area", ["src/billing/charge.ts"], [])
            self.assertEqual(outside["status"], "WARNING")

    POLYGLOT = {
        "main.go":     'package main\nimport "fmt"\nfunc helper() int { return 1 }\nfunc Run() int { return helper() }\n',
        "lib.rs":      "use std::fmt;\nfn helper() -> i32 { 1 }\npub fn run() -> i32 { helper() }\n",
        "App.java":    "public class App {\n    int helper() { return 1; }\n    int run() { return helper(); }\n}\n",
        "Service.cs":  "public class Service {\n    int Helper() { return 1; }\n    int Run() { return Helper(); }\n}\n",
        "app.rb":      "def helper; 1; end\ndef run; helper; end\n",
        "index.php":   "<?php\nfunction helper() { return 1; }\nfunction run() { return helper(); }\n",
        "Main.kt":     "fun helper(): Int = 1\nfun run(): Int = helper()\n",
        "App.swift":   "func helper() -> Int { return 1 }\nfunc run() -> Int { return helper() }\n",
        "engine.cpp":  "int helper() { return 1; }\nint run() { return helper(); }\n",
    }

    def test_every_supported_language_yields_symbols_and_a_call_graph(self):
        if not capabilities()["tree_sitter_installed"]:
            self.skipTest("grammars not installed: pip install 'codeledger[languages]'")
        from codeledger.providers import analyze
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name, source in self.POLYGLOT.items():
                (root/name).write_text(source)
            for name in self.POLYGLOT:
                with self.subTest(language=name):
                    path=root/name
                    symbols, edges, provider, coverage = analyze(path, path.read_text())
                    defined={item.name for item in symbols}
                    self.assertTrue(defined, f"{name}: no symbols extracted")
                    self.assertEqual(coverage, "full")
                    internal=[(a, b) for a, b, _ in edges if b in defined and a != "__module__"]
                    self.assertTrue(internal, f"{name}: no call graph, only {edges[:5]}")

    def test_full_coverage_is_never_claimed_without_symbols(self):
        """A grammar that yields nothing must degrade, not claim full coverage."""
        from codeledger.providers import analyze
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"thing.go"
            path.write_text("func helper() int { return 1 }\n")
            _symbols, _edges, _provider, coverage = analyze(path, path.read_text())
            self.assertIn(coverage, ("full", "shallow"))
            if not capabilities()["tree_sitter_installed"]:
                self.assertEqual(coverage, "shallow")   # regex provider must be honest about its limits

    def test_lookup_treats_wildcards_as_literal_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"main.py").write_text("def run():\n    pass\n")
            ledger=Ledger(root); ledger.init()
            self.assertEqual(ledger.lookup("%"), []); self.assertEqual(ledger.lookup("_"), [])
            self.assertEqual(len(ledger.lookup("run", limit=1)), 1)

    def test_scope_guard_flags_an_unrelated_sibling_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"src").mkdir(); (root/"src"/"auth.py").write_text("def authenticate():\n pass\n")
            ledger=Ledger(root); ledger.init()
            sibling=ledger.scope_check("authenticate", ["src/billing/charge.py"], [])
            self.assertEqual(sibling["status"], "WARNING"); self.assertIn("src/billing/charge.py", sibling["unexpected_files"])
            same_dir=ledger.scope_check("authenticate", ["src/session.py"], [])
            self.assertEqual(same_dir["status"], "SAFE")

    def test_verify_command_records_pass_and_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=Ledger(Path(directory))
            passed=ledger.verify_command("project", "project", "TEST", [sys.executable, "-c", "pass"])
            failed=ledger.verify_command("project", "project", "TEST", [sys.executable, "-c", "raise SystemExit(1)"])
            self.assertEqual(passed["result"], "PASSED"); self.assertEqual(failed["result"], "FAILED")
            self.assertEqual(failed["regressions"][0]["status"], "REGRESSION")
            with self.assertRaises(ValueError): ledger.verify_command("project", "project", "TEST", [])

    def test_export_writes_derived_markdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"main.py").write_text("def run():\n pass\n")
            ledger=Ledger(root); ledger.init()
            written=[Path(path) for path in ledger.export()]
            self.assertTrue(all(path.exists() for path in written))
            self.assertIn("CodeLedger Project Memory", written[0].read_text(encoding="utf-8"))

    def test_cli_parser_accepts_every_subcommand(self):
        parser=build_parser()
        cases=[["init", "--quick"], ["status"], ["refresh", "--changed", "--agent", "codex"], ["lookup", "x"],
               ["impact", "x", "--scan"], ["context", "x"], ["history", "x"], ["why", "x"], ["restore-info", "x"],
               ["scope", "task", "--files", "a.py"], ["plan", "task"], ["prompt", "task"],
               ["handshake", "task", "--ai-plan", "p"], ["tests", "--symbols", "s"], ["changes"], ["issues"],
               ["decisions"], ["export"], ["doctor"], ["issue", "K", "T"], ["decision", "K", "T"], ["feature", "N"],
               ["features", "--infer"], ["verify", "project", "p", "TEST", "PASSED"],
               ["verify-run", "project", "p", "TEST", "--", "true"], ["regressions"], ["git-import"],
               ["session", "start"], ["session", "list"], ["session", "reconcile"], ["session", "status"],
               ["record", "summary"], ["mcp"], ["setup-agent", "codex"],
               ["agent-config", "codex"], ["setup-codex"], ["watch", "--max-interval", "5"],
               ["run", "--request", "task", "--", "echo"]]
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertEqual(parser.parse_args(argv).command, argv[0])
