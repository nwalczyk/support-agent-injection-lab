"""Interactive chat with the agent: pick a submitting account, type a ticket,
and watch each tool call, its result and the final reply.

    python harness/chat.py                      # choose the account from a list
    python harness/chat.py --account ACC-10022  # skip the prompt
    python harness/chat.py --config NAME        # apply configs/NAME.yaml
    python harness/chat.py --attack t6 --variant baseline

End each message with a line containing only "." (or Ctrl-D); Ctrl-D on an
empty message quits. After the agent replies you can answer as the customer:
the reply continues the same ticket (same transcript, same tool state, same
per-ticket step budget). Type /new to close the ticket and start a fresh one.
A ticket that stops for any reason other than a normal reply (step limit,
model error) can't be continued, and the chat starts a new ticket.

--attack t6 --variant baseline sets up T6's scenario before the chat starts:
the payload is planted in the account data, and the submitting account is
preselected. Type your own ticket, or send just "." to use T6's ticket.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.config import resolve_config
from agent.context import LabData, Ticket, load_lab_data
from agent.loop import COMPLETED, RunResult, continue_ticket, run_ticket
from agent.model import build_model
from agent.tools import ToolCall
from attacks import Scenario
from harness.run import load_attack


def choose_account(data: LabData, preset: Optional[str]) -> str:
    ids = [a["account_id"] for a in data.accounts]
    if preset:
        if preset not in ids:
            sys.exit(f"unknown account '{preset}'. Known: {', '.join(ids)}")
        return preset
    print("Submitting account:")
    for i, account in enumerate(data.accounts, 1):
        print(f"  {i}. {account['account_id']}  {account['email']}")
    while True:
        choice = input("Choose a number or account ID: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(ids):
            return ids[int(choice) - 1]
        if choice.upper() in ids:
            return choice.upper()
        print("Not a listed account.")


NEW_TICKET = "/new"


def read_message(prompt: str, default: Optional[str] = None, commands: tuple = ()) -> Optional[str]:
    """A typed multi-line message ended by a line containing only ".".

    An empty message ended with "." returns default; Ctrl-D on an empty
    message returns None. A first line that is exactly one of commands is
    returned immediately, without waiting for "."."""
    print(prompt)
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            return "\n".join(lines).strip() or None
        if not lines and line.strip() in commands:
            return line.strip()
        if line.strip() == ".":
            return "\n".join(lines).strip() or default
        lines.append(line)


def read_ticket(default: Optional[str] = None) -> Optional[str]:
    hint = ' ("." alone sends the attack ticket)' if default else ""
    return read_message(
        f'\nType the ticket. End with a line containing only "."{hint}. Ctrl-D on an empty ticket quits.', default
    )


def read_follow_up() -> Optional[str]:
    return read_message(
        f'\nReply as the customer (end with "."), or {NEW_TICKET} for a new ticket. Ctrl-D quits.',
        commands=(NEW_TICKET,),
    )


def apply_attack(data: LabData, name: str, variant: Optional[str]) -> Scenario:
    attack = load_attack(name)
    if variant not in attack.VARIANTS:
        sys.exit(f"--attack {name} needs --variant, one of: {', '.join(attack.VARIANTS)}")
    scenario = attack.setup(data, variant)
    print(f"Planted {name} variant={variant}. Attack ticket from {scenario.submitter_account_id}:\n")
    print(scenario.ticket)
    return scenario


def print_result(result: RunResult, turn: int) -> None:
    print(f"\n=== Reply, turn {turn} ({result.stop_reason}) ===")
    print(result.final_text if result.final_text is not None else f"(no reply: {result.error})")
    print(f"\nticket totals: steps={result.steps} model_calls={result.model_calls} tokens={result.total_tokens}")


def print_tool_call(call: ToolCall) -> None:
    args = json.dumps(call.args) if call.args is not None else call.arguments
    print(f"\n-> {call.name}({args})")
    print(f"<- {json.dumps(call.result, indent=2)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--account", help="submitting account ID, e.g. ACC-10022")
    parser.add_argument("--config", help="mitigation override: a configs/ name or a .yaml path")
    parser.add_argument("--model", help="model profile from config.yaml models: (default: active_model)")
    parser.add_argument("--attack", help="plant an attack's payload for the session, e.g. t6")
    parser.add_argument("--variant", help="the attack variant to plant, e.g. baseline")
    args = parser.parse_args()
    if args.variant and not args.attack:
        parser.error("--variant needs --attack")

    config = resolve_config(args.config, model=args.model)
    data = load_lab_data()
    model = build_model(config)
    print(f"Model: {config['model']['name']}  Config: {config['config_name']}")
    scenario = apply_attack(data, args.attack, args.variant) if args.attack else None
    account_id = choose_account(data, args.account or (scenario and scenario.submitter_account_id))

    while True:
        body = read_ticket(default=scenario.ticket if scenario else None)
        if body is None:
            return 0
        ticket = Ticket(submitter_account_id=account_id, body=body)
        print(f"\n[{ticket.ticket_id} from {account_id}] running...")
        result = run_ticket(ticket, model, config=config, data=data, on_tool_call=print_tool_call)
        turn = 1
        print_result(result, turn)

        # Back and forth on the same ticket until /new, Ctrl-D, or a stop.
        while result.stop_reason == COMPLETED:
            message = read_follow_up()
            if message is None:
                return 0
            if message == NEW_TICKET:
                break
            turn += 1
            print(f"\n[{ticket.ticket_id} turn {turn}] running...")
            result = continue_ticket(result, message, model, config=config, on_tool_call=print_tool_call)
            print_result(result, turn)
        if result.stop_reason != COMPLETED:
            print(f"\nTicket {ticket.ticket_id} ended ({result.stop_reason}); it can't be continued.")


if __name__ == "__main__":
    sys.exit(main())
