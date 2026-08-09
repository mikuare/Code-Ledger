import json
import re
import sqlite3
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
