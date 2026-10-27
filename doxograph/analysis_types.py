"""Shared requests and normalized results at the model-provider boundary."""

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from typing import Any, Literal, NotRequired, Protocol, TypedDict, Unpack

from jsonschema import Draft202012Validator, ValidationError

from .model_errors import ProviderResponseError

JSONSchema = dict[str, Any]


class Message(TypedDict):
    role: Literal["user", "assistant"]
    # Document blocks follow the provider's JSON format, including PDF sources.
    content: str | list[dict[str, Any]]


class OutputFormat(TypedDict):
    schema: JSONSchema
    type: NotRequired[str]


class OutputConfig(TypedDict, total=False):
    effort: str
    format: OutputFormat


class ProviderRequest(TypedDict):
    model: str
    system: str
    messages: list[Message]
    max_tokens: NotRequired[int]
    thinking: NotRequired[dict[str, Any]]
    output_config: NotRequired[OutputConfig]


class MessageTransport(Protocol):
    def create(self, **kwargs: Unpack[ProviderRequest]) -> object: ...


class MessageClient(Protocol):
    @property
    def messages(self) -> MessageTransport: ...


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None

    @classmethod
    def from_provider(cls, value: object) -> "TokenUsage | None":
        if value is None:
            return None

        def count(name: str) -> int | None:
            number = value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
            return number if isinstance(number, int) and not isinstance(number, bool) and number >= 0 else None

        return cls(count("input_tokens"), count("output_tokens"), count("cache_creation_input_tokens"),
                   count("cache_read_input_tokens") if count("cache_read_input_tokens") is not None
                   else count("cached_input_tokens"))


@dataclass(frozen=True)
class TextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True)
class StopDetails:
    explanation: str = ""


@dataclass(frozen=True)
class ModelResponse:
    content: tuple[TextBlock, ...]
    stop_reason: str = "end_turn"
    usage: TokenUsage | None = None
    stop_details: StopDetails = field(default_factory=StopDetails)

    @property
    def text(self) -> str | None:
        return "".join(block.text for block in self.content) if self.content else None

    @classmethod
    def from_text(cls, text: str, usage: object = None) -> "ModelResponse":
        return cls((TextBlock(text),), usage=TokenUsage.from_provider(usage))


def normalize_response(value: object) -> ModelResponse:
    """Convert SDK objects at entry; their shapes never reach merge code."""
    if isinstance(value, ModelResponse):
        return value
    reason = getattr(value, "stop_reason", None)
    content = getattr(value, "content", None)
    if not isinstance(reason, str) or not isinstance(content, (list, tuple)):
        raise ProviderResponseError("AI analysis returned an unreadable response; nothing was saved")
    blocks = []
    for block in content:
        if getattr(block, "type", None) != "text":
            continue
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            raise ProviderResponseError("AI analysis returned unreadable text; nothing was saved")
        blocks.append(TextBlock(text))
    detail = getattr(getattr(value, "stop_details", None), "explanation", "")
    return ModelResponse(tuple(blocks), reason, TokenUsage.from_provider(getattr(value, "usage", None)),
                         StopDetails(detail if isinstance(detail, str) else ""))


def validate_response(response: ModelResponse, schema: JSONSchema) -> ModelResponse:
    # Preserve refusals and truncation so the caller can report the operation
    # and paper involved. They must never be parsed or merged as valid output.
    if response.stop_reason != "end_turn" or response.text is None:
        return response
    try:
        payload = json.loads(response.text)
        Draft202012Validator(schema).validate(payload)
    except (ValueError, ValidationError) as exc:
        raise ProviderResponseError("The selected model returned invalid analysis JSON; nothing was saved. "
                                    "Use a model and endpoint that support structured JSON output.") from exc
    return response
