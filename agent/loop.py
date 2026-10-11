"""The agent loop: one ticket in, tool calls and a final reply out.

The conversation is accumulated by hand: each response's output items are
appended to the input list, followed by one function_call_output per tool
call, and the whole list is sent again. Nothing relies on server-side
conversation state.

Budgets: only the step limit (budgets.max_steps_per_ticket) is enforced. A
step is one tool call; a call that would exceed the limit is never executed
and the run stops. Token usage is summed into RunResult.total_tokens but not
enforced yet (T10, later milestone).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from agent.config import resolve_config
from agent.context import LabData, Ticket, build_system_prompt, build_user_message, load_lab_data
from agent.model import Model, ModelResponse, TransientModelError
from agent.tools import TOOL_SCHEMAS, ToolCall, ToolState, execute_tool

# stop_reason values
COMPLETED = "completed"  # the model gave a final reply with no tool calls
STEP_LIMIT = "step_limit"  # the model asked for more tool calls than the budget allows
INCOMPLETE = "incomplete"  # every attempt came back incomplete (e.g. max_output_tokens)
MODEL_ERROR = "model_error"  # every attempt failed with a transient error or a failed status


@dataclass
class RunResult:
    ticket: Ticket
    stop_reason: str
    final_text: Optional[str]
    state: ToolState
    steps: int
    model_calls: int
    total_tokens: int
    conversation: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None


def _call_model(
    model: Model,
    instructions: str,
    conversation: List[Dict[str, Any]],
    max_retries: int,
    retry_backoff_s: float,
    sleep: Callable[[float], None],
) -> Tuple[Optional[ModelResponse], int, int, Optional[str], Optional[str]]:
    """One logical model call with retries.

    Retries transient errors, failed responses and incomplete responses, up
    to max_retries extra attempts. Output from an unusable attempt is
    discarded, never appended. Other exceptions propagate (bad config, auth).

    Returns (response or None, attempts, tokens used, stop_reason, error)."""
    attempts = tokens = 0
    stop_reason = error = None
    for attempt in range(max_retries + 1):
        if attempt:
            sleep(retry_backoff_s * 2 ** (attempt - 1))
        attempts += 1
        try:
            response = model.create(instructions=instructions, input=conversation, tools=TOOL_SCHEMAS)
        except TransientModelError as exc:
            stop_reason, error = MODEL_ERROR, str(exc)
            continue
        tokens += response.total_tokens
        if response.status == "incomplete":
            stop_reason, error = INCOMPLETE, f"incomplete response ({response.incomplete_reason or 'no reason given'})"
            continue
        if response.status == "failed":
            stop_reason, error = MODEL_ERROR, "response status 'failed'"
            continue
        return response, attempts, tokens, None, None
    return None, attempts, tokens, stop_reason, error


def run_ticket(
    ticket: Ticket,
    model: Model,
    *,
    config: Optional[Mapping[str, Any]] = None,
    data: Optional[LabData] = None,
    on_tool_call: Optional[Callable[[ToolCall], None]] = None,
    max_retries: int = 3,
    retry_backoff_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> RunResult:
    """Run one ticket to completion with a fresh ToolState.

    on_tool_call, if given, is called after each tool call executes (chat.py
    uses it to print calls as they happen)."""
    config = config if config is not None else resolve_config()
    data = data if data is not None else load_lab_data()
    max_steps = config["budgets"]["max_steps_per_ticket"]

    instructions = build_system_prompt()
    conversation: List[Dict[str, Any]] = [{"role": "user", "content": build_user_message(ticket, data)}]
    state = ToolState.fresh(data.accounts)
    steps = model_calls = total_tokens = 0

    def finish(stop_reason: str, final_text: Optional[str] = None, error: Optional[str] = None) -> RunResult:
        return RunResult(
            ticket=ticket,
            stop_reason=stop_reason,
            final_text=final_text,
            state=state,
            steps=steps,
            model_calls=model_calls,
            total_tokens=total_tokens,
            conversation=conversation,
            error=error,
        )

    while True:
        response, attempts, tokens, stop_reason, error = _call_model(
            model, instructions, conversation, max_retries, retry_backoff_s, sleep
        )
        model_calls += attempts
        total_tokens += tokens
        if response is None:
            return finish(stop_reason, error=error)

        conversation.extend(response.output_items)
        if not response.tool_calls:
            return finish(COMPLETED, final_text=response.text)

        for request in response.tool_calls:
            if steps >= max_steps:
                return finish(STEP_LIMIT, error=f"step limit of {max_steps} reached; '{request.name}' not executed")
            steps += 1
            call = execute_tool(state, request.name, request.arguments)
            conversation.append(
                {"type": "function_call_output", "call_id": request.call_id, "output": json.dumps(call.result)}
            )
            if on_tool_call:
                on_tool_call(call)
