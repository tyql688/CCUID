from __future__ import annotations

from collections.abc import Callable, AsyncIterator

from acp.schema import PromptResponse

from gsuid_core.bot import Bot
from gsuid_core.logger import logger

from .errors import user_error
from .render import ChatBlock
from .acp.backend import PromptUsage, BackendError
from .presentation.blocks import (
    ToolDisplayMode,
    _classify,
    _RenderBuffer,
    _format_usage_footer,
)
from ..cc_config.cc_config import CCUIDConfig
from .presentation.delivery import (
    RenderContext,
    send_blocks,
    send_permission_request,
)


async def render(
    bot: Bot,
    events: AsyncIterator[object],
    ctx: RenderContext,
    *,
    usage_provider: Callable[[], PromptUsage | None] | None = None,
) -> None:
    """Stream events → blocks。`ask` 权限请求要 mid-stream flush，否则 agent 等审批、用户看不到卡片。

    策略：
      - agent_md / tool / plan 等累积成一批
      - stream fragments (agent/think) 各自累积到 buf，遇非匹配 kind / 流末 flush 成 ChatBlock
      - PermissionEvent: flush 全部 + 发审批卡 + 继续
      - 流末: flush 残余 + 追加 usage footer

    usage_provider 返回 None = 该 engine 不报数据（如 claude/cursor）→ 不显示 footer。"""
    show_thinking = bool(CCUIDConfig.get_config("ShowThinking").data)
    tool_display: ToolDisplayMode = CCUIDConfig.get_config("ToolDisplay").data
    show_auto_perms = bool(CCUIDConfig.get_config("ShowAutoPermissions").data)
    buffer = _RenderBuffer()

    async def flush_pending() -> None:
        blocks = buffer.pop_pending()
        if blocks:
            await send_blocks(bot, blocks, ctx)

    try:
        async for ev in events:
            if isinstance(ev, PromptResponse):
                continue
            out = _classify(ev, show_thinking, tool_display)
            if out is None:
                continue
            if isinstance(out, tuple):
                kind, text, message_id = out
                buffer.append_fragment(kind, text, message_id)
                continue
            if out.kind == "permission":
                if out.meta["decision"] != "ask" and not show_auto_perms:
                    continue
                await flush_pending()
                if out.meta["decision"] == "ask":
                    await send_permission_request(bot, out, ctx)
                else:
                    await send_blocks(bot, [out], ctx)
            else:
                buffer.append_block(out)
    except BackendError as e:
        buffer.append_block(ChatBlock("error", user_error(e)))
    except Exception:
        logger.exception(f"[CCUID/{ctx.engine}] event stream crashed")

    buffer.flush_streams()
    if usage_provider is not None:
        usage = usage_provider()
        if usage is not None:
            line = _format_usage_footer(usage)
            if line:
                buffer.append_block(ChatBlock("usage_footer", line))
    blocks = buffer.pop_pending()
    if blocks:
        await send_blocks(bot, blocks, ctx)
