from __future__ import annotations
import argparse, contextlib, json, os, signal, sys, subprocess, time
from pathlib import Path
from .core import Ledger
from . import __version__

def emit(value, as_json=False):
    if as_json: print(json.dumps(value, indent=2, default=str)); return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, list) and len(item) > 20:
                print(f"{key.replace('_',' ').title()}: {len(item)} items")
            else:
                print(f"{key.replace('_',' ').title()}: {item}")
    elif isinstance(value, list):
        for item in value: print(item if isinstance(item, str) else json.dumps(item, default=str))
    else: print(value)

@contextlib.contextmanager
def closing_session(ledger, session, result):
    """Close `session` however this process ends — short of being killed outright.

    One cleanup path serves the normal return, Ctrl+C, and `kill`/SIGTERM, so
    there is no second copy to drift. It cannot cover SIGKILL, a WSL shutdown or
    power loss, and it does not pretend to: those are caught afterwards by
    `reconcile_sessions`, which is why the heartbeat exists. Closing is
    idempotent, so the signal path and the exit path may both run.
    """
    if not session:
        yield; return
    previous = {}

    def finish(signum, _frame):
        ledger.end_session(session, f"{result} (signal {signum})")
        handler = previous.get(signum)
        # Restore the default disposition and re-raise, so the caller still sees
        # a process that died of the signal it was sent rather than exit 0.
        signal.signal(signum, handler if callable(handler) else signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for name in ("SIGTERM", "SIGHUP", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None: continue
        try:
            previous[sig] = signal.signal(sig, finish)
        except (ValueError, OSError):
            pass   # not the main thread, or unsupported on this platform
    try:
        yield
    except KeyboardInterrupt:
        pass
    finally:
        ledger.end_session(session, result)
        for sig, handler in previous.items():
            try: signal.signal(sig, handler)
            except (ValueError, OSError): pass

def emit_sessions(value, as_json=False):
    """Group sessions by what is true of them, not by row order.

    A flat dump of rows all reading `status=active` is how a session whose
    process died last week kept presenting itself as a working agent.
    """
    if as_json: print(json.dumps(value, indent=2, default=str)); return
    if not value["by_status"]: print("No sessions recorded."); return
    for status in ("active", "idle", "stale", "crashed", "ended", "completed", "failed", "unknown"):
        rows = value["by_status"].get(status)
        if not rows: continue
        print(f"{status.upper()}:")
        for row in rows:
            detail = f" — {row['status_reason']}" if row["status_reason"] and status not in ("active", "ended") else ""
            print(f"   {row['agent']} {row['session_id']} (pid={row['pid'] or 'unrecorded'}, last activity {row['last_activity_at']}){detail}")

def emit_doctor(value, as_json=False):
    if as_json: print(json.dumps(value, indent=2, default=str)); return
    print("CODELEDGER DOCTOR\n")
    width = max(len(name) for name in value["checks"])
    for name, outcome in value["checks"].items():
        print(f"{name.replace('_',' ').title():<{width + 2}} {outcome}")
    if value["stale_sessions"]:
        print("\nStale sessions:")
        for row in value["stale_sessions"]:
            print(f"   {row['agent']} {row['session_id']} — {row['status_reason']}")
    print("\nRecommended:")
    for action in value["recommended_actions"]: print(f"   {action}")

AGENT_CHOICES = ("codex", "claude-code", "gemini", "aider", "cursor")
SUBJECT_CHOICES = ("symbol", "feature", "project")

def build_parser():
    parser = argparse.ArgumentParser(prog="codeledger", description="Persistent project memory and change intelligence")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, help=None, json=True):
        """Register a subcommand. Most accept --json after the subcommand too."""
        cmd = sub.add_parser(name, help=help)
        if json:
            cmd.add_argument("--json", action="store_true", dest="as_json")
        return cmd

    def add_agent_flags(cmd, session_default=""):
        cmd.add_argument("--agent", default="unknown")
        cmd.add_argument("--session", default=session_default)
        cmd.add_argument("--request", default="")
        return cmd

    init_cmd = add("init", "Create the ledger and index the project")
    init_cmd.add_argument("--quick", action="store_true", help="Record file metadata only; skip semantic parsing")
    init_cmd.add_argument("--verbose", action="store_true")

    add("status", "Show index counts and staleness")

    refresh = add("refresh", "Reindex the project, optionally only what changed")
    refresh.add_argument("--changed", action="store_true")
    refresh.add_argument("--verbose", action="store_true")
    add_agent_flags(refresh)

    for name in ("lookup", "context", "history", "why", "restore-info"):
        add(name).add_argument("query")

    impact = add("impact", "Find dependents of a symbol")
    impact.add_argument("query")
    impact.add_argument("--scan", action="store_true", help="Also read every source file instead of trusting the index")

    scope = add("scope", "Check whether changed paths fit a task boundary")
    scope.add_argument("request")
    scope.add_argument("--files", nargs="*", default=[])
    scope.add_argument("--symbols", nargs="*", default=[])

    add("plan", "Generate pre-change intelligence").add_argument("request")
    add("progress", "Check whether prior attempts at this request achieved anything").add_argument("request")
    add("prompt", "Normalize a user request into an agent task brief").add_argument("request")

    handshake = add("handshake", "Compare the user request with the AI implementation plan")
    handshake.add_argument("request")
    handshake.add_argument("--ai-plan", default="")

    tests_cmd = add("tests", "Suggest tests affected by changed files/symbols")
    tests_cmd.add_argument("--files", nargs="*", default=[])
    tests_cmd.add_argument("--symbols", nargs="*", default=[])

    for name in ("changes", "issues", "decisions", "export", "doctor"):
        add(name)

    issue = add("issue", "Record or update a known issue")
    issue.add_argument("key"); issue.add_argument("title")
    issue.add_argument("--description", default="")
    issue.add_argument("--severity", default="MEDIUM")
    issue.add_argument("--status", default="OPEN")

    decision = add("decision", "Record or update an architecture decision")
    decision.add_argument("key"); decision.add_argument("title")
    decision.add_argument("--rationale", default="")
    decision.add_argument("--status", default="ACTIVE")

    feature = add("feature", "Record or update a feature")
    feature.add_argument("name")
    feature.add_argument("--description", default="")
    feature.add_argument("--status", default="UNKNOWN")

    add("features", "List features").add_argument("--infer", action="store_true")

    verify = add("verify", "Record a verification result")
    verify.add_argument("subject_type", choices=SUBJECT_CHOICES)
    verify.add_argument("subject_id"); verify.add_argument("kind"); verify.add_argument("result")
    verify.add_argument("--evidence", default="")

    verify_run = add("verify-run", "Run and record a verification command")
    verify_run.add_argument("subject_type", choices=SUBJECT_CHOICES)
    verify_run.add_argument("subject_id"); verify_run.add_argument("kind")
    verify_run.add_argument("--evidence", default="")
    verify_run.add_argument("verify_command", nargs=argparse.REMAINDER)

    regressions = add("regressions", "Find subjects that passed and later failed")
    regressions.add_argument("--subject-type", default=None)
    regressions.add_argument("--subject-id", default=None)

    add("git-import", "Import commit metadata").add_argument("--limit", type=int, default=200)

    session_cmd = add("session", "Start, end, list, reconcile, or summarize agent sessions")
    session_cmd.add_argument("action", choices=("start", "end", "list", "reconcile", "status"))
    session_cmd.add_argument("--agent", default="unknown")
    session_cmd.add_argument("--request", default="")
    session_cmd.add_argument("--session-id", default="")
    session_cmd.add_argument("--result", default="completed")

    record = add("record", "Record a change by hand", json=False)
    add_agent_flags(record, session_default="manual")
    record.add_argument("summary")
    record.add_argument("--result", default="unverified")

    add("mcp", "Serve the local stdio MCP server").add_argument("--root", type=Path, dest="mcp_root")

    add("setup-agent", "Register CodeLedger MCP with a supported agent").add_argument("agent", choices=AGENT_CHOICES)
    add("agent-config", "Show MCP setup template for an agent").add_argument("agent")
    add("setup-codex", "Register CodeLedger as a local Codex MCP server")

    watch = add("watch", "Record external edits as they happen")
    add_agent_flags(watch)
    watch.add_argument("--interval", type=float, default=2.0, help="Seconds between scans while changes are arriving")
    watch.add_argument("--max-interval", type=float, default=30.0, help="Longest idle backoff; 0 disables backoff")

    run = add("run", "Run an agent command with automatic CodeLedger lifecycle", json=False)
    run.add_argument("--agent", default="unknown")
    run.add_argument("--request", required=True)
    run.add_argument("--session", default="")
    run.add_argument("agent_command", nargs=argparse.REMAINDER)
    return parser

def main(argv=None):
    parser = build_parser()
    args=parser.parse_args(argv); ledger=Ledger(getattr(args, "mcp_root", None) or args.root)
    if args.command == "mcp":
        from .mcp import serve
        serve(ledger.root); return
    if args.command == "setup-codex":
        import shutil
        ledger._write_instructions()
        codex = shutil.which("codex")
        command = ["codex", "mcp", "add", "codeledger", "--", "codeledger", "mcp", "--root", str(ledger.root)]
        if not codex:
            value = {"configured": False, "reason": "codex executable was not found", "command": command}
        else:
            completed = subprocess.run(command, cwd=ledger.root, text=True, capture_output=True)
            value = {"configured": completed.returncode == 0, "command": command, "stdout": completed.stdout.strip(), "stderr": completed.stderr.strip()}
        emit(value, args.as_json); return 0 if value["configured"] else 1
    if args.command == "setup-agent":
        import shutil
        ledger._write_instructions(); executable = {"codex":"codex", "claude-code":"claude", "gemini":"gemini", "aider":"aider", "cursor":"cursor"}[args.agent]
        command = [executable, "mcp", "add", "codeledger", "--", "codeledger", "mcp", "--root", str(ledger.root)]
        if not shutil.which(executable): value={"configured":False,"agent":args.agent,"reason":f"{executable} executable was not found","command":command,"config":ledger.agent_config(args.agent)}
        else:
            completed=subprocess.run(command,cwd=ledger.root,text=True,capture_output=True); value={"configured":completed.returncode==0,"agent":args.agent,"command":command,"stdout":completed.stdout.strip(),"stderr":completed.stderr.strip(),"config":ledger.agent_config(args.agent)}
        emit(value,args.as_json); return 0 if value["configured"] else 1
    if args.command == "watch":
        session = args.session or ledger.start_session(args.agent, args.request, owns_process=True)["session_id"]
        print(f"CodeLedger watching {ledger.root} (agent={args.agent}, session={session}, pid={os.getpid()})", flush=True)
        # Each poll walks the tree, which is expensive on large repositories and
        # on /mnt/c under WSL. Idle polls back off toward --max-interval and snap
        # back to --interval the moment a change lands, so an active session
        # stays responsive without burning a core while nothing is happening.
        base = max(0.25, args.interval); ceiling = max(base, args.max_interval); delay = base
        with closing_session(ledger, session if not args.session else "", "watch stopped"):
            while True:
                result = ledger.refresh(True, args.agent, session, args.request)
                # The heartbeat is what lets a later run tell "this watcher is
                # alive and nothing is happening" from "this watcher is gone".
                ledger.touch_session(session, heartbeat=True)
                if result["change_id"] is not None:
                    message = f"Recorded change #{result['change_id']}: {', '.join(result['files'])}"
                    if result.get("scope", {}).get("status") == "WARNING": message += f"\nSCOPE WARNING: {result['scope']['unexpected_files']}"
                    print(json.dumps(result) if args.as_json else message, flush=True)
                    delay = base
                else:
                    delay = min(ceiling, delay * 1.5)
                time.sleep(delay)
        return 0
    if args.command == "run":
        command = list(args.agent_command)
        if command and command[0] == "--": command = command[1:]
        if not command:
            parser.error("run requires a command after --, for example: codeledger run --request 'Fix login' -- codex")
        session = args.session or ledger.start_session(args.agent, args.request, owns_process=True)["session_id"]
        context = ledger.context(args.request)
        print("[CODELEDGER CONTEXT]")
        print(json.dumps(context, indent=2, default=str))
        # Codex, Claude, Gemini, and most terminal agents accept a positional
        # prompt. Passing it here makes --request the actual agent task rather
        # than metadata that only CodeLedger can see. Users can still pass a
        # fully customized command by including its prompt explicitly after --.
        if not any(args.request == part for part in command[1:]):
            command.append(args.request)
        # If this process is killed while the agent runs, the session must not be
        # left active forever; the same cleanup path serves both outcomes.
        with closing_session(ledger, session if not args.session else "", "interrupted"):
            completed = subprocess.run(command, cwd=ledger.root)
            refresh = ledger.refresh(True, args.agent, session, args.request)
            # Close with the real outcome here; the guard above only fires when
            # this line is never reached, and closing is idempotent either way.
            if not args.session: ledger.end_session(session, "completed" if completed.returncode == 0 else "failed")
        print(json.dumps({"session_id": session, "exit_code": completed.returncode, "refresh": refresh}, indent=2, default=str))
        # Say plainly when an agent's turn achieved nothing. Without this the
        # only signal is an empty file list buried in the JSON, which is exactly
        # how an agent ends up repeating the same edit.
        if refresh["effect"] != "symbols-changed":
            detail = "no file changed at all" if refresh["effect"] == "none" else f"changed {len(refresh['files'])} file(s) but no symbol"
            print(f"\n[CODELEDGER] NO EFFECT: this attempt {detail}.", flush=True)
            print(f"[CODELEDGER] Run `codeledger progress {args.request!r}` before retrying.", flush=True)
        return completed.returncode
    if args.command == "doctor": emit_doctor(ledger.doctor(), args.as_json); return 0
    if args.command == "init": value=ledger.init(args.quick, args.verbose); value["root"]=str(ledger.root)
    elif args.command == "status": value=ledger.status()
    elif args.command == "refresh": value=ledger.refresh(args.changed, args.agent, args.session, args.request, verbose=args.verbose)
    elif args.command == "lookup": value=ledger.lookup(args.query)
    elif args.command == "impact": value=ledger.impact(args.query, scan=args.scan)
    elif args.command == "context": value=ledger.context(args.query)
    elif args.command == "history": value=ledger.history(args.query)
    elif args.command == "why": value=ledger.why(args.query)
    elif args.command == "restore-info": value={"query":args.query,"last_known_versions":ledger.lookup(args.query),"history":ledger.history(args.query),"automatic_restore":False}
    elif args.command == "scope": value=ledger.scope_check(args.request, args.files, args.symbols)
    elif args.command == "plan": value=ledger.plan(args.request)
    elif args.command == "progress": value=ledger.progress(args.request)
    elif args.command == "prompt": value=ledger.analyze_prompt(args.request)
    elif args.command == "handshake": value=ledger.handshake(args.request, args.ai_plan)
    elif args.command == "tests": value=ledger.suggest_tests(args.files, args.symbols)
    elif args.command == "changes": value=[dict(row) for row in ledger.db.execute("SELECT * FROM changes ORDER BY id DESC LIMIT 50")]
    elif args.command == "issues": value=[dict(row) for row in ledger.db.execute("SELECT * FROM issues ORDER BY updated_at DESC")]
    elif args.command == "decisions": value=[dict(row) for row in ledger.db.execute("SELECT * FROM decisions ORDER BY created_at DESC")]
    elif args.command == "session":
        if args.action == "start": value=ledger.start_session(args.agent, args.request, args.session_id or None)
        elif args.action == "end": value=ledger.end_session(args.session_id, args.result)
        elif args.action == "reconcile": value=ledger.reconcile_sessions()
        elif args.action == "status": value={"active_agents": ledger.active_agents(), **ledger.sessions(reconcile=False)}
        else: emit_sessions(ledger.sessions(), args.as_json); return 0
    elif args.command == "verify": value=ledger.verify(args.subject_type,args.subject_id,args.kind,args.result,args.evidence)
    elif args.command == "verify-run":
        command = list(args.verify_command)
        if command and command[0] == "--": command = command[1:]
        value = ledger.verify_command(args.subject_type, args.subject_id, args.kind, command, args.evidence)
    elif args.command == "regressions": value=ledger.regressions(args.subject_type, args.subject_id)
    elif args.command == "issue": value=ledger.upsert_issue(args.key,args.title,args.description,args.severity,args.status)
    elif args.command == "decision": value=ledger.upsert_decision(args.key,args.title,args.rationale,args.status)
    elif args.command == "feature": value=ledger.upsert_feature(args.name,args.description,args.status)
    elif args.command == "features": value=ledger.infer_features() if args.infer else ledger.features()
    elif args.command == "agent-config": value=ledger.agent_config(args.agent)
    elif args.command == "git-import": value=ledger.import_git(args.limit)
    elif args.command == "record": value={"change_id":ledger.record_change(args.agent,args.session,args.request,args.summary,args.result)}
    elif args.command == "export": value=ledger.export()
    else:
        status = ledger.status(); analysis = status["analysis"]
        actions = []
        if status["stale_files"]: actions.append("codeledger refresh --changed")
        if analysis["shallow_languages"]: actions.append(analysis["hint"])
        value = {"database": "OK", "files": status["files"], "symbols": status["symbols"],
                 "tree_sitter_installed": analysis["tree_sitter_installed"],
                 "shallow_languages": analysis["shallow_languages"] or "none",
                 "recommended_action": actions or "none"}
    emit(value,args.as_json)

if __name__ == "__main__": main()
