import tempfile
import unittest
import os
import sys
from pathlib import Path
from codeledger.cli import build_parser
from codeledger.core import Ledger

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
            self.assertEqual(ledger.end_session(session["session_id"])["status"], "completed")

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
            self.assertEqual(result["source"], "index")
            self.assertEqual(result["referencing_files"], ["src/components/UserList.tsx"])
            kinds={(row["target_name"], row["kind"]) for row in result["dependencies"]}
            self.assertIn(("useAuth", "uses"), kinds)

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
               ["session", "start"], ["record", "summary"], ["mcp"], ["setup-agent", "codex"],
               ["agent-config", "codex"], ["setup-codex"], ["watch", "--max-interval", "5"],
               ["run", "--request", "task", "--", "echo"]]
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertEqual(parser.parse_args(argv).command, argv[0])
