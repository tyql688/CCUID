from __future__ import annotations

from dataclasses import dataclass

from acp.schema import ToolCall, ToolKind, ToolCallUpdate


@dataclass(slots=True, frozen=True)
class ToolCallSnapshot:
    kind: ToolKind | None = None
    title: str | None = None


class ToolCallState:
    """Materialize ACP tool_call_update patch fields by toolCallId."""

    def __init__(self) -> None:
        self._calls: dict[str, ToolCallSnapshot] = {}

    def normalize(self, event: object) -> object:
        if isinstance(event, ToolCall):
            self._remember(event.tool_call_id, event.kind, event.title)
            return event
        if isinstance(event, ToolCallUpdate):
            return self.normalize_update(event)
        return event

    def normalize_update(self, update: ToolCallUpdate) -> ToolCallUpdate:
        previous = self._calls.get(update.tool_call_id)
        kind = update.kind if update.kind is not None else (previous.kind if previous is not None else None)
        title = update.title if update.title is not None else (previous.title if previous is not None else None)
        self._remember(update.tool_call_id, kind, title)
        if kind == update.kind and title == update.title:
            return update
        return update.model_copy(update={"kind": kind, "title": title})

    def _remember(self, tool_call_id: str, kind: ToolKind | None, title: str | None) -> None:
        previous = self._calls.get(tool_call_id)
        self._calls[tool_call_id] = ToolCallSnapshot(
            kind=kind if kind is not None else (previous.kind if previous is not None else None),
            title=title if title is not None else (previous.title if previous is not None else None),
        )
