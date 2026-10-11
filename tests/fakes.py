"""A scripted stand-in for the model, so the loop can be tested offline."""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Union

from agent.model import ModelResponse, ToolCallRequest, TransientModelError


def tool_call(name: str, args: Union[Dict[str, Any], str], call_id: str) -> ToolCallRequest:
    """args may be a dict (serialized to JSON) or a raw string, to script malformed arguments."""
    return ToolCallRequest(call_id=call_id, name=name, arguments=args if isinstance(args, str) else json.dumps(args))


def calls(*requests: ToolCallRequest, tokens: int = 10) -> ModelResponse:
    """A response asking for one or more tool calls."""
    items = [
        {"type": "function_call", "call_id": r.call_id, "name": r.name, "arguments": r.arguments} for r in requests
    ]
    return ModelResponse(status="completed", output_items=items, tool_calls=list(requests), total_tokens=tokens)


def reply(text: str, tokens: int = 10) -> ModelResponse:
    """A final response with no tool calls."""
    item = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    return ModelResponse(status="completed", output_items=[item], text=text, total_tokens=tokens)


def incomplete(reason: str = "max_output_tokens", tokens: int = 10) -> ModelResponse:
    item = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "partial"}]}
    return ModelResponse(
        status="incomplete", output_items=[item], text="partial", total_tokens=tokens, incomplete_reason=reason
    )


class ScriptedModel:
    """Returns (or raises) the scripted items in order and records every request."""

    def __init__(self, script: List[Union[ModelResponse, Exception]]):
        self.script = list(script)
        self.requests: List[Dict[str, Any]] = []

    def create(self, *, instructions, input, tools) -> ModelResponse:
        # Snapshot the input: the loop keeps appending to the same list.
        self.requests.append({"instructions": instructions, "input": copy.deepcopy(input), "tools": tools})
        if not self.script:
            raise AssertionError("ScriptedModel ran out of scripted responses")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def transient(message: str = "rate limited") -> TransientModelError:
    return TransientModelError(message)
