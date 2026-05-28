from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EngineSpec:
    name: str
    display: str
    cmd: tuple[str, ...]
    install_url: str


ENGINES: dict[str, EngineSpec] = {
    e.name: e
    for e in (
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
            install_url="https://moonshotai.github.io/kimi-cli/en/guides/getting-started.html",
        ),
        EngineSpec(
            "gemini",
            "Gemini CLI",
            ("gemini", "--acp"),
            install_url="https://geminicli.com/docs/cli/acp-mode/",
        ),
    )
}

DEFAULT_ENGINE = next(iter(ENGINES))


def get_engine(name: str) -> EngineSpec:
    return ENGINES[name]


def list_engines() -> list[EngineSpec]:
    return list(ENGINES.values())


def resolve(token: str) -> EngineSpec | None:
    low = token.strip().lower()
    for e in ENGINES.values():
        if low in (e.name, e.display.lower()):
            return e
    if low.isdigit():
        idx = int(low) - 1
        engines = list_engines()
        if 0 <= idx < len(engines):
            return engines[idx]
    return None
