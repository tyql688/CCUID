from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EngineSpec:
    name: str
    display: str
    cmd: tuple[str, ...]
    install_url: str
    initial_mode_override: tuple[str, str] | None = None
    grok_metadata: bool = False


ENGINE_SPECS: tuple[EngineSpec, ...] = (
    EngineSpec(
        "claude",
        "Claude Code",
        ("npx", "-y", "@agentclientprotocol/claude-agent-acp@0.63.0"),
        install_url="https://github.com/agentclientprotocol/claude-agent-acp",
        initial_mode_override=("bypassPermissions", "default"),
    ),
    EngineSpec(
        "codex",
        "Codex",
        ("npx", "-y", "@agentclientprotocol/codex-acp@1.1.7"),
        install_url="https://github.com/agentclientprotocol/codex-acp",
    ),
    EngineSpec(
        "cursor",
        "Cursor",
        ("cursor-agent", "acp"),
        install_url="https://docs.cursor.com/cli/installation",
    ),
    EngineSpec(
        "opencode",
        "OpenCode",
        ("opencode", "acp"),
        install_url="https://opencode.ai/docs/acp/",
    ),
    EngineSpec(
        "kimi",
        "Kimi Code CLI",
        ("kimi", "acp"),
        install_url="https://moonshotai.github.io/kimi-code/",
    ),
    EngineSpec(
        "gemini",
        "Gemini CLI",
        ("gemini", "--acp"),
        install_url="https://geminicli.com/docs/cli/acp-mode/",
    ),
    EngineSpec(
        "grok",
        "Grok",
        ("grok", "agent", "stdio"),
        install_url="https://x.ai/cli",
        grok_metadata=True,
    ),
)

ENGINES: dict[str, EngineSpec] = {engine.name: engine for engine in ENGINE_SPECS}

DEFAULT_ENGINE = ENGINE_SPECS[0].name


def get_engine(name: str) -> EngineSpec:
    return ENGINES[name]


def list_engines() -> tuple[EngineSpec, ...]:
    return ENGINE_SPECS


def has_engine(name: str | None) -> bool:
    return name in ENGINES


def resolve(token: str) -> EngineSpec | None:
    low = token.strip().lower()
    for e in ENGINE_SPECS:
        if low in (e.name, e.display.lower()):
            return e
    if low.isdigit():
        idx = int(low) - 1
        if 0 <= idx < len(ENGINE_SPECS):
            return ENGINE_SPECS[idx]
    return None
