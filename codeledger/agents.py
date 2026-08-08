"""Small provider-neutral agent adapter seam.

Adapters do not execute agents. They identify the caller and provide lifecycle
metadata while CodeLedger remains local and deterministic.
"""
from dataclasses import dataclass

KNOWN_AGENTS = {"codex", "claude-code", "gemini", "aider", "cursor", "human", "unknown"}

@dataclass(frozen=True)
class Agent:
    name: str
    provider: str

def identify(name: str | None) -> Agent:
    normalized = (name or "unknown").strip().lower()
    return Agent(normalized, normalized if normalized in KNOWN_AGENTS else "generic")
