"""Minimal local MCP-compatible stdio server.

It implements the MCP JSON-RPC tool surface without networking or uploading
source. Launch with: ``codeledger mcp``.
"""
from __future__ import annotations
import json, sys
from .core import Ledger

TOOLS = [
    ("codeledger_get_context", "Retrieve compact project context for a task."),
    ("codeledger_get_plan", "Generate pre-change intelligence and recommendations."),
    ("codeledger_get_progress", "Check whether previous attempts at this request changed anything or are repeating. Call before retrying a task that did not work."),
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
    ("codeledger_refresh", "Incrementally refresh changed files."),
]

SCHEMAS = {
    "codeledger_get_context": {"type": "object", "properties": {"query": {"type": "string"}, "task": {"type": "string"}}},
    "codeledger_get_plan": {"type": "object", "properties": {"request": {"type": "string"}, "task": {"type": "string"}}},
    "codeledger_get_progress": {"type": "object", "properties": {"request": {"type": "string"}, "task": {"type": "string"}}, "required": ["request"]},
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
    "codeledger_refresh": {"type": "object", "properties": {"agent": {"type": "string"}, "session": {"type": "string"}, "request": {"type": "string"}, "task": {"type": "string"}}},
}

def arg(args, *names, default=""):
    for name in names:
        if args.get(name): return args[name]
    return default

def _result(value):
    return {"content": [{"type": "text", "text": json.dumps(value, default=str)}], "structuredContent": value}

def serve(root):
    ledger = Ledger(root)
    for line in sys.stdin:
        try:
            request = json.loads(line); method = request.get("method"); request_id = request.get("id")
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "codeledger", "version": "0.1.0"}}
            elif method == "tools/list":
                result = {"tools": [{"name": name, "description": description, "inputSchema": SCHEMAS.get(name, {"type": "object"})} for name, description in TOOLS]}
            elif method == "tools/call":
                params = request.get("params", {}); name = params.get("name"); args = params.get("arguments", {})
                if name == "codeledger_get_context": result = _result(ledger.context(arg(args, "query", "task")))
                elif name == "codeledger_get_plan": result = _result(ledger.plan(arg(args, "request", "task", "query")))
                elif name == "codeledger_get_progress": result = _result(ledger.progress(arg(args, "request", "task", "query")))
                elif name == "codeledger_analyze_prompt": result = _result(ledger.analyze_prompt(arg(args, "prompt", "task", "query")))
                elif name == "codeledger_task_handshake": result = _result(ledger.handshake(arg(args, "request", "task"), args.get("ai_plan", "")))
                elif name == "codeledger_find_symbol": result = _result(ledger.lookup(arg(args, "query", "task")))
                elif name == "codeledger_get_impact": result = _result(ledger.impact(arg(args, "query", "task"), scan=bool(args.get("scan", False))))
                elif name == "codeledger_get_history": result = _result(ledger.history(arg(args, "query", "task")))
                elif name == "codeledger_get_changes": result = _result([dict(row) for row in ledger.db.execute("SELECT * FROM changes ORDER BY id DESC LIMIT ?", (int(args.get("limit", 20)),))])
                elif name == "codeledger_get_recent_changes": result = _result([dict(row) for row in ledger.db.execute("SELECT * FROM changes ORDER BY id DESC LIMIT 10")])
                elif name == "codeledger_get_issues": result = _result([dict(row) for row in ledger.db.execute("SELECT * FROM issues WHERE status='OPEN' ORDER BY updated_at DESC")])
                elif name == "codeledger_get_decisions": result = _result([dict(row) for row in ledger.db.execute("SELECT * FROM decisions WHERE status='ACTIVE' ORDER BY created_at DESC")])
                elif name == "codeledger_record_change": result = _result({"change_id": ledger.record_change(args.get("agent", "unknown"), args.get("session", ""), args.get("request", ""), args.get("summary", "NOT RECORDED"), args.get("result", "unverified"), args.get("files", []), args.get("symbols", []))})
                elif name == "codeledger_mark_verified": result = _result(ledger.verify(args.get("subject_type", "project"), args.get("subject_id", "project"), args.get("kind", "manual"), args.get("result", "UNVERIFIED"), args.get("evidence", "")))
                elif name == "codeledger_get_regressions": result = _result(ledger.regressions(args.get("subject_type"), args.get("subject_id")))
                elif name == "codeledger_suggest_tests": result = _result(ledger.suggest_tests(args.get("files", []), args.get("symbols", [])))
                elif name == "codeledger_get_features": result = _result(ledger.features())
                elif name == "codeledger_refresh": result = _result(ledger.refresh(True, args.get("agent", "unknown"), args.get("session", ""), arg(args, "request", "task")))
                else: raise ValueError(f"Unknown tool: {name}")
            elif method == "notifications/initialized": continue
            else: raise ValueError(f"Unsupported method: {method}")
            if request_id is not None:
                print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
        except Exception as exc:
            if "request_id" in locals() and request_id is not None:
                print(json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(exc)}}), flush=True)
