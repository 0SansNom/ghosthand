import base64
import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic

from .errors import GhosthandError

DEFAULT_MODEL = os.environ.get("GHOSTHAND_MODEL", "claude-opus-5-5")
# One action per step needs little deliberation; raise it for harder apps.
DEFAULT_EFFORT = os.environ.get("GHOSTHAND_EFFORT", "low")
MAX_TOKENS = 16000

FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(GhosthandError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens


@dataclass
class Decision:
    tool: str | None
    input: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    stop_reason: str | None = None
    usage: Usage = field(default_factory=Usage)


@dataclass
class Judgment:
    data: dict[str, Any] | None
    usage: Usage = field(default_factory=Usage)
    error: str = ""


class DecisionMaker(Protocol):
    def decide(self, system: list[str], text: str, image_jpeg: bytes | None, tools: list[dict]) -> Decision: ...

    def judge(self, system: list[str], text: str, image_jpeg: bytes | None, schema: dict,
              effort: str | None = None) -> Judgment: ...


class Claude:
    def __init__(self, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT):
        self.model = model
        self.effort = effort
        # Resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile.
        try:
            self.client = anthropic.Anthropic()
        except anthropic.AnthropicError as error:
            raise LLMError(f"No usable Anthropic credentials ({error}). Set ANTHROPIC_API_KEY "
                           "or run `ant auth login`.") from error

    def decide(self, system: list[str], text: str, image_jpeg: bytes | None, tools: list[dict]) -> Decision:
        response = self._create(system, text, image_jpeg, self.effort, tools=tools,
                                tool_choice={"type": "auto", "disable_parallel_tool_use": True})
        decision = Decision(tool=None, stop_reason=response.stop_reason, usage=_usage(response))
        if response.stop_reason == "refusal":
            decision.text = _refusal(response)
            return decision
        for block in response.content:
            if block.type == "text":
                decision.text += block.text
            elif block.type == "tool_use" and decision.tool is None:
                decision.tool, decision.input = block.name, dict(block.input)
        return decision

    def judge(self, system: list[str], text: str, image_jpeg: bytes | None, schema: dict,
              effort: str | None = None) -> Judgment:
        response = self._create(system, text, image_jpeg, effort or self.effort,
                                format={"type": "json_schema", "schema": schema})
        if response.stop_reason == "refusal":
            return Judgment(None, _usage(response), _refusal(response))
        raw = "".join(block.text for block in response.content if block.type == "text")
        try:
            return Judgment(json.loads(raw), _usage(response))
        except json.JSONDecodeError:  # e.g. cut off by max_tokens
            return Judgment(None, _usage(response), f"unreadable answer (stop_reason={response.stop_reason})")

    def _create(self, system: list[str], text: str, image_jpeg: bytes | None, effort: str,
                format: dict | None = None, **kwargs: Any):
        content: list[dict] = [{"type": "text", "text": text}]
        if image_jpeg:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(image_jpeg).decode(),
            }})
        # System blocks are stable for a whole run; cache up to the last one.
        system_blocks = [{"type": "text", "text": block} for block in system]
        system_blocks[-1]["cache_control"] = {"type": "ephemeral"}
        output_config: dict[str, Any] = {"effort": effort}
        if format:
            output_config["format"] = format
        if self.model in FALLBACK_MODELS:
            kwargs.update(fallbacks="default", betas=[FALLBACK_BETA])
        try:
            return self.client.beta.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system_blocks,
                messages=[{"role": "user", "content": content}],
                output_config=output_config,
                **kwargs,
            )
        except anthropic.AuthenticationError as error:
            raise LLMError("Anthropic credentials rejected: set ANTHROPIC_API_KEY (or run `ant auth login`).") from error
        except anthropic.PermissionDeniedError as error:
            raise LLMError(f"Anthropic API permission denied: {error.message}") from error
        except anthropic.NotFoundError as error:
            raise LLMError(f"Unknown model {self.model!r}: {error.message}") from error
        except anthropic.BadRequestError as error:
            raise LLMError(f"Anthropic API rejected the request: {error.message}") from error
        except anthropic.APIConnectionError as error:
            raise LLMError(f"Cannot reach the Anthropic API: {error}") from error
        except anthropic.CredentialsError as error:
            raise LLMError("No Anthropic credentials: set ANTHROPIC_API_KEY (or run `ant auth login`).") from error
        except TypeError as error:
            # The SDK raises a bare TypeError when it finds no credentials at all.
            if "authentication method" not in str(error):
                raise
            raise LLMError("No Anthropic credentials: set ANTHROPIC_API_KEY (or run `ant auth login`).") from error
        # RateLimitError and 5xx are retried by the SDK; past that they propagate as-is.


def _usage(response) -> Usage:
    usage = response.usage
    return Usage(
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        cache_write_tokens=usage.cache_creation_input_tokens or 0,
    )


def _refusal(response) -> str:
    details = response.stop_details
    return f"Request declined ({details.category if details else 'no category'})."
