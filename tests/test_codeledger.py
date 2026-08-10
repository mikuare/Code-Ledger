import json
import re
import sqlite3
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from codeledger.cli import build_parser
from codeledger.core import Ledger, process_alive
from codeledger import db as db_module
from codeledger.db import SCHEMA
from codeledger.parser import digest_bytes
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

    def test_a_phantom_session_does_not_poison_attribution_forever(self):
        """The end-to-end bug: one hard shutdown used to make every later edit `unknown`."""
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"a.py"
            source.write_text("def alpha():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            ghost=ledger.start_session("codex", "killed by closing WSL")
            ledger.db.execute("UPDATE sessions SET pid=? WHERE session_id=?", (dead_pid(), ghost["session_id"]))
            ledger.db.commit()

            ledger.start_session("claude-code", "real work")
            source.write_text("def alpha():\n    return 2\n")
            observed=ledger.refresh(changed_only=True, agent="claude-code", observed=True)
            # Observed edits are never credited to a name, but the dead codex
            # session must not still be presented as a live competing agent.
            self.assertNotIn("codex", observed["attribution"]["reason"])
            self.assertIn("claude-code", observed["attribution"]["reason"])
            self.assertEqual(ledger.status()["active_agents"], ["claude-code"])
            self.assertEqual(len(ledger.status()["stale_sessions"]), 1)

            # And an explicit refresh is unaffected by the phantom entirely.
            source.write_text("def alpha():\n    return 3\n")
            claimed=ledger.refresh(changed_only=True, agent="claude-code", request="real work")
            self.assertEqual((claimed["agent"], claimed["attribution"]["confidence"]), ("claude-code", "HIGH"))

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


class AttributionTests(unittest.TestCase):
    def test_explicit_refresh_outranks_anything_the_watcher_infers(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"a.py"
            source.write_text("def alpha():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            ledger.start_session("codex", "work")

            source.write_text("def alpha():\n    return 2\n")
            claimed=ledger.refresh(changed_only=True, agent="codex", request="own work")
            self.assertEqual(claimed["attribution"], {
                "source": "explicit-agent-refresh", "confidence": "HIGH",
                "reason": "codex recorded this refresh on its own behalf."})

            source.write_text("def alpha():\n    return 3\n")
            nameless=ledger.refresh(changed_only=True, agent="", observed=False)
            self.assertEqual(nameless["attribution"]["confidence"], "UNKNOWN")
            self.assertEqual(nameless["agent"], "unknown")

            stored=ledger.db.execute("SELECT attribution_confidence FROM changes ORDER BY id").fetchall()
            self.assertEqual([row[0] for row in stored], ["HIGH", "UNKNOWN"])

    def test_a_watcher_never_credits_the_name_it_was_launched_with(self):
        """The watcher's `--agent` flag is a label, not evidence of authorship.

        Crediting the sole live agent looked reasonable, but it is exactly the
        inference the filesystem cannot support: `watch --agent codex` records
        an edit a developer, an editor or a formatter may have made.
        """
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"a.py"
            source.write_text("def alpha():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            ledger.start_session("codex", "watch")      # the only live agent
            source.write_text("def alpha():\n    return 2\n")
            result=ledger.refresh(changed_only=True, agent="codex", observed=True)
            self.assertEqual(result["agent"], "unknown")
            self.assertEqual(result["attribution"]["confidence"], "LOW")
            self.assertEqual(result["attribution"]["source"], "filesystem-watcher")
            self.assertEqual(ledger.db.execute("SELECT agent FROM changes ORDER BY id DESC").fetchone()["agent"], "unknown")
            self.assertEqual(ledger.db.execute("SELECT last_modified_by FROM symbols WHERE name='alpha'").fetchone()[0], "unknown")

    def test_an_observed_edit_with_no_agent_session_is_not_credited_to_anyone(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"a.py"
            source.write_text("def alpha():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            source.write_text("def alpha():\n    return 2\n")   # a human edit, no session
            result=ledger.refresh(changed_only=True, agent="codex", observed=True)
            self.assertEqual(result["agent"], "unknown")
            self.assertEqual(result["attribution"]["confidence"], "LOW")
            self.assertIn("no agent session active", result["attribution"]["reason"])


class EffectClassificationTests(unittest.TestCase):
    """Only a change to what the code *does* counts as a symbol change."""

    def _effects(self, root, ledger, versions):
        seen = []
        for source in versions:
            (root/"a.py").write_text(source)
            result = ledger.refresh(changed_only=True, agent="claude-code", request="edit login")
            seen.append((result["effect"], result["symbols"]))
        return seen

    def test_comments_docstrings_and_formatting_are_text_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"a.py").write_text("def login(user):\n    return user is not None\n")
            ledger=Ledger(root); ledger.init()
            effects=self._effects(root, ledger, [
                "def login(user):\n    # improved comment\n    return user is not None\n",
                'def login(user):\n    """Explain the check."""\n    return user is not None\n',
                "def login(user):\n\n\n        return user is not None\n",
            ])
            self.assertEqual([effect for effect, _ in effects], ["text-only"] * 3, effects)
            self.assertEqual([symbols for _, symbols in effects], [[]] * 3)

    def test_effect_does_not_depend_on_whether_tree_sitter_is_installed(self):
        """The same edit must classify the same way under either provider.

        A docstring is a bare string statement, not a comment node, so the
        tree-sitter path saw documentation as code while the AST path did not —
        the same file classified differently depending on an optional install.
        """
        from codeledger.providers import provider_for, TreeSitterProvider
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"a.py"
            source.write_text("def login(user):\n    return user is not None\n")
            ledger=Ledger(root); ledger.init()
            if not isinstance(provider_for(source), TreeSitterProvider):
                self.skipTest("tree-sitter grammars are not installed")
            for edit in ('def login(user):\n    """Explain the check."""\n    return user is not None\n',
                         'def login(user):\n    """A different explanation."""\n    return user is not None\n'):
                source.write_text(edit)
                self.assertEqual(ledger.refresh(changed_only=True, agent="a", request="doc")["effect"], "text-only")

    def test_shallow_languages_report_effect_at_low_confidence(self):
        """Line patterns cannot separate a comment from code; that must be said."""
        from codeledger.providers import provider_for, TreeSitterProvider
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"a.go"
            source.write_text("func F(u int) int {\n\treturn u\n}\n")
            ledger=Ledger(root); ledger.init()
            source.write_text("func F(u int) int {\n\t// a comment\n\treturn u\n}\n")
            result=ledger.refresh(changed_only=True, agent="a", request="doc")
            if isinstance(provider_for(source), TreeSitterProvider):
                self.assertEqual(result["effect"], "text-only")
                self.assertEqual(result["effect_confidence"]["level"], "HIGH")
            else:
                # Without grammars the comment reads as a code change, so the
                # answer must be marked untrustworthy rather than asserted.
                self.assertEqual(result["effect"], "symbols-changed")
                self.assertEqual(result["effect_confidence"]["level"], "LOW")
                self.assertIn("a.go", result["effect_confidence"]["shallow_files"])
                self.assertIn("cannot separate comments", result["effect_confidence"]["reason"])

    def test_python_effect_is_reported_at_high_confidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"a.py"
            source.write_text("def f(u):\n    return u\n")
            ledger=Ledger(root); ledger.init()
            source.write_text("def f(u):\n    return u is not None\n")
            result=ledger.refresh(changed_only=True, agent="a", request="fix")
            self.assertEqual(result["effect"], "symbols-changed")
            self.assertEqual(result["effect_confidence"]["level"], "HIGH")

    def test_a_real_logic_change_is_still_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/"a.py").write_text("def login(user):\n    return user is not None\n")
            ledger=Ledger(root); ledger.init()
            [(effect, symbols)]=self._effects(root, ledger, ["def login(user):\n    return user is not None and user.active\n"])
            self.assertEqual((effect, symbols), ("symbols-changed", ["login"]))

    def test_rewriting_a_file_with_identical_bytes_has_no_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"a.py"
            source.write_text("def login(user):\n    return user is not None\n")
            ledger=Ledger(root); ledger.init()
            os.utime(source, (0, 0))   # touched, but the content is unchanged
            result=ledger.refresh(changed_only=True, agent="claude-code", request="edit login")
            self.assertEqual(result["effect"], "none"); self.assertIsNone(result["change_id"])

    def test_a_comment_edit_does_not_reassign_authorship_of_the_symbol(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"a.py"
            source.write_text("def login(user):\n    return user is not None\n")
            ledger=Ledger(root); ledger.init()
            source.write_text("def login(user):\n    return user is not None and user.active\n")
            ledger.refresh(changed_only=True, agent="codex", request="fix login")
            source.write_text("def login(user):\n    # a later note from another agent\n    return user is not None and user.active\n")
            ledger.refresh(changed_only=True, agent="claude-code", request="document login")
            owner=ledger.db.execute("SELECT last_modified_by FROM symbols WHERE name='login'").fetchone()[0]
            self.assertEqual(owner, "codex", "a comment must not transfer credit for the code")


class WatcherClaimWindowTests(unittest.TestCase):
    """The watcher is a safety net, not a competitor for the same change.

    Indexing an edit is destructive to attribution: once the file matches the
    index, the author's own refresh finds nothing left to record. A watcher that
    polled first therefore took the change, credited it to `unknown`, and — with
    no request text attached — left `progress` reporting NO_PRIOR_ATTEMPTS for
    work that had just happened, disabling repeat detection entirely.
    """

    def _edited(self, directory):
        root=Path(directory); source=root/"login.py"
        source.write_text("def login(u):\n    return u\n")
        ledger=Ledger(root); ledger.init()
        ledger.start_session("claude-code", "fix login timeout")
        source.write_text("def login(u):\n    return u is not None\n")
        return root, ledger

    def test_the_watcher_leaves_a_fresh_edit_for_its_author_to_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root, ledger = self._edited(directory)
            watched = ledger.refresh(changed_only=True, agent="codex", session="w",
                                     observed=True, settle_seconds=90)
            self.assertIsNone(watched["change_id"], "a fresh edit must not be recorded yet")
            self.assertEqual(watched["scan"]["files_awaiting_claim"], 1)

            claimed = ledger.refresh(changed_only=True, agent="claude-code", request="fix login timeout")
            self.assertEqual(claimed["agent"], "claude-code")
            self.assertEqual(claimed["attribution"]["confidence"], "HIGH")
            self.assertEqual(ledger.db.execute("SELECT last_modified_by FROM symbols WHERE name='login'").fetchone()[0],
                             "claude-code")
            # The part that actually matters: repeat detection still works.
            self.assertEqual(ledger.progress("fix login timeout")["status"], "PROGRESSING")

    def test_an_edit_nobody_claims_is_still_recorded_eventually(self):
        with tempfile.TemporaryDirectory() as directory:
            root, ledger = self._edited(directory)
            # Nothing reported it, and the window has passed.
            old = time.time() - 600
            os.utime(root/"login.py", (old, old))
            result = ledger.refresh(changed_only=True, agent="codex", session="w",
                                    observed=True, settle_seconds=90)
            self.assertIsNotNone(result["change_id"], "the safety net must still catch it")
            self.assertEqual(result["agent"], "unknown")
            self.assertEqual(result["attribution"]["confidence"], "LOW")
            self.assertEqual(result["scan"]["files_awaiting_claim"], 0)

    def test_an_agent_refresh_never_waits(self):
        """Only the watcher holds back. An agent reporting its own work must not."""
        with tempfile.TemporaryDirectory() as directory:
            root, ledger = self._edited(directory)
            result = ledger.refresh(changed_only=True, agent="claude-code", request="fix login timeout")
            self.assertIsNotNone(result["change_id"])
            self.assertEqual(result["scan"]["files_awaiting_claim"], 0)


class ConflictTests(unittest.TestCase):
    def test_the_same_symbol_outranks_merely_the_same_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"session.py"
            source.write_text("def login():\n    return 1\n\ndef logout():\n    return 2\n")
            ledger=Ledger(root); ledger.init()
            ledger.start_session("codex", "codex work"); ledger.start_session("claude-code", "claude work")
            source.write_text("def login():\n    return 10\n\ndef logout():\n    return 2\n")
            ledger.refresh(changed_only=True, agent="codex", request="rework login")

            same_symbol=ledger.conflicts("claude-code", ["session.py"], ["login"])
            self.assertEqual(same_symbol["status"], "HIGH")
            self.assertEqual(same_symbol["conflicts"][0]["shared_symbols"], ["login"])
            self.assertIn("POTENTIAL CONFLICT", same_symbol["message"])

            same_file_only=ledger.conflicts("claude-code", ["session.py"], ["logout"])
            self.assertEqual(same_file_only["status"], "MEDIUM")

            self.assertEqual(ledger.conflicts("claude-code", ["unrelated.py"], ["other"])["status"], "NONE")

    def test_a_dead_agent_cannot_raise_a_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"session.py"
            source.write_text("def login():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            ghost=ledger.start_session("codex", "work")
            source.write_text("def login():\n    return 10\n")
            ledger.refresh(changed_only=True, agent="codex", request="rework login")
            self.assertEqual(ledger.conflicts("claude-code", ["session.py"], ["login"])["status"], "HIGH")

            ledger.db.execute("UPDATE sessions SET pid=? WHERE session_id=?", (dead_pid(), ghost["session_id"]))
            ledger.db.commit()
            self.assertEqual(ledger.conflicts("claude-code", ["session.py"], ["login"])["status"], "NONE")


class ConcurrencyAndRecoveryTests(unittest.TestCase):
    """Two agents and a watcher share one database; nothing may be lost."""

    def test_concurrent_refreshes_from_separate_connections_lose_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for i in range(8): (root/f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")
            Ledger(root).init()
            # Every file must genuinely differ: `return 0` and `return 0 * 100`
            # are the same text, and a file that did not change proves nothing.
            for i in range(8): (root/f"m{i}.py").write_text(f"def f{i}():\n    return {i} + 1\n")

            errors, done = [], []
            def worker(name):
                try:
                    # A separate Ledger means a separate sqlite connection, which
                    # is what two agent processes actually have.
                    done.append(Ledger(root).refresh(changed_only=True, agent=name, request=f"{name} work"))
                except Exception as exc:                      # noqa: BLE001 - surfaced below
                    errors.append(f"{name}: {exc}")
            threads=[threading.Thread(target=worker, args=(f"agent-{i}",)) for i in range(4)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=60)

            self.assertEqual(errors, [], "concurrent writers must not collide")
            ledger=Ledger(root)
            self.assertEqual(ledger.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            # Whichever writer got there first recorded the edits; the rest saw a
            # clean tree. What must never happen is the edits vanishing.
            recorded={row["path"] for row in ledger.db.execute("SELECT path FROM change_files")}
            self.assertEqual(recorded, {f"m{i}.py" for i in range(8)})

    def test_concurrent_refreshes_do_not_each_claim_the_same_edit(self):
        """One edit must produce one change record, credited to one agent.

        Reads inside a refresh used a snapshot taken when its transaction began,
        so an agent could not see an edit another agent had already recorded and
        would index and record it again. Four agents refreshing together
        produced four HIGH-confidence records for the same edit — fabricated
        authorship for three of them, and inflated attempt counts that
        `progress` would later read as a repeat.
        """
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for i in range(8): (root/f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")
            Ledger(root).init()
            for i in range(8): (root/f"m{i}.py").write_text(f"def f{i}():\n    return {i} + 1\n")

            errors=[]
            def worker(name):
                try: Ledger(root).refresh(changed_only=True, agent=name, request=f"{name} work")
                except Exception as exc:                      # noqa: BLE001 - surfaced below
                    errors.append(f"{name}: {type(exc).__name__}: {exc}")
            threads=[threading.Thread(target=worker, args=(f"agent-{i}",)) for i in range(4)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=60)

            self.assertEqual(errors, [])
            ledger=Ledger(root)
            self.assertEqual(ledger.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            counts={row["path"]: row["c"] for row in ledger.db.execute(
                "SELECT path,count(*) AS c FROM change_files GROUP BY path")}
            self.assertEqual(set(counts), {f"m{i}.py" for i in range(8)}, "no edit may be lost")
            self.assertEqual([path for path, count in counts.items() if count > 1], [],
                             "no edit may be recorded more than once")

    def test_a_change_record_and_its_index_update_commit_together(self):
        """A crash between the two used to leave indexed files with no change row."""
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"a.py"
            source.write_text("def alpha():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            before=ledger.db.execute("SELECT hash FROM files WHERE path='a.py'").fetchone()[0]
            source.write_text("def alpha():\n    return 2\n")

            real=ledger.record_change
            def explode(*args, **kwargs):
                real(*args, **kwargs)          # write the change rows...
                raise RuntimeError("process killed mid-refresh")
            ledger.record_change=explode
            with self.assertRaises(RuntimeError): ledger.refresh(changed_only=True, agent="codex", request="fix alpha")

            # The interrupted transaction must roll back whole. A fresh
            # connection sees either all of it or none of it — never a file
            # marked current with no change row explaining it.
            fresh=Ledger(root)
            self.assertEqual(fresh.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(fresh.db.execute("SELECT count(*) FROM changes").fetchone()[0], 0)
            self.assertEqual(fresh.db.execute("SELECT hash FROM files WHERE path='a.py'").fetchone()[0], before,
                             "the index update must roll back with the change record")
            # And the edit is still pending, so the next refresh picks it up.
            self.assertEqual(fresh.refresh(changed_only=True, agent="codex", request="fix alpha")["effect"], "symbols-changed")

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


class StaleMemoryTests(unittest.TestCase):
    """The source always outranks the ledger's memory of it."""

    def test_context_reanalyses_a_file_that_changed_behind_its_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"service.py"
            source.write_text("def authenticateUser():\n    return True\n")
            ledger=Ledger(root); ledger.init()
            self.assertEqual(ledger.lookup("authenticateUser")[0]["status"], "active")

            # Edited outside CodeLedger — no refresh, exactly what happens when a
            # developer or another tool writes to the tree.
            source.write_text("def login():\n    return True\n")
            context=ledger.context("authenticateUser")
            self.assertTrue(context["efficiency"]["stale_records_reanalyzed"])
            self.assertEqual(ledger.lookup("authenticateUser")[0]["status"], "deleted",
                             "a symbol that no longer exists must not be reported as active")

    def test_context_retires_symbols_whose_file_was_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"service.py"
            source.write_text("def authenticateUser():\n    return True\n")
            ledger=Ledger(root); ledger.init()
            source.unlink()
            ledger.context("authenticateUser")
            self.assertEqual(ledger.lookup("authenticateUser")[0]["status"], "deleted")

    def test_context_reports_what_it_avoided_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for i in range(30): (root/f"m{i}.py").write_text(f"def helper{i}():\n    return {i}\n")
            (root/"auth.py").write_text("def authenticateUser():\n    return True\n")
            ledger=Ledger(root); ledger.init()
            efficiency=ledger.context("authenticateUser")["efficiency"]
            self.assertEqual(efficiency["files_relevant"], 1)
            self.assertEqual(efficiency["files_in_repository"], 31)
            self.assertEqual(efficiency["files_avoided"], 30)
            self.assertFalse(efficiency["full_scan_required"])

    def test_a_targeted_reanalysis_does_not_retire_files_it_never_looked_at(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"a.py").write_text("def alpha():\n    return 1\n")
            (root/"b.py").write_text("def beta():\n    return 2\n")
            ledger=Ledger(root); ledger.init()
            (root/"a.py").write_text("def alpha():\n    return 99\n")
            ledger.refresh(changed_only=True, only={"a.py"}, record=False)
            self.assertEqual(ledger.lookup("beta")[0]["status"], "active")


class IncrementalRefreshTests(unittest.TestCase):
    """A refresh must look at every file, but analyse only what changed."""

    def _project(self, directory, count=60, settled=True):
        root = Path(directory)
        for i in range(count):
            (root/"src").mkdir(exist_ok=True)
            (root/"src"/f"m{i}.py").write_text(f"def helper{i}():\n    return {i}\n")
        noise = root/"node_modules"/"pkg"; noise.mkdir(parents=True)
        for i in range(40): (noise/f"dep{i}.js").write_text("module.exports = 1;\n")
        if settled:
            # A real project's files were not written milliseconds ago. Age them
            # so the racily-clean guard is not (correctly) re-reading everything.
            old = time.time() - 3600
            for path in root.rglob("*"):
                if path.is_file(): os.utime(path, (old, old))
        return root

    def test_a_no_op_refresh_parses_and_hashes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=self._project(directory); ledger=Ledger(root); ledger.init()
            result=ledger.refresh(changed_only=True)
            self.assertEqual(result["effect"], "none")
            self.assertEqual(result["files"], [])
            # Proving nothing changed costs one stat per file and no reads.
            self.assertEqual(result["timing"]["hashing_seconds"], 0.0)
            self.assertEqual(result["timing"]["parsing_seconds"], 0.0)
            self.assertEqual(result["scan"]["files_checked"], 60)
            self.assertEqual(result["scan"]["files_analyzed"], 0)

    def test_ignored_directories_are_never_stat_ed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=self._project(directory); ledger=Ledger(root); ledger.init()
            scan=ledger.refresh(changed_only=True)["scan"]
            self.assertEqual(scan["files_checked"], 60, "node_modules must not be stat-ed")
            self.assertGreaterEqual(scan["directories_pruned"], 1)

    def test_one_changed_file_leaves_the_rest_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root=self._project(directory); ledger=Ledger(root); ledger.init()
            before={row["path"]: row["last_analyzed"] for row in ledger.db.execute("SELECT path,last_analyzed FROM files")}
            (root/"src"/"m7.py").write_text("def helper7():\n    return 700\n")
            result=ledger.refresh(changed_only=True, agent="codex", request="fix helper7")
            self.assertEqual(result["files"], ["src/m7.py"])
            self.assertEqual(result["scan"]["files_analyzed"], 1)
            after={row["path"]: row["last_analyzed"] for row in ledger.db.execute("SELECT path,last_analyzed FROM files")}
            moved=[path for path in before if before[path] != after[path]]
            self.assertEqual(moved, ["src/m7.py"], "only the edited file may be re-analysed")

    def test_a_new_file_is_indexed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=self._project(directory); ledger=Ledger(root); ledger.init()
            (root/"src"/"brand_new.py").write_text("def freshly_added():\n    return 1\n")
            result=ledger.refresh(changed_only=True, agent="codex", request="add it")
            self.assertEqual(result["files_added"], 1)
            self.assertEqual(ledger.lookup("freshly_added")[0]["status"], "active")

    def test_a_deleted_file_is_marked_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=self._project(directory); ledger=Ledger(root); ledger.init()
            (root/"src"/"m3.py").unlink()
            result=ledger.refresh(changed_only=True, agent="codex", request="remove it")
            self.assertIn("src/m3.py", result["files"])
            self.assertEqual(ledger.db.execute("SELECT status FROM files WHERE path='src/m3.py'").fetchone()[0], "deleted")
            # `lookup` is a substring match, so name helper3 exactly: helper30
            # through helper39 are still very much alive.
            self.assertEqual(ledger.db.execute("SELECT status FROM symbols WHERE name='helper3'").fetchone()[0], "deleted")

    def test_a_renamed_file_retires_the_old_path_and_indexes_the_new_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root=self._project(directory); ledger=Ledger(root); ledger.init()
            (root/"src"/"m5.py").rename(root/"src"/"renamed.py")
            ledger.refresh(changed_only=True, agent="codex", request="rename")
            self.assertEqual(ledger.db.execute("SELECT status FROM files WHERE path='src/m5.py'").fetchone()[0], "deleted")
            self.assertEqual(ledger.db.execute("SELECT status FROM files WHERE path='src/renamed.py'").fetchone()[0], "current")

    def test_a_reverted_file_is_detected_even_though_it_matches_the_old_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root=self._project(directory); source=root/"src"/"m2.py"
            original=source.read_text()
            ledger=Ledger(root); ledger.init()
            source.write_text("def helper2():\n    return 999\n")
            ledger.refresh(changed_only=True, agent="codex", request="change")
            source.write_text(original)                      # reverted
            result=ledger.refresh(changed_only=True, agent="codex", request="revert")
            self.assertEqual(result["files"], ["src/m2.py"])
            self.assertEqual(ledger.db.execute("SELECT hash FROM files WHERE path='src/m2.py'").fetchone()[0],
                             digest_bytes(original.encode()))

    def test_a_same_size_rewrite_within_one_timestamp_tick_is_still_detected(self):
        """The racily-clean case: identical size, identical mtime, different bytes.

        Filesystem timestamps are granular — a few milliseconds here — so two
        writes can share an mtime. If the second one also happens to preserve the
        file's size (changing a digit, a comparison, a boolean), then size and
        mtime both match what was indexed and the edit is invisible. It stays
        invisible until something else touches the file, so the ledger reports
        "nothing changed" about code that did.
        """
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"a.py"
            source.write_text("def alpha():\n    return 10\n")
            ledger=Ledger(root); ledger.init()
            missed=0
            for i in range(40):
                # No sleep: land the rewrite inside the same tick as the refresh
                # that recorded the previous one. Same length every time.
                source.write_text(f"def alpha():\n    return {20 + i}\n")
                if ledger.refresh(changed_only=True, agent="codex", request="edit")["change_id"] is None:
                    missed += 1
            self.assertEqual(missed, 0, "a same-size edit sharing an mtime must not be skipped")
            self.assertEqual(ledger.db.execute("SELECT hash FROM files WHERE path='a.py'").fetchone()[0],
                             digest_bytes(source.read_bytes()))

    def test_a_settled_file_is_not_rehashed(self):
        """The race guard must not cost anything once a file has stopped moving."""
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"a.py"
            source.write_text("def alpha():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            old = time.time() - 3600
            os.utime(source, (old, old))          # last touched an hour ago
            ledger.refresh(changed_only=True)     # settle the recorded mtime
            result = ledger.refresh(changed_only=True)
            self.assertEqual(result["effect"], "none")
            self.assertEqual(result["timing"]["hashing_seconds"], 0.0,
                             "an old, unchanged file must be trusted on metadata alone")

    def test_parallel_and_serial_discovery_agree_exactly(self):
        """The fast path may only be faster — never a different answer."""
        from codeledger import core
        with tempfile.TemporaryDirectory() as directory:
            root=self._project(directory); ledger=Ledger(root); ledger.init()
            (root/"src"/"big.py").write_text("x = 1\n" * 10)
            serial=[(rel, size, mtime) for _p, rel, size, mtime, _m in ledger._discover()]
            original=core.SLOW_FS_SECONDS
            try:
                core.SLOW_FS_SECONDS = -1.0        # force every stat through the thread pool
                parallel=[(rel, size, mtime) for _p, rel, size, mtime, _m in ledger._discover()]
                self.assertEqual(ledger._last_discovery_metrics.stat_mode, "parallel")
            finally:
                core.SLOW_FS_SECONDS = original
            self.assertEqual(serial, parallel)
            self.assertTrue(serial)


class ReleaseTests(unittest.TestCase):
    """Guard the packaging mistake that has already shipped twice.

    The version stayed still while the code moved, so `pip install --upgrade`
    found the requirement satisfied and did nothing: the command succeeded,
    reported the old version, and left the old code running. Nothing looked
    wrong, which is why it happened again. A released version must therefore be
    declared in exactly one place as far as anyone can tell, and must be written
    down in the changelog.
    """

    ROOT = Path(__file__).resolve().parents[1]

    def _declared_version(self) -> str:
        text = (self.ROOT/"pyproject.toml").read_text(encoding="utf-8")
        # tomllib is 3.11+, and this must run on 3.10 too.
        match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
        self.assertIsNotNone(match, "pyproject.toml declares no version")
        return match.group(1)

    def test_the_package_and_the_module_agree_on_the_version(self):
        from codeledger import __version__
        self.assertEqual(self._declared_version(), __version__,
                         "pyproject.toml and codeledger/__init__.py disagree; "
                         "an upgrade would install one and report the other")

    def test_the_released_version_is_written_down(self):
        version = self._declared_version()
        changelog = (self.ROOT/"CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{version}]", changelog,
                      f"CHANGELOG.md has no section for {version}. Move the entry out of "
                      "[Unreleased] when releasing, or nobody can tell what they upgraded into")


class PathAndSecretTests(unittest.TestCase):
    def test_spaces_parentheses_and_unicode_in_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/"HD anti gravity"/"clever-ticket (94-96)"
            (root/"src"/"café").mkdir(parents=True)
            (root/"src"/"café"/"módulo.py").write_text('def naïve_señor():\n    return "ok"\n', encoding="utf-8")
            ledger=Ledger(root); result=ledger.init()
            self.assertEqual(result["files_added"], 1)
            self.assertEqual(ledger.lookup("naïve_señor")[0]["path"], "src/café/módulo.py")

    def test_secrets_are_never_indexed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"app.py").write_text("def run():\n    return 1\n")
            (root/".env").write_text("API_KEY=sk-live-do-not-index\n")
            (root/".env.production").write_text("DB_PASSWORD=hunter2\n")
            (root/"id_rsa").write_text("-----BEGIN PRIVATE KEY-----\n")
            ledger=Ledger(root); ledger.init()
            indexed={row["path"] for row in ledger.db.execute("SELECT path FROM files")}
            self.assertEqual(indexed, {"app.py"})
            # And no secret value reaches the database in any column.
            blob=" ".join(str(value) for row in ledger.db.execute("SELECT * FROM files") for value in row)
            self.assertNotIn("sk-live", blob); self.assertNotIn("hunter2", blob)


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
            self.assertIn("Authentication", names); self.assertEqual(ledger.lookup("authenticate")[0]["kind"], "function")
            # Was `assertIn("codeledger mcp", …)`, which passed only while the
            # bare name was registered — the defect itself. The property that
            # actually matters is that the agent is handed something it can
            # launch without inheriting our PATH.
            # Form-agnostic on purpose: a console-script install and an
            # interpreter `-m` fallback are both valid, and which one applies
            # depends on how CodeLedger was installed.
            config = ledger.agent_config("codex")
            launch = config["launch_command"]
            self.assertTrue(Path(launch[0]).is_absolute())
            self.assertEqual(launch[launch.index("mcp") + 1], "--root")
            self.assertEqual(launch[-1], str(root))

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

    def test_observed_edits_are_unattributed_when_two_agents_are_active(self):
        """`watch` cannot see who wrote a file; with two agents it must not guess."""
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/"a.py"
            source.write_text("def alpha():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            ledger.start_session("codex", "work")
            source.write_text("def alpha():\n    return 10\n")
            solo=ledger.refresh(changed_only=True, agent="codex", observed=True)
            # Even as the only live agent, the watcher cannot claim authorship.
            self.assertEqual(solo["attribution"]["confidence"], "LOW")
            self.assertEqual(solo["attribution"]["source"], "filesystem-watcher")
            self.assertEqual(ledger.db.execute("SELECT agent FROM changes ORDER BY id DESC").fetchone()["agent"], "unknown")

            ledger.start_session("claude-code", "other work")
            source.write_text("def alpha():\n    return 20\n")
            shared=ledger.refresh(changed_only=True, agent="codex", observed=True)
            self.assertIn("attribution_note", shared)
            self.assertEqual(shared["attribution"]["confidence"], "LOW")
            self.assertEqual(ledger.db.execute("SELECT agent FROM changes ORDER BY id DESC").fetchone()["agent"], "unknown")
            self.assertEqual(ledger.db.execute("SELECT last_modified_by FROM symbols WHERE name='alpha'").fetchone()[0], "unknown")

            # An agent refreshing on its own behalf is authoritative, not observed.
            source.write_text("def alpha():\n    return 30\n")
            ledger.refresh(changed_only=True, agent="claude-code", session="s2", request="Update alpha")
            self.assertEqual(ledger.db.execute("SELECT agent FROM changes ORDER BY id DESC").fetchone()["agent"], "claude-code")

    def test_since_reports_what_another_agent_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); a=root/"a.py"; b=root/"b.py"
            a.write_text("def alpha():\n    return 1\n"); b.write_text("def beta():\n    return 2\n")
            ledger=Ledger(root); ledger.init()
            a.write_text("def alpha():\n    return 10\n")
            ledger.refresh(changed_only=True, agent="codex", request="Update alpha")
            b.write_text("def beta():\n    return 20\n")
            ledger.refresh(changed_only=True, agent="claude-code", request="Update beta")

            handoff=ledger.since(agent="codex")            # since codex last recorded anything
            self.assertEqual(handoff["changes_by_other_agents"], 1)
            self.assertEqual(handoff["files_changed"], ["b.py"])
            self.assertEqual(handoff["agents"], ["claude-code"])
            self.assertIn("claude-code", handoff["summary"])

            everything=ledger.since("0")                   # since change id 0
            self.assertEqual(len(everything["changes"]), 2)
            self.assertEqual(ledger.since(agent="nobody-yet")["since"]["timestamp"], "NOT RECORDED")

    def test_since_prints_the_handoff_in_the_documented_form(self):
        """The generic dict dump buried the answer; README documents this shape."""
        import io, contextlib
        from codeledger.cli import emit_since
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); a=root/"a.py"
            a.write_text("def alpha():\n    return 1\n")
            ledger=Ledger(root); ledger.init()
            a.write_text("def alpha():\n    return 10\n")
            ledger.refresh(changed_only=True, agent="codex", request="Update alpha")

            buffer=io.StringIO()
            with contextlib.redirect_stdout(buffer): emit_since(ledger.since(agent="claude-code"))
            lines=buffer.getvalue().splitlines()
            self.assertEqual(lines[0], "1 change(s) by codex: 1 file(s), 1 symbol(s). 1 was made by another agent.")
            self.assertRegex(lines[1], r"^   #1 by codex  \['a\.py'\]  symbols=\['alpha'\]  effect=symbols-changed$")

            empty=io.StringIO()
            with contextlib.redirect_stdout(empty): emit_since(ledger.since(agent="codex"))
            self.assertEqual(empty.getvalue().strip(), "Nothing has been recorded since that point.")

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
               ["since"], ["since", "--agent", "codex"], ["since", "42"],
               ["record", "summary"], ["mcp"], ["setup-agent", "codex"],
               ["agent-config", "codex"], ["setup-codex"], ["watch", "--max-interval", "5"],
               ["run", "--request", "task", "--", "echo"]]
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertEqual(parser.parse_args(argv).command, argv[0])


class CheckpointTests(unittest.TestCase):
    """A session's working memory must survive the session that produced it."""

    def _project(self, root: Path) -> Ledger:
        (root / "src").mkdir()
        (root / "src" / "auth.py").write_text("def authenticate_user(token):\n    return token\n\ndef session_timeout():\n    return 30\n")
        (root / "src" / "dashboard.py").write_text("def render_dashboard():\n    return 'css'\n")
        ledger = Ledger(root); ledger.init()
        return ledger

    def test_a_long_session_can_be_checkpointed_and_resumed_by_a_later_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = self._project(root)
            session = ledger.start_session("claude-code", "Fix authentication timeout")["session_id"]
            for index in range(4):
                ledger.record_change("claude-code", session, "Fix authentication timeout",
                                     f"attempt {index}", "unverified", ["src/auth.py"], ["session_timeout"])

            state = ledger.session_state(session)
            self.assertEqual(state["status"], "READY")
            self.assertEqual(len(state["changes"]), 4)
            self.assertEqual(state["files_touched"], ["src/auth.py"])
            # CodeLedger assembles what it observed and refuses to invent the rest.
            self.assertIn("goal", state["supply_to_checkpoint"])

            ledger.record_checkpoint(session_id=session, goal="Fix authentication timeout",
                                     summary="The timeout is set in session_timeout()",
                                     current_state="Changed but not verified in production",
                                     next_action="run production verification",
                                     accomplished=["identified the timeout source"],
                                     unresolved=["production verification"],
                                     failed_attempts=["raising the client-side timeout did nothing"],
                                     files=["src/auth.py"], symbols=["session_timeout"])

            # A new Ledger is a new session: nothing carried over in memory.
            resumed = Ledger(root).resume("Fix authentication timeout")
            self.assertEqual(resumed["status"], "RESUME")
            self.assertEqual(resumed["goal"], "Fix authentication timeout")
            self.assertEqual(resumed["next_action"], "run production verification")
            self.assertEqual(resumed["failed_attempts"], ["raising the client-side timeout did nothing"])
            self.assertEqual(resumed["important_symbols"], ["session_timeout"])
            self.assertFalse(resumed["efficiency"]["full_repository_scan_required"])

    def test_a_checkpoint_without_a_goal_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._project(Path(directory))
            # Selection is by goal, so a checkpoint without one could never be
            # retrieved: it would be stored and silently never found again.
            with self.assertRaises(ValueError):
                ledger.record_checkpoint(session_id="session-x", goal="   ")

    def test_a_checkpoint_crosses_agents_without_sharing_a_conversation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = self._project(root)
            claude = ledger.start_session("claude-code", "Fix authentication timeout")["session_id"]
            ledger.record_change("claude-code", claude, "Fix authentication timeout", "widened the window",
                                 "unverified", ["src/auth.py"], ["session_timeout"])
            ledger.record_checkpoint(session_id=claude, goal="Fix authentication timeout",
                                     current_state="changed, unverified", next_action="run production verification",
                                     failed_attempts=["client-side timeout had no effect"],
                                     files=["src/auth.py"], symbols=["session_timeout"])
            ledger.end_session(claude)

            # A different agent, a different process, no access to the first
            # conversation. The memory belongs to the project, not the model.
            codex = Ledger(root)
            codex.start_session("codex", "Fix authentication timeout")
            resumed = codex.resume("Fix authentication timeout")
            self.assertEqual(resumed["status"], "RESUME")
            self.assertEqual(resumed["recorded_by"]["agent"], "claude-code")
            self.assertEqual(resumed["recorded_by"]["provider"], "anthropic")
            self.assertEqual(resumed["next_action"], "run production verification")
            self.assertIn("client-side timeout had no effect", resumed["failed_attempts"])

    def test_a_checkpoint_never_outranks_the_source_it_describes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = self._project(root)
            session = ledger.start_session("claude-code", "Fix authentication timeout")["session_id"]
            ledger.record_checkpoint(session_id=session, goal="Fix authentication timeout",
                                     next_action="verify it", files=["src/auth.py"],
                                     symbols=["session_timeout", "authenticate_user"])

            # The symbol the checkpoint names is deleted from the source.
            (root / "src" / "auth.py").write_text("def authenticate_user(token):\n    return token\n")
            ledger.refresh(changed_only=True, agent="claude-code", session=session)

            resumed = ledger.resume("Fix authentication timeout")
            self.assertEqual(resumed["status"], "RESUME")
            # Excluded from the body rather than reported as current truth...
            self.assertNotIn("session_timeout", resumed["important_symbols"])
            self.assertIn("authenticate_user", resumed["important_symbols"])
            # ...and the reason is stated, because what changed underneath the
            # work is itself worth knowing.
            stale = {item["value"]: item["reason"] for item in resumed["stale_items"]}
            self.assertIn("session_timeout", stale)
            self.assertIn("deleted", stale["session_timeout"])

    def test_resume_selects_by_task_not_by_recency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = self._project(root)
            auth = ledger.start_session("claude-code", "Fix authentication timeout")["session_id"]
            ledger.record_checkpoint(session_id=auth, goal="Fix authentication timeout",
                                     next_action="verify the auth fix", files=["src/auth.py"])
            dashboard = ledger.start_session("codex", "Improve dashboard CSS")["session_id"]
            ledger.record_checkpoint(session_id=dashboard, goal="Improve dashboard CSS styling",
                                     next_action="restyle the dashboard header", files=["src/dashboard.py"])

            # The dashboard checkpoint is the most recent one. Recency must not
            # win: loading it would point the agent at the wrong subsystem.
            resumed = ledger.resume("Fix authentication timeout")
            self.assertEqual(resumed["goal"], "Fix authentication timeout")
            self.assertEqual(resumed["next_action"], "verify the auth fix")
            self.assertEqual(resumed["important_files"], ["src/auth.py"])
            self.assertNotIn("src/dashboard.py", resumed["important_files"])

    def test_an_unrelated_task_loads_no_checkpoint_at_all(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = self._project(root)
            session = ledger.start_session("claude-code", "Improve dashboard CSS")["session_id"]
            ledger.record_checkpoint(session_id=session, goal="Improve dashboard CSS styling",
                                     next_action="restyle the header", files=["src/dashboard.py"])

            # Nothing recorded describes payments. Handing over the dashboard
            # checkpoint would be worse than handing over nothing.
            resumed = ledger.resume("Fix payment processing")
            self.assertEqual(resumed["status"], "NO_RELEVANT_CHECKPOINT")
            self.assertNotIn("next_action", resumed)
            self.assertEqual([item["goal"] for item in resumed["open_checkpoints"]], ["Improve dashboard CSS styling"])

    def test_a_task_with_no_distinctive_words_says_so_instead_of_matching_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = self._project(root)
            session = ledger.start_session("claude-code", "Fix authentication timeout")["session_id"]
            ledger.record_checkpoint(session_id=session, goal="Fix authentication timeout", next_action="verify it")

            # "fix the code" is entirely stopwords: there is nothing to match on,
            # so scoring it would reject every checkpoint and look like a
            # considered decision. The fallback is stated rather than hidden.
            resumed = ledger.resume("fix the code")
            self.assertEqual(resumed["status"], "RESUME")
            self.assertEqual(resumed["selection"]["match_score"], 0.0)
            self.assertIn("could not be assessed", resumed["selection"]["basis"])

    def test_a_superseded_checkpoint_is_kept_but_no_longer_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = self._project(root)
            session = ledger.start_session("claude-code", "Fix authentication timeout")["session_id"]
            first = ledger.record_checkpoint(session_id=session, goal="Fix authentication timeout",
                                             next_action="first guess")
            second = ledger.record_checkpoint(session_id=session, goal="Fix authentication timeout",
                                              next_action="second and better guess")
            self.assertEqual(ledger.resume("Fix authentication timeout")["next_action"], "second and better guess")
            # History is the product: the old one is marked, never deleted.
            self.assertEqual(ledger.checkpoint(first["id"])["status"], "SUPERSEDED")
            self.assertEqual(ledger.checkpoint(first["id"])["superseded_by"], second["id"])

    def test_a_project_with_no_checkpoints_still_reports_unfinished_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = self._project(root)
            session = ledger.start_session("codex", "Fix authentication timeout")["session_id"]
            ledger.record_change("codex", session, "Fix authentication timeout", "changed the window",
                                 "unverified", ["src/auth.py"], ["session_timeout"])
            # Every project upgrading to this version starts here. Answering
            # "nothing to resume" would be wrong; claiming recorded intent that
            # was never captured would also be wrong.
            resumed = ledger.resume("Fix authentication timeout")
            self.assertEqual(resumed["status"], "NO_CHECKPOINTS_RECENT_WORK_FOUND")
            self.assertEqual(len(resumed["recent_work"]), 1)
            self.assertIn("inferred", resumed["guidance"])

    def test_resume_stays_compact_and_scans_nothing_on_a_large_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "src").mkdir()
            for index in range(300):
                (root / "src" / f"module_{index}.py").write_text(f"def function_{index}():\n    return {index}\n")
            (root / "src" / "auth.py").write_text("def session_timeout():\n    return 30\n")
            ledger = Ledger(root); ledger.init()
            session = ledger.start_session("claude-code", "Fix authentication timeout")["session_id"]
            ledger.record_checkpoint(session_id=session, goal="Fix authentication timeout",
                                     next_action="verify it", files=["src/auth.py"], symbols=["session_timeout"])

            resumed = ledger.resume("Fix authentication timeout")
            efficiency = resumed["efficiency"]
            self.assertGreaterEqual(efficiency["files_in_repository"], 300)
            self.assertEqual(efficiency["files_relevant"], 1)
            self.assertGreaterEqual(efficiency["files_avoided"], 300)
            self.assertFalse(efficiency["full_repository_scan_required"])
            # The whole point is that continuation costs a fraction of rediscovery.
            self.assertLess(efficiency["estimated_total_tokens"], 2000)


class ModelMetadataTests(unittest.TestCase):
    """Model identity is recorded when the runtime provides it, and never guessed."""

    def test_a_model_the_runtime_reports_is_stored_against_the_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "a.py").write_text("def alpha():\n    return 1\n")
            ledger = Ledger(root); ledger.init()
            started = ledger.start_session("claude-code", "work", model="claude-opus", model_version="4.5")
            self.assertEqual(started["model"], "claude-opus")
            self.assertEqual(started["model_version"], "4.5")
            self.assertEqual(started["provider"], "anthropic")
            row = ledger.db.execute("SELECT provider,model,model_version FROM sessions WHERE session_id=?",
                                    (started["session_id"],)).fetchone()
            self.assertEqual((row["provider"], row["model"], row["model_version"]), ("anthropic", "claude-opus", "4.5"))

    def test_a_model_the_runtime_does_not_expose_is_unknown_and_breaks_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "a.py").write_text("def alpha():\n    return 1\n")
            ledger = Ledger(root); ledger.init()
            started = ledger.start_session("codex", "Fix authentication timeout")
            # An agent name says which program is running, not which model it is
            # driving today. Guessing one would store a fiction as evidence.
            self.assertEqual(started["model"], "UNKNOWN")
            self.assertEqual(started["model_version"], "UNKNOWN")
            self.assertEqual(started["provider"], "openai")
            checkpoint = ledger.record_checkpoint(session_id=started["session_id"],
                                                  goal="Fix authentication timeout", next_action="continue")
            self.assertEqual(checkpoint["model"], "UNKNOWN")
            self.assertEqual(ledger.resume("Fix authentication timeout")["recorded_by"]["model"], "UNKNOWN")

    def test_an_unrecognised_agent_gets_a_generic_provider_rather_than_a_vendor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "a.py").write_text("def alpha():\n    return 1\n")
            ledger = Ledger(root); ledger.init()
            started = ledger.start_session("some-new-agent-2030", "work")
            self.assertEqual(started["provider"], "generic")
            self.assertEqual(started["model"], "UNKNOWN")


class ContextWindowTests(unittest.TestCase):
    """Context usage is advice when offered, and absent without consequence."""

    def _ledger(self, root: Path) -> Ledger:
        (root / "a.py").write_text("def alpha():\n    return 1\n")
        ledger = Ledger(root); ledger.init(); return ledger

    def test_usage_past_the_threshold_recommends_a_checkpoint_without_forcing_one(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._ledger(Path(directory))
            usage = ledger.context_usage(200_000, 164_000)
            self.assertEqual(usage["context_used_pct"], 82.0)
            self.assertTrue(usage["checkpoint_recommended"])
            self.assertEqual(usage["recommendation"], "CHECKPOINT_RECOMMENDED")

    def test_usage_below_the_threshold_recommends_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._ledger(Path(directory))
            usage = ledger.context_usage(200_000, 20_000)
            self.assertFalse(usage["checkpoint_recommended"])
            self.assertEqual(usage["recommendation"], "NOT_YET")

    def test_a_runtime_that_reports_no_context_still_checkpoints_normally(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = self._ledger(root)
            session = ledger.start_session("gemini", "Fix authentication timeout")["session_id"]
            usage = ledger.session_state(session)["context"]
            # No numbers means no percentage — not a fabricated one, and not a
            # feature that stops working.
            self.assertEqual(usage["context_used_pct"], "UNKNOWN")
            self.assertEqual(usage["recommendation"], "UNKNOWN_CONTEXT")
            self.assertFalse(usage["checkpoint_recommended"])
            ledger.record_checkpoint(session_id=session, goal="Fix authentication timeout", next_action="carry on")
            self.assertEqual(ledger.resume("Fix authentication timeout")["status"], "RESUME")


class McpSessionTests(unittest.TestCase):
    """The MCP server is the only long-lived process, so it owns the session."""

    def _serve(self, root: Path, lines: list[dict]) -> list[dict]:
        import io
        from codeledger import mcp
        stdin, stdout = sys.stdin, sys.stdout
        sys.stdin = io.StringIO("".join(json.dumps(line) + "\n" for line in lines))
        sys.stdout = io.StringIO()
        try:
            mcp.serve(root)
            return [json.loads(line) for line in sys.stdout.getvalue().splitlines() if line.strip()]
        finally:
            sys.stdin, sys.stdout = stdin, stdout

    def test_the_server_starts_a_session_on_initialize_and_ends_it_on_disconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "a.py").write_text("def alpha():\n    return 1\n")
            Ledger(root).init()
            self._serve(root, [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"clientInfo": {"name": "claude-code"}}}])
            ledger = Ledger(root)
            rows = ledger._session_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["agent"], "claude-code")
            self.assertEqual(rows[0]["provider"], "anthropic")
            # stdin closing is the client disconnecting: the session must not be
            # left active forever, which is what happened before it had one.
            self.assertEqual(rows[0]["status"], "ended")
            self.assertEqual(ledger.active_agents(), [])

    def test_an_unrecognised_mcp_client_is_recorded_as_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "a.py").write_text("def alpha():\n    return 1\n")
            Ledger(root).init()
            self._serve(root, [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"clientInfo": {"name": "some-future-agent"}}}])
            row = Ledger(root)._session_rows()[0]
            self.assertEqual(row["agent"], "some-future-agent")
            self.assertEqual(row["provider"], "generic")
            self.assertEqual(row["model"], "UNKNOWN")

    def test_a_disconnect_without_an_agent_checkpoint_records_a_low_confidence_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "a.py").write_text("def alpha():\n    return 1\n")
            Ledger(root).init()
            self._serve(root, [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "codeledger_record_change",
                            "arguments": {"agent": "codex", "request": "Fix authentication timeout",
                                          "summary": "changed the window", "files": ["a.py"]}}}])
            ledger = Ledger(root)
            checkpoints = ledger.checkpoints()
            self.assertEqual(len(checkpoints), 1)
            # Nobody was there to be asked what the work meant, and the record
            # says so rather than reading like an agent wrote it.
            self.assertEqual(checkpoints[0]["source"], "mechanical")
            self.assertEqual(checkpoints[0]["confidence"], "LOW")
            self.assertIn("NOT RECORDED", ledger.resume("Fix authentication timeout")["next_action"])

    def test_an_agent_checkpoint_is_never_replaced_by_the_mechanical_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "a.py").write_text("def alpha():\n    return 1\n")
            Ledger(root).init()
            self._serve(root, [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "codeledger_record_change",
                            "arguments": {"agent": "codex", "request": "Fix authentication timeout",
                                          "summary": "changed the window", "files": ["a.py"]}}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "codeledger_record_checkpoint",
                            "arguments": {"goal": "Fix authentication timeout",
                                          "next_action": "run production verification"}}}])
            checkpoints = Ledger(root).checkpoints()
            self.assertEqual([item["source"] for item in checkpoints], ["agent"])
            self.assertEqual(Ledger(root).resume("Fix authentication timeout")["next_action"],
                             "run production verification")

    def test_resume_and_checkpoint_are_reachable_over_mcp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "a.py").write_text("def alpha():\n    return 1\n")
            Ledger(root).init()
            responses = self._serve(root, [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "claude-code"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "codeledger_record_checkpoint",
                            "arguments": {"goal": "Fix authentication timeout", "next_action": "verify in production",
                                          "failed_attempts": ["client-side timeout did nothing"]}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "codeledger_get_resume", "arguments": {"task": "Fix authentication timeout"}}}])
            listed = {tool["name"] for tool in responses[1]["result"]["tools"]}
            self.assertLessEqual({"codeledger_get_resume", "codeledger_record_checkpoint",
                                  "codeledger_get_session_state"}, listed)
            resumed = responses[3]["result"]["structuredContent"]
            self.assertEqual(resumed["status"], "RESUME")
            self.assertEqual(resumed["next_action"], "verify in production")

    def test_a_live_mcp_session_does_not_make_the_watcher_claim_authorship(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "a.py").write_text("def alpha():\n    return 1\n")
            ledger = Ledger(root); ledger.init()
            # An MCP session is now live for claude-code. The filesystem still
            # cannot show which process wrote a file, so an observed edit must
            # stay UNKNOWN — a live session is not evidence of authorship.
            ledger.start_session("claude-code", "some task", owns_process=True)
            (root / "a.py").write_text("def alpha():\n    return 2\n")
            result = ledger.refresh(changed_only=True, agent="claude-code", observed=True)
            change = ledger.db.execute("SELECT agent,attribution_confidence FROM changes ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(change["agent"], "unknown")
            self.assertEqual(change["attribution_confidence"], "LOW")


class CheckpointMigrationTests(unittest.TestCase):
    def test_a_database_predating_checkpoints_upgrades_and_keeps_its_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "a.py").write_text("def alpha():\n    return 1\n")
            path = root / ".ai" / "codeledger" / "codeledger.db"; path.parent.mkdir(parents=True)
            legacy = sqlite3.connect(path)
            # The 0.3.0 shape: sessions without model columns, no checkpoint tables.
            legacy.executescript("\n".join(line for line in SCHEMA.splitlines() if "checkpoint" not in line.lower()))
            legacy.execute("INSERT INTO agents(name,provider,created_at) VALUES('codex','codex','2020-01-01T00:00:00+00:00')")
            legacy.execute("INSERT INTO sessions(session_id,agent_id,working_directory,start_time,request,status) "
                           "VALUES('session-legacy',1,?,'2020-01-01T00:00:00+00:00','old work','ended')", (str(root),))
            legacy.execute("INSERT INTO changes(timestamp,agent,session_id,user_request,summary,result) "
                           "VALUES('2020-01-01T00:00:00+00:00','codex','session-legacy','old work','did a thing','unverified')")
            legacy.commit(); legacy.close()

            ledger = Ledger(root)
            columns = {row["name"] for row in ledger.db.execute("PRAGMA table_info(sessions)")}
            self.assertLessEqual({"provider", "model", "model_version"}, columns)
            tables = {row["name"] for row in ledger.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertLessEqual({"checkpoints", "checkpoint_items"}, tables)
            # Nothing recorded before the upgrade is lost or rewritten.
            self.assertEqual(ledger.db.execute("SELECT user_request FROM changes").fetchone()[0], "old work")
            legacy_session = ledger.db.execute("SELECT model,provider FROM sessions WHERE session_id='session-legacy'").fetchone()
            self.assertIsNone(legacy_session["model"])
            # A session predating model tracking stays valid and reads UNKNOWN.
            state = ledger.session_state("session-legacy")
            self.assertEqual(state["model"], "UNKNOWN")
            self.assertEqual(state["status"], "READY")


class CheckpointSurfaceParityTests(unittest.TestCase):
    """Every surface must be able to express what the schema stores.

    Each of these was found by running the workflow end to end rather than by
    unit-testing a function: the storage layer was correct and the way in was
    not, which no test of `record_checkpoint` alone would have caught.
    """

    def test_the_cli_can_attach_decisions_issues_and_verifications(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "a.py").write_text("def alpha():\n    return 1\n")
            ledger = Ledger(root); ledger.init()
            ledger.upsert_decision("server-side-expiry", "Expiry stays server-side", "the client clock is not trusted")
            ledger.upsert_issue("ttl-unused", "ttl parameter is unused", severity="LOW")
            ledger.verify("project", "project", "TEST", "PASSED", "suite green")
            verification_id = str(ledger.db.execute("SELECT id FROM verifications").fetchone()["id"])
            session = ledger.start_session("claude-code", "Fix authentication timeout")["session_id"]

            parser = build_parser()
            args = parser.parse_args(["checkpoint", "create", "--goal", "Fix authentication timeout",
                                      "--session", session, "--next-action", "verify in production",
                                      "--decisions", "server-side-expiry", "--issues", "ttl-unused",
                                      "--verifications", verification_id])
            # The recorded decision and issue must reach the checkpoint. Without
            # these flags they were silently dropped and `resume` reported no
            # decisions at all, while the MCP path stored them correctly.
            self.assertEqual(args.decisions, ["server-side-expiry"])
            self.assertEqual(args.issues, ["ttl-unused"])
            self.assertEqual(args.verifications, [verification_id])

            ledger.record_checkpoint(session_id=session, goal="Fix authentication timeout",
                                     next_action="verify in production", decisions=args.decisions,
                                     issues=args.issues, verifications=args.verifications, source="cli")
            resumed = ledger.resume("Fix authentication timeout")
            self.assertEqual([item["key"] for item in resumed["decisions"]], ["server-side-expiry"])
            self.assertEqual([item["key"] for item in resumed["known_issues"]], ["ttl-unused"])
            self.assertEqual(resumed["verification"], [verification_id])

    def test_the_mcp_schema_advertises_every_list_the_handler_accepts(self):
        from codeledger.mcp import SCHEMAS
        advertised = set(SCHEMAS["codeledger_record_checkpoint"]["properties"])
        # `verifications` was accepted by the handler and missing from the
        # schema, so no agent could discover it. A field the handler reads and
        # the schema hides is a field nobody will ever send.
        accepted = {"goal", "summary", "current_state", "next_action", "accomplished", "unresolved",
                    "failed_attempts", "questions", "decisions", "issues", "verifications", "files", "symbols"}
        self.assertLessEqual(accepted, advertised)

    def test_the_mcp_handshake_tells_the_client_how_to_use_the_memory(self):
        from codeledger.mcp import INSTRUCTIONS
        # A server cannot push context into a model's turn, so the handshake is
        # the only in-protocol place to say "call resume first".
        self.assertIn("codeledger_get_resume", INSTRUCTIONS)
        self.assertIn("codeledger_record_checkpoint", INSTRUCTIONS)


class McpHandshakeTests(unittest.TestCase):
    def test_initialize_reports_the_real_version_and_carries_instructions(self):
        import io
        from codeledger import mcp
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "a.py").write_text("def alpha():\n    return 1\n")
            Ledger(root).init()
            stdin, stdout = sys.stdin, sys.stdout
            sys.stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                "params": {"clientInfo": {"name": "claude-code"}}}) + "\n")
            sys.stdout = io.StringIO()
            try:
                mcp.serve(root)
                response = json.loads(sys.stdout.getvalue().splitlines()[0])
            finally:
                sys.stdin, sys.stdout = stdin, stdout
            from codeledger import __version__
            # The server used to report a hardcoded 0.1.0 regardless of release.
            self.assertEqual(response["result"]["serverInfo"]["version"], __version__)
            self.assertIn("codeledger_get_resume", response["result"]["instructions"])


def shared_theme_project(root: Path) -> Ledger:
    """A shared provider used by four pages, plus one self-contained module."""
    (root / "src" / "theme").mkdir(parents=True)
    (root / "src" / "pages").mkdir(parents=True)
    (root / "src" / "solo").mkdir(parents=True)
    (root / "src/theme/ThemeProvider.py").write_text(
        "THEME_COLORS = {'primary': 'blue'}\n\n"
        "def ThemeProvider(children):\n    return children\n\n"
        "def useTheme():\n    return THEME_COLORS\n")
    for page in ("Landing", "Payment", "Queue", "Dashboard"):
        (root / "src/pages" / f"{page}.py").write_text(
            f"from src.theme.ThemeProvider import useTheme\n\n"
            f"def {page}Page():\n    return useTheme()\n")
    # Nothing outside its own directory depends on this one.
    (root / "src/solo/report.py").write_text(
        "def build_report(rows):\n    return format_rows(rows)\n\n"
        "def format_rows(rows):\n    return list(rows)\n")
    ledger = Ledger(root); ledger.init()
    return ledger


class BlastRadiusTests(unittest.TestCase):
    """`plan` must report what a change reaches, not only where it is defined."""

    def test_plan_reports_every_page_that_depends_on_a_shared_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = shared_theme_project(root)
            plan = ledger.plan("Remove the theme color")
            # The regression this exists for: `plan` used `lookup`, which returns
            # where a symbol is defined, and so reported one file while the
            # dependency graph already knew about all four pages.
            for page in ("Landing", "Payment", "Queue", "Dashboard"):
                self.assertIn(f"src/pages/{page}.py", plan["existing_files"], f"{page} missing from the plan")
            self.assertIn("src/theme/ThemeProvider.py", plan["existing_files"])
            radius = plan["blast_radius"]
            self.assertGreaterEqual(radius["file_count"], 4)
            self.assertLessEqual({"Landing", "Payment", "Queue", "Dashboard"}, set(radius["areas"]))
            self.assertTrue(plan["shared_dependencies"])
            self.assertTrue(plan["shared_dependencies"][0]["shared"])

    def test_sibling_files_under_a_container_directory_count_as_separate_areas(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = shared_theme_project(root)
            # Naming the directory would collapse four pages into one "pages",
            # which is exactly the distinction the user needs to answer "which?".
            self.assertEqual(ledger._area_for_path("src/pages/Landing.py"), "Landing")
            self.assertEqual(ledger._area_for_path("src/theme/ThemeProvider.py"), "theme")

    def test_planning_never_reads_the_working_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = shared_theme_project(root)
            def forbidden(*args, **kwargs):
                raise AssertionError("plan() performed a repository-wide scan")
            ledger._scan_for_names = forbidden
            plan = ledger.plan("Remove the theme color")     # must not raise
            self.assertGreaterEqual(plan["blast_radius"]["file_count"], 4)


class ScopeAmbiguityTests(unittest.TestCase):
    """Ask when the scope is genuinely unclear, and stay quiet otherwise."""

    def test_a_shared_change_with_no_stated_scope_asks_which_areas(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = shared_theme_project(Path(directory))
            ambiguity = ledger.plan("Remove the theme color")["scope_ambiguity"]
            self.assertIsNotNone(ambiguity)
            self.assertEqual(ambiguity["status"], "SCOPE_AMBIGUOUS")
            self.assertLessEqual({"Landing", "Payment", "Queue", "Dashboard"}, set(ambiguity["affected_areas"]))
            self.assertTrue(ambiguity["evidence"])
            # Never nominate one area as the default answer.
            self.assertNotIn("Dashboard only", ambiguity["options"])
            self.assertTrue(any("all affected areas" == option for option in ambiguity["options"]))

    def test_a_request_that_names_its_scope_is_not_questioned(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = shared_theme_project(Path(directory))
            plan = ledger.plan("Remove the theme color from Landing only")
            self.assertIsNone(plan["scope_ambiguity"])
            # The blast radius is still reported — the user settled the scope,
            # not the question of what the change touches.
            self.assertGreaterEqual(plan["blast_radius"]["file_count"], 4)

    def test_a_local_change_produces_no_scope_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = shared_theme_project(Path(directory))
            plan = ledger.plan("Rename format_rows to normalise_rows")
            self.assertIsNone(plan["scope_ambiguity"])
            self.assertEqual(plan["shared_dependencies"], [])
            self.assertNotIn("shared", plan["recommendation"])

    def test_adding_to_a_shared_module_is_not_treated_as_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = shared_theme_project(Path(directory))
            # Additive work does not change what existing dependents already do,
            # so there is no scope to settle and asking would be noise.
            self.assertIsNone(ledger.plan("Add a contrast helper to useTheme")["scope_ambiguity"])

    def test_two_areas_alone_is_not_enough_to_ask(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src" / "helpers").mkdir(parents=True)
            (root / "src" / "pages").mkdir(parents=True)
            (root / "src/helpers/text.py").write_text("def tidy(value):\n    return value.strip()\n")
            for page in ("First", "Second"):
                (root / "src/pages" / f"{page}.py").write_text(
                    f"from src.helpers.text import tidy\n\ndef {page}Page():\n    return tidy('{page}')\n")
            ledger = Ledger(root); ledger.init()
            plan = ledger.plan("Change tidy to also strip tabs")
            # Exactly two areas, so the cheap area check does not settle it: the
            # second-signal rule has to. An ordinary helper in no shared
            # location with a two-file radius must not raise a question —
            # warning here is how a guard teaches agents to ignore it.
            self.assertEqual(set(plan["blast_radius"]["areas"]), {"First", "Second"})
            self.assertFalse(any(item["shared"] for item in plan["shared_dependencies"]))
            self.assertIsNone(plan["scope_ambiguity"])


class CoverageHonestyTests(unittest.TestCase):
    """Absence of dependency evidence is never evidence of safety."""

    def test_shallow_coverage_lowers_confidence_and_says_why(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = shared_theme_project(root)
            ledger.db.execute("UPDATE files SET coverage='shallow' WHERE path='src/theme/ThemeProvider.py'")
            ledger.db.commit()
            plan = ledger.plan("Remove the theme color")
            self.assertEqual(plan["blast_radius"]["confidence"], "LOW")
            self.assertIsNotNone(plan["coverage_caveat"])
            self.assertIn("unproven", plan["coverage_caveat"])

    def test_no_dependents_under_shallow_coverage_is_not_reported_as_contained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = shared_theme_project(root)
            ledger.db.execute("UPDATE files SET coverage='shallow'")
            ledger.db.commit()
            plan = ledger.plan("Rewrite build_report")
            self.assertEqual(plan["blast_radius"]["confidence"], "LOW")
            self.assertIsNotNone(plan["coverage_caveat"])
            # Nothing in the advice may read as a guarantee.
            for text in (plan["recommendation"], plan["coverage_caveat"]):
                self.assertNotIn("safe", text.lower())


class DuplicateImplementationTests(unittest.TestCase):
    """Warn before a second implementation of something that already exists."""

    def _drawer_project(self, root: Path) -> Ledger:
        (root / "src" / "components").mkdir(parents=True)
        (root / "src" / "state").mkdir(parents=True)
        (root / "src/state/drawerState.py").write_text("def useDrawerState():\n    return {'open': False}\n")
        (root / "src/components/SharedDrawer.py").write_text(
            "from src.state.drawerState import useDrawerState\n\n"
            "def SharedDrawer(side):\n    return slideAnimation(side)\n\n"
            "def slideAnimation(side):\n    return 'slide-in-' + side\n")
        (root / "src/components/OrderPanel.py").write_text(
            "from src.components.SharedDrawer import SharedDrawer\n"
            "from src.state.drawerState import useDrawerState\n\n"
            "def OrderPanel(order):\n    useDrawerState()\n    return SharedDrawer('right')\n")
        ledger = Ledger(root); ledger.init()
        return ledger

    def test_a_plan_to_build_a_parallel_flow_is_warned_about(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._drawer_project(Path(directory))
            result = ledger.handshake("Make this new button open the same kind of panel",
                                      "I will create a new CheckoutFlyout component with its own slide "
                                      "animation and its own open/close state.")
            duplicate = result["duplicate_implementation"]
            self.assertIsNotNone(duplicate, "a parallel implementation was proposed and not flagged")
            self.assertIn("CheckoutFlyout", duplicate["proposed_new"])
            names = {item["symbol"] for item in duplicate["existing_implementation"]}
            # The entry point alone is not useful: the shared drawer and the
            # shared state underneath it are what reuse actually means.
            self.assertIn("OrderPanel", names)
            self.assertLessEqual({"SharedDrawer", "useDrawerState"}, names)

    def test_the_warning_recommends_rather_than_rejects(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._drawer_project(Path(directory))
            result = ledger.handshake("Make this new button open the same kind of panel",
                                      "I will create a new CheckoutFlyout component.")
            # A new implementation is sometimes the right call, so this must read
            # as advice. Nothing may present itself as a refusal or a block.
            self.assertEqual(result["status"], "WARNING")
            self.assertIn("recommendation, not a rejection", result["duplicate_implementation"]["guidance"])
            self.assertEqual(result["ai_plan"], "I will create a new CheckoutFlyout component.")

    def test_genuinely_new_work_is_not_flagged_as_duplication(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._drawer_project(Path(directory))
            result = ledger.handshake("Add CSV export for invoices",
                                      "I will create a CsvExporter module that serialises invoice rows.")
            self.assertIsNone(result["duplicate_implementation"])
            self.assertEqual(result["status"], "ALIGNED")

    def test_a_plan_that_reuses_existing_symbols_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._drawer_project(Path(directory))
            result = ledger.handshake("Make this new button open the same kind of panel",
                                      "I will reuse SharedDrawer and useDrawerState from OrderPanel.")
            self.assertIsNone(result["duplicate_implementation"])


class PreChangeRegressionTests(unittest.TestCase):
    """The pre-change layer's existing behaviour must not shift."""

    def test_existing_plan_scope_impact_and_history_still_behave(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = shared_theme_project(root)
            plan = ledger.plan("Remove the theme color")
            for key in ("request", "task_analysis", "existing_files", "relevant_symbols", "recent_changes",
                        "known_issues", "decisions", "risk", "recommendation", "full_scan_required", "suggested_tests"):
                self.assertIn(key, plan)
            self.assertEqual(ledger.impact("useTheme", fallback=False)["query"], "useTheme")
            scope = ledger.scope_check("Remove the theme color", ["src/pages/Landing.py"])
            self.assertIn(scope["status"], ("SAFE", "WARNING", "UNKNOWN", "NO_CHANGES"))
            session = ledger.start_session("codex", "Remove the theme color")["session_id"]
            ledger.record_change("codex", session, "Remove the theme color", "dropped the colour",
                                 "unverified", ["src/theme/ThemeProvider.py"], ["useTheme"])
            self.assertEqual(len(ledger.history("theme")), 1)
            self.assertEqual(ledger.progress("Remove the theme color")["attempt_count"], 1)

    def test_recording_a_change_does_not_run_the_scope_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = shared_theme_project(root)
            def forbidden(*args, **kwargs):
                raise AssertionError("record_change computed shared dependencies")
            ledger.shared_dependencies = forbidden
            session = ledger.start_session("codex", "Remove the theme color")["session_id"]
            # `record_change` derives risk from the request on every refresh and
            # must not pay for dependency queries to do it.
            ledger.record_change("codex", session, "Remove the theme color", "dropped it", "unverified")

    def test_analyze_prompt_keeps_its_text_only_contract_when_scope_is_off(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = shared_theme_project(Path(directory))
            bare = ledger.analyze_prompt("Remove the theme color", scope=False)
            self.assertNotIn("scope_ambiguity", bare)
            self.assertEqual(bare["risk"], "HIGH")
            full = ledger.analyze_prompt("Remove the theme color")
            self.assertIn("scope_ambiguity", full)
            self.assertEqual(bare["intent"], full["intent"])


class ScopeIntelligenceMcpParityTests(unittest.TestCase):
    """MCP must expose exactly what the core computes."""

    def _call(self, root, name, arguments):
        import io
        from codeledger import mcp
        stdin, stdout = sys.stdin, sys.stdout
        sys.stdin = io.StringIO("".join(json.dumps(line) + "\n" for line in [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}}]))
        sys.stdout = io.StringIO()
        try:
            mcp.serve(root)
            return json.loads(sys.stdout.getvalue().splitlines()[1])["result"]["structuredContent"]
        finally:
            sys.stdin, sys.stdout = stdin, stdout

    def test_mcp_plan_prompt_and_handshake_carry_the_scope_intelligence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = shared_theme_project(root)
            plan = self._call(root, "codeledger_get_plan", {"request": "Remove the theme color"})
            self.assertEqual(plan["blast_radius"]["file_count"], ledger.plan("Remove the theme color")["blast_radius"]["file_count"])
            self.assertLessEqual({"Landing", "Payment", "Queue", "Dashboard"}, set(plan["blast_radius"]["areas"]))
            self.assertIsNotNone(plan["scope_ambiguity"])

            prompt = self._call(root, "codeledger_analyze_prompt", {"prompt": "Remove the theme color"})
            self.assertIsNotNone(prompt["scope_ambiguity"])
            self.assertTrue(prompt["shared_dependencies"])

            shake = self._call(root, "codeledger_task_handshake",
                               {"request": "Remove the theme color", "ai_plan": "I will create a NewThemeSwitcher."})
            self.assertIn("duplicate_implementation", shake)
            self.assertIsNotNone(shake["duplicate_implementation"])


class ChangelogIntegrityTests(unittest.TestCase):
    """A released version must keep its entry forever.

    Editing the changelog to add a new release is a heading insertion, and an
    insertion done wrong is a deletion: writing `## [0.4.0]` over `## [0.3.0]`
    leaves a file that still parses, still has a section for the version being
    released, and still passes every release guard — while 0.3.0's entry has
    silently become part of 0.4.0's. The existing guards check the version being
    shipped; nothing was watching the ones already shipped.
    """

    def _headings(self) -> list[str]:
        changelog = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
        return re.findall(r"^## \[(\d+\.\d+\.\d+)\]", changelog.read_text(encoding="utf-8"), re.M)

    def test_every_tagged_release_still_has_a_changelog_section(self):
        root = Path(__file__).resolve().parent.parent
        try:
            completed = subprocess.run(["git", "tag", "--list", "v*"], cwd=root,
                                       capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            raise unittest.SkipTest("git is not available")
        tags = [line.strip().lstrip("v") for line in completed.stdout.splitlines() if line.strip()]
        if not tags:
            # A shallow clone without tags — CI checkouts do not fetch them by
            # default. Say so rather than passing on an empty comparison.
            raise unittest.SkipTest("no release tags present in this checkout")
        missing = [tag for tag in tags if tag not in self._headings()]
        self.assertEqual(missing, [], f"released version(s) lost their changelog section: {missing}")

    def test_changelog_versions_are_unique_and_newest_first(self):
        headings = self._headings()
        self.assertEqual(len(headings), len(set(headings)), f"duplicate changelog sections: {headings}")
        order = [tuple(int(part) for part in version.split(".")) for version in headings]
        self.assertEqual(order, sorted(order, reverse=True), f"changelog sections are out of order: {headings}")
        self.assertEqual(headings[0], __import__("codeledger").__version__,
                         "the version being released must head the changelog")


# The verbatim schema of a real CodeLedger database found in the field, captured
# from a project that had been in daily use since an early version. It is kept
# literally rather than generated from `SCHEMA`, because a fixture built from the
# current schema is not a legacy database — it already has every column the
# current code expects, which is exactly why the previous migration test passed
# while real upgrades failed. Note what is absent: files.coverage,
# files.analysis_provider, dependencies.source_file_id, changes.effect, the
# attribution columns, and every session column added after 0.1.
LEGACY_SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, language TEXT, size INTEGER, hash TEXT, mtime REAL, git_status TEXT, status TEXT NOT NULL DEFAULT 'current', last_analyzed TEXT, analysis_version TEXT, last_modified_by TEXT, last_modified_session TEXT, mtime_ns INTEGER);
CREATE TABLE symbols (id INTEGER PRIMARY KEY, name TEXT NOT NULL, qualified_name TEXT, kind TEXT NOT NULL, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE, line_start INTEGER, line_end INTEGER, signature TEXT, hash TEXT, status TEXT NOT NULL DEFAULT 'active', created_at TEXT, updated_at TEXT, deleted_at TEXT, last_modified_by TEXT, last_modified_session TEXT, last_verified TEXT);
CREATE TABLE dependencies (id INTEGER PRIMARY KEY, source_symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE, target_name TEXT NOT NULL, target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL, kind TEXT, UNIQUE(source_symbol_id, target_name, kind));
CREATE TABLE features (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT, status TEXT NOT NULL DEFAULT 'UNKNOWN', last_verified TEXT, last_changed TEXT);
CREATE TABLE feature_symbols (feature_id INTEGER REFERENCES features(id) ON DELETE CASCADE, symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE, PRIMARY KEY(feature_id, symbol_id));
CREATE TABLE symbol_versions (id INTEGER PRIMARY KEY, symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE, hash TEXT, snapshot TEXT, commit_hash TEXT, status TEXT, recorded_at TEXT NOT NULL);
CREATE TABLE agents (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, provider TEXT, created_at TEXT NOT NULL);
CREATE TABLE sessions (id INTEGER PRIMARY KEY, session_id TEXT UNIQUE NOT NULL, agent_id INTEGER REFERENCES agents(id), working_directory TEXT, start_time TEXT NOT NULL, end_time TEXT, request TEXT, result TEXT, status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE changes (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, agent TEXT, session_id TEXT, user_request TEXT, summary TEXT, risk TEXT, git_commit TEXT, result TEXT, files_added INTEGER DEFAULT 0, files_modified INTEGER DEFAULT 0, files_deleted INTEGER DEFAULT 0, symbols_added INTEGER DEFAULT 0, symbols_modified INTEGER DEFAULT 0, symbols_deleted INTEGER DEFAULT 0);
CREATE TABLE change_files (change_id INTEGER REFERENCES changes(id) ON DELETE CASCADE, file_id INTEGER REFERENCES files(id) ON DELETE SET NULL, path TEXT NOT NULL, status TEXT, PRIMARY KEY(change_id, path));
CREATE TABLE change_symbols (change_id INTEGER REFERENCES changes(id) ON DELETE CASCADE, symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL, name TEXT NOT NULL, status TEXT, PRIMARY KEY(change_id, name));
CREATE TABLE issues (id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, title TEXT NOT NULL, description TEXT, status TEXT NOT NULL DEFAULT 'OPEN', severity TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE decisions (id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, title TEXT NOT NULL, rationale TEXT, status TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT NOT NULL);
CREATE TABLE verifications (id INTEGER PRIMARY KEY, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, kind TEXT NOT NULL, result TEXT NOT NULL, evidence TEXT, recorded_at TEXT NOT NULL);
CREATE TABLE git_commits (commit_hash TEXT PRIMARY KEY, parent_hash TEXT, author TEXT, timestamp TEXT, subject TEXT);
CREATE TABLE git_commit_files (commit_hash TEXT REFERENCES git_commits(commit_hash) ON DELETE CASCADE, path TEXT NOT NULL, status TEXT, PRIMARY KEY(commit_hash,path));
CREATE INDEX idx_changes_time ON changes(timestamp DESC);
CREATE INDEX idx_files_hash ON files(hash);
CREATE INDEX idx_symbols_file ON symbols(file_id);
CREATE INDEX idx_symbols_name ON symbols(name);
"""

def schema_shape(conn):
    """Tables with their columns, and index names — what a database *is*."""
    tables = {row[0]: [column[1] for column in conn.execute(f"PRAGMA table_info({row[0]})")]
              for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    indexes = sorted(row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"))
    return tables, indexes


class LegacyDatabaseMigrationTests(unittest.TestCase):
    """A database from an early version must open, and keep everything it had.

    This is the failure a real project hit on upgrading: `no such column:
    coverage`, raised before a single migration had run, leaving CodeLedger
    unable to open a perfectly intact database at all.
    """

    def _legacy(self, root: Path) -> Path:
        path = root / ".ai" / "codeledger" / "codeledger.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        old = sqlite3.connect(path)
        old.executescript(LEGACY_SCHEMA)
        old.execute("INSERT INTO agents(name,provider,created_at) VALUES('codex','codex','2026-01-01T00:00:00+00:00')")
        old.execute("INSERT INTO sessions(session_id,agent_id,working_directory,start_time,request,status) "
                    "VALUES('session-legacy',1,?,'2026-01-01T00:00:00+00:00','historic work','ended')", (str(root),))
        old.execute("INSERT INTO files(path,language,size,hash,status) VALUES('legacy.py','python',10,'abc','current')")
        old.execute("INSERT INTO symbols(name,kind,file_id,status,created_at) VALUES('legacy_symbol','function',1,'active','2026-01-01T00:00:00+00:00')")
        old.execute("INSERT INTO dependencies(source_symbol_id,target_name,kind) VALUES(1,'helper','calls')")
        old.execute("INSERT INTO changes(timestamp,agent,session_id,user_request,summary,result) "
                    "VALUES('2026-01-01T00:00:00+00:00','codex','session-legacy','historic work','did a thing','unverified')")
        old.execute("INSERT INTO change_files(change_id,file_id,path,status) VALUES(1,1,'legacy.py','changed')")
        old.execute("INSERT INTO change_symbols(change_id,symbol_id,name,status) VALUES(1,1,'legacy_symbol','changed')")
        old.execute("INSERT INTO decisions(key,title,rationale,status,created_at) VALUES('legacy-decision','Keep it','because','ACTIVE','2026-01-01T00:00:00+00:00')")
        old.execute("INSERT INTO issues(key,title,description,status,severity,created_at,updated_at) VALUES('legacy-issue','Old bug','still here','OPEN','HIGH','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')")
        old.execute("INSERT INTO verifications(subject_type,subject_id,kind,result,evidence,recorded_at) VALUES('project','project','TEST','PASSED','green','2026-01-01T00:00:00+00:00')")
        old.execute("INSERT INTO git_commits(commit_hash,parent_hash,author,timestamp,subject) VALUES('abc123','def456','someone','2026-01-01T00:00:00+00:00','a commit')")
        old.execute("INSERT INTO git_commit_files(commit_hash,path,status) VALUES('abc123','legacy.py','M')")
        old.execute("INSERT INTO symbol_versions(symbol_id,hash,status,recorded_at) VALUES(1,'abc','active','2026-01-01T00:00:00+00:00')")
        old.commit(); old.close()
        return path

    def _counts(self, conn):
        return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("files", "symbols", "changes", "dependencies", "sessions", "decisions",
                              "issues", "verifications", "git_commits", "git_commit_files",
                              "change_files", "change_symbols", "agents")}

    def test_an_early_database_opens_instead_of_raising_no_such_column(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = self._legacy(root)
            (root / "legacy.py").write_text("def legacy_symbol():\n    return 1\n")
            raw = sqlite3.connect(path)
            self.assertNotIn("coverage", {row[1] for row in raw.execute("PRAGMA table_info(files)")})
            self.assertNotIn("idx_files_coverage", {row[0] for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")})
            before = self._counts(raw); raw.close()

            # The regression: `CREATE INDEX IF NOT EXISTS idx_files_coverage ON
            # files(coverage)` ran before the migration adding that column, so
            # opening the database raised and nothing could proceed.
            ledger = Ledger(root)
            self.assertEqual(self._counts(ledger.db), before, "history changed during migration")
            columns = {row["name"] for row in ledger.db.execute("PRAGMA table_info(files)")}
            self.assertLessEqual({"coverage", "analysis_provider"}, columns)
            indexes = {row["name"] for row in ledger.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertLessEqual({"idx_files_coverage", "idx_dependencies_source_file"}, indexes)

    def test_the_whole_command_surface_works_after_migrating(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self._legacy(root)
            (root / "legacy.py").write_text("def legacy_symbol():\n    return 1\n")
            ledger = Ledger(root)
            before = self._counts(ledger.db)
            ledger.init()
            self.assertEqual(ledger.status()["changes"], before["changes"])
            self.assertEqual(ledger.doctor()["checks"]["migrations"], "OK")
            self.assertEqual(ledger.doctor()["checks"]["schema"], "OK")
            self.assertIn("files_added", ledger.refresh(changed_only=True))
            # Everything recorded under the old schema is still readable.
            self.assertEqual(len(ledger.history("historic")), 1)
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM decisions").fetchone()[0], 1)
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM issues").fetchone()[0], 1)
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM verifications").fetchone()[0], 1)
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM git_commits").fetchone()[0], 1)

    def test_a_migrated_database_ends_up_shaped_like_a_fresh_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self._legacy(root)
            (root / "legacy.py").write_text("def legacy_symbol():\n    return 1\n")
            migrated_tables, migrated_indexes = schema_shape(Ledger(root).db)
            fresh_root = root / "fresh"; fresh_root.mkdir()
            (fresh_root / "a.py").write_text("def alpha():\n    return 1\n")
            fresh_tables, fresh_indexes = schema_shape(Ledger(fresh_root).db)
            # The general invariant, rather than a list of columns to keep in
            # sync by hand: whatever a new database has, an upgraded one has too.
            self.assertEqual(migrated_indexes, fresh_indexes)
            for table, columns in fresh_tables.items():
                self.assertIn(table, migrated_tables, f"{table} missing after migration")
                self.assertEqual(set(columns) - set(migrated_tables[table]), set(),
                                 f"{table} is missing columns after migration")
            # Tables from older versions are kept, never dropped.
            self.assertLessEqual({"symbol_versions", "feature_symbols"}, set(migrated_tables))

    def test_old_rows_get_null_for_fields_that_did_not_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self._legacy(root)
            (root / "legacy.py").write_text("def legacy_symbol():\n    return 1\n")
            ledger = Ledger(root)
            session = ledger.db.execute("SELECT provider,model,model_version,pid,host FROM sessions").fetchone()
            self.assertEqual(tuple(session), (None, None, None, None, None))
            change = ledger.db.execute("SELECT effect,attribution_source,attribution_confidence FROM changes").fetchone()
            self.assertEqual(tuple(change), (None, None, None))
            self.assertIsNone(ledger.db.execute("SELECT coverage FROM files").fetchone()["coverage"])
            # Read back through the API, absence reads as UNKNOWN rather than as
            # a value somebody invented for it.
            self.assertEqual(ledger.session_state("session-legacy")["model"], "UNKNOWN")
            self.assertEqual(ledger.session_state("session-legacy")["provider"], "UNKNOWN")

    def test_migrating_twice_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self._legacy(root)
            (root / "legacy.py").write_text("def legacy_symbol():\n    return 1\n")
            first = Ledger(root); shape_one = schema_shape(first.db); counts_one = self._counts(first.db)
            first.db.close()
            second = Ledger(root); shape_two = schema_shape(second.db); counts_two = self._counts(second.db)
            second.db.close()
            third = Ledger(root)
            self.assertEqual(shape_one, shape_two)
            self.assertEqual(shape_two, schema_shape(third.db))
            self.assertEqual(counts_one, counts_two)
            self.assertEqual(counts_two, self._counts(third.db))

    def test_a_failing_migration_leaves_the_old_schema_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self._legacy(root)
            broken = list(db_module.MIGRATIONS) + [("files", "never_added", "ALTER TABLE nonexistent ADD COLUMN never_added TEXT")]
            original = db_module.MIGRATIONS
            db_module.MIGRATIONS = broken
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    Ledger(root)
            finally:
                db_module.MIGRATIONS = original
            # A half-migrated database is worse than one that refused to open:
            # the failure must roll the whole upgrade back, not leave some
            # columns added and others not.
            raw = sqlite3.connect(root / ".ai" / "codeledger" / "codeledger.db")
            self.assertNotIn("coverage", {row[1] for row in raw.execute("PRAGMA table_info(files)")})
            self.assertEqual(raw.execute("SELECT count(*) FROM changes").fetchone()[0], 1)
            raw.close()
            # And the next open, with the real migration list, still succeeds.
            self.assertIn("coverage", {row["name"] for row in Ledger(root).db.execute("PRAGMA table_info(files)")})

    def test_a_fresh_database_needs_no_migration_and_a_current_one_is_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "a.py").write_text("def alpha():\n    return 1\n")
            fresh = Ledger(root); fresh.init()
            shape_before = schema_shape(fresh.db)
            session = fresh.start_session("claude-code", "work")["session_id"]
            fresh.record_checkpoint(session_id=session, goal="work", next_action="continue")
            fresh.db.close()
            reopened = Ledger(root)
            self.assertEqual(schema_shape(reopened.db), shape_before)
            self.assertEqual(reopened.db.execute("SELECT count(*) FROM checkpoints").fetchone()[0], 1)


class SchemaIndexOrderingTests(unittest.TestCase):
    """No index may name a column that only a migration provides... too early."""

    def test_every_indexed_column_exists_by_the_time_indexes_are_created(self):
        migrated = {(table, column) for table, column, _ in db_module.MIGRATIONS}
        declared = {}
        for match in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\);", db_module.TABLES, re.S):
            body, depth, current, columns = match.group(2), 0, "", []
            for character in body:
                if character == "(": depth += 1
                elif character == ")": depth -= 1
                if character == "," and depth == 0: columns.append(current); current = ""
                else: current += character
            columns.append(current)
            # Match constraint keywords as whole words: a prefix test drops
            # `checkpoint_id` for starting with "CHECK".
            constraints = {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"}
            declared[match.group(1)] = {item.strip().split()[0] for item in columns
                                        if item.strip() and item.strip().upper().split()[0] not in constraints}
        offenders = []
        for match in re.finditer(r"CREATE INDEX IF NOT EXISTS (\w+) ON (\w+)\(([^)]*)\)", db_module.INDEXES):
            index, table, spec = match.groups()
            for column in (part.strip().split()[0] for part in spec.split(",")):
                # A column that a migration adds is fine *here*, because INDEXES
                # runs after migrations. What must never happen is an index in
                # TABLES, which runs before them.
                if column not in declared.get(table, set()) and (table, column) not in migrated:
                    offenders.append(f"{index} names {table}.{column}, which nothing creates")
        self.assertEqual(offenders, [])
        self.assertNotIn("CREATE INDEX", db_module.TABLES,
                         "indexes must live in INDEXES, which runs after the migrations")


class ShallowSymbolPrecisionTests(unittest.TestCase):
    """The line-pattern provider must not promote keywords or prose to symbols.

    These exercise `regex_symbols` directly rather than `analyze`, so they run
    identically with and without grammars. That is deliberate: the shallow
    provider is the floor every unsupported language falls to, and the floor is
    where the reported garbage came from.
    """

    KEYWORD_SOURCE = """
export function save(payload) {
  if (payload) {
    track(payload);
  }
  for (const item of payload.items) {
    visit(item);
  }
  while (pending) {
    step();
  }
  switch (payload.mode) {
    case 1: break;
  }
  try {
    submit(payload);
  }
  catch (error) {
    report(error);
  }
}
"""

    PROSE_SOURCE = """
// This function will type the value into the box.
/* It will interface Foo with bar, and the
   class of service is decided later. */
# type the name here
const total = 1;
"""

    def test_control_flow_keywords_are_never_symbols(self):
        """`if (x) {` is a statement, not a declaration."""
        from codeledger.parser import regex_symbols
        names = {item.name for item in regex_symbols(self.KEYWORD_SOURCE)}
        for keyword in ("if", "for", "while", "switch", "catch", "try"):
            self.assertNotIn(keyword, names, f"{keyword!r} was indexed as a project symbol")
        self.assertIn("save", names, "the real function must survive the keyword guard")

    def test_prose_and_comments_do_not_produce_symbols(self):
        """English sentences mentioning `type`/`class` are not declarations."""
        from codeledger.parser import regex_symbols
        names = {item.name for item in regex_symbols(self.PROSE_SOURCE)}
        for invented in ("the", "of", "Foo"):
            self.assertNotIn(invented, names, f"{invented!r} was mined out of a comment")

    def test_real_declarations_are_still_indexed(self):
        """The recall half. Precision that costs real symbols is not a fix.

        `start`, `stop`, `move` and `submit` are here on purpose: they appeared
        in a scope warning during the reported session, but they are ordinary
        object methods and filtering them would hide a scope bug behind a
        parser regression.
        """
        from codeledger.parser import regex_symbols
        source = """
export function CheckoutPanel(props) { return null; }
export const useCheckout = () => { return 1; };
export const PaymentProvider = ({ children }) => children;
class WidgetService {
  start() { return 1; }
  stop() { return 2; }
  move(delta) { return delta; }
  submit(event) { return event; }
}
export interface CheckoutProps { total: number }
"""
        names = {item.name for item in regex_symbols(source)}
        for expected in ("CheckoutPanel", "useCheckout", "PaymentProvider", "WidgetService",
                         "start", "stop", "move", "submit", "CheckoutProps"):
            self.assertIn(expected, names, f"{expected!r} was lost to the precision fix")

    def test_the_shallow_provider_stays_low_confidence(self):
        """Precision improves; the honesty about coverage does not change."""
        from codeledger.providers import RegexProvider, SHALLOW
        self.assertEqual(RegexProvider.coverage, SHALLOW)


class TreeSitterSymbolPrecisionTests(unittest.TestCase):
    """Grammar vocabulary is not a project symbol vocabulary."""

    def setUp(self):
        if not capabilities()["tree_sitter_installed"]:
            self.skipTest("grammars not installed: pip install 'code-ledger[languages]'")

    def analyze_text(self, name, text):
        from codeledger.providers import analyze
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / name
            path.write_text(text)
            symbols, _edges, provider, coverage = analyze(path, text)
        return {item.name for item in symbols}, provider, coverage

    SQL_SOURCE = """
CREATE OR REPLACE FUNCTION check_user(p_id int) RETURNS void AS $$
DECLARE
  v_email text;
  v_username text;
  column_name text;
  new_value text;
BEGIN
  SELECT email INTO v_email FROM users WHERE id = p_id;
  UPDATE audit SET column_name = new_value WHERE id = p_id;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE users (
  id serial PRIMARY KEY,
  email text NOT NULL
);

CREATE VIEW active_users AS SELECT * FROM users;
CREATE TRIGGER audit_users AFTER INSERT ON users
  FOR EACH ROW EXECUTE FUNCTION check_user();
"""

    def test_plpgsql_locals_and_columns_are_not_project_symbols(self):
        names, provider, _coverage = self.analyze_text("schema.sql", self.SQL_SOURCE)
        self.assertEqual(provider, "tree-sitter")
        for local in ("v_email", "v_username", "column_name", "new_value"):
            self.assertNotIn(local, names, f"{local!r} is a PL/pgSQL local, not a project symbol")
        for column in ("id", "email"):
            self.assertNotIn(column, names, f"{column!r} is a table column, not a project symbol")

    def test_sql_definitions_are_actually_discovered(self):
        """The recall half of the SQL fix: the real definitions were missing."""
        names, _provider, _coverage = self.analyze_text("schema.sql", self.SQL_SOURCE)
        for expected in ("check_user", "users", "active_users", "audit_users"):
            self.assertIn(expected, names, f"{expected!r} is a real SQL definition and must be indexed")

    def test_a_trigger_is_named_by_itself_not_by_its_table(self):
        names, _provider, _coverage = self.analyze_text(
            "trig.sql", "CREATE TRIGGER audit_users AFTER INSERT ON accounts\n"
                        "  FOR EACH ROW EXECUTE FUNCTION log_change();\n")
        self.assertIn("audit_users", names)

    def test_struct_fields_receivers_and_class_fields_are_not_symbols(self):
        go_names, _p, _c = self.analyze_text(
            "main.go", "package main\ntype Server struct { addr string }\n"
                       "func (s *Server) Start() error { return nil }\n")
        self.assertNotIn("addr", go_names, "a struct field is not a project symbol")
        self.assertNotIn("s", go_names, "a method receiver is not a project symbol")
        self.assertIn("Start", go_names); self.assertIn("Server", go_names)

        ts_names, _p, _c = self.analyze_text(
            "Store.ts", "export class Store {\n  private items: string[] = [];\n"
                        "  handleClick = () => { return 1; };\n  add(item: string) { this.items.push(item); }\n}\n")
        self.assertNotIn("items", ts_names, "a plain class field is not a project symbol")
        # A class property bound to a function is a real method by another name.
        self.assertIn("handleClick", ts_names)
        self.assertIn("add", ts_names); self.assertIn("Store", ts_names)

    def test_typescript_and_tsx_recall_is_unchanged(self):
        names, _provider, coverage = self.analyze_text("Checkout.tsx", """
import { useState } from 'react';
export interface CheckoutProps { total: number }
export function CheckoutPanel({ total }: CheckoutProps) {
  const [open, setOpen] = useState(false);
  if (total > 0) { track(total); }
  return <div onClick={() => setOpen(!open)}>{total}</div>;
}
export function useCheckout() { return useState(0); }
export const PaymentProvider = ({ children }) => children;
export default class Store { add(item) { return item; } }
""")
        self.assertEqual(coverage, "full")
        for expected in ("CheckoutProps", "CheckoutPanel", "useCheckout", "PaymentProvider", "Store", "add"):
            self.assertIn(expected, names, f"{expected!r} was lost")
        self.assertNotIn("if", names)

    def test_an_unsupported_construct_is_not_reported_as_full_coverage(self):
        """CREATE PROCEDURE is not in the SQL grammar; it must not read as parsed.

        Honesty over recall: the grammar produces an ERROR node here, so there
        is no structural evidence to extract. Inventing a symbol from the text
        would be exactly the fabrication this project exists to avoid.
        """
        names, _provider, coverage = self.analyze_text(
            "proc.sql", "CREATE PROCEDURE do_work(a int) LANGUAGE plpgsql AS $$ BEGIN END; $$;\n")
        self.assertNotIn("do_work", names)
        self.assertEqual(coverage, "shallow", "a failed parse must not claim full coverage")


class ScopeWarningQualityTests(unittest.TestCase):
    """A warning must mean scope risk, not lexical mismatch with the request.

    The reported session produced `unexpected_symbols` for symbols the plan had
    named, in files that were correct for the request. A guard that fires on
    ordinary work teaches an agent to ignore it, so these tests pin both halves:
    legitimate work stays quiet, and genuinely unrelated work still warns.
    """

    def build(self, directory):
        root = Path(directory)
        (root / "src" / "components").mkdir(parents=True)
        (root / "src" / "state").mkdir(parents=True)
        (root / "src" / "payments").mkdir(parents=True)
        (root / "src" / "App.jsx").write_text(
            "import { PeriodNav } from './components/PeriodNav';\n"
            "export function App() {\n  return PeriodNav();\n}\n")
        (root / "src" / "components" / "PeriodNav.jsx").write_text(
            "export function PeriodNav() {\n"
            "  function move(delta) { return delta; }\n"
            "  function start() { return 0; }\n"
            "  function stop() { return 1; }\n"
            "  function submit() { return 2; }\n"
            "  return move(1);\n}\n")
        (root / "src" / "state" / "drawerState.js").write_text(
            "export function openDrawer() { return true; }\n")
        (root / "src" / "payments" / "PaymentProvider.jsx").write_text(
            "export function PaymentProvider() { return null; }\n")
        ledger = Ledger(root); ledger.init()
        return ledger

    def test_a_legitimate_change_to_a_dependent_file_is_not_flagged(self):
        """The reproduced failure: App.jsx imports PeriodNav, so it is in scope.

        Structural evidence, not word matching — the dependency graph already
        knows App.jsx reaches PeriodNav, which is exactly what makes it a
        legitimate place for 'add period navigation' to land.
        """
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.build(directory)
            result = ledger.scope_check("Add period navigation",
                                        ["src/App.jsx", "src/components/PeriodNav.jsx"],
                                        ["App", "move", "PeriodNav"])
            self.assertEqual(result["status"], "SAFE", result)
            self.assertEqual(result["unexpected_files"], [])
            self.assertEqual(result["unexpected_symbols"], [])

    def test_implementation_details_inside_an_in_scope_file_are_not_violations(self):
        """A symbol is in scope because its file is, not because it was named."""
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.build(directory)
            result = ledger.scope_check("Add period navigation",
                                        ["src/components/PeriodNav.jsx"],
                                        ["move", "start", "stop", "submit", "storedLogYears"])
            self.assertEqual(result["status"], "SAFE", result)
            self.assertEqual(result["unexpected_symbols"], [])

    def test_symbols_named_in_the_plan_are_in_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.build(directory)
            result = ledger.scope_check("Add period navigation", ["src/state/drawerState.js"],
                                        ["openDrawer"], plan_files=["src/state/drawerState.js"],
                                        plan_symbols=["openDrawer"])
            self.assertEqual(result["status"], "SAFE", result)

    def test_a_genuinely_unrelated_file_still_warns(self):
        """The true positive this guard exists for must survive the fix."""
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.build(directory)
            result = ledger.scope_check("Add period navigation",
                                        ["src/components/PeriodNav.jsx", "src/payments/PaymentProvider.jsx"],
                                        ["PeriodNav", "PaymentProvider"])
            self.assertEqual(result["status"], "WARNING", result)
            self.assertIn("src/payments/PaymentProvider.jsx", result["unexpected_files"])
            self.assertIn("PaymentProvider", result["unexpected_symbols"])
            # The in-scope half is not swept into the warning.
            self.assertNotIn("src/components/PeriodNav.jsx", result["unexpected_files"])
            self.assertNotIn("PeriodNav", result["unexpected_symbols"])

    def test_symbols_alone_can_never_produce_a_warning(self):
        """With every changed file inside the boundary there is no scope risk."""
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.build(directory)
            result = ledger.scope_check("Add period navigation", ["src/components/PeriodNav.jsx"],
                                        ["if", "catch", "totallyUnrelatedName"])
            self.assertEqual(result["unexpected_files"], [])
            self.assertEqual(result["status"], "SAFE",
                             "an empty unexpected_files must not yield WARNING")

    def test_generic_request_words_do_not_manufacture_a_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.build(directory)
            result = ledger.scope_check("update the code", ["src/components/PeriodNav.jsx"], ["PeriodNav"])
            self.assertIn(result["status"], ("SAFE", "UNKNOWN"))
            self.assertNotEqual(result["status"], "WARNING",
                                "generic wording is missing evidence, not a scope violation")

    def test_explicit_user_scope_is_respected(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.build(directory)
            result = ledger.scope_check("Only touch src/state/drawerState.js for this fix",
                                        ["src/state/drawerState.js"], ["openDrawer"])
            self.assertEqual(result["status"], "SAFE", result)

    def test_the_boundary_is_not_clipped_at_twenty_matches(self):
        """The presentation cap and the safety boundary are different numbers."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "src").mkdir()
            for index in range(40):
                (root / "src" / f"widget_{index}.py").write_text(f"def widget_handler_{index}():\n    return {index}\n")
            ledger = Ledger(root); ledger.init()
            changed = [f"src/widget_{index}.py" for index in range(40)]
            result = ledger.scope_check("update widget handler", changed, [])
            self.assertEqual(result["unexpected_files"], [],
                             "files beyond the 20th match were dropped from the boundary")
            self.assertGreater(len(result["allowed_files"]), 20)


class HandshakeValidationTests(unittest.TestCase):
    """A handshake that approves everything is worse than no handshake.

    Word overlap made 'fix payment calculation' and 'change payment animation'
    look like the same task, and a plan that contradicted an explicit user scope
    returned ALIGNED. These pin the checks to structural evidence the ledger
    already holds: named paths, relevant files, and the dependency graph.
    """

    def build(self, directory):
        root = Path(directory)
        (root / "src" / "payments").mkdir(parents=True)
        (root / "src" / "components").mkdir(parents=True)
        (root / "src" / "payments" / "calc.js").write_text(
            "export function calculatePaymentTotal(items) { return items.length; }\n")
        (root / "src" / "payments" / "anim.js").write_text(
            "export function animatePayment(element) { return element; }\n")
        (root / "src" / "components" / "PeriodNav.jsx").write_text(
            "export function PeriodNav() { return null; }\n")
        (root / "src" / "components" / "Unrelated.jsx").write_text(
            "export function Unrelated() { return null; }\n")
        ledger = Ledger(root); ledger.init()
        return ledger

    def test_a_plan_that_violates_an_explicit_user_scope_warns(self):
        """The user named the file. The plan touches two others."""
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.build(directory)
            result = ledger.handshake(
                "Only change src/payments/calc.js. Fix payment calculation.",
                "Modify src/components/PeriodNav.jsx and src/components/Unrelated.jsx")
            self.assertEqual(result["status"], "WARNING", result)
            self.assertTrue(result["scope_violations"], result)

    def test_a_plan_outside_the_relevant_scope_warns(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.build(directory)
            result = ledger.handshake(
                "Fix payment calculation",
                "Rewrite the entire authentication layer in src/components/Unrelated.jsx")
            self.assertEqual(result["status"], "WARNING", result)

    def test_a_relevant_plan_is_aligned(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.build(directory)
            result = ledger.handshake(
                "Fix payment calculation",
                "Modify calculatePaymentTotal in src/payments/calc.js so it rounds correctly")
            self.assertEqual(result["status"], "ALIGNED", result)
            self.assertEqual(result["scope_violations"], [])

    def test_a_plan_naming_no_paths_is_not_punished_for_it(self):
        """Warn on a path the plan names and gets wrong, never on silence."""
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.build(directory)
            result = ledger.handshake("Fix payment calculation",
                                      "Round the total to two decimal places before returning it")
            self.assertEqual(result["scope_violations"], [])
            self.assertNotEqual(result["status"], "WARNING", result)

    def test_no_indexed_evidence_is_not_reported_as_alignment(self):
        """Absence of evidence must not read as agreement."""
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.build(directory)
            result = ledger.handshake("Recalibrate the flux capacitor telemetry",
                                      "Adjust the telemetry sampling interval")
            self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE", result)
            self.assertIn("evidence", result["message"].lower())

    def test_the_handshake_still_scans_nothing(self):
        """Structural checks must stay indexed-only."""
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.build(directory)
            visited = []
            original = Ledger._discover
            try:
                Ledger._discover = lambda self, verbose=False: (visited.append(1), original(self, verbose))[1]
                ledger.handshake("Fix payment calculation",
                                 "Modify calculatePaymentTotal in src/payments/calc.js")
            finally:
                Ledger._discover = original
            self.assertEqual(visited, [], "handshake walked the repository")

    def test_duplicate_implementation_reuse_is_still_surfaced(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.build(directory)
            result = ledger.handshake(
                "payment calculation",
                "Create a new PaymentTotalWidget component in src/payments/calc.js")
            self.assertIsNotNone(result["duplicate_implementation"], result)


class HandshakeEvidenceBoundaryTests(unittest.TestCase):
    """What the handshake still cannot do, written down rather than implied.

    The audit asked for the boundary of the evidence to be named instead of
    papered over. This test exists to record it honestly: if a later change
    makes CodeLedger able to tell these apart, this test should be updated to
    demand the better answer, not deleted.
    """

    def test_two_plans_over_the_same_indexed_files_cannot_be_told_apart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "src" / "payments").mkdir(parents=True)
            (root / "src" / "payments" / "calc.js").write_text(
                "export function calculatePaymentTotal(items) { return items.length; }\n")
            (root / "src" / "payments" / "anim.js").write_text(
                "export function animatePayment(element) { return element; }\n")
            ledger = Ledger(root); ledger.init()
            result = ledger.handshake("Fix payment calculation",
                                      "Change payment animation timing in src/payments/anim.js")
            # Both files are indexed as relevant to "payment", and no structural
            # evidence separates "calculation" from "animation". CodeLedger does
            # not warn here, and must not pretend the plan was verified either.
            self.assertEqual(result["scope_violations"], [],
                             "anim.js is genuinely inside the indexed relevant scope")
            self.assertIn("src/payments/anim.js", result["plan_paths"],
                          "the path the plan named is reported so a human can judge it")


class DeletedSymbolConsistencyTests(unittest.TestCase):
    """One rule: a deleted symbol is a historical record, and reads like one.

    The reported contradiction was a symbol carrying `status='deleted'` next to
    a live line number and a current signature, so nothing downstream could tell
    a symbol that exists from one that used to. The chosen behaviour is to keep
    deleted symbols retrievable — `codeledger_find_symbol` documents that — but
    never to present their position as current.
    """

    def build(self, directory):
        root = Path(directory)
        (root / "drawer.py").write_text(
            "def openDrawer():\n    return True\n\ndef closeDrawer():\n    return False\n")
        ledger = Ledger(root); ledger.init()
        return ledger, root / "drawer.py"

    def test_a_deleted_symbol_never_presents_a_live_position(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, source = self.build(directory)
            live = ledger.lookup("openDrawer")[0]
            self.assertEqual(live["status"], "active")
            self.assertEqual(live["line_start"], 1)
            self.assertTrue(live["signature"])

            source.write_text("def closeDrawer():\n    return False\n")
            ledger.refresh()

            gone = ledger.lookup("openDrawer")[0]
            self.assertEqual(gone["status"], "deleted")
            self.assertIsNone(gone["line_start"], "a deleted symbol has no current line")
            self.assertIsNone(gone["line_end"])
            self.assertIsNone(gone["signature"], "a deleted symbol has no current signature")
            # The record is not destroyed, only relabelled.
            self.assertTrue(gone["deleted_at"])
            self.assertEqual(gone["historical"]["line_start"], 1)
            self.assertTrue(gone["historical"]["signature"])

    def test_recreating_a_symbol_revives_it_instead_of_duplicating(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, source = self.build(directory)
            original_id = ledger.lookup("openDrawer")[0]["id"]

            source.write_text("def closeDrawer():\n    return False\n"); ledger.refresh()
            self.assertEqual(ledger.lookup("openDrawer")[0]["status"], "deleted")

            source.write_text("def openDrawer():\n    return True\n\ndef closeDrawer():\n    return False\n")
            ledger.refresh()

            rows = ledger.lookup("openDrawer")
            self.assertEqual(len(rows), 1, f"recreation duplicated the row: {rows}")
            self.assertEqual(rows[0]["status"], "active")
            self.assertEqual(rows[0]["id"], original_id, "history was re-pointed at a new row")
            self.assertIsNone(rows[0]["deleted_at"], "stale deletion metadata leaked into a live symbol")
            self.assertEqual(rows[0]["line_start"], 1)
            self.assertNotIn("historical", rows[0])

    def test_impact_does_not_count_a_deleted_symbol_as_live(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, source = self.build(directory)
            source.write_text("def closeDrawer():\n    return False\n"); ledger.refresh()
            impact = ledger.impact("openDrawer", fallback=False)
            self.assertEqual(impact["defining_files"], [],
                             "a deleted symbol's file was reported as a live defining file")
            self.assertTrue(impact["historical_symbols"], "the deleted symbol should still be retrievable")
            self.assertEqual(impact["symbols"], [])

    def test_context_does_not_offer_a_deleted_symbol_as_current(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, source = self.build(directory)
            source.write_text("def closeDrawer():\n    return False\n"); ledger.refresh()
            context = ledger.context("openDrawer")
            for symbol in context["symbols"]:
                if symbol["name"] == "openDrawer":
                    self.assertEqual(symbol["status"], "deleted")
                    self.assertIsNone(symbol["line_start"])

    def test_a_file_deletion_retires_its_symbols_the_same_way(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, source = self.build(directory)
            source.unlink(); ledger.refresh()
            gone = ledger.lookup("openDrawer")[0]
            self.assertEqual(gone["status"], "deleted")
            self.assertIsNone(gone["line_start"])
            self.assertEqual(gone["historical"]["line_start"], 1)


class ColdStartPayloadTests(unittest.TestCase):
    """A cold start must cost the same on a large repository as a small one.

    `_resume_without_checkpoint` capped its own file and symbol lists at 20, but
    embedded change records from `since()` whose per-change lists were unbounded
    — so one reindexing change carrying 300 paths was returned in full to say
    'there is no checkpoint'.
    """

    def project(self, root: Path, modules: int):
        (root / "src").mkdir()
        for index in range(modules):
            (root / "src" / f"module_{index}.py").write_text(f"def function_{index}():\n    return {index}\n")
        ledger = Ledger(root); ledger.init()
        # One change touching every file, which is what an initial index or a
        # broad refactor actually produces.
        for index in range(modules):
            (root / "src" / f"module_{index}.py").write_text(f"def function_{index}():\n    return {index + 1}\n")
        ledger.refresh(True, agent="claude-code", request="renumber every module")
        return ledger

    def payload(self, value) -> int:
        return len(json.dumps(value, default=str))

    def test_cold_start_does_not_return_a_repository_file_list(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.project(Path(directory), 120)
            resumed = ledger.resume("fix function_7")
            self.assertEqual(resumed["status"], "NO_CHECKPOINTS_RECENT_WORK_FOUND")
            for entry in resumed["recent_work"]:
                self.assertLessEqual(len(entry["files"]), 20,
                                     "a change record dumped its whole file list")
                self.assertLessEqual(len(entry["symbols"]), 20)
            # Nothing is hidden: the totals are still reported.
            self.assertEqual(resumed["recent_work"][0]["files_total"], 120)

    def test_cold_start_payload_does_not_grow_with_the_repository(self):
        sizes = []
        for modules in (60, 240):
            with tempfile.TemporaryDirectory() as directory:
                ledger = self.project(Path(directory), modules)
                sizes.append(self.payload(ledger.resume("fix function_7")))
        small, large = sizes
        self.assertLess(large, small * 1.5,
                        f"cold start grew with repository size: {small} -> {large} chars")
        self.assertLess(large, 6000, f"cold start payload is {large} chars")

    def test_cold_start_still_says_what_recently_happened(self):
        """Bounded is not empty. The orienting facts must survive."""
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.project(Path(directory), 120)
            resumed = ledger.resume("fix function_7")
            entry = resumed["recent_work"][0]
            self.assertEqual(entry["request"], "renumber every module")
            self.assertEqual(entry["agent"], "claude-code")
            self.assertTrue(entry["files"], "a truncated list is not an empty one")
            self.assertTrue(resumed["guidance"])
            self.assertIn("files_in_repository", resumed["efficiency"])

    def test_since_reports_totals_alongside_its_bounded_lists(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.project(Path(directory), 120)
            moved = ledger.since(limit=10)
            change = moved["changes"][0]
            self.assertLessEqual(len(change["files"]), 20)
            self.assertEqual(change["files_total"], 120)
            self.assertTrue(change["files_truncated"])

    def test_a_small_change_is_not_truncated_at_all(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "src").mkdir()
            (root / "src" / "one.py").write_text("def one():\n    return 1\n")
            ledger = Ledger(root); ledger.init()
            (root / "src" / "one.py").write_text("def one():\n    return 2\n")
            ledger.refresh(True, agent="codex", request="bump one")
            change = ledger.since(limit=5)["changes"][0]
            self.assertEqual(change["files"], ["src/one.py"])
            self.assertFalse(change["files_truncated"])


class RequestPropagationTests(unittest.TestCase):
    """The request is the memory. Propagate it where it is known; never invent it.

    A change recorded as request 'NOT RECORDED' by agent 'unknown' while a live
    session in the same database held both is a propagation failure, not missing
    information. Where the information genuinely does not exist, UNKNOWN stays.
    """

    def project(self, root: Path):
        (root / "app.py").write_text("def run():\n    return 1\n")
        ledger = Ledger(root); ledger.init()
        return ledger

    def edit(self, root: Path, value: int):
        (root / "app.py").write_text(f"def run():\n    return {value}\n")

    def test_a_refresh_recovers_the_request_but_never_the_author(self):
        """The task is knowable from session state. Who typed the command is not.

        A live session proves work is underway, not that this process is the one
        doing it — a developer refreshing in a second terminal looks identical.
        So the request is attached and authorship stays UNKNOWN.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = self.project(root)
            ledger.start_session("claude-code", "Add period navigation")
            self.edit(root, 2)
            result = ledger.refresh(True)          # the CLI defaults: no agent, no session, no request
            self.assertEqual(result["agent"], "unknown", "a live session is not proof of authorship")
            self.assertEqual(result["attribution"]["confidence"], "UNKNOWN")
            row = ledger.db.execute("SELECT agent,user_request,session_id FROM changes WHERE id=?",
                                    (result["change_id"],)).fetchone()
            self.assertEqual(row["agent"], "unknown")
            self.assertEqual(row["user_request"], "Add period navigation",
                             "the request was recoverable and was thrown away")
            self.assertTrue(row["session_id"], "the change was not linked to the session it happened in")
            self.assertEqual(ledger.since(limit=1)["changes"][0]["request"], "Add period navigation")
            # The reason must say plainly that the task was inherited and the
            # author was not, so the two are never read as one claim.
            self.assertIn("does not prove who ran", result["attribution"]["reason"])

    def test_an_explicit_agent_always_wins_over_inheritance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = self.project(root)
            ledger.start_session("claude-code", "Add period navigation")
            self.edit(root, 3)
            result = ledger.refresh(True, agent="codex", request="Fix the totals")
            self.assertEqual(result["agent"], "codex")
            self.assertEqual(result["attribution"]["confidence"], "HIGH")
            row = ledger.db.execute("SELECT user_request FROM changes WHERE id=?", (result["change_id"],)).fetchone()
            self.assertEqual(row["user_request"], "Fix the totals")

    def test_two_live_sessions_leave_authorship_unknown(self):
        """Ambiguous evidence is not evidence. Nothing is guessed."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = self.project(root)
            ledger.start_session("claude-code", "Add period navigation")
            ledger.start_session("codex", "Fix the totals")
            self.edit(root, 4)
            result = ledger.refresh(True)
            self.assertEqual(result["agent"], "unknown")
            self.assertEqual(result["attribution"]["confidence"], "UNKNOWN")
            row = ledger.db.execute("SELECT user_request FROM changes WHERE id=?", (result["change_id"],)).fetchone()
            self.assertIn(row["user_request"], (None, ""))

    def test_no_live_session_stays_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = self.project(root)
            self.edit(root, 5)
            result = ledger.refresh(True)
            self.assertEqual(result["agent"], "unknown")
            self.assertEqual(result["attribution"]["confidence"], "UNKNOWN")

    def test_the_watcher_never_inherits_a_name(self):
        """An observed edit cannot be attributed, however many sessions are live.

        The filesystem records that a file changed, not which process changed
        it. This is the rule the whole attribution model rests on.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = self.project(root)
            ledger.start_session("claude-code", "Add period navigation")
            self.edit(root, 6)
            result = ledger.refresh(True, observed=True)
            self.assertEqual(result["agent"], "unknown")
            self.assertEqual(result["attribution"]["confidence"], "LOW")
            self.assertEqual(result["attribution"]["source"], "filesystem-watcher")
            row = ledger.db.execute("SELECT user_request FROM changes WHERE id=?", (result["change_id"],)).fetchone()
            self.assertIn(row["user_request"], (None, ""))

    def test_a_session_with_no_request_yields_no_request(self):
        """Inherit what is recorded; do not manufacture the rest."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ledger = self.project(root)
            ledger.start_session("claude-code")          # started without a request
            self.edit(root, 7)
            result = ledger.refresh(True)
            self.assertEqual(result["agent"], "unknown")
            row = ledger.db.execute("SELECT user_request FROM changes WHERE id=?", (result["change_id"],)).fetchone()
            self.assertIn(row["user_request"], (None, ""), "a request was invented")


class McpAttributionTests(unittest.TestCase):
    """The MCP server knows who it is talking to; the record should say so."""

    def test_refresh_falls_back_to_the_agent_from_the_handshake(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "app.py").write_text("def run():\n    return 1\n")
            Ledger(root).init()
            (root / "app.py").write_text("def run():\n    return 2\n")
            requests = "\n".join(json.dumps(message) for message in [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"clientInfo": {"name": "claude-code"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "codeledger_refresh", "arguments": {"request": "Fix the run total"}}},
            ])
            completed = subprocess.run([sys.executable, "-m", "codeledger.cli", "mcp", "--root", str(root)],
                                       input=requests, text=True, capture_output=True,
                                       cwd=Path(__file__).resolve().parent.parent)
            payloads = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
            refresh = payloads[-1]["result"]["structuredContent"]
            self.assertEqual(refresh["agent"], "claude-code",
                             "the server started the session as claude-code and then recorded 'unknown'")
            row = Ledger(root).db.execute(
                "SELECT agent,user_request FROM changes ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(row["agent"], "claude-code")
            self.assertEqual(row["user_request"], "Fix the run total")


class IndexedOnlyTraversalTests(unittest.TestCase):
    """Pre-change intelligence answers from the index, not from the disk.

    Discover once, retrieve later. These pin the traversal budget of each
    agent-facing call so a future change cannot quietly reintroduce a
    repository walk into the hot path.
    """

    def project(self, root: Path):
        (root / "src").mkdir()
        for index in range(40):
            (root / "src" / f"module_{index}.py").write_text(f"def function_{index}():\n    return {index}\n")
        (root / "src" / "checkout.py").write_text("def CheckoutPanel():\n    return 1\n")
        ledger = Ledger(root); ledger.init()
        return ledger

    def walks(self, ledger, call) -> int:
        counter = []
        original = Ledger._discover
        try:
            Ledger._discover = lambda self, verbose=False: (counter.append(1), original(self, verbose))[1]
            call()
        finally:
            Ledger._discover = original
        return len(counter)

    def test_the_indexed_only_calls_never_walk_the_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.project(Path(directory))
            for label, call in (
                ("context", lambda: ledger.context("fix the checkout panel")),
                ("handshake", lambda: ledger.handshake("fix the checkout panel", "edit src/checkout.py")),
                ("resume", lambda: ledger.resume("fix the checkout panel")),
                ("scope_check", lambda: ledger.scope_check("fix the checkout panel", ["src/checkout.py"], ["CheckoutPanel"])),
                ("impact", lambda: ledger.impact("CheckoutPanel", fallback=False)),
                ("since", lambda: ledger.since(limit=10)),
            ):
                with self.subTest(call=label):
                    self.assertEqual(self.walks(ledger, call), 0, f"{label} walked the repository")

    def test_plan_walks_the_tree_exactly_once_for_test_suggestions(self):
        """A known, pre-existing cost, pinned rather than allowed to grow.

        `suggest_tests` reads the tree to find test files. That predates this
        work and is deliberately left alone; this test exists so the count
        cannot climb without somebody deciding it should.
        """
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.project(Path(directory))
            self.assertEqual(self.walks(ledger, lambda: ledger.plan("fix the checkout panel")), 1)


class AnalysisUpgradeTests(unittest.TestCase):
    """An index built before the extraction fixes upgrades itself, keeping history.

    No migration and no re-init: `files.analysis_version` already drives a
    reparse when the analyser changes, so bumping the stamp is what retires
    symbols like `if` and `v_email` from databases in the field.
    """

    def test_a_previous_release_index_retires_its_bad_symbols_on_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.js").write_text("export function save(payload) {\n  if (payload) { go(); }\n}\n")
            connection = db_module.connect(root)
            connection.execute(
                "INSERT INTO files(path,language,size,hash,mtime,mtime_ns,status,analysis_version,analysis_provider,coverage) "
                "VALUES('app.js','javascript',1,'stale',1,1,'current','regex:1','regex','shallow')")
            file_id = connection.execute("SELECT id FROM files WHERE path='app.js'").fetchone()[0]
            for name in ("save", "if"):
                connection.execute(
                    "INSERT INTO symbols(name,qualified_name,kind,file_id,line_start,line_end,signature,hash,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,1,1,'sig','h','active','t','t')", (name, name, "method", file_id))
            connection.execute("INSERT INTO changes(timestamp,agent,user_request,summary,risk,result) "
                               "VALUES('2020-01-01','codex','historical work','old change','LOW','verified')")
            connection.commit(); connection.close()

            ledger = Ledger(root)
            self.assertEqual({row["name"] for row in ledger.db.execute("SELECT name FROM symbols WHERE status='active'")},
                             {"save", "if"})
            ledger.refresh(True)

            active = {row["name"] for row in ledger.db.execute("SELECT name FROM symbols WHERE status='active'")}
            self.assertEqual(active, {"save"}, "the stale keyword symbol was not retired")
            retired = {row["name"] for row in ledger.db.execute("SELECT name FROM symbols WHERE status='deleted'")}
            self.assertIn("if", retired, "the record was destroyed rather than retired")
            # History is the product and survives untouched.
            kept = ledger.db.execute("SELECT agent,user_request FROM changes WHERE user_request='historical work'").fetchone()
            self.assertEqual(kept["agent"], "codex")
            self.assertEqual(ledger.doctor()["checks"]["schema"], "OK")
            self.assertEqual(ledger.doctor()["checks"]["migrations"], "OK")


class McpLaunchCommandTests(unittest.TestCase):
    """The registered command must be launchable by a process that is not us.

    The reported failure: `setup-agent` registered the bare name `codeledger`,
    which the agent resolves against its own PATH. A desktop app or an
    IDE-spawned agent does not inherit the shell that activated a project venv,
    so the recommended install layout was the one case the registration could
    not express — and it failed silently.
    """

    def test_the_registered_executable_is_never_a_bare_name(self):
        from codeledger.core import mcp_launch_command
        command = mcp_launch_command(Path("/tmp/project"))
        self.assertTrue(Path(command[0]).is_absolute(),
                        f"{command[0]!r} would be resolved against the agent's PATH")
        self.assertIn("--root", command)
        self.assertEqual(command[command.index("--root") + 1], "/tmp/project")

    def test_a_project_local_virtualenv_is_preferred(self):
        """The console script beside the running interpreter wins."""
        from codeledger import core
        with tempfile.TemporaryDirectory() as directory:
            venv_bin = Path(directory) / ".venv" / "bin"; venv_bin.mkdir(parents=True)
            script = venv_bin / ("codeledger.exe" if os.name == "nt" else "codeledger")
            script.write_text("#!/bin/sh\n"); script.chmod(0o755)
            original = sys.executable
            try:
                sys.executable = str(venv_bin / "python")
                command = core.mcp_launch_command(Path("/tmp/project"))
            finally:
                sys.executable = original
            self.assertEqual(command[0], str(script))

    def test_a_virtualenv_symlink_is_not_resolved_away(self):
        """`.venv/bin/python` symlinks to the system interpreter.

        Resolving it walks out of the virtualenv and loses the install being
        looked for — which is exactly what the first draft of this did, and what
        the doctor self-test caught.
        """
        from codeledger import core
        with tempfile.TemporaryDirectory() as directory:
            system_bin = Path(directory) / "usr" / "bin"; system_bin.mkdir(parents=True)
            (system_bin / "python3").write_text("#!/bin/sh\n")
            venv_bin = Path(directory) / ".venv" / "bin"; venv_bin.mkdir(parents=True)
            script = venv_bin / ("codeledger.exe" if os.name == "nt" else "codeledger")
            script.write_text("#!/bin/sh\n"); script.chmod(0o755)
            try:
                (venv_bin / "python").symlink_to(system_bin / "python3")
            except OSError:
                self.skipTest("symlinks unavailable")
            original = sys.executable
            try:
                sys.executable = str(venv_bin / "python")
                command = core.mcp_launch_command(Path("/tmp/project"))
            finally:
                sys.executable = original
            self.assertEqual(command[0], str(script),
                             "the venv symlink was resolved away and the system interpreter was chosen")

    def test_paths_containing_spaces_survive_as_argv(self):
        from codeledger.core import mcp_launch_command
        root = Path("/tmp/a project/with spaces")
        command = mcp_launch_command(root)
        self.assertEqual(command[command.index("--root") + 1], str(root),
                         "the root must stay one argv element, not be split on the space")

    def test_the_interpreter_fallback_needs_no_console_script(self):
        """With no script and nothing on PATH, `-m` still works."""
        from codeledger import core
        with tempfile.TemporaryDirectory() as directory:
            empty_bin = Path(directory) / "bin"; empty_bin.mkdir()
            original_exe, original_which = sys.executable, core.shutil.which
            try:
                sys.executable = str(empty_bin / "python")
                core.shutil.which = lambda name: None
                command = core.mcp_launch_command(Path("/tmp/project"))
            finally:
                sys.executable = original_exe; core.shutil.which = original_which
            self.assertEqual(command[:3], [str(empty_bin / "python"), "-m", "codeledger.cli"])

    def test_agent_config_quotes_only_what_needs_quoting(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory))
            config = ledger.agent_config("claude-code")
            self.assertIsInstance(config["launch_command"], list)
            self.assertNotIn(" -- codeledger mcp", config["command"],
                             "the bare-name form must not be emitted any more")
            for part in config["launch_command"]:
                if " " in part:
                    self.assertIn(f'"{part}"', config["command"])


class McpSelfTestTests(unittest.TestCase):
    """Doctor must prove an MCP client can connect, not that a file exists."""

    def test_a_real_handshake_succeeds_and_reports_the_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "a project with spaces"; root.mkdir()
            (root / "app.py").write_text("def run():\n    return 1\n")
            ledger = Ledger(root); ledger.init()
            result = ledger.mcp_selftest()
            self.assertEqual(result["status"], "OK", result)
            self.assertEqual(Path(result["reported_root"]), root.resolve())
            self.assertGreaterEqual(result["tools_exposed"], 20)
            self.assertEqual(result["stderr"], "")
            self.assertFalse(result["agent_environment_verified"],
                             "doctor must not claim to have verified the agent's own environment")

    def test_a_missing_executable_is_reported_as_a_launch_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "app.py").write_text("def run():\n    return 1\n")
            ledger = Ledger(root); ledger.init()
            ledger.mcp_command = lambda: ["/nonexistent/codeledger", "mcp", "--root", str(root)]
            result = ledger.mcp_selftest()
            self.assertEqual(result["status"], "LAUNCH_FAILED", result)
            self.assertIn("does not exist", result["detail"])

    def test_an_executable_that_is_not_codeledger_fails_to_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "app.py").write_text("def run():\n    return 1\n")
            ledger = Ledger(root); ledger.init()
            ledger.mcp_command = lambda: [sys.executable, "-c", "raise SystemExit(3)", "mcp", "--root", str(root)]
            result = ledger.mcp_selftest()
            self.assertEqual(result["status"], "LAUNCH_FAILED", result)

    def test_a_server_that_never_answers_is_a_handshake_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "app.py").write_text("def run():\n    return 1\n")
            ledger = Ledger(root); ledger.init()
            # Answers --version, then exits without speaking MCP.
            ledger.mcp_command = lambda: [
                sys.executable, "-c",
                "import sys\n"
                "if '--version' in sys.argv: print('0.0.0'); raise SystemExit(0)\n"
                "raise SystemExit(0)\n",
                "mcp", "--root", str(root)]
            result = ledger.mcp_selftest()
            self.assertEqual(result["status"], "HANDSHAKE_FAILED", result)

    def test_a_server_bound_to_another_project_is_a_root_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "intended"; root.mkdir()
            (root / "app.py").write_text("def run():\n    return 1\n")
            ledger = Ledger(root); ledger.init()
            elsewhere = Path(directory) / "elsewhere"; elsewhere.mkdir()
            (elsewhere / "other.py").write_text("def other():\n    return 2\n")
            Ledger(elsewhere).init()
            command = ledger.mcp_command()
            ledger.mcp_command = lambda: [*command[:command.index("--root")], "--root", str(elsewhere)]
            result = ledger.mcp_selftest()
            self.assertEqual(result["status"], "ROOT_MISMATCH", result)
            self.assertIn("different project", result["detail"])

    def test_a_reduced_tool_surface_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "app.py").write_text("def run():\n    return 1\n")
            ledger = Ledger(root); ledger.init()
            ledger.mcp_command = lambda: [
                sys.executable, "-c",
                "import sys, json\n"
                "if '--version' in sys.argv: print('0.0.0'); raise SystemExit(0)\n"
                "for line in sys.stdin:\n"
                "    m = json.loads(line)\n"
                "    if m.get('method') == 'initialize':\n"
                "        r = {'protocolVersion':'2024-11-05','serverInfo':{'name':'codeledger','version':'x',"
                f"        'root': {str(root)!r}, 'status':'READY'}}}}\n"
                "    else:\n"
                "        r = {'tools': [{'name': 'codeledger_get_context'}]}\n"
                "    print(json.dumps({'jsonrpc':'2.0','id':m.get('id'),'result':r}), flush=True)\n",
                "mcp", "--root", str(root)]
            result = ledger.mcp_selftest()
            self.assertEqual(result["status"], "TOOL_SURFACE_MISMATCH", result)
            self.assertTrue(result["missing_tools"])

    def test_doctor_surfaces_the_mcp_result_and_can_skip_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "app.py").write_text("def run():\n    return 1\n")
            ledger = Ledger(root); ledger.init()
            self.assertNotIn("mcp_server", ledger.doctor(check_mcp=False)["checks"])
            report = ledger.doctor()
            self.assertIn("mcp_server", report["checks"])
            self.assertTrue(report["checks"]["mcp_server"].startswith("OK"), report["checks"]["mcp_server"])

    def test_doctor_recommends_re_registration_when_mcp_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "app.py").write_text("def run():\n    return 1\n")
            ledger = Ledger(root); ledger.init()
            ledger.mcp_command = lambda: ["/nonexistent/codeledger", "mcp", "--root", str(root)]
            report = ledger.doctor()
            self.assertTrue(report["checks"]["mcp_server"].startswith("LAUNCH_FAILED"))
            self.assertTrue(any("setup-agent" in action for action in report["recommended_actions"]))


class McpRootSafetyTests(unittest.TestCase):
    """An MCP server must never invent a project by being started in one."""

    def run_server(self, cwd, args, calls=()):
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "x"}}}]
        messages += [{"jsonrpc": "2.0", "id": index + 2, "method": "tools/call",
                      "params": {"name": name, "arguments": {}}} for index, name in enumerate(calls)]
        completed = subprocess.run([sys.executable, "-m", "codeledger.cli", "mcp", *args],
                                   input="\n".join(json.dumps(m) for m in messages) + "\n",
                                   capture_output=True, text=True, cwd=cwd,
                                   env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)})
        return [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]

    def test_an_uninitialised_directory_is_reported_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            replies = self.run_server(directory, [], calls=["codeledger_get_context"])
            info = replies[0]["result"]["serverInfo"]
            self.assertEqual(info["status"], "NOT_INITIALISED")
            self.assertEqual(Path(info["root"]), Path(directory).resolve())
            self.assertEqual(replies[1]["result"]["structuredContent"]["status"], "NOT_INITIALISED")
            self.assertFalse((Path(directory) / ".ai").exists(),
                             "a ledger was created in a directory that was never initialised")

    def test_an_initialised_project_reports_its_root_in_the_handshake(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "a project with spaces"; root.mkdir()
            (root / "app.py").write_text("def run():\n    return 1\n")
            Ledger(root).init()
            elsewhere = Path(directory) / "elsewhere"; elsewhere.mkdir()
            replies = self.run_server(elsewhere, ["--root", str(root)])
            info = replies[0]["result"]["serverInfo"]
            self.assertEqual(info["status"], "READY")
            self.assertEqual(Path(info["root"]), root.resolve())
            self.assertGreaterEqual(info["indexed_files"], 1)
            self.assertFalse((elsewhere / ".ai").exists())


class VerificationInvalidationTests(unittest.TestCase):
    """A verification describes a code state, not a name.

    Reproduced before the fix: verify(symbol, PASSED) → edit the symbol →
    refresh → the symbol still advertised PASSED. That is the one field capable
    of talking an agent out of running the test that would catch a regression.
    """

    def project(self, directory):
        root = Path(directory)
        (root / "pay.py").write_text("def charge(amount):\n    return amount * 2\n\n\ndef refund(amount):\n    return amount\n")
        ledger = Ledger(root); ledger.init()
        return ledger, root / "pay.py"

    def symbol(self, ledger, name):
        return next(row for row in ledger.lookup(name) if row["name"] == name)

    def test_an_unchanged_symbol_keeps_its_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _source = self.project(directory)
            ledger.verify("symbol", "charge", "TEST", "PASSED", "3 tests")
            ledger.refresh(True)
            charge = self.symbol(ledger, "charge")
            self.assertEqual(charge["last_verified"], "PASSED")
            self.assertEqual(charge["verification"]["applicability"], "CURRENT")
            self.assertEqual(ledger.verification_state("symbol", "charge")["status"], "PASSED")

    def test_changing_the_verified_symbol_makes_the_evidence_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, source = self.project(directory)
            ledger.verify("symbol", "charge", "TEST", "PASSED", "3 tests")
            source.write_text("def charge(amount):\n    return amount * 3\n\n\ndef refund(amount):\n    return amount\n")
            ledger.refresh(True)

            charge = self.symbol(ledger, "charge")
            self.assertIsNone(charge["last_verified"], "a changed symbol still advertised a PASSED verification")
            state = charge["verification"]
            self.assertEqual(state["applicability"], "SUPERSEDED")
            self.assertEqual(state["status"], "UNKNOWN")
            self.assertEqual(state["result_recorded"], "PASSED", "the historical result must remain visible")
            self.assertIn("no longer exists", state["reason"])

    def test_changing_an_unrelated_symbol_leaves_the_verification_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, source = self.project(directory)
            ledger.verify("symbol", "charge", "TEST", "PASSED", "3 tests")
            source.write_text("def charge(amount):\n    return amount * 2\n\n\ndef refund(amount):\n    return amount + 1\n")
            ledger.refresh(True)
            self.assertEqual(self.symbol(ledger, "charge")["verification"]["applicability"], "CURRENT")
            self.assertEqual(self.symbol(ledger, "charge")["last_verified"], "PASSED")

    def test_a_comment_edit_does_not_invalidate_a_verification(self):
        """Staleness follows the code hash, which ignores comments and layout."""
        with tempfile.TemporaryDirectory() as directory:
            ledger, source = self.project(directory)
            ledger.verify("symbol", "charge", "TEST", "PASSED")
            source.write_text("def charge(amount):\n    # unchanged behaviour\n    return amount * 2\n\n\ndef refund(amount):\n    return amount\n")
            ledger.refresh(True)
            self.assertEqual(self.symbol(ledger, "charge")["verification"]["applicability"], "CURRENT")

    def test_history_is_preserved_when_evidence_goes_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, source = self.project(directory)
            ledger.verify("symbol", "charge", "TEST", "PASSED", "first run")
            source.write_text("def charge(amount):\n    return amount * 3\n\n\ndef refund(amount):\n    return amount\n")
            ledger.refresh(True)
            rows = ledger.db.execute("SELECT result,evidence FROM verifications WHERE subject_id='charge'").fetchall()
            self.assertEqual([row["result"] for row in rows], ["PASSED"], "the record was deleted rather than superseded")
            self.assertEqual(rows[0]["evidence"], "first run")
            self.assertEqual(ledger.verification_state("symbol", "charge")["history_count"], 1)

    def test_a_deleted_symbol_cannot_remain_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, source = self.project(directory)
            ledger.verify("symbol", "charge", "TEST", "PASSED")
            source.write_text("def refund(amount):\n    return amount\n")
            ledger.refresh(True)
            state = ledger.verification_state("symbol", "charge")
            self.assertEqual(state["applicability"], "SUPERSEDED")
            self.assertIn("deleted", state["reason"])

    def test_nothing_verified_stays_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _source = self.project(directory)
            state = ledger.verification_state("symbol", "refund")
            self.assertEqual(state["status"], "UNKNOWN")
            self.assertEqual(state["applicability"], "NONE")
            self.assertNotIn("verification", self.symbol(ledger, "refund"))

    def test_a_project_verification_is_superseded_by_any_later_change(self):
        """Unscopable evidence errs toward UNKNOWN, never toward PASSED."""
        with tempfile.TemporaryDirectory() as directory:
            ledger, source = self.project(directory)
            ledger.verify("project", "project", "TEST", "PASSED", "suite green")
            self.assertEqual(ledger.verification_state("project", "project")["status"], "PASSED")
            source.write_text("def charge(amount):\n    return amount * 9\n\n\ndef refund(amount):\n    return amount\n")
            ledger.refresh(True)
            state = ledger.verification_state("project", "project")
            self.assertEqual(state["status"], "UNKNOWN")
            self.assertEqual(state["result_recorded"], "PASSED")
            self.assertIn("cannot be scoped", state["reason"])

    def test_verify_reports_what_the_result_is_worth_now(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _source = self.project(directory)
            record = ledger.verify("symbol", "charge", "TEST", "PASSED")
            self.assertEqual(record["state"]["applicability"], "CURRENT")

    def test_a_legacy_last_verified_column_is_not_trusted(self):
        """A value with no dated evidence behind it cannot be dated against code."""
        with tempfile.TemporaryDirectory() as directory:
            ledger, _source = self.project(directory)
            ledger.db.execute("UPDATE symbols SET last_verified='PASSED' WHERE name='charge'")
            ledger.db.commit()
            charge = self.symbol(ledger, "charge")
            self.assertIsNone(charge["last_verified"])
            self.assertEqual(charge["verification"]["applicability"], "UNVERIFIABLE")


def git_project(root: Path, files: dict[str, str]):
    """A real repository with one commit, or a skip if git is unavailable."""
    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if shutil.which("git") is None:
        raise unittest.SkipTest("git is not available")
    root.mkdir(parents=True, exist_ok=True)
    if git("init", "-q").returncode != 0:
        raise unittest.SkipTest("git init failed")
    git("config", "user.email", "test@example.invalid"); git("config", "user.name", "Test")
    for name, text in files.items():
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
    git("add", "-A"); git("commit", "-q", "-m", "initial")
    return git


class VerificationProvenanceTests(unittest.TestCase):
    """A verification records the state it was measured against, or says it cannot.

    Timestamps alone answer "has this symbol changed since". They cannot answer
    "which code was this measured against" — that information is discarded the
    moment a command finishes and is not recoverable later. These four columns
    are the smallest thing that makes a verification a measurement rather than
    an undated claim.
    """

    def latest(self, ledger):
        return dict(ledger.db.execute("SELECT * FROM verifications ORDER BY id DESC LIMIT 1").fetchone())

    def test_a_successful_command_records_all_four_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            git_project(root, {"pay.py": "def charge(a):\n    return a * 2\n"})
            ledger = Ledger(root); ledger.init()
            record = ledger.verify_command("project", "project", "TEST", [sys.executable, "-c", "print('ok')"])
            self.assertEqual(record["result"], "PASSED")
            row = self.latest(ledger)
            self.assertEqual(len(row["git_commit"]), 40, "the commit under test was not recorded")
            self.assertEqual(json.loads(row["command"])[1:], ["-c", "print('ok')"])
            self.assertEqual(row["exit_code"], 0)
            self.assertIsNotNone(row["tree_dirty"])

    def test_a_failed_command_records_its_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            git_project(root, {"pay.py": "def charge(a):\n    return a * 2\n"})
            ledger = Ledger(root); ledger.init()
            record = ledger.verify_command("project", "project", "TEST", [sys.executable, "-c", "raise SystemExit(7)"])
            self.assertEqual(record["result"], "FAILED")
            row = self.latest(ledger)
            self.assertEqual(row["exit_code"], 7, "a non-zero exit code must be preserved, not flattened to FAILED")
            self.assertEqual(len(row["git_commit"]), 40)

    def test_a_clean_tree_is_recorded_as_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            git = git_project(root, {"pay.py": "def charge(a):\n    return a * 2\n"})
            # `codeledger init` writes protocol files, so commit them first.
            ledger = Ledger(root); ledger.init(); git("add", "-A"); git("commit", "-q", "-m", "ledger")
            ledger.verify("symbol", "charge", "TEST", "PASSED")
            self.assertEqual(self.latest(ledger)["tree_dirty"], 0)
            self.assertNotIn("caveat", ledger.verification_state("symbol", "charge"))

    def test_a_dirty_tree_is_recorded_and_caveated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            git_project(root, {"pay.py": "def charge(a):\n    return a * 2\n"})
            ledger = Ledger(root); ledger.init()      # leaves untracked files behind
            ledger.verify("symbol", "charge", "TEST", "PASSED")
            self.assertEqual(self.latest(ledger)["tree_dirty"], 1)
            state = ledger.verification_state("symbol", "charge")
            self.assertIn("caveat", state)
            self.assertIn("uncommitted", state["caveat"])
            # A dirty tree is a caveat on the provenance, not a verdict of its own.
            self.assertEqual(state["applicability"], "CURRENT")

    def test_a_project_without_git_is_not_punished_for_it(self):
        """Empty commit means 'no git here', which is not a provenance gap."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pay.py").write_text("def charge(a):\n    return a * 2\n")
            ledger = Ledger(root); ledger.init()
            ledger.verify("symbol", "charge", "TEST", "PASSED")
            self.assertEqual(self.latest(ledger)["git_commit"], "")
            self.assertIsNone(self.latest(ledger)["tree_dirty"])
            self.assertEqual(ledger.verification_state("symbol", "charge")["applicability"], "CURRENT")

    def test_a_legacy_row_without_provenance_is_unverifiable(self):
        """The pre-migration shape: a result with no commit behind it."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pay.py").write_text("def charge(a):\n    return a * 2\n")
            ledger = Ledger(root); ledger.init()
            ledger.db.execute(
                "INSERT INTO verifications(subject_type,subject_id,kind,result,evidence,recorded_at) "
                "VALUES('symbol','charge','TEST','PASSED','old run',?)", (datetime.now(timezone.utc).isoformat(),))
            ledger.db.commit()
            state = ledger.verification_state("symbol", "charge")
            self.assertEqual(state["applicability"], "UNVERIFIABLE")
            self.assertEqual(state["status"], "UNKNOWN")
            self.assertEqual(state["result_recorded"], "PASSED", "the historical result must stay readable")
            self.assertIn("provenance", state["reason"])
            symbol = next(row for row in ledger.lookup("charge") if row["name"] == "charge")
            self.assertIsNone(symbol["last_verified"])
            self.assertEqual(symbol["verification"]["applicability"], "UNVERIFIABLE")

    def test_a_changed_symbol_is_superseded_not_merely_unverifiable(self):
        """Proving it does not apply outranks being unable to prove it does."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pay.py"; source.write_text("def charge(a):\n    return a * 2\n")
            ledger = Ledger(root); ledger.init()
            ledger.db.execute(
                "INSERT INTO verifications(subject_type,subject_id,kind,result,evidence,recorded_at) "
                "VALUES('symbol','charge','TEST','PASSED','old run',?)", (datetime.now(timezone.utc).isoformat(),))
            ledger.db.commit()
            source.write_text("def charge(a):\n    return a * 5\n"); ledger.refresh(True)
            self.assertEqual(ledger.verification_state("symbol", "charge")["applicability"], "SUPERSEDED")

    def test_a_project_result_does_not_survive_a_new_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            git = git_project(root, {"pay.py": "def charge(a):\n    return a * 2\n"})
            ledger = Ledger(root); ledger.init()
            ledger.verify("project", "project", "BUILD", "PASSED", "build green")
            self.assertEqual(ledger.verification_state("project", "project")["status"], "PASSED")

            (root / "notes.md").write_text("unrelated\n")
            git("add", "-A"); git("commit", "-q", "-m", "second")
            state = ledger.verification_state("project", "project")
            self.assertEqual(state["applicability"], "SUPERSEDED")
            self.assertIn("does not carry across a commit", state["reason"])
            self.assertEqual(state["result_recorded"], "PASSED")

    def test_a_symbol_result_survives_an_unrelated_commit(self):
        """The content hash is sharper than the commit and decides for symbols."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            git = git_project(root, {"pay.py": "def charge(a):\n    return a * 2\n"})
            ledger = Ledger(root); ledger.init()
            ledger.verify("symbol", "charge", "TEST", "PASSED")
            (root / "notes.md").write_text("unrelated\n")
            git("add", "-A"); git("commit", "-q", "-m", "second")
            ledger.refresh(True)
            self.assertEqual(ledger.verification_state("symbol", "charge")["applicability"], "CURRENT")

    def test_a_file_edited_behind_the_index_is_unverifiable(self):
        """Checkout without a refresh: the index cannot speak for the disk."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pay.py"; source.write_text("def charge(a):\n    return a * 2\n")
            ledger = Ledger(root); ledger.init()
            ledger.verify("symbol", "charge", "TEST", "PASSED")
            self.assertEqual(ledger.verification_state("symbol", "charge")["applicability"], "CURRENT")
            time.sleep(0.01)
            source.write_text("def charge(a):\n    return a * 99\n")     # no refresh
            state = ledger.verification_state("symbol", "charge")
            self.assertEqual(state["applicability"], "UNVERIFIABLE")
            self.assertIn("changed on disk", state["reason"])

    def test_provenance_is_reported_alongside_the_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            git_project(root, {"pay.py": "def charge(a):\n    return a * 2\n"})
            ledger = Ledger(root); ledger.init()
            ledger.verify_command("project", "project", "TEST", [sys.executable, "-c", "print('ok')"])
            provenance = ledger.verification_state("project", "project")["provenance"]
            self.assertEqual(len(provenance["git_commit"]), 40)
            self.assertEqual(provenance["exit_code"], 0)
            self.assertEqual(provenance["command"][1:], ["-c", "print('ok')"])
            self.assertIsInstance(provenance["tree_dirty"], bool)

    def test_history_survives_the_migration_and_later_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pay.py").write_text("def charge(a):\n    return a * 2\n")
            ledger = Ledger(root); ledger.init()
            ledger.db.execute(
                "INSERT INTO verifications(subject_type,subject_id,kind,result,evidence,recorded_at) "
                "VALUES('symbol','charge','TEST','PASSED','the old run','2020-01-01T00:00:00+00:00')")
            ledger.db.commit()
            ledger.verify("symbol", "charge", "TEST", "FAILED", "the new run")
            rows = [dict(r) for r in ledger.db.execute("SELECT * FROM verifications ORDER BY id")]
            self.assertEqual([r["evidence"] for r in rows], ["the old run", "the new run"])
            self.assertIsNone(rows[0]["git_commit"], "a legacy row must not be given a fabricated commit")
            self.assertEqual(rows[1]["git_commit"], "")
            self.assertEqual(ledger.verification_state("symbol", "charge")["history_count"], 2)

    def test_evidence_kinds_stay_independent(self):
        """No pipeline: a passing test says nothing about a build or a deploy."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pay.py").write_text("def charge(a):\n    return a * 2\n")
            ledger = Ledger(root); ledger.init()
            ledger.verify("project", "project", "TEST", "PASSED", "suite green")
            self.assertEqual(ledger.verification_state("project", "project")["status"], "PASSED")
            # Nothing was recorded about any other subject, and nothing is implied.
            for subject in ("build", "deployment", "runtime"):
                state = ledger.verification_state("project", subject)
                self.assertEqual(state["status"], "UNKNOWN", f"{subject} was inferred from a passing test")
                self.assertEqual(state["applicability"], "NONE")


class ProvenanceMigrationTests(unittest.TestCase):
    """A database written before P0-E must upgrade without losing anything."""

    def test_a_pre_provenance_database_upgrades_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pay.py").write_text("def charge(a):\n    return a * 2\n")
            # Build the pre-P0-E table shape by hand, then let `connect` migrate.
            path = root / ".ai" / "codeledger" / "codeledger.db"; path.parent.mkdir(parents=True)
            legacy = sqlite3.connect(path)
            legacy.executescript(
                "CREATE TABLE verifications (id INTEGER PRIMARY KEY, subject_type TEXT NOT NULL, "
                "subject_id TEXT NOT NULL, kind TEXT NOT NULL, result TEXT NOT NULL, evidence TEXT, "
                "recorded_at TEXT NOT NULL);"
                "INSERT INTO verifications(subject_type,subject_id,kind,result,evidence,recorded_at) "
                "VALUES('project','project','TEST','PASSED','historical evidence','2020-01-01T00:00:00+00:00');")
            legacy.commit(); legacy.close()

            connection = db_module.connect(root)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(verifications)")}
            self.assertLessEqual({"git_commit", "command", "exit_code", "tree_dirty"}, columns)
            row = dict(connection.execute("SELECT * FROM verifications").fetchone())
            self.assertEqual(row["evidence"], "historical evidence")
            self.assertIsNone(row["git_commit"], "existing rows must not be backfilled")
            self.assertIsNone(row["command"]); self.assertIsNone(row["exit_code"]); self.assertIsNone(row["tree_dirty"])
            connection.close()

            ledger = Ledger(root)
            self.assertEqual(ledger.verification_state("project", "project")["applicability"], "UNVERIFIABLE")
            self.assertEqual(ledger.doctor(check_mcp=False)["checks"]["schema"], "OK")
            self.assertEqual(ledger.doctor(check_mcp=False)["checks"]["migrations"], "OK")

    def test_the_migration_is_additive_and_declared(self):
        entries = {(table, column) for table, column, _ in db_module.MIGRATIONS}
        self.assertLessEqual({("verifications", "git_commit"), ("verifications", "command"),
                              ("verifications", "exit_code"), ("verifications", "tree_dirty")}, entries)
        for table, column, statement in db_module.MIGRATIONS:
            if table == "verifications":
                self.assertTrue(statement.startswith("ALTER TABLE verifications ADD COLUMN"), statement)
                self.assertNotIn("NOT NULL", statement, "a NOT NULL column cannot be added to existing rows")


class VerificationSubjectTests(unittest.TestCase):
    """Non-code subjects are first-class, and are described as what they are.

    Widening the enum alone would have shipped an endpoint probe reporting
    "The symbol has not changed since it was verified" — confidently wrong
    prose in the one system whose value is not producing any.
    """

    def project(self, directory):
        root = Path(directory)
        (root / "pay.py").write_text("def charge(a):\n    return a * 2\n")
        ledger = Ledger(root); ledger.init()
        return ledger, root / "pay.py"

    def test_the_cli_accepts_non_code_subjects(self):
        parser = build_parser()
        for subject in ("symbol", "feature", "project", "endpoint", "deployment", "artifact"):
            with self.subTest(subject=subject):
                args = parser.parse_args(["verify", subject, "x", "TEST", "PASSED"])
                self.assertEqual(args.subject_type, subject)
        with self.assertRaises(SystemExit):
            parser.parse_args(["verify", "nonsense", "x", "TEST", "PASSED"])

    def test_a_current_endpoint_is_not_described_as_a_symbol(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _source = self.project(directory)
            ledger.verify("endpoint", "https://api.example.com/health", "RUNTIME_PROBE", "PASSED", '{"ok":true}')
            state = ledger.verification_state("endpoint", "https://api.example.com/health")
            self.assertEqual(state["applicability"], "CURRENT")
            self.assertNotIn("symbol", state["reason"].lower())
            self.assertIn("endpoint", state["reason"])

    def test_a_superseded_endpoint_is_not_described_as_changed_code(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, source = self.project(directory)
            ledger.verify("endpoint", "https://api.example.com/health", "RUNTIME_PROBE", "PASSED")
            source.write_text("def charge(a):\n    return a * 3\n"); ledger.refresh(True)
            state = ledger.verification_state("endpoint", "https://api.example.com/health")
            self.assertEqual(state["applicability"], "SUPERSEDED")
            self.assertNotIn("The endpoint changed", state["reason"])
            self.assertIn("source changed", state["reason"])

    def test_subject_wording_uses_the_right_article(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, source = self.project(directory)
            for subject in ("endpoint", "artifact", "deployment", "project"):
                with self.subTest(subject=subject):
                    ledger.verify(subject, f"subject-{subject}", "RUNTIME_PROBE", "PASSED")
                    # A distinct body each time, so the symbol genuinely changes
                    # and the SUPERSEDED wording is the one under test.
                    source.write_text(f"def charge(a):\n    return a * {abs(hash(subject)) % 997}\n")
                    ledger.refresh(True)
                    reason = ledger.verification_state(subject, f"subject-{subject}")["reason"]
                    expected = "An" if subject[0] in "aeiou" else "A"
                    wrong = "A" if expected == "An" else "An"
                    self.assertIn(f"{expected} {subject}-level", reason, reason)
                    self.assertNotIn(f"{wrong} {subject}-level", reason, f"ungrammatical article in: {reason}")

    def test_a_code_subject_keeps_its_original_wording(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, source = self.project(directory)
            ledger.verify("symbol", "charge", "TEST", "PASSED")
            self.assertIn("The symbol has not changed",
                          ledger.verification_state("symbol", "charge")["reason"])
            source.write_text("def charge(a):\n    return a * 7\n"); ledger.refresh(True)
            self.assertIn("The symbol changed at", ledger.verification_state("symbol", "charge")["reason"])

    def test_a_non_code_subject_cannot_pollute_symbol_presentation(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _source = self.project(directory)
            # Deliberately collide the subject_id with a real symbol name.
            ledger.verify("endpoint", "charge", "RUNTIME_PROBE", "PASSED")
            symbol = next(row for row in ledger.lookup("charge") if row["name"] == "charge")
            self.assertIsNone(symbol["last_verified"], "an endpoint result leaked onto a symbol")
            self.assertNotIn("verification", symbol)
            self.assertIsNone(ledger.db.execute(
                "SELECT last_verified FROM symbols WHERE name='charge'").fetchone()[0])

    def test_regressions_work_for_a_non_code_subject(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _source = self.project(directory)
            ledger.verify("endpoint", "https://api/health", "RUNTIME_PROBE", "PASSED")
            ledger.verify("endpoint", "https://api/health", "RUNTIME_PROBE", "FAILED", "503")
            found = ledger.regressions("endpoint", "https://api/health")
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["status"], "REGRESSION")


class VerificationExpiryTests(unittest.TestCase):
    """Evidence about a running system decays; evidence about code does not.

    Expiry is derived from `recorded_at` and a per-kind lifetime in Config.
    No column stores it, and no kind expires unless it is configured to.
    """

    def project(self, directory, ttl=None):
        root = Path(directory)
        (root / "pay.py").write_text("def charge(a):\n    return a * 2\n")
        ledger = Ledger(root); ledger.init()
        if ttl is not None:
            ledger.config.evidence_ttl_seconds = ttl
        return ledger, root / "pay.py"

    def age(self, ledger, subject_type, subject_id, seconds):
        """Simulate time passing with nothing changing, rather than sleeping.

        The code must stay *older* than the record: backdating only the record
        would make the untouched index look newer than the observation and
        supersede it, which is a different state from the one under test.
        """
        recorded = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        ledger.db.execute("UPDATE verifications SET recorded_at=? WHERE subject_type=? AND subject_id=?",
                          (recorded.isoformat(), subject_type, subject_id))
        older = (recorded - timedelta(hours=1)).isoformat()
        ledger.db.execute("UPDATE symbols SET updated_at=?", (older,))
        ledger.db.execute("UPDATE files SET last_analyzed=?", (older,))
        ledger.db.commit()

    def test_a_runtime_probe_expires_after_its_configured_lifetime(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _source = self.project(directory, {"RUNTIME_PROBE": 60})
            ledger.verify("endpoint", "https://api/health", "RUNTIME_PROBE", "PASSED")
            self.assertEqual(ledger.verification_state("endpoint", "https://api/health")["applicability"], "CURRENT")
            self.age(ledger, "endpoint", "https://api/health", 120)
            state = ledger.verification_state("endpoint", "https://api/health")
            self.assertEqual(state["applicability"], "EXPIRED")
            self.assertEqual(state["status"], "UNKNOWN")
            self.assertEqual(state["result_recorded"], "PASSED", "the recorded result must stay visible")
            self.assertIn("too old", state["reason"])

    def test_a_kind_with_no_configured_lifetime_never_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _source = self.project(directory, {"RUNTIME_PROBE": 60})
            ledger.verify("symbol", "charge", "TEST", "PASSED")
            self.age(ledger, "symbol", "charge", 86_400 * 365)
            self.assertEqual(ledger.verification_state("symbol", "charge")["applicability"], "CURRENT",
                             "code evidence must be invalidated by code, not by the clock")

    def test_the_default_config_expires_runtime_kinds_only(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _source = self.project(directory)
            self.assertIsNone(ledger.config.evidence_ttl("TEST"))
            self.assertIsNone(ledger.config.evidence_ttl("BUILD"))
            self.assertTrue(ledger.config.evidence_ttl("RUNTIME_PROBE"))
            self.assertEqual(ledger.config.evidence_ttl("runtime_probe"),
                             ledger.config.evidence_ttl("RUNTIME_PROBE"), "lookup must be case-insensitive")

    def test_precedence_superseded_beats_expired(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, source = self.project(directory, {"RUNTIME_PROBE": 60})
            ledger.verify("endpoint", "https://api/health", "RUNTIME_PROBE", "PASSED")
            self.age(ledger, "endpoint", "https://api/health", 120)
            source.write_text("def charge(a):\n    return a * 4\n"); ledger.refresh(True)
            self.assertEqual(ledger.verification_state("endpoint", "https://api/health")["applicability"],
                             "SUPERSEDED", "a demonstrated change outranks mere age")

    def test_precedence_unverifiable_beats_expired(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _source = self.project(directory, {"TEST": 60})
            ledger.db.execute(
                "INSERT INTO verifications(subject_type,subject_id,kind,result,evidence,recorded_at) "
                "VALUES('symbol','charge','TEST','PASSED','legacy',?)",
                ((datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(),))
            ledger.db.commit()
            self.age(ledger, "symbol", "charge", 120)
            self.assertEqual(ledger.verification_state("symbol", "charge")["applicability"], "UNVERIFIABLE",
                             "a provenance gap outranks age")

    def test_expiry_reaches_the_symbol_lookup_path(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _source = self.project(directory, {"TEST": 60})
            ledger.verify("symbol", "charge", "TEST", "PASSED")
            self.age(ledger, "symbol", "charge", 120)
            symbol = next(row for row in ledger.lookup("charge") if row["name"] == "charge")
            self.assertEqual(symbol["verification"]["applicability"], "EXPIRED")
            self.assertIsNone(symbol["last_verified"], "expired evidence must not read as current")


class EnvironmentDependencyTests(unittest.TestCase):
    """The repository can prove a reference. It can never prove production."""

    def test_python_environment_reads_are_extracted_structurally(self):
        from codeledger.parser import ast_edges
        source = ('import os\n'
                  'a = os.environ["DATABASE_URL"]\n'
                  'b = os.environ.get("SUPABASE_KEY")\n'
                  'c = os.getenv("HCAPTCHA_SECRET", "fallback")\n'
                  'd = os.environ[computed]\n'
                  'e = "DATABASE_URL"\n'
                  'f = {"NOT_AN_ENV_VAR": 1}\n')
        found = {target for _s, target, kind in ast_edges(source) if kind == "env"}
        self.assertLessEqual({"DATABASE_URL", "SUPABASE_KEY", "HCAPTCHA_SECRET"}, found)
        self.assertIn("<dynamic>", found, "a computed key is a real read with an unknowable name")
        self.assertNotIn("NOT_AN_ENV_VAR", found, "a dict key is not an environment read")

    def test_a_string_that_merely_names_a_variable_is_not_a_dependency(self):
        from codeledger.parser import ast_edges
        source = 'msg = "set DATABASE_URL before running"\nother = ["SUPABASE_KEY"]\n'
        self.assertEqual([t for _s, t, k in ast_edges(source) if k == "env"], [])

    def test_javascript_environment_reads_are_extracted_structurally(self):
        if not capabilities()["tree_sitter_installed"]:
            self.skipTest("grammars not installed")
        from codeledger.providers import analyze
        source = ('const url = import.meta.env.VITE_SUPABASE_URL;\n'
                  'const key = import.meta.env["VITE_SUPABASE_ANON_KEY"];\n'
                  'const mode = process.env.NODE_ENV;\n'
                  'const dyn = import.meta.env[chosen];\n'
                  'const nope = user.env.thing;\n'
                  'const s = "VITE_SUPABASE_URL is only a string";\n')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.tsx"; path.write_text(source)
            found = {t for _s, t, k in analyze(path, source)[1] if k == "env"}
        self.assertLessEqual({"VITE_SUPABASE_URL", "VITE_SUPABASE_ANON_KEY", "NODE_ENV"}, found)
        self.assertIn("<dynamic>", found)
        self.assertNotIn("thing", found, "an unrelated member access is not an environment read")

    def test_dotenv_files_are_never_read_or_indexed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text('import os\nKEY = os.environ["SUPABASE_KEY"]\n')
            (root / ".env").write_text("SUPABASE_KEY=super-secret-value\n")
            (root / ".env.production").write_text("SUPABASE_KEY=prod-secret\n")
            ledger = Ledger(root); ledger.init()
            indexed = {row["path"] for row in ledger.db.execute("SELECT path FROM files")}
            self.assertNotIn(".env", indexed); self.assertNotIn(".env.production", indexed)
            blob = " ".join(str(value) for table in ("files", "symbols", "dependencies")
                            for row in ledger.db.execute(f"SELECT * FROM {table}") for value in row)
            self.assertNotIn("super-secret-value", blob, "a secret value reached the database")
            self.assertNotIn("prod-secret", blob)
            # The *name* is still known, which is the whole point.
            self.assertIn("SUPABASE_KEY", blob)

    def test_the_inventory_separates_proven_from_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "src").mkdir()
            (root / "src" / "db.py").write_text(
                'import os\nimport psycopg2\nfrom .local import helper\n'
                'def connect():\n    return psycopg2.connect(os.environ["DATABASE_URL"])\n')
            (root / "src" / "local.py").write_text("def helper():\n    return 1\n")
            ledger = Ledger(root); ledger.init()
            report = ledger.external_dependencies(["src/db.py"])
            names = {item["name"] for item in report["environment_variables"]}
            self.assertIn("DATABASE_URL", names)
            self.assertTrue(all(item["evidence"] == "PROVEN" for item in report["environment_variables"]))
            packages = {item["name"] for item in report["external_packages"]}
            self.assertIn("psycopg2", packages)
            self.assertNotIn("local", packages, "a module defined in this project is not external")
            self.assertTrue(report["cannot_prove"], "the unknowns must be stated, not implied")
            self.assertTrue(any("cannot see" in item["why"] for item in report["cannot_prove"]))

    def test_a_source_reference_never_claims_production_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text('import os\nK = os.environ["SUPABASE_KEY"]\n')
            ledger = Ledger(root); ledger.init()
            report = ledger.external_dependencies(["app.py"])
            serialised = json.dumps(report).lower()
            for claim in ("is set", "exists in production", "configured correctly", "is available"):
                self.assertNotIn(claim, serialised, f"the inventory asserted {claim!r}")
            self.assertIn("UNKNOWN", json.dumps(report))

    def test_plan_surfaces_the_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "supabase.py").write_text(
                'import os\n\ndef supabase_connection():\n    return os.environ["VITE_SUPABASE_URL"]\n')
            ledger = Ledger(root); ledger.init()
            plan = ledger.plan("fix the supabase connection")
            self.assertIn("external_dependencies", plan)
            self.assertIn("VITE_SUPABASE_URL",
                          {item["name"] for item in plan["external_dependencies"]["environment_variables"]})


class DependencyKindIsolationTests(unittest.TestCase):
    """An environment variable is not a symbol, however alike the names look.

    `export const DATABASE_URL = process.env.DATABASE_URL` is ordinary code, and
    without kind filtering the env edge would drag every file that reads the
    variable into the blast radius of the constant.
    """

    def colliding_project(self, directory):
        # The colliding name must be a *symbol the index actually holds*, or
        # `impact` matches nothing and the test passes without exercising the
        # filter at all. A module-level assignment is not indexed as a symbol by
        # any provider, so the accessor is a function deliberately named after
        # the variable it wraps.
        root = Path(directory); (root / "src").mkdir()
        (root / "src" / "config.py").write_text(
            'import os\n\ndef DATABASE_URL():\n    return os.environ["DATABASE_URL"]\n')
        (root / "src" / "unrelated.py").write_text(
            'import os\n\ndef ping():\n    return os.environ["DATABASE_URL"]\n')
        ledger = Ledger(root); ledger.init()
        return ledger

    def test_env_edges_are_recorded_but_never_resolved_to_a_symbol(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.colliding_project(directory)
            rows = ledger.db.execute(
                "SELECT target_name,target_symbol_id FROM dependencies WHERE kind='env'").fetchall()
            self.assertTrue(rows, "environment reads were not recorded at all")
            for row in rows:
                self.assertIsNone(row["target_symbol_id"],
                                  "an env edge was linked to a same-named code symbol")

    def test_impact_ignores_environment_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.colliding_project(directory)
            report = ledger.impact("DATABASE_URL", fallback=False)
            self.assertTrue(report["symbols"], "the colliding symbol must be indexed for this to test anything")
            referencing = report["referencing_files"]
            self.assertNotIn("src/unrelated.py", referencing,
                             "an env read contaminated the blast radius of a code symbol")
            for row in ledger.impact("DATABASE_URL", fallback=False)["dependencies"]:
                self.assertNotEqual(row["kind"], "env")

    def test_the_task_boundary_ignores_environment_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.colliding_project(directory)
            result = ledger.scope_check("update DATABASE_URL handling", ["src/config.py"], ["DATABASE_URL"])
            self.assertNotIn("src/unrelated.py", result["allowed_files"],
                             "an env read widened the task boundary")

    def test_existing_import_edges_are_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "src").mkdir()
            (root / "src" / "a.py").write_text("import os\nimport psycopg2\nfrom b import helper\n")
            (root / "src" / "b.py").write_text("def helper():\n    return 1\n")
            ledger = Ledger(root); ledger.init()
            kinds = {row["kind"] for row in ledger.db.execute("SELECT DISTINCT kind FROM dependencies")}
            self.assertIn("imports", kinds)
            imported = {row["target_name"] for row in ledger.db.execute(
                "SELECT target_name FROM dependencies WHERE kind='imports'")}
            # `from b import helper` records the bound name, as it always has.
            self.assertLessEqual({"os", "psycopg2", "helper"}, imported)


class ExternalInventoryPresentationTests(unittest.TestCase):
    """The inventory has to reach a human, and has to be worth reading.

    `plan --json` carried this from the start; the default text output did not,
    so the half that answers "why might correct-looking code still fail?" was
    invisible unless you already knew to ask for JSON.
    """

    def python_project(self, directory):
        root = Path(directory); (root / "src").mkdir()
        (root / "src" / "db.py").write_text(
            'import os\nimport json\nimport psycopg2\n\n'
            'def supabase_connection():\n    return psycopg2.connect(os.environ["VITE_SUPABASE_URL"])\n')
        ledger = Ledger(root); ledger.init()
        return ledger

    def render(self, plan):
        import io, contextlib
        from codeledger.cli import emit_plan
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer): emit_plan(plan)
        return buffer.getvalue()

    def test_the_text_plan_shows_variables_and_the_unknowns(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.python_project(directory)
            output = self.render(ledger.plan("fix the supabase connection"))
            self.assertIn("VITE_SUPABASE_URL", output)
            self.assertIn("CANNOT PROVE", output, "the unknowns must be stated, not only the proven half")
            self.assertIn("cannot see", output)

    def test_the_text_plan_survives_a_plan_with_no_inventory(self):
        # `external_dependencies` is absent from plans recorded by older
        # releases, and emitting one must not crash on them.
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.python_project(directory)
            plan = ledger.plan("fix the supabase connection"); plan.pop("external_dependencies")
            self.assertIn("CODELEDGER PLAN", self.render(plan))

    def test_the_standard_library_is_not_an_external_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.python_project(directory)
            packages = {item["name"] for item in
                        ledger.external_dependencies(["src/db.py"])["external_packages"]}
            self.assertIn("psycopg2", packages)
            for builtin in ("os", "json"):
                self.assertNotIn(builtin, packages,
                                 "the standard library is always present and is never the missing piece")

    def test_javascript_imports_are_not_filtered_by_python_rules(self):
        """`sys.stdlib_module_names` describes this interpreter, not npm."""
        if not capabilities()["tree_sitter_installed"]:
            self.skipTest("grammars not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "src").mkdir()
            # Every one of these is a real npm package name that collides with
            # a Python standard-library module.
            (root / "src" / "app.jsx").write_text(
                'import types from "types";\nimport signal from "signal";\nimport copy from "copy";\n'
                'export function App() { return types; }\n')
            ledger = Ledger(root); ledger.init()
            packages = {item["name"] for item in
                        ledger.external_dependencies(["src/app.jsx"])["external_packages"]}
            self.assertLessEqual({"types", "signal", "copy"}, packages,
                                 "a real npm dependency was dropped by a Python-only rule")


class ModuleSpecifierTests(unittest.TestCase):
    """An external package is the module an import names, not the words in it.

    The import edge bag is a coarse `re.findall` over the statement text, which
    is fine for name-matching in the dependency graph and useless as an answer
    to "which packages does this need?" — it reported
    `import { createClient } from "@supabase/supabase-js"` as five packages
    including `from` and `js`. The module specifier is now read structurally.
    """

    def test_package_name_cuts_each_specifier_on_its_own_terms(self):
        from codeledger.parser import package_name
        self.assertEqual(package_name("@supabase/supabase-js"), "@supabase/supabase-js")
        self.assertEqual(package_name("@supabase/supabase-js/dist/module"), "@supabase/supabase-js")
        self.assertEqual(package_name("react-dom/client"), "react-dom")
        self.assertEqual(package_name("lodash.debounce"), "lodash.debounce", "a dot is legal in a package name")
        for local in ("./config", "../lib/x", "/etc/thing", "", "   "):
            self.assertIsNone(package_name(local), f"{local!r} is not a package")
        self.assertIsNone(package_name("https://deno.land/x/foo.ts"), "a URL import is not a package name")
        self.assertIsNone(package_name("@scope"), "a bare scope is not installable")

    def test_a_scoped_javascript_package_is_named_once_and_correctly(self):
        if not capabilities()["tree_sitter_installed"]:
            self.skipTest("grammars not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "src").mkdir()
            (root / "src" / "client.jsx").write_text(
                'import { createClient } from "@supabase/supabase-js";\n'
                'import React from "react";\n'
                'import { local } from "./local";\n'
                'export function client() { return createClient(React, local); }\n')
            (root / "src" / "local.js").write_text("export const local = 1;\n")
            ledger = Ledger(root); ledger.init()
            packages = {item["name"] for item in
                        ledger.external_dependencies(["src/client.jsx"])["external_packages"]}
            self.assertEqual(packages, {"@supabase/supabase-js", "react"})
            for noise in ("from", "import", "js", "supabase", "createClient", "React", "local"):
                self.assertNotIn(noise, packages, f"{noise!r} is not a package")

    def test_a_relative_python_import_is_not_a_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "src").mkdir()
            (root / "src" / "__init__.py").write_text("")
            (root / "src" / "db.py").write_text(
                "import psycopg2\nfrom .helpers import assist\nfrom os import path\n\n"
                "def go():\n    return psycopg2, assist, path\n")
            (root / "src" / "helpers.py").write_text("def assist():\n    return 1\n")
            ledger = Ledger(root); ledger.init()
            packages = {item["name"] for item in
                        ledger.external_dependencies(["src/db.py"])["external_packages"]}
            self.assertIn("psycopg2", packages)
            self.assertNotIn("helpers", packages, "a relative import is inside this project")
            self.assertNotIn("assist", packages, "a bound name is not a package")
            self.assertNotIn("os", packages, "`from os import path` depends on the standard library")

    def test_module_edges_are_never_resolved_to_a_same_named_symbol(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "src").mkdir()
            # A local symbol deliberately sharing a name with an imported package.
            # Edges resolve against the symbols that exist when the importing
            # file is processed, so the importer is named to sort *after* the
            # definition. Otherwise the symbol simply does not exist yet, the
            # edge resolves to NULL for that reason rather than by rule, and the
            # test passes without exercising anything.
            (root / "src" / "requests.py").write_text("def requests():\n    return 1\n")
            (root / "src" / "z_app.py").write_text("import requests\n\ndef go():\n    return requests\n")
            ledger = Ledger(root); ledger.init()
            rows = ledger.db.execute("SELECT target_symbol_id FROM dependencies WHERE kind='module'").fetchall()
            self.assertTrue(rows, "no module edge was recorded, so nothing was tested")
            self.assertTrue(ledger.db.execute("SELECT 1 FROM symbols WHERE name='requests'").fetchone(),
                            "the colliding symbol must exist for this to test anything")
            for row in rows:
                self.assertIsNone(row["target_symbol_id"], "a module edge was fused to a code symbol")

    def test_module_edges_stay_out_of_the_blast_radius(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "src").mkdir()
            (root / "src" / "core.py").write_text("import psycopg2\n\ndef psycopg2_helper():\n    return 1\n")
            (root / "src" / "other.py").write_text("import psycopg2\n\ndef ping():\n    return 2\n")
            ledger = Ledger(root); ledger.init()
            for row in ledger.impact("psycopg2_helper", fallback=False)["dependencies"]:
                self.assertNotIn(row["kind"], ("module", "env"))


class ExpiryEdgeCaseTests(unittest.TestCase):
    """Expiry is the weakest doubt, and must never be the only one that speaks."""

    def project(self, directory, ttl):
        root = Path(directory)
        (root / "pay.py").write_text("def charge(a):\n    return a * 2\n")
        ledger = Ledger(root); ledger.init()
        ledger.config.evidence_ttl_seconds = ttl
        return ledger

    def test_an_unreadable_timestamp_never_reports_current(self):
        """A configured lifetime makes age load-bearing; an unparseable
        `recorded_at` makes it unanswerable, and unanswerable is not CURRENT."""
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.project(directory, {"RUNTIME_PROBE": 60})
            ledger.verify("endpoint", "https://api/health", "RUNTIME_PROBE", "PASSED")
            ledger.db.execute("UPDATE verifications SET recorded_at='not-a-timestamp'")
            ledger.db.commit()
            state = ledger.verification_state("endpoint", "https://api/health")
            self.assertEqual(state["applicability"], "UNVERIFIABLE")
            self.assertEqual(state["status"], "UNKNOWN")
            self.assertEqual(state["result_recorded"], "PASSED", "the recorded result must stay visible")

    def test_a_zero_or_negative_lifetime_is_not_a_lifetime(self):
        """A lifetime of zero would expire evidence the instant it was recorded,
        so `Config` drops it at construction rather than honouring it."""
        from codeledger.config import Config
        def build(ttl): return Config(project_name="t", root=".", ignores=[], evidence_ttl_seconds=ttl)
        config = build({"RUNTIME_PROBE": 0, "DEPLOY": -1, "SMOKE": 30})
        self.assertIsNone(config.evidence_ttl("RUNTIME_PROBE"))
        self.assertIsNone(config.evidence_ttl("DEPLOY"))
        self.assertEqual(config.evidence_ttl("SMOKE"), 30, "a real lifetime still survives")
        self.assertIsNone(build({"JUNK": "soon"}).evidence_ttl("JUNK"),
                          "a non-numeric lifetime is not a lifetime")

    def test_a_zero_lifetime_does_not_expire_a_fresh_record(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.project(directory, {"RUNTIME_PROBE": 0})
            ledger.verify("endpoint", "https://api/health", "RUNTIME_PROBE", "PASSED")
            self.assertEqual(ledger.verification_state("endpoint", "https://api/health")["applicability"],
                             "CURRENT")

    def test_expiry_never_downgrades_a_failure_into_reassurance(self):
        """An expired FAILED must not read as though the failure went away."""
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.project(directory, {"RUNTIME_PROBE": 60})
            ledger.verify("endpoint", "https://api/health", "RUNTIME_PROBE", "FAILED", "503")
            recorded = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
            ledger.db.execute("UPDATE verifications SET recorded_at=?", (recorded,))
            older = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
            ledger.db.execute("UPDATE symbols SET updated_at=?", (older,))
            ledger.db.execute("UPDATE files SET last_analyzed=?", (older,))
            ledger.db.commit()
            state = ledger.verification_state("endpoint", "https://api/health")
            self.assertEqual(state["applicability"], "EXPIRED")
            self.assertEqual(state["status"], "UNKNOWN", "an expired result is unknown, not passing")
            self.assertEqual(state["result_recorded"], "FAILED")


class ExternalInventoryCorrectnessTests(unittest.TestCase):
    """The inventory names real things or says nothing. Every case here was a
    confident, wrong answer found by review before it was fixed."""

    def build(self, directory, files):
        root = Path(directory)
        for name, body in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
        ledger = Ledger(root); ledger.init()
        return ledger

    def test_a_top_level_module_is_not_an_external_package(self):
        """Paths are stored root-relative, so a top-level module has no `/` in
        its path. Matching only `%/name.%` reported the project's own modules
        as third-party dependencies."""
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.build(directory, {
                "app.py": "import helpers\nimport my_utils\nimport psycopg2\nimport pkg\n\n"
                          "def go():\n    return helpers, my_utils, psycopg2, pkg\n",
                "helpers.py": "def h():\n    return 1\n",
                "my_utils.py": "def u():\n    return 2\n",
                "pkg/__init__.py": "x = 1\n"})
            packages = {item["name"] for item in
                        ledger.external_dependencies(["app.py"])["external_packages"]}
            self.assertEqual(packages, {"psycopg2"})

    def test_an_underscore_in_a_module_name_is_not_a_wildcard(self):
        """`_` is a LIKE wildcard, so an unescaped `my_utils` matched
        `myXutils.py` and was wrongly dismissed as local."""
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.build(directory, {
                "app.py": "import my_utils\n\ndef go():\n    return my_utils\n",
                "myXutils.py": "def x():\n    return 1\n"})
            packages = {item["name"] for item in
                        ledger.external_dependencies(["app.py"])["external_packages"]}
            self.assertIn("my_utils", packages, "a same-shaped filename is not this module")

    def test_a_bound_name_is_never_reported_as_a_package(self):
        """The regex provider is the default install. `GENERIC_IMPORT` captures
        the bound name for JavaScript, so `import React from "react"` reported
        packages named `React` and, for `import x from "./local"`, `x`."""
        from codeledger.parser import regex_edges
        source = ('import React from "react";\nimport x from "./local";\n'
                  'import { a } from "@scope/pkg/sub";\nconst r = require("lodash");\n')
        found = {target for _s, target, kind in regex_edges(Path("a.jsx"), source, []) if kind == "module"}
        self.assertEqual(found, {"react", "@scope/pkg", "lodash"})
        for bound in ("React", "x", "a", "local"):
            self.assertNotIn(bound, found, f"{bound!r} is a bound name, not a package")

    def test_go_import_paths_are_not_collapsed_into_their_host(self):
        from codeledger.parser import package_name
        self.assertEqual(package_name("github.com/gin-gonic/gin", "go"), "github.com/gin-gonic/gin")
        self.assertEqual(package_name("github.com/gin-gonic/gin/binding", "go"), "github.com/gin-gonic/gin")
        self.assertEqual(package_name("net/http", "go"), "net/http", "the standard library keeps its full path")
        self.assertEqual(package_name("react-dom/client"), "react-dom", "npm is still cut at the first segment")

    def test_a_shallow_file_says_environment_reads_were_not_looked_for(self):
        """Without grammars there is no env extraction, and an empty list is
        'not looked for', not 'none found'."""
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.build(directory, {
                "src/a.jsx": 'const u = import.meta.env.VITE_URL;\nexport function go(){ return u; }\n'})
            ledger.db.execute("UPDATE files SET coverage='shallow'"); ledger.db.commit()
            report = ledger.external_dependencies(["src/a.jsx"])
            self.assertTrue(any("environment variables these files read" in item["what"]
                                for item in report["cannot_prove"]),
                            "a shallow analysis reported no environment reads without saying why")

    def test_a_truncated_inventory_carries_its_total(self):
        with tempfile.TemporaryDirectory() as directory:
            body = "import os\n" + "".join(f'v{i} = os.environ["VAR_{i:02d}"]\n' for i in range(20))
            ledger = self.build(directory, {"big.py": body})
            report = ledger.external_dependencies(["big.py"], limit=12)
            self.assertEqual(len(report["environment_variables"]), 12)
            self.assertEqual(report["environment_variables_total"], 20)
            self.assertTrue(report["environment_variables_truncated"])


class ConfigRobustnessTests(unittest.TestCase):
    """`load` promises a bad config degrades to defaults rather than breaking
    every command, and that has to survive a value of the wrong shape."""

    def config(self, ttl):
        from codeledger.config import Config
        return Config(project_name="t", root=".", ignores=[], evidence_ttl_seconds=ttl)

    def test_a_malformed_lifetime_map_does_not_break_every_command(self):
        for broken in (["RUNTIME_PROBE"], "3600", 5, True):
            with self.subTest(value=broken):
                config = self.config(broken)
                self.assertIsInstance(config.evidence_ttl_seconds, dict)
                self.assertTrue(config.evidence_ttl("RUNTIME_PROBE"), "it should fall back to the defaults")

    def test_a_lifetime_written_as_a_float_is_honoured(self):
        """JSON has one number type, so a lifetime can arrive as 3600.0.
        Dropping it silently would read as 'never expires'."""
        self.assertEqual(self.config({"RUNTIME_PROBE": 3600.5}).evidence_ttl("RUNTIME_PROBE"), 3600)
        self.assertEqual(self.config({"RUNTIME_PROBE": "900"}).evidence_ttl("RUNTIME_PROBE"), 900)
        self.assertEqual(self.config({"RUNTIME_PROBE": "3600.5"}).evidence_ttl("RUNTIME_PROBE"), 3600,
                         "a lifetime serialised as a decimal string is still a lifetime")

    def test_a_config_file_of_the_wrong_shape_still_loads(self):
        from codeledger.config import Config
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / ".ai" / "codeledger").mkdir(parents=True)
            (root / ".ai" / "codeledger" / "config.json").write_text(
                json.dumps({"evidence_ttl_seconds": ["RUNTIME_PROBE"]}))
            config = Config.load(root)
            self.assertTrue(config.evidence_ttl("RUNTIME_PROBE"))


class EnvironmentReadPrecisionTests(unittest.TestCase):
    """A read is recorded when the structure proves one, and not otherwise."""

    def envs(self, source, name="a.jsx"):
        from codeledger.providers import analyze
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / name; path.write_text(source)
            return {target for _s, target, kind in analyze(path, source)[1] if kind == "env"}

    def test_a_chained_access_is_one_read_not_a_dynamic_one(self):
        """`process.env.NODE_ENV.toLowerCase()` recorded NODE_ENV *and*
        `<dynamic>`, so `plan` claimed a runtime-computed key in code that
        computes nothing."""
        if not capabilities()["tree_sitter_installed"]: self.skipTest("grammars not installed")
        self.assertEqual(self.envs("const m = process.env.NODE_ENV.toLowerCase();\n"), {"NODE_ENV"})

    def test_an_identifier_that_merely_starts_with_the_prefix_is_not_a_read(self):
        if not capabilities()["tree_sitter_installed"]: self.skipTest("grammars not installed")
        self.assertEqual(self.envs("const z = process.environment.foo;\n"), set())

    def test_an_interpolated_key_is_unknown_not_a_fabricated_name(self):
        if not capabilities()["tree_sitter_installed"]: self.skipTest("grammars not installed")
        self.assertEqual(self.envs("const v = process.env[`${prefix}_URL`];\n"), {"<dynamic>"})
        self.assertEqual(self.envs('import os\nv = os.getenv(f"{p}_URL")\n', "a.py"), {"<dynamic>"},
                         "an f-string key must not become a variable literally named f\"{p}_URL\"")

    def test_the_ordinary_forms_still_resolve(self):
        if not capabilities()["tree_sitter_installed"]: self.skipTest("grammars not installed")
        self.assertEqual(
            self.envs('const a = import.meta.env.VITE_URL;\nconst b = process.env["A_B"];\n'
                      'const c = import.meta.env[chosen];\nconst s = "VITE_URL is a string";\n'),
            {"VITE_URL", "A_B", "<dynamic>"})


class EnvironmentEdgeUpgradeTests(unittest.TestCase):
    """An index built by the previous release must gain the new edges.

    `refresh --changed` skips any file whose analysis stamp is already current,
    so shipping new extraction without re-stamping leaves every existing project
    with no `env` or `module` edges at all — and the inventory then reports an
    empty list for code that plainly reads the environment. Nothing else catches
    it: the file was fully parsed, just by a version that did not look, so the
    coverage caveat stays silent and the answer reads as "needs nothing".
    """

    def test_an_index_from_the_previous_stamp_is_reparsed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text(
                'import os\nimport psycopg2\n\ndef connect():\n'
                '    return psycopg2.connect(os.environ["DATABASE_URL"])\n')
            ledger = Ledger(root); ledger.init()

            # Simulate the state a previous release leaves behind: the file is
            # indexed and stamped current, but carries none of the new edges.
            ledger.db.execute("DELETE FROM dependencies WHERE kind IN ('env','module')")
            ledger.db.execute("UPDATE files SET analysis_version='ast:2'")
            ledger.db.commit()
            self.assertEqual(
                [], ledger.db.execute("SELECT 1 FROM dependencies WHERE kind='env'").fetchall())

            ledger.refresh(changed_only=True)
            kinds = {row["kind"] for row in ledger.db.execute("SELECT DISTINCT kind FROM dependencies")}
            self.assertIn("env", kinds, "an upgraded index never gained environment edges")
            self.assertIn("module", kinds, "an upgraded index never gained module edges")
            report = ledger.external_dependencies(["app.py"])
            self.assertIn("DATABASE_URL", {i["name"] for i in report["environment_variables"]})
            self.assertIn("psycopg2", {i["name"] for i in report["external_packages"]})

    def test_the_upgrade_preserves_symbols_and_history(self):
        """Re-stamping reparses; it must not destroy what was already recorded."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text('import os\n\ndef connect():\n    return os.environ["DB"]\n')
            ledger = Ledger(root); ledger.init()
            before = ledger.db.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
            ledger.db.execute("UPDATE files SET analysis_version='ast:2'"); ledger.db.commit()
            ledger.refresh(changed_only=True)
            self.assertIn("connect", {row["name"] for row in ledger.db.execute(
                "SELECT name FROM symbols WHERE status='active'")}, "a live symbol was retired by the upgrade")
            self.assertGreaterEqual(ledger.db.execute("SELECT COUNT(*) FROM changes").fetchone()[0], before,
                                    "change history was lost")
