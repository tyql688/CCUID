from __future__ import annotations

from typing import Any
from dataclasses import dataclass

from pydantic import Field, AliasPath, BaseModel, AliasChoices, ValidationError

from acp.schema import AvailableCommand, SessionConfigSelectOption
from gsuid_core.logger import logger


class _GrokModel(BaseModel):
    model_id: str = Field(alias="modelId")
    name: str
    description: str | None = None


class _GrokMetadata(BaseModel):
    current_model_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            AliasPath("x.ai/sessionDetail", "currentModelId"),
            AliasPath("modelState", "currentModelId"),
        ),
    )
    models: list[_GrokModel] = Field(
        default_factory=list,
        validation_alias=AliasPath("modelState", "availableModels"),
    )
    available_commands: list[AvailableCommand] = Field(
        default_factory=list,
        alias="availableCommands",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class GrokPresentation:
    model_id: str | None = None
    model_name: str | None = None
    available_models: tuple[SessionConfigSelectOption, ...] = ()
    available_commands: tuple[AvailableCommand, ...] = ()


def extract_grok_presentation(
    initialize_metadata: dict[str, Any] | None,
    session_metadata: dict[str, Any] | None,
) -> GrokPresentation:
    raw: dict[str, Any] = {}
    if initialize_metadata is not None:
        raw.update(initialize_metadata)
    if session_metadata is not None:
        raw.update(session_metadata)
    try:
        metadata = _GrokMetadata.model_validate(raw)
    except ValidationError as error:
        logger.warning(f"[CCUID/grok] ACP extension metadata invalid: {error!r}")
        return GrokPresentation()

    available = tuple(
        SessionConfigSelectOption(
            value=model.model_id,
            name=model.name,
            description=model.description,
        )
        for model in metadata.models
    )
    current_model_name = next(
        (model.name for model in available if model.value == metadata.current_model_id),
        None,
    )
    return GrokPresentation(
        model_id=metadata.current_model_id,
        model_name=current_model_name,
        available_models=available,
        available_commands=tuple(metadata.available_commands),
    )
