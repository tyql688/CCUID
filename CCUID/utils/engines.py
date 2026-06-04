from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EngineSpec:
    name: str
    display: str
    cmd: tuple[str, ...]
    install_url: str


ENGINE_SPECS: tuple[EngineSpec, ...] = (
    EngineSpec(
        "claude",
        "Claude Code",
        ("npx", "-y", "@zed-industries/claude-code-acp"),
        install_url="https://github.com/zed-industries/claude-code-acp",
    ),
    EngineSpec(
        "codex",
        "Codex",
        ("npx", "-y", "@zed-industries/codex-acp"),
        install_url="https://github.com/zed-industries/codex-acp",
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
)

ENGINES: dict[str, EngineSpec] = {engine.name: engine for engine in ENGINE_SPECS}
ENGINE_NAMES = frozenset(ENGINES)

DEFAULT_ENGINE = ENGINE_SPECS[0].name


def get_engine(name: str) -> EngineSpec:
    return ENGINES[name]


def list_engines() -> list[EngineSpec]:
    return list(ENGINE_SPECS)


def has_engine(name: str | None) -> bool:
    return name in ENGINE_NAMES


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
