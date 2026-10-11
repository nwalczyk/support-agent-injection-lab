"""Interactive chat with the agent: pick a submitting account, type a ticket,
and watch each tool call, its result and the final reply.

    python harness/chat.py                      # choose the account from a list
    python harness/chat.py --account ACC-10022  # skip the prompt
    python harness/chat.py --config NAME        # apply configs/NAME.yaml

Each ticket is a separate run with fresh tool state. End a ticket with a line
containing only "." (or Ctrl-D); Ctrl-D on an empty ticket quits.
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
from agent.loop import run_ticket
from agent.model import build_model
from agent.tools import ToolCall


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


def read_ticket() -> Optional[str]:
    print('\nType the ticket. End with a line containing only "." (Ctrl-D on an empty ticket quits).')
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == ".":
            break
        lines.append(line)
    text = "\n".join(lines).strip()
    return text or None


def print_tool_call(call: ToolCall) -> None:
    args = json.dumps(call.args) if call.args is not None else call.arguments
    print(f"\n-> {call.name}({args})")
    print(f"<- {json.dumps(call.result, indent=2)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--account", help="submitting account ID, e.g. ACC-10022")
    parser.add_argument("--config", help="override config: a configs/ name or a .yaml path")
    args = parser.parse_args()

    config = resolve_config(args.config)
    data = load_lab_data()
    model = build_model(config)
    print(f"Model: {config['model']['name']}  Config: {config['config_name']}")
    account_id = choose_account(data, args.account)

    while True:
        body = read_ticket()
        if body is None:
            return 0
        ticket = Ticket(submitter_account_id=account_id, body=body)
        print(f"\n[{ticket.ticket_id} from {account_id}] running...")
        result = run_ticket(ticket, model, config=config, data=data, on_tool_call=print_tool_call)

        print(f"\n=== Reply ({result.stop_reason}) ===")
        print(result.final_text if result.final_text is not None else f"(no reply: {result.error})")
        print(f"\nsteps={result.steps} model_calls={result.model_calls} tokens={result.total_tokens}")


if __name__ == "__main__":
    sys.exit(main())
