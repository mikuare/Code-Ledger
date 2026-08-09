"""Minimal local MCP-compatible stdio server.

It implements the MCP JSON-RPC tool surface without networking or uploading
source. Launch with: ``codeledger mcp``.
"""
from __future__ import annotations
import json, os, sys
from . import __version__
from .core import Ledger

TOOLS = [
    ("codeledger_get_resume", "Resume previous work on a task without the old conversation or a repository scan. Call this FIRST in a new session, passing the user's request as `task`."),
    ("codeledger_get_session_state", "Everything CodeLedger recorded this session, ready to be turned into a checkpoint. Call before ending a long session, or when context is running out."),
    ("codeledger_record_checkpoint", "Store a compact summary of where this task has got to, so a later session can continue it. Supply the goal and next action yourself; CodeLedger cannot see the conversation."),
    ("codeledger_get_context", "Retrieve compact project context for a task."),
    ("codeledger_get_plan", "Generate pre-change intelligence and recommendations."),
    ("codeledger_get_progress", "Check whether previous attempts at this request changed anything or are repeating. Call before retrying a task that did not work."),
    ("codeledger_get_changes_since", "What changed since a point in time, and which agent did it. Call at the start of a turn when more than one agent works on this project."),
    ("codeledger_analyze_prompt", "Convert a user request into an explicit task brief."),
    ("codeledger_task_handshake", "Compare the user request with the AI implementation plan before editing."),
    ("codeledger_find_symbol", "Find active or deleted symbols."),
    ("codeledger_get_impact", "Find dependencies and likely affected files."),
    ("codeledger_get_history", "Retrieve recorded change history."),
    ("codeledger_get_changes", "Retrieve recent change records."),
    ("codeledger_get_recent_changes", "Retrieve the most recent project changes."),
    ("codeledger_get_issues", "Retrieve known open issues."),
    ("codeledger_get_decisions", "Retrieve active architecture decisions."),
    ("codeledger_record_change", "Record a completed change with evidence."),
    ("codeledger_mark_verified", "Record test, build, typecheck, or manual verification."),
    ("codeledger_get_regressions", "Find functionality that was verified working and later failed."),
    ("codeledger_suggest_tests", "Suggest tests affected by changed files and symbols."),
    ("codeledger_get_features", "Retrieve high-level project functionality records."),
    ("codeledger_get_active_agents", "Which agents genuinely hold a live session here, with dead sessions reconciled first."),
    ("codeledger_check_conflicts", "Check whether another live agent recently changed the same files or symbols. Call before editing when another agent is active."),
    ("codeledger_doctor", "Diagnose the ledger: database, schema, sessions, index coverage, and agent protocols."),
    ("codeledger_refresh", "Incrementally refresh changed files."),
]

SCHEMAS = {
    "codeledger_get_resume": {"type": "object", "properties": {"task": {"type": "string", "description": "what you have been asked to do now; selection is by relevance, so an unrelated checkpoint is never returned"}}},
    "codeledger_get_session_state": {"type": "object", "properties": {"session": {"type": "string"}, "request": {"type": "string"}, "context_window": {"type": "integer", "description": "omit unless your runtime actually reports it"}, "context_used": {"type": "integer", "description": "omit unless your runtime actually reports it"}}},
    "codeledger_record_checkpoint": {"type": "object", "properties": {
        "goal": {"type": "string", "description": "what this work is trying to achieve; a later session matches its task against this"},
        "summary": {"type": "string"}, "current_state": {"type": "string"},
        "next_action": {"type": "string", "description": "the single most useful thing for the next session to do"},
        "accomplished": {"type": "array", "items": {"type": "string"}},
        "unresolved": {"type": "array", "items": {"type": "string"}},
        "failed_attempts": {"type": "array", "items": {"type": "string"}, "description": "approaches that did not work, so they are not repeated"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}, "description": "decision keys already recorded with codeledger decision"},
        "issues": {"type": "array", "items": {"type": "string"}, "description": "issue keys already recorded"},
        "verifications": {"type": "array", "items": {"type": "string"}, "description": "verification ids, as returned by codeledger_get_session_state"},
        "files": {"type": "array", "items": {"type": "string"}}, "symbols": {"type": "array", "items": {"type": "string"}},
        "session": {"type": "string"}, "agent": {"type": "string"},
        "model": {"type": "string", "description": "only if your runtime reports it; never guess"},
        "context_window": {"type": "integer"}, "context_used": {"type": "integer"}},
        "required": ["goal"]},
    "codeledger_get_context": {"type": "object", "properties": {"query": {"type": "string"}, "task": {"type": "string"}}},
    "codeledger_get_plan": {"type": "object", "properties": {"request": {"type": "string"}, "task": {"type": "string"}}},
    "codeledger_get_progress": {"type": "object", "properties": {"request": {"type": "string"}, "task": {"type": "string"}}, "required": ["request"]},
    "codeledger_get_changes_since": {"type": "object", "properties": {"marker": {"type": "string", "description": "change id, session id, or ISO timestamp"}, "agent": {"type": "string", "description": "your own agent name; means 'since I last recorded anything'"}}},
    "codeledger_analyze_prompt": {"type": "object", "properties": {"prompt": {"type": "string"}, "task": {"type": "string"}}, "required": ["prompt"]},
    "codeledger_task_handshake": {"type": "object", "properties": {"request": {"type": "string"}, "task": {"type": "string"}, "ai_plan": {"type": "string"}}, "required": ["ai_plan"]},
    "codeledger_find_symbol": {"type": "object", "properties": {"query": {"type": "string"}, "task": {"type": "string"}}, "required": ["query"]},
    "codeledger_get_impact": {"type": "object", "properties": {"query": {"type": "string"}, "task": {"type": "string"}, "scan": {"type": "boolean", "default": False, "description": "Read every source file instead of trusting the index. Slow on large repositories."}}, "required": ["query"]},
    "codeledger_get_history": {"type": "object", "properties": {"query": {"type": "string"}, "task": {"type": "string"}}},
    "codeledger_get_changes": {"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}},
    "codeledger_get_recent_changes": {"type": "object", "properties": {}},
    "codeledger_get_issues": {"type": "object", "properties": {}},
    "codeledger_get_decisions": {"type": "object", "properties": {}},
    "codeledger_record_change": {"type": "object", "properties": {"agent": {"type": "string"}, "session": {"type": "string"}, "request": {"type": "string"}, "summary": {"type": "string"}, "result": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}, "symbols": {"type": "array", "items": {"type": "string"}}}},
    "codeledger_mark_verified": {"type": "object", "properties": {"subject_type": {"type": "string"}, "subject_id": {"type": "string"}, "kind": {"type": "string"}, "result": {"type": "string"}, "evidence": {"type": "string"}}},
    "codeledger_get_regressions": {"type": "object", "properties": {"subject_type": {"type": "string"}, "subject_id": {"type": "string"}}},
    "codeledger_suggest_tests": {"type": "object", "properties": {"files": {"type": "array", "items": {"type": "string"}}, "symbols": {"type": "array", "items": {"type": "string"}}}},
    "codeledger_get_features": {"type": "object", "properties": {}},
    "codeledger_get_active_agents": {"type": "object", "properties": {}},
    "codeledger_check_conflicts": {"type": "object", "properties": {"agent": {"type": "string", "description": "your own agent name"}, "files": {"type": "array", "items": {"type": "string"}}, "symbols": {"type": "array", "items": {"type": "string"}}}, "required": ["agent"]},
    "codeledger_doctor": {"type": "object", "properties": {}},
    "codeledger_refresh": {"type": "object", "properties": {"agent": {"type": "string"}, "session": {"type": "string"}, "request": {"type": "string"}, "task": {"type": "string"}}},
}

def arg(args, *names, default=""):
    for name in names:
        if args.get(name): return args[name]
    return default

def _result(value):
    return {"content": [{"type": "text", "text": json.dumps(value, default=str)}], "structuredContent": value}

# Returned in the `initialize` result, which is where MCP lets a server say how
# it expects to be used. A server cannot push context into a model's turn —
# tools are pull-only — so this is the strongest available nudge toward calling
# resume first, short of the project's own protocol file. Clients are free to
# ignore it, which is why the protocol file says the same thing.
INSTRUCTIONS = (
    "This project has durable engineering memory. Before reading source files, call "
    "codeledger_get_resume with the user's request as `task` — it returns any previous unfinished work on "
    "that task, including approaches already known to have failed, and returns NO_RELEVANT_CHECKPOINT when "
    "nothing matches. Before the session ends, or if your context runs low, call codeledger_get_session_state "
    "and then codeledger_record_checkpoint with the goal, current state, what failed, and the next action; "
    "CodeLedger records what it observed but cannot see this conversation. Treat everything a checkpoint says "
    "as a summary that ranks below the current source."
)

def client_agent(params) -> str:
    """Which agent is on the other end of this pipe, as it described itself.

    Taken from the MCP handshake rather than guessed, and normalised by the same
    adapter seam as every other entry point, so an unrecognised client is
    recorded as itself with a generic provider instead of being mapped onto a
    vendor CodeLedger has no evidence for. An explicit --agent or
    CODELEDGER_AGENT wins, because the operator knows better than the handshake.
    """
    override = os.environ.get("CODELEDGER_AGENT")
    if override and override.strip(): return override.strip()
    name = ((params or {}).get("clientInfo") or {}).get("name") or ""
    return name.strip() or "unknown"

def serve(root, agent: str = "", session: str = ""):
    ledger = Ledger(root)
    # The MCP server is the one long-lived process in an agent's session, so it
    # is the only place a session can begin and end without asking the user to
    # remember a command. Before this an MCP-connected agent had no session at
    # all: nothing to heartbeat, nothing to hang a checkpoint on, and no entry
    # in `active_agents` for another agent's conflict check to see.
    owned = ""
    try:
        for line in sys.stdin:
            try:
                request = json.loads(line); method = request.get("method"); request_id = request.get("id")
                if method == "initialize":
                    params = request.get("params", {})
                    if not owned and not session:
                        owned = ledger.start_session(agent or client_agent(params), owns_process=True)["session_id"]
                    result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "codeledger", "version": __version__},
                              "instructions": INSTRUCTIONS}
                elif method == "tools/list":
                    result = {"tools": [{"name": name, "description": description, "inputSchema": SCHEMAS.get(name, {"type": "object"})} for name, description in TOOLS]}
                elif method == "tools/call":
                    params = request.get("params", {}); name = params.get("name"); args = params.get("arguments", {})
                    # A tool call is the only proof this session is still doing
                    # anything. `revive` matters because a user who steps away
                    # for longer than the stale limit would otherwise return to
                    # a session reconciled as dead and never brought back.
                    current = session or owned
                    if current: ledger.touch_session(current, revive=True)
                    if name == "codeledger_get_resume": result = _result(ledger.resume(arg(args, "task", "request", "query")))
                    elif name == "codeledger_get_session_state":
                        result = _result(ledger.session_state(args.get("session") or current, arg(args, "request", "task"),
                                                              args.get("context_window"), args.get("context_used")))
                    elif name == "codeledger_record_checkpoint":
                        result = _result(ledger.record_checkpoint(
                            session_id=args.get("session") or current, goal=args.get("goal", ""),
                            summary=args.get("summary", ""), current_state=args.get("current_state", ""),
                            next_action=args.get("next_action", ""), accomplished=args.get("accomplished"),
                            unresolved=args.get("unresolved"), failed_attempts=args.get("failed_attempts"),
                            questions=args.get("questions"), decisions=args.get("decisions"),
                            issues=args.get("issues"), verifications=args.get("verifications"),
                            files=args.get("files"), symbols=args.get("symbols"), agent=args.get("agent", ""),
                            provider=args.get("provider", ""), model=args.get("model", ""),
                            model_version=args.get("model_version", ""), context_window=args.get("context_window"),
                            context_used=args.get("context_used")))
                    elif name == "codeledger_get_context": result = _result(ledger.context(arg(args, "query", "task")))
                    elif name == "codeledger_get_plan": result = _result(ledger.plan(arg(args, "request", "task", "query")))
                    elif name == "codeledger_get_progress": result = _result(ledger.progress(arg(args, "request", "task", "query")))
                    elif name == "codeledger_get_changes_since": result = _result(ledger.since(args.get("marker", ""), args.get("agent", "")))
                    elif name == "codeledger_analyze_prompt": result = _result(ledger.analyze_prompt(arg(args, "prompt", "task", "query")))
                    elif name == "codeledger_task_handshake": result = _result(ledger.handshake(arg(args, "request", "task"), args.get("ai_plan", "")))
                    elif name == "codeledger_find_symbol": result = _result(ledger.lookup(arg(args, "query", "task")))
                    elif name == "codeledger_get_impact": result = _result(ledger.impact(arg(args, "query", "task"), scan=bool(args.get("scan", False))))
                    elif name == "codeledger_get_history": result = _result(ledger.history(arg(args, "query", "task")))
                    elif name == "codeledger_get_changes": result = _result([dict(row) for row in ledger.db.execute("SELECT * FROM changes ORDER BY id DESC LIMIT ?", (int(args.get("limit", 20)),))])
                    elif name == "codeledger_get_recent_changes": result = _result([dict(row) for row in ledger.db.execute("SELECT * FROM changes ORDER BY id DESC LIMIT 10")])
                    elif name == "codeledger_get_issues": result = _result([dict(row) for row in ledger.db.execute("SELECT * FROM issues WHERE status='OPEN' ORDER BY updated_at DESC")])
                    elif name == "codeledger_get_decisions": result = _result([dict(row) for row in ledger.db.execute("SELECT * FROM decisions WHERE status='ACTIVE' ORDER BY created_at DESC")])
                    elif name == "codeledger_record_change": result = _result({"change_id": ledger.record_change(args.get("agent", "unknown"), args.get("session", "") or current, args.get("request", ""), args.get("summary", "NOT RECORDED"), args.get("result", "unverified"), args.get("files", []), args.get("symbols", []))})
                    elif name == "codeledger_mark_verified": result = _result(ledger.verify(args.get("subject_type", "project"), args.get("subject_id", "project"), args.get("kind", "manual"), args.get("result", "UNVERIFIED"), args.get("evidence", "")))
                    elif name == "codeledger_get_regressions": result = _result(ledger.regressions(args.get("subject_type"), args.get("subject_id")))
                    elif name == "codeledger_suggest_tests": result = _result(ledger.suggest_tests(args.get("files", []), args.get("symbols", [])))
                    elif name == "codeledger_get_features": result = _result(ledger.features())
                    elif name == "codeledger_get_active_agents": result = _result({"active_agents": ledger.active_agents(), **ledger.sessions(reconcile=False)})
                    elif name == "codeledger_check_conflicts": result = _result(ledger.conflicts(args.get("agent", "unknown"), args.get("files", []), args.get("symbols", [])))
                    elif name == "codeledger_doctor": result = _result(ledger.doctor())
                    elif name == "codeledger_refresh": result = _result(ledger.refresh(True, args.get("agent", "unknown"), args.get("session", "") or current, arg(args, "request", "task")))
                    else: raise ValueError(f"Unknown tool: {name}")
                elif method == "notifications/initialized": continue
                else: raise ValueError(f"Unsupported method: {method}")
                if request_id is not None:
                    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
            except Exception as exc:
                if "request_id" in locals() and request_id is not None:
                    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(exc)}}), flush=True)
    finally:
        # stdin closing is the client disconnecting. The agent is already gone,
        # so nothing here can ask it what the work meant: the fallback records
        # only what was observed, at LOW confidence, and says so. A session that
        # ends without any checkpoint at all would lose even that.
        if owned:
            try:
                ledger._mechanical_checkpoint(owned, "the MCP client disconnected")
            except Exception:
                pass    # a best-effort checkpoint must never mask the real exit
            ledger.end_session(owned, "mcp client disconnected")
