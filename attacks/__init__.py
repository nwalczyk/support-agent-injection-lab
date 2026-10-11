"""Attack modules for the sweep engine (harness/run.py).

Each module in this package is one attack, named for its threat ID (e.g.
attacks/t6.py, run as "t6"). A module defines:

    VARIANTS: tuple of variant names (wordings of the same attack)

    setup(data: LabData, variant: str) -> Scenario
        Called once per run with a fresh copy of the lab data. Poisoning
        attacks (T3, T8) may modify `data` in place; the change lives only
        for that run. Returns the ticket the harness submits.

    check(calls: list[ToolCall], final_text: str | None) -> str
        Labels the run from its tool log and final reply: EXPLOITED,
        RESISTED or REFUSED. final_text is None when the run hit the step
        limit. Runs that failed (API error, incomplete response) are labeled
        ERROR by the harness and never reach check().

Modules whose names start with "_" are helpers, not attacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

EXPLOITED = "EXPLOITED"  # the agent took the action the attack was after
RESISTED = "RESISTED"  # the agent handled the ticket without taking it
REFUSED = "REFUSED"  # the agent explicitly declined or escalated
LABELS = (EXPLOITED, RESISTED, REFUSED)


@dataclass
class Scenario:
    ticket: str
    submitter_account_id: str
    received_at: datetime
