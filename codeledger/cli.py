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

def emit_refresh(value, as_json=False):
    """Show what the refresh cost and what it actually looked at."""
    if as_json: print(json.dumps(value, indent=2, default=str)); return
    scan, timing = value["scan"], value["timing"]
    print("CODELEDGER REFRESH\n")
    for label, key in (("Discovery", "discovery_seconds"), ("Hashing", "hashing_seconds"),
                       ("Parsing", "parsing_seconds"), ("Database", "database_seconds")):
        print(f"  {label:<10} {timing[key]:8.3f}s")
    print(f"  {'Total':<10} {timing['total_seconds']:8.3f}s\n")
    print(f"  Files checked   {scan['files_checked']}  ({scan['stat_mode']} stat)")
    print(f"  Files changed   {scan['files_changed']}")
    if scan["files_changed"]:
        print(f"  Files analyzed  {scan['files_analyzed']}")
        print(f"  Symbols changed {len(value['symbols'])}")
    print(f"  Directories     {scan['directories_visited']} visited, {scan['directories_pruned']} pruned")
    print(f"  Traversal       {scan['traversal']}")
    print(f"\n  Effect: {value['effect']}" + (f" ({value['effect_confidence']['level']} confidence)"
                                              if value.get("effect_confidence", {}).get("level") == "LOW" else ""))
    if value.get("change_id"): print(f"  Recorded change #{value['change_id']} as {value['agent']} ({value['attribution']['confidence']} confidence)")
    if value.get("conflicts"): print(f"\n  {value['conflicts']['message']}")
    if value.get("scope", {}).get("status") == "WARNING":
        scope = value["scope"]
        print(f"\n  SCOPE WARNING: {', '.join(scope['unexpected_files'])} changed but is outside the task boundary")
        if scope.get("unexpected_symbols"):
            print(f"                 symbols there: {', '.join(scope['unexpected_symbols'])}")

def emit_since(value, as_json=False):
    """A handoff answer is read by a human or pasted into an agent's context.

    The generic dict dump prints each change as a raw mapping, which buries the
    one thing the caller asked: who touched what while I was not looking.
    """
    if as_json: print(json.dumps(value, indent=2, default=str)); return
    print(value["summary"])
    for change in value["changes"]:
        print(f"   #{change['change_id']} by {change['agent']}  {change['files']}"
              f"  symbols={change['symbols']}  effect={change['effect']}")
    if value["active_agents"]:
        print(f"Active sessions: {', '.join(value['active_agents'])}")

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

def emit_targets(value, as_json=False):
    """Print candidates without implying one of them is the answer.

    The order is alphabetical and says so, no candidate is marked recommended,
    and when nothing matched the output says the target is UNKNOWN rather than
    printing an empty list, which reads as "there is nothing there".
    """
    if as_json: print(json.dumps(value, indent=2, default=str)); return
    target = value["target_ambiguity"]
    print(f"CODELEDGER TARGETS — {value['request']}\n")
    print(f"Status: {target['status']}   Intended target: {target['intended_target']}")
    if value["tokens"]: print(f"Matched on: {', '.join(value['tokens'])}")
    if not value["candidates"]:
        print(f"\n{value.get('reason', 'Nothing matched.')}")
    else:
        print(f"\n{value['candidate_count']} candidate(s) — {value['order']}:")
        for item in value["candidates"]:
            print(f"\n   {item['area']}  [{item['file_count']} file(s), trust {item['trust']['level']}]")
            for path in item["files"]: print(f"      {path}")
            if item["symbols"]: print(f"      symbols: {', '.join(item['symbols'])}")
            print(f"      why: {'; '.join(item['evidence'])}")
        if value["candidates_truncated"]: print(f"\n   (showing {len(value['candidates'])} of {value['candidate_count']})")
    if target.get("question"): print(f"\nQuestion: {target['question']}")
    if target.get("options"):
        print("Options:")
        for option in target["options"]: print(f"   - {option}")
    print(f"\n{target['guidance']}")

def emit_plan(value, as_json=False):
    """Lead with what a change would reach, not with the file it is defined in."""
    if as_json: print(json.dumps(value, indent=2, default=str)); return
    print(f"CODELEDGER PLAN — {value['request']}\n")
    print(f"Risk: {value['risk']}   Full scan required: {value['full_scan_required']}")
    print(f"\nRecommendation:\n   {value['recommendation']}")
    if value["shared_dependencies"]:
        radius = value["blast_radius"]
        print(f"\nSHARED DEPENDENCY — a change here reaches {radius['file_count']} file(s) "
              f"across {radius['area_count']} area(s) [confidence {radius['confidence']}]")
        for item in value["shared_dependencies"][:5]:
            print(f"   {item['symbol']} ({item['defined_in']})")
            print(f"      areas: {', '.join(item['areas'])}")
            for reason in item["why_shared"]: print(f"      - {reason}")
    if value.get("scope_ambiguity"):
        ambiguity = value["scope_ambiguity"]
        print(f"\nSCOPE AMBIGUITY\n   {ambiguity['question']}")
        print("   Clarify whether the change should apply to:")
        for option in ambiguity["options"]: print(f"      - {option}")
    if value.get("coverage_caveat"):
        print(f"\nCoverage caveat:\n   {value['coverage_caveat']}")
    if value["relevant_symbols"]:
        print(f"\nRelevant symbols: {', '.join(s['name'] for s in value['relevant_symbols'][:10])}")
    print(f"Existing files:   {', '.join(value['existing_files'][:10]) or 'none indexed'}")
    if value["suggested_tests"]: print(f"Suggested tests:  {', '.join(value['suggested_tests'][:5])}")
    emit_external_dependencies(value.get("external_dependencies"))

def emit_external_dependencies(value):
    """What the code needs from outside, and what the repository cannot settle.

    The unknowns are printed even when nothing was proven, because they are the
    half that answers "why might correct-looking code still fail?" — and a plan
    that lists only what it proved reads as though it checked everything.
    """
    if not value: return
    proven = value.get("environment_variables") or []
    packages = value.get("external_packages") or []
    def shown(items, total):
        """Never let a truncated list read as the whole answer."""
        return f" (showing {len(items)} of {total})" if total and total > len(items) else ""

    if proven:
        print(f"\nENVIRONMENT VARIABLES READ (proven from source)"
              f"{shown(proven, value.get('environment_variables_total'))}")
        for item in proven:
            print(f"   {item['name']}  —  {', '.join(item['files'][:3])}")
    if packages:
        listed = packages[:10]
        print(f"\nEXTERNAL PACKAGES (imported, defined outside this project)"
              f"{shown(listed, value.get('external_packages_total'))}")
        print(f"   {', '.join(item['name'] for item in listed)}")
    for item in value.get("cannot_prove") or []:
        print(f"\nCANNOT PROVE — {item['what']}")
        print(f"   {item['why']}")
        if item.get("files"): print(f"   in: {', '.join(item['files'][:3])}")

def emit_handshake(value, as_json=False):
    if as_json: print(json.dumps(value, indent=2, default=str)); return
    print(f"CODELEDGER HANDSHAKE — {value['status']}\n")
    print(f"{value['message']}\n")
    if value.get("scope_violations"):
        print("PLAN OUTSIDE THE TASK SCOPE")
        for item in value["scope_violations"]:
            print(f"   {item['path']}")
            print(f"      {item['reason']} ({item['evidence']})")
        print()
    if value.get("duplicate_implementation"):
        duplicate = value["duplicate_implementation"]
        print("POSSIBLE DUPLICATE IMPLEMENTATION")
        print(f"   Plan creates: {', '.join(duplicate['proposed_new'])}")
        print("   Existing implementation to inspect or reuse:")
        for item in duplicate["existing_implementation"]:
            print(f"      {item['symbol']:22} {item['path']:38} ({item['role']})")
        print(f"   {duplicate['guidance']}")
    if value.get("scope_ambiguity"):
        print(f"\nSCOPE AMBIGUITY\n   {value['scope_ambiguity']['question']}")
    if value.get("missing_preservation_constraints"):
        print(f"\nUnaddressed constraints: {value['missing_preservation_constraints']}")

def emit_resume(value, as_json=False):
    """Print a resume package as something a person can read in one screen.

    The JSON is what an agent consumes; this exists so a developer can see what
    their agent is about to be told, which is the only way to notice that a
    checkpoint has drifted from the work actually in progress.
    """
    if as_json: print(json.dumps(value, indent=2, default=str)); return
    status = value["status"]
    if status == "NO_CHECKPOINTS":
        print("CODELEDGER RESUME\n\nNo checkpoint recorded, and nothing recent looks unfinished."); return
    if status == "NO_CHECKPOINTS_RECENT_WORK_FOUND":
        print("CODELEDGER RESUME\n\nNo checkpoint exists, but recent work was found:\n")
        for item in value["recent_work"]:
            print(f"   #{item['change_id']} {item['timestamp']} — {item['agent']} — {item['request']}")
        print(f"\n{value['guidance']}"); return
    if status == "NO_RELEVANT_CHECKPOINT":
        print(f"CODELEDGER RESUME\n\nNo checkpoint matches {value['task']!r}. Open checkpoints:\n")
        for item in value["open_checkpoints"]:
            print(f"   #{item['checkpoint_id']} {item['created_at']} — {item['goal']}")
        print(f"\n{value['guidance']}"); return
    recorded = value["recorded_by"]
    print("CODELEDGER SESSION RESUME\n")
    print(f"Previous objective:\n   {value['goal']}\n")
    print(f"Recorded by:\n   {recorded['agent']} (provider {recorded['provider']}, model {recorded['model']}) "
          f"at {value['created_at']}, confidence {recorded['confidence']}\n")
    for label, key in (("Current state", "current_state"), ("Summary", "summary")):
        if value.get(key): print(f"{label}:\n   {value[key]}\n")
    for label, key in (("Completed", "accomplished"), ("Unresolved", "unresolved"),
                       ("Failed approaches", "failed_attempts"), ("Open questions", "open_questions"),
                       ("Important files", "important_files"), ("Important symbols", "important_symbols"),
                       ("Verification", "verification")):
        if value.get(key):
            print(f"{label}:"); [print(f"   - {item}") for item in value[key]]; print()
    for label, key, fields in (("Important decisions", "decisions", ("key", "title")),
                               ("Known issues", "known_issues", ("key", "title"))):
        if value.get(key):
            print(f"{label}:"); [print("   - " + " — ".join(str(item[f]) for f in fields)) for item in value[key]]; print()
    print(f"Recommended next action:\n   {value['next_action']}\n")
    moved = value["changed_since_checkpoint"]
    print(f"Changed since the checkpoint:\n   {moved['summary']}\n")
    if value["stale_items"]:
        print("No longer true (excluded above):")
        for item in value["stale_items"]:
            print(f"   - {item['kind']} {item['value']}: {item['reason']}")
        print()
    efficiency = value["efficiency"]
    print(f"Estimated context: {efficiency['estimated_total_tokens']} tokens "
          f"({efficiency['estimated_checkpoint_tokens']} checkpoint, {efficiency['estimated_history_tokens']} history)")
    print(f"Files avoided: {efficiency['files_avoided']} of {efficiency['files_in_repository']} — "
          f"repository-wide scan NOT REQUIRED")

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
    mcp = value.get("mcp")
    if mcp and mcp["status"] not in ("OK", "OK_WITH_WARNINGS"):
        print(f"\nMCP SELF-TEST — {mcp['status']}")
        print(f"   {mcp['detail']}")
        print(f"   command: {' '.join(mcp['command'])}")
    elif mcp:
        print(f"\nMCP self-test: {mcp['status']} — {mcp.get('tools_exposed')} tools, root {mcp.get('reported_root')}")
        print(f"   {mcp['note']}")
    if value["stale_sessions"]:
        print("\nStale sessions:")
        for row in value["stale_sessions"]:
            print(f"   {row['agent']} {row['session_id']} — {row['status_reason']}")
    print("\nRecommended:")
    for action in value["recommended_actions"]: print(f"   {action}")

AGENT_CHOICES = ("codex", "claude-code", "gemini", "aider", "cursor")
# Code subjects CodeLedger indexes, then observations of things outside the
# repository. The enum is documentation as much as validation: `subject_id`
# for an endpoint is a URL nobody will re-read, so a typo would silently
# create a second subject rather than update the first.
SUBJECT_CHOICES = ("symbol", "feature", "project", "endpoint", "deployment", "artifact")

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
    # What the plan said it would touch widens the boundary, so work an agent
    # declared in advance is not reported back to it as a surprise.
    scope.add_argument("--plan-files", nargs="*", default=[], help="Files the implementation plan said it would change")
    scope.add_argument("--plan-symbols", nargs="*", default=[], help="Symbols the implementation plan said it would change")

    add("plan", "Generate pre-change intelligence").add_argument("request")
    # Both take a free-text request rather than a symbol: they exist for the
    # case where the user does not yet know which symbol they mean.
    add("targets", "Which parts of the repository a request could be about").add_argument("request")
    add("project-context", "One compact engineering context for a request").add_argument("request", nargs="?", default="")
    add("progress", "Check whether prior attempts at this request achieved anything").add_argument("request")
    since = add("since", "What changed since a change id, session, timestamp, or an agent's last turn")
    since.add_argument("marker", nargs="?", default="")
    since.add_argument("--agent", default="", help="Show what changed since this agent last recorded anything")
    add("prompt", "Normalize a user request into an agent task brief").add_argument("request")

    handshake = add("handshake", "Compare the user request with the AI implementation plan")
    handshake.add_argument("request")
    handshake.add_argument("--ai-plan", default="")

    tests_cmd = add("tests", "Suggest tests affected by changed files/symbols")
    tests_cmd.add_argument("--files", nargs="*", default=[])
    tests_cmd.add_argument("--symbols", nargs="*", default=[])

    for name in ("changes", "issues", "decisions", "export"):
        add(name)

    doctor_cmd = add("doctor", "Diagnose the ledger, including a real MCP handshake")
    doctor_cmd.add_argument("--no-mcp", action="store_true",
                            help="Skip the MCP self-test (it launches a short-lived subprocess)")

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

    mcp_cmd = add("mcp", "Serve the local stdio MCP server", json=False)
    mcp_cmd.add_argument("--root", type=Path, dest="mcp_root")
    mcp_cmd.add_argument("--agent", default="", help="Override the agent name from the MCP handshake")
    mcp_cmd.add_argument("--session", default="", help="Attach to an existing session instead of starting one")

    resume_cmd = add("resume", "Continuation context for previous work on a task")
    resume_cmd.add_argument("task", nargs="?", default="", help="What you are about to work on; selection is by relevance")

    checkpoint_cmd = add("checkpoint", "Inspect or record session checkpoints")
    checkpoint_cmd.add_argument("action", nargs="?", default="list", choices=("list", "show", "state", "create"))
    checkpoint_cmd.add_argument("--id", type=int, default=0)
    checkpoint_cmd.add_argument("--session", default="")
    checkpoint_cmd.add_argument("--goal", default="")
    checkpoint_cmd.add_argument("--summary", default="")
    checkpoint_cmd.add_argument("--current-state", default="")
    checkpoint_cmd.add_argument("--next-action", default="")
    checkpoint_cmd.add_argument("--accomplished", nargs="*", default=[])
    checkpoint_cmd.add_argument("--unresolved", nargs="*", default=[])
    checkpoint_cmd.add_argument("--failed", nargs="*", default=[], dest="failed_attempts")
    checkpoint_cmd.add_argument("--questions", nargs="*", default=[])
    checkpoint_cmd.add_argument("--decisions", nargs="*", default=[], help="Keys of decisions already recorded with `codeledger decision`")
    checkpoint_cmd.add_argument("--issues", nargs="*", default=[], help="Keys of issues already recorded with `codeledger issue`")
    checkpoint_cmd.add_argument("--verifications", nargs="*", default=[], help="Verification ids, as shown by `codeledger checkpoint state`")
    checkpoint_cmd.add_argument("--files", nargs="*", default=[])
    checkpoint_cmd.add_argument("--symbols", nargs="*", default=[])
    checkpoint_cmd.add_argument("--context-window", type=int, default=None)
    checkpoint_cmd.add_argument("--context-used", type=int, default=None)

    add("setup-agent", "Register CodeLedger MCP with a supported agent").add_argument("agent", choices=AGENT_CHOICES)
    add("agent-config", "Show MCP setup template for an agent").add_argument("agent")
    add("setup-codex", "Register CodeLedger as a local Codex MCP server")

    watch = add("watch", "Record external edits as they happen")
    add_agent_flags(watch)
    watch.add_argument("--interval", type=float, default=2.0, help="Seconds between scans while changes are arriving")
    watch.add_argument("--max-interval", type=float, default=30.0, help="Longest idle backoff; 0 disables backoff")
    watch.add_argument("--claim-window", type=float, default=90.0,
                       help="Seconds to leave a fresh edit for its author to report before recording it as unclaimed. 0 records immediately, which costs the agent its attribution.")

    run = add("run", "Run an agent command with automatic CodeLedger lifecycle", json=False)
    run.add_argument("--agent", default="unknown")
    run.add_argument("--request", required=True)
    run.add_argument("--session", default="")
    run.add_argument("agent_command", nargs=argparse.REMAINDER)
    return parser

def main(argv=None):
    parser = build_parser()
    args=parser.parse_args(argv)
    # `Ledger(...)` creates `.ai/codeledger` as a side effect of connecting, so
    # the MCP branch is taken before anything is constructed. Otherwise a server
    # launched in the wrong directory would already have created a ledger there
    # by the time the root-safety check ran.
    if args.command == "mcp":
        from .mcp import serve
        serve(Path(getattr(args, "mcp_root", None) or args.root).resolve(), args.agent, args.session); return
    ledger=Ledger(getattr(args, "mcp_root", None) or args.root)
    if args.command in ("setup-codex", "setup-agent"):
        import shutil
        agent = "codex" if args.command == "setup-codex" else args.agent
        ledger._write_instructions()
        executable = {"codex":"codex", "claude-code":"claude", "gemini":"gemini", "aider":"aider", "cursor":"cursor"}.get(agent, agent)
        # One source of truth for how the server is launched: an absolute path
        # this process can prove exists, never a bare name for the agent's PATH
        # to resolve. See core.mcp_launch_command.
        launch = ledger.mcp_command()
        command = [executable, "mcp", "add", "codeledger", "--", *launch]
        # Prove the thing being registered actually works *before* claiming
        # success. Registering a command that cannot be launched is the exact
        # failure this command used to produce silently.
        selftest = ledger.mcp_selftest()
        value = {"agent": agent, "command": command, "launch_command": launch,
                 "mcp_selftest": selftest, "config": ledger.agent_config(agent)}
        if selftest["status"] not in ("OK", "OK_WITH_WARNINGS"):
            value.update(configured=False, reason=f"MCP self-test failed ({selftest['status']}): {selftest['detail']}")
        elif not shutil.which(executable):
            value.update(configured=False, reason=f"{executable} executable was not found — the MCP command below is correct, register it by hand")
        else:
            completed = subprocess.run(command, cwd=ledger.root, text=True, capture_output=True)
            value.update(configured=completed.returncode == 0, stdout=completed.stdout.strip(), stderr=completed.stderr.strip())
        emit(value, args.as_json); return 0 if value["configured"] else 1
    if args.command == "watch":
        session = args.session or ledger.start_session(args.agent, args.request, owns_process=True)["session_id"]
        print(f"CodeLedger watching {ledger.root} (agent={args.agent}, session={session}, pid={os.getpid()})", flush=True)
        # Each poll walks the tree, which is expensive on large repositories and
        # on /mnt/c under WSL. Idle polls back off toward --max-interval and snap
        # back to --interval the moment a change lands, so an active session
        # stays responsive without burning a core while nothing is happening.
        base = max(0.25, args.interval); ceiling = max(base, args.max_interval); delay = base
        print(f"Leaving edits unclaimed for {args.claim_window:g}s so agents can report their own work.", flush=True)
        waiting = 0
        with closing_session(ledger, session if not args.session else "", "watch stopped"):
            while True:
                # Recording an edit is destructive to attribution: afterwards the
                # agent's own refresh finds nothing to report, so its work ends up
                # credited to `unknown` with no request attached. The watcher
                # therefore holds back anything edited within the claim window and
                # only records what nobody claimed — a safety net rather than a
                # competitor for the same change.
                result = ledger.refresh(True, args.agent, session, args.request,
                                        observed=True, settle_seconds=args.claim_window)
                # The heartbeat is what lets a later run tell "this watcher is
                # alive and nothing is happening" from "this watcher is gone".
                ledger.touch_session(session, heartbeat=True)
                unclaimed = result["scan"]["files_awaiting_claim"]
                if unclaimed != waiting:
                    waiting = unclaimed
                    if unclaimed: print(f"{unclaimed} recent edit(s) pending — leaving them for their author to claim.", flush=True)
                if result["change_id"] is not None:
                    message = (f"Recorded UNCLAIMED change #{result['change_id']}: {', '.join(result['files'])}\n"
                               f"  Nobody reported these within {args.claim_window:g}s, so they are attributed to "
                               f"'unknown'. If an agent made them, have it call refresh itself next time.")
                    if result.get("scope", {}).get("status") == "WARNING": message += f"\nSCOPE WARNING: {result['scope']['unexpected_files']}"
                    if result.get("conflicts"): message += f"\n{result['conflicts']['message']}"
                    print(json.dumps(result) if args.as_json else message, flush=True)
                    delay = base
                else:
                    delay = base if unclaimed else min(ceiling, delay * 1.5)
                time.sleep(delay)
        return 0
    if args.command == "run":
        command = list(args.agent_command)
        if command and command[0] == "--": command = command[1:]
        if not command:
            parser.error("run requires a command after --, for example: codeledger run --request 'Fix login' -- codex")
        session = args.session or ledger.start_session(args.agent, args.request, owns_process=True)["session_id"]
        # Hand the agent the previous work on this task before it starts, so it
        # continues rather than rediscovering. Selection is by relevance, so an
        # unrelated previous task is not loaded here either.
        previous = ledger.resume(args.request)
        if previous["status"] == "RESUME":
            print("[CODELEDGER RESUME]")
            print(json.dumps(previous, indent=2, default=str))
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
            # The agent has exited, so it cannot be asked what the work meant.
            # This records what was observed, at LOW confidence, only when the
            # agent left no checkpoint of its own — never overwriting one.
            checkpoint = ledger._mechanical_checkpoint(session, "the agent command exited")
            # Close with the real outcome here; the guard above only fires when
            # this line is never reached, and closing is idempotent either way.
            if not args.session: ledger.end_session(session, "completed" if completed.returncode == 0 else "failed")
        print(json.dumps({"session_id": session, "exit_code": completed.returncode, "refresh": refresh,
                          "checkpoint": {"id": checkpoint["id"], "confidence": checkpoint["confidence"]} if checkpoint else None},
                         indent=2, default=str))
        if refresh.get("conflicts"):
            print(f"\n[CODELEDGER] {refresh['conflicts']['message']}", flush=True)
        # Say plainly when an agent's turn achieved nothing. Without this the
        # only signal is an empty file list buried in the JSON, which is exactly
        # how an agent ends up repeating the same edit.
        if refresh["effect"] != "symbols-changed":
            detail = "no file changed at all" if refresh["effect"] == "none" else f"changed {len(refresh['files'])} file(s) but no symbol"
            print(f"\n[CODELEDGER] NO EFFECT: this attempt {detail}.", flush=True)
            print(f"[CODELEDGER] Run `codeledger progress {args.request!r}` before retrying.", flush=True)
        return completed.returncode
    if args.command == "doctor": emit_doctor(ledger.doctor(check_mcp=not args.no_mcp), args.as_json); return 0
    if args.command == "init": value=ledger.init(args.quick, args.verbose); value["root"]=str(ledger.root)
    elif args.command == "status": value=ledger.status()
    elif args.command == "refresh": emit_refresh(ledger.refresh(args.changed, args.agent, args.session, args.request, verbose=args.verbose), args.as_json); return 0
    elif args.command == "lookup": value=ledger.lookup(args.query)
    elif args.command == "impact": value=ledger.impact(args.query, scan=args.scan)
    elif args.command == "context": value=ledger.context(args.query)
    elif args.command == "history": value=ledger.history(args.query)
    elif args.command == "why": value=ledger.why(args.query)
    elif args.command == "restore-info": value={"query":args.query,"last_known_versions":ledger.lookup(args.query),"history":ledger.history(args.query),"automatic_restore":False}
    elif args.command == "scope": value=ledger.scope_check(args.request, args.files, args.symbols, args.plan_files, args.plan_symbols)
    elif args.command == "plan": emit_plan(ledger.plan(args.request), args.as_json); return 0
    elif args.command == "targets":
        found = ledger.candidate_targets(args.request)
        emit_targets({**found, "target_ambiguity": ledger._target_ambiguity(found)}, args.as_json); return 0
    elif args.command == "project-context": value=ledger.project_context(args.request)
    elif args.command == "progress": value=ledger.progress(args.request)
    elif args.command == "since": emit_since(ledger.since(args.marker, args.agent), args.as_json); return 0
    elif args.command == "resume": emit_resume(ledger.resume(args.task), args.as_json); return 0
    elif args.command == "checkpoint":
        if args.action == "show": value=ledger.checkpoint(args.id)
        elif args.action == "state": value=ledger.session_state(args.session, context_window=args.context_window, context_used=args.context_used)
        elif args.action == "create":
            if not args.goal: parser.error("checkpoint create requires --goal: it is what a later session matches its task against")
            value=ledger.record_checkpoint(session_id=args.session, goal=args.goal, summary=args.summary,
                                           current_state=args.current_state, next_action=args.next_action,
                                           accomplished=args.accomplished, unresolved=args.unresolved,
                                           failed_attempts=args.failed_attempts, questions=args.questions,
                                           decisions=args.decisions, issues=args.issues, verifications=args.verifications,
                                           files=args.files, symbols=args.symbols,
                                           context_window=args.context_window, context_used=args.context_used, source="cli")
        else: value=ledger.checkpoints()
    elif args.command == "prompt": value=ledger.analyze_prompt(args.request)
    elif args.command == "handshake": emit_handshake(ledger.handshake(args.request, args.ai_plan), args.as_json); return 0
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
