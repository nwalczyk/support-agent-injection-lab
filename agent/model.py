"""Model adapter. The loop talks to a model only through `create()` and the
normalized ModelResponse, so tests can swap in a scripted fake and a new
provider only needs a new adapter.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Protocol

# openai and dotenv are imported inside OpenAIResponsesModel, so the offline
# tests never touch them.


class TransientModelError(Exception):
    """A model call failed in a way worth retrying (rate limit, timeout,
    connection drop, provider 5xx). Anything else is raised as-is."""


@dataclass
class ToolCallRequest:
    call_id: str
    name: str
    arguments: str  # raw JSON string, possibly malformed


@dataclass
class ModelResponse:
    # "completed", "incomplete" or "failed"
    status: str
    # Output items in the provider's input format, appended verbatim to the
    # conversation so the next request carries them (reasoning items included).
    output_items: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: List[ToolCallRequest] = field(default_factory=list)
    text: str = ""
    total_tokens: int = 0
    incomplete_reason: Optional[str] = None


class Model(Protocol):
    def create(self, *, instructions: str, input: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> ModelResponse: ...


class OpenAIResponsesModel:
    """OpenAI Responses API (or any compatible gateway via base_url)."""

    def __init__(self, model_cfg: Mapping[str, Any]):
        import openai
        from dotenv import load_dotenv

        load_dotenv()
        api_key = os.environ.get(model_cfg["api_key_env"])
        if not api_key:
            raise RuntimeError(f"{model_cfg['api_key_env']} not set in environment / .env")
        # The loop does its own retries, so the SDK's are turned off.
        self._client = openai.OpenAI(api_key=api_key, base_url=model_cfg.get("base_url") or None, max_retries=0)
        self._transient = (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError)
        self._cfg = model_cfg

    def create(self, *, instructions, input, tools) -> ModelResponse:
        kwargs: Dict[str, Any] = dict(
            model=self._cfg["name"],
            instructions=instructions,
            input=input,
            tools=tools,
            max_output_tokens=self._cfg.get("max_tokens", 1024),
        )
        if self._cfg.get("temperature") is not None:
            kwargs["temperature"] = self._cfg["temperature"]
        effort = self._cfg.get("reasoning_effort")
        if effort and str(effort).lower() != "none":
            kwargs["reasoning"] = {"effort": effort}

        try:
            response = self._client.responses.create(**kwargs)
        except self._transient as exc:
            raise TransientModelError(f"{type(exc).__name__}: {exc}") from exc

        details = getattr(response, "incomplete_details", None)
        return ModelResponse(
            status=response.status or "completed",
            output_items=[item.model_dump(exclude_none=True) for item in response.output],
            tool_calls=[
                ToolCallRequest(call_id=item.call_id, name=item.name, arguments=item.arguments)
                for item in response.output
                if item.type == "function_call"
            ],
            text=response.output_text or "",
            total_tokens=response.usage.total_tokens if response.usage else 0,
            incomplete_reason=getattr(details, "reason", None),
        )


def build_model(config: Mapping[str, Any]) -> Model:
    return OpenAIResponsesModel(config["model"])
