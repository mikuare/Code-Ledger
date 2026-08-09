"""Small provider-neutral agent adapter seam.

Adapters do not execute agents. They identify the caller and provide lifecycle
metadata while CodeLedger remains local and deterministic.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

KNOWN_AGENTS = {"codex", "claude-code", "gemini", "aider", "cursor", "human", "unknown"}

# Everything CodeLedger does not actually know is spelled this way, in one
# place, so a missing value can never be mistaken for a real one.
UNKNOWN = "UNKNOWN"

# The vendor behind each known agent, kept as data rather than behaviour.
# CodeLedger never branches on which vendor an agent belongs to: this table
# exists so `provider` can be reported honestly, and an agent that is not listed
# is 'generic' rather than guessed at. Adding an agent is a one-line edit here.
AGENT_PROVIDERS = {
    "claude-code": "anthropic",
    "codex": "openai",
    "gemini": "google",
    "cursor": "cursor",
    "aider": "generic",
    "human": "human",
    "unknown": UNKNOWN,
}

@dataclass(frozen=True)
class Agent:
    name: str
    provider: str

def identify(name: str | None) -> Agent:
    normalized = (name or "unknown").strip().lower()
    return Agent(normalized, normalized if normalized in KNOWN_AGENTS else "generic")

def provider_for_agent(agent: str | None) -> str:
    """Which vendor is behind this agent, as far as the table above records.

    Deliberately separate from `identify`, whose `provider` field predates this
    and means "is this a recognised agent" rather than "whose model is it".
    Existing rows keep that older meaning; nothing is rewritten.
    """
    return AGENT_PROVIDERS.get((agent or "").strip().lower(), "generic")

def resolve_model(agent: str | None = None, provider: str | None = None,
                  model: str | None = None, model_version: str | None = None) -> dict:
    """Decide what is actually known about the model behind a session.

    Nothing here is inferred from the agent name. An agent identifies a program;
    it does not tell you which model that program is driving today, and a
    plausible-looking guess is worse than an honest UNKNOWN because it would be
    stored and later read back as evidence. Values arrive explicitly from the
    caller or from the environment, or they stay UNKNOWN.
    """
    def pick(value: str | None, *env_names: str) -> str:
        if value and value.strip():
            return value.strip()
        for name in env_names:
            found = os.environ.get(name)
            if found and found.strip():
                return found.strip()
        return UNKNOWN

    return {
        "provider": pick(provider, "CODELEDGER_PROVIDER") if provider or os.environ.get("CODELEDGER_PROVIDER")
                    else provider_for_agent(agent),
        "model": pick(model, "CODELEDGER_MODEL"),
        "model_version": pick(model_version, "CODELEDGER_MODEL_VERSION"),
    }
