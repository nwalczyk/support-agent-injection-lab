"""Model adapter. The loop talks to a model only through `create()` and the
normalized ModelResponse, so tests can swap in a scripted fake and a new
provider only needs a new adapter.

Two adapters included: OpenAIResponsesModel (OpenAI Responses API, used for the
strong model) and ChatCompletionsModel (OpenAI Chat Completions, used for
local servers like Ollama / LM Studio / llama.cpp, which do not implement the
Responses API). The loop's transcript is always in the format of Responses; the Chat
Completions adapter translates to and from that format internally, so the distinction
doesn't need to be handled in the loop or attack harness.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Protocol

# openai and dotenv are imported inside the adapter classes, so the offline
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


# --- Responses <-> Chat Completions translation (pure, unit-tested) ---------
#
# The loop keeps the conversation in Responses shape: a user message dict, the
# model's output_items (message / function_call items), and the loop's own
# function_call_output items. These helpers map that to Chat Completions
# messages on the way in, and a Chat Completions reply back to a ModelResponse
# whose output_items are in the same Responses shape on the way out, so the
# loop's accumulation stays consistent across turns.


def _item_text(item: Dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def responses_tools_to_chat(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flat Responses tool schema -> nested Chat Completions tool schema."""
    out = []
    for tool in tools:
        function = {"name": tool["name"], "parameters": tool["parameters"]}
        if tool.get("description"):
            function["description"] = tool["description"]
        out.append({"type": "function", "function": function})
    return out


def responses_input_to_chat_messages(instructions: str, input_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Translate the Responses-shaped transcript into Chat Completions messages.

    Consecutive assistant items (a message plus its function_call items) are
    merged into one assistant message carrying both content and tool_calls, as
    Chat Completions expects."""
    messages: List[Dict[str, Any]] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    pending: Optional[Dict[str, Any]] = None

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        message: Dict[str, Any] = {"role": "assistant"}
        if pending["tool_calls"]:
            message["tool_calls"] = pending["tool_calls"]
            message["content"] = pending["content"] or ""
        else:
            message["content"] = pending["content"]
        messages.append(message)
        pending = None

    def ensure() -> Dict[str, Any]:
        nonlocal pending
        if pending is None:
            pending = {"content": "", "tool_calls": []}
        return pending

    for item in input_items:
        itype = item.get("type")
        role = item.get("role")
        if itype == "function_call":
            ensure()["tool_calls"].append(
                {
                    "id": item["call_id"],
                    "type": "function",
                    "function": {"name": item["name"], "arguments": item["arguments"]},
                }
            )
        elif itype == "function_call_output":
            flush()
            messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]})
        elif itype == "reasoning":
            continue
        elif role == "assistant":
            ensure()["content"] += _item_text(item)
        elif role in ("user", "system"):
            flush()
            messages.append({"role": role, "content": _item_text(item)})
        # Anything else (unknown item types) is skipped.
    flush()
    return messages


def chat_response_to_model_response(payload: Mapping[str, Any]) -> ModelResponse:
    """Chat Completions response (as a dict, e.g. from .model_dump()) ->
    ModelResponse with Responses-shaped output_items."""
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    finish = choice.get("finish_reason")
    content = message.get("content") or ""
    raw_tool_calls = message.get("tool_calls") or []

    output_items: List[Dict[str, Any]] = []
    if content:
        output_items.append(
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": content}]}
        )
    tool_calls: List[ToolCallRequest] = []
    for i, call in enumerate(raw_tool_calls):
        function = call.get("function", {})
        call_id = call.get("id") or f"call_{i}"
        arguments = function.get("arguments", "")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments)
        output_items.append(
            {"type": "function_call", "call_id": call_id, "name": function.get("name", ""), "arguments": arguments}
        )
        tool_calls.append(ToolCallRequest(call_id=call_id, name=function.get("name", ""), arguments=arguments))

    usage = payload.get("usage") or {}
    return ModelResponse(
        status="incomplete" if finish == "length" else "completed",
        output_items=output_items,
        tool_calls=tool_calls,
        text=content,
        total_tokens=usage.get("total_tokens", 0),
        incomplete_reason="max_tokens" if finish == "length" else None,
    )


class ChatCompletionsModel:
    """OpenAI Chat Completions API: local servers (Ollama, LM Studio,
    llama.cpp) and compatible gateways that do not implement the Responses
    API. Translates to and from the loop's Responses-shaped transcript."""

    def __init__(self, model_cfg: Mapping[str, Any]):
        import openai
        from dotenv import load_dotenv

        load_dotenv()
        # Local servers ignore the key but the SDK still requires a non-empty
        # one, so fall back to a placeholder rather than failing.
        api_key = os.environ.get(model_cfg["api_key_env"]) or "local-no-key"
        self._client = openai.OpenAI(api_key=api_key, base_url=model_cfg.get("base_url") or None, max_retries=0)
        self._transient = (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError)
        self._cfg = model_cfg

    def create(self, *, instructions, input, tools) -> ModelResponse:
        kwargs: Dict[str, Any] = dict(
            model=self._cfg["name"],
            messages=responses_input_to_chat_messages(instructions, input),
            tools=responses_tools_to_chat(tools),
            max_tokens=self._cfg.get("max_tokens", 1024),
        )
        if self._cfg.get("temperature") is not None:
            kwargs["temperature"] = self._cfg["temperature"]
        try:
            response = self._client.chat.completions.create(**kwargs)
        except self._transient as exc:
            raise TransientModelError(f"{type(exc).__name__}: {exc}") from exc
        return chat_response_to_model_response(response.model_dump())


_ADAPTERS = {"responses": OpenAIResponsesModel, "chat_completions": ChatCompletionsModel}


def build_model(config: Mapping[str, Any]) -> Model:
    model_cfg = config["model"]
    api = str(model_cfg.get("api") or "responses").lower()
    try:
        adapter = _ADAPTERS[api]
    except KeyError:
        raise ValueError(f"unknown model.api {api!r}; expected one of {sorted(_ADAPTERS)}") from None
    return adapter(model_cfg)
