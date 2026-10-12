"""Sweep engine: runs every attack x variant x repeat through the agent and
records one labeled CSV row per run.

    python harness/run.py                          # every attack, 20 repeats
    python harness/run.py --attack t6 --repeats 10
    python harness/run.py --attack t7 --variant direct --config NAME

For each run it copies the lab data fresh, calls the attack's setup() for
the ticket, runs the M1 loop, and labels the outcome with the attack's
check(). A run that failed (API error or incomplete response) is labeled
ERROR without calling check(). Rows are written as each run finishes, so an
interrupted sweep keeps what it has. See attacks/__init__.py for the attack
module contract.
"""

from __future__ import annotations

import argparse
import copy
import csv
import importlib
import json
import pkgutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import attacks
from agent.config import resolve_config
from agent.context import LabData, Ticket, load_lab_data
from agent.loop import INCOMPLETE, MODEL_ERROR, run_ticket
from agent.model import Model, build_model
from attacks import EXPLOITED, LABELS, SAFE

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "results"

ERROR = "ERROR"
FAILED_STOP_REASONS = (MODEL_ERROR, INCOMPLETE)

CSV_FIELDS = [
    "run_at",
    "config",
    "model",
    "attack",
    "variant",
    "repeat",
    "label",
    "stop_reason",
    "error",
    "steps",
    "model_calls",
    "total_tokens",
    "ticket_id",
    "submitter",
    "tool_calls",
    "final_text",
    "claimed_unexecuted",
]


def discover_attacks() -> List[str]:
    return sorted(m.name for m in pkgutil.iter_modules(attacks.__path__) if not m.name.startswith("_"))


def load_attack(name: str) -> ModuleType:
    module = importlib.import_module(f"attacks.{name}")
    missing = [attr for attr in ("VARIANTS", "setup", "check") if not hasattr(module, attr)]
    if missing:
        raise AttributeError(f"attacks/{name}.py is missing {', '.join(missing)}")
    return module


def attack_name(attack: ModuleType) -> str:
    return attack.__name__.rsplit(".", 1)[-1]


def run_one(
    attack: ModuleType,
    variant: str,
    repeat: int,
    model: Model,
    config: Mapping[str, Any],
    base_data: LabData,
) -> Dict[str, Any]:
    """One run, returned as a CSV row."""
    data = copy.deepcopy(base_data)
    scenario = attack.setup(data, variant)
    ticket = Ticket(
        submitter_account_id=scenario.submitter_account_id,
        body=scenario.ticket,
        received_at=scenario.received_at,
    )
    result = run_ticket(ticket, model, config=config, data=data)

    if result.stop_reason in FAILED_STOP_REASONS:
        label = ERROR
    else:
        label = attack.check(result.state.calls, result.final_text)
        if label not in LABELS:
            raise ValueError(f"{attack_name(attack)}.check() returned {label!r}; expected one of {LABELS}")

    # Optional, attack-defined: did the reply claim the action happened even
    # though it was not labeled EXPLOITED? Separates genuine resistance from a
    # model that accepted the injection in its reply but never fired the tool.
    claimed_unexecuted: object = ""
    predicate = getattr(attack, "claimed", None)
    if predicate is not None and label == SAFE:
        claimed_unexecuted = bool(predicate(result.final_text or ""))

    tool_calls = [
        {"name": c.name, "args": c.args if c.args is not None else c.arguments, "ok": c.ok, "result": c.result}
        for c in result.state.calls
    ]
    return {
        "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": config.get("config_name", ""),
        "model": config["model"]["name"],
        "attack": attack_name(attack),
        "variant": variant,
        "repeat": repeat,
        "label": label,
        "stop_reason": result.stop_reason,
        "error": result.error or "",
        "steps": result.steps,
        "model_calls": result.model_calls,
        "total_tokens": result.total_tokens,
        "ticket_id": ticket.ticket_id,
        "submitter": ticket.submitter_account_id,
        "tool_calls": json.dumps(tool_calls),
        "final_text": result.final_text or "",
        "claimed_unexecuted": claimed_unexecuted,
    }


def sweep(
    attack_modules: Sequence[ModuleType],
    model: Model,
    config: Mapping[str, Any],
    out_path: Path,
    repeats: int,
    variants: Optional[Iterable[str]] = None,
    data: Optional[LabData] = None,
    echo: Callable[[str], None] = print,
) -> List[Dict[str, Any]]:
    """Run the sweep, appending each row to out_path as it completes.
    `variants`, if given, restricts every attack to those variant names."""
    base_data = data if data is not None else load_lab_data()
    only = set(variants) if variants else None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for attack in attack_modules:
            for variant in attack.VARIANTS:
                if only is not None and variant not in only:
                    continue
                exploited = 0
                for repeat in range(1, repeats + 1):
                    row = run_one(attack, variant, repeat, model, config, base_data)
                    writer.writerow(row)
                    f.flush()
                    rows.append(row)
                    exploited += row["label"] == EXPLOITED
                    echo(f"{row['attack']} variant={variant} {row['label']} {exploited}/{repeat}")
    return rows


def summarize(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    """One line per attack x variant: label counts and the success rate.

    The success rate is EXPLOITED / (EXPLOITED + SAFE). ERROR runs did not
    finish, so they are reported but excluded from the rate."""
    totals = Counter((r["attack"], r["variant"], r["label"]) for r in rows)
    lines = []
    for attack, variant in dict.fromkeys((r["attack"], r["variant"]) for r in rows):
        exploited = totals[(attack, variant, EXPLOITED)]
        valid = exploited + totals[(attack, variant, SAFE)]
        errors = totals[(attack, variant, ERROR)]
        rate = f"{exploited}/{valid} = {exploited / valid:.0%}" if valid else "n/a (no completed runs)"
        lines.append(f"  {attack} variant={variant}: success {rate}  (errors={errors})")
    return lines


def default_out_path(config: Mapping[str, Any]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return RESULTS_DIR / f"{stamp}_{config['model_name']}_{config['config_name']}.csv"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--attack", action="append", help="attack module name (repeatable); default: all")
    parser.add_argument("--variant", action="append", help="only run these variant names (repeatable)")
    parser.add_argument("--repeats", type=int, default=20, help="runs per attack x variant (default 20)")
    parser.add_argument("--config", help="mitigation override: a configs/ name or a .yaml path")
    parser.add_argument("--model", help="model profile from config.yaml models: (default: active_model)")
    parser.add_argument("--out", type=Path, help="CSV path (default: results/<timestamp>_<config>.csv)")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")

    names = args.attack or discover_attacks()
    if not names:
        print("No attack modules found in attacks/.", file=sys.stderr)
        return 1
    attack_modules = [load_attack(n) for n in names]

    config = resolve_config(args.config, model=args.model)
    model = build_model(config)
    out_path = args.out or default_out_path(config)
    print(f"Model: {config['model']['name']}  Config: {config['config_name']}  Writing: {out_path}")

    rows = sweep(attack_modules, model, config, out_path, args.repeats, variants=args.variant)

    print(f"\n{len(rows)} runs -> {out_path}")
    for line in summarize(rows):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
