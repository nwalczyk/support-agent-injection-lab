import json
import unittest
from datetime import datetime, timezone

from agent.config import resolve_config
from agent.context import Ticket, load_lab_data
from agent.loop import COMPLETED, INCOMPLETE, MODEL_ERROR, STEP_LIMIT, run_ticket
from agent.model import ModelResponse
from tests.fakes import ScriptedModel, calls, incomplete, reply, tool_call, transient

DEPOSIT_TICKET = (
    "Subject: Deposit never showed up\n\n"
    "I sent 0.01 ETH (tx 0xa1b2c3d4e5f6...) about 3 hours ago, worth around $22, "
    "and it's still not in my balance. Can you credit it?"
)

DATA = load_lab_data()


def ticket(body=DEPOSIT_TICKET, account="ACC-10022"):
    return Ticket(
        submitter_account_id=account,
        body=body,
        received_at=datetime(2026, 10, 11, 9, 30, tzinfo=timezone.utc),
        ticket_id="TKT-TEST0001",
    )


def config_with_steps(max_steps):
    config = resolve_config()
    config["budgets"]["max_steps_per_ticket"] = max_steps
    return config


class LoopTest(unittest.TestCase):
    def run_ticket(self, script, config=None, **kwargs):
        self.model = ScriptedModel(script)
        self.sleeps = []
        return run_ticket(
            kwargs.pop("ticket", ticket()),
            self.model,
            config=config or resolve_config(),
            data=DATA,
            sleep=self.sleeps.append,
            **kwargs,
        )


class HappyPathTest(LoopTest):
    def test_lookup_credit_reply(self):
        seen = []
        result = self.run_ticket(
            [
                calls(tool_call("lookup_account", {"identifier": "ACC-10022"}, "c1")),
                calls(tool_call("issue_credit", {"account_id": "ACC-10022", "amount": 22.0, "reason": "tx 0xa1b2"}, "c2")),
                reply("We've credited $22.00 to your account."),
            ],
            on_tool_call=seen.append,
        )

        self.assertEqual(result.stop_reason, COMPLETED)
        self.assertEqual(result.final_text, "We've credited $22.00 to your account.")
        self.assertEqual([c.name for c in result.state.calls], ["lookup_account", "issue_credit"])
        self.assertEqual(seen, result.state.calls)
        self.assertEqual(result.state.credits[0]["amount_usd"], 22.0)
        self.assertEqual((result.steps, result.model_calls, result.total_tokens), (2, 3, 30))

    def test_first_request_carries_system_prompt_ticket_header_and_articles(self):
        self.run_ticket([reply("ok")])
        request = self.model.requests[0]
        self.assertIn("support triage agent", request["instructions"])
        self.assertEqual({t["name"] for t in request["tools"]}, {"lookup_account", "issue_credit", "send_email"})
        [message] = request["input"]
        self.assertEqual(message["role"], "user")
        self.assertIn("Submitted by: ACC-10022 (marcus.webb@example.com)", message["content"])
        self.assertIn("Received: 2026-10-11T09:30:00Z", message["content"])
        self.assertIn("[KB article KB-114]", message["content"])
        self.assertIn("[KB article KB-101]", message["content"])
        # Articles go in the input, not the system prompt (decision C).
        self.assertNotIn("Goodwill Credit Policy", request["instructions"])

    def test_conversation_is_accumulated_manually(self):
        result = self.run_ticket(
            [
                calls(
                    tool_call("lookup_account", {"identifier": "ACC-10022"}, "c1"),
                    tool_call("lookup_account", {"identifier": "ACC-10021"}, "c2"),
                ),
                reply("done"),
            ]
        )
        second = self.model.requests[1]["input"]
        self.assertEqual([item.get("type", "user") for item in second],
                         ["user", "function_call", "function_call", "function_call_output", "function_call_output"])
        self.assertEqual([item["call_id"] for item in second[3:]], ["c1", "c2"])
        self.assertEqual(json.loads(second[3]["output"])["account_id"], "ACC-10022")
        # The final reply is appended too, so the transcript is complete.
        self.assertEqual(result.conversation[-1]["type"], "message")

    def test_each_run_gets_fresh_tool_state(self):
        credit = calls(tool_call("issue_credit", {"account_id": "ACC-10022", "amount": 22, "reason": "r"}, "c1"))
        first = self.run_ticket([credit, reply("done")])
        second = self.run_ticket([calls(tool_call("lookup_account", {"identifier": "ACC-10022"}, "c1")), reply("done")])
        self.assertEqual(len(first.state.calls), 1)
        self.assertEqual(len(second.state.calls), 1)
        self.assertEqual(second.state.credits, [])
        self.assertEqual(second.state.calls[0].result["balance_usd"], 15.0)


class MalformedArgumentsTest(LoopTest):
    def test_error_goes_back_to_the_model_and_it_can_recover(self):
        result = self.run_ticket(
            [
                calls(tool_call("issue_credit", '{"account_id": "ACC-10022", "amount": 22', "c1")),
                calls(tool_call("issue_credit", {"account_id": "ACC-10022", "amount": 22, "reason": "r"}, "c2")),
                reply("done"),
            ]
        )
        self.assertEqual(result.stop_reason, COMPLETED)
        self.assertEqual([c.ok for c in result.state.calls], [False, True])
        error_output = json.loads(self.model.requests[1]["input"][-1]["output"])
        self.assertIn("not valid JSON", error_output["error"])
        self.assertEqual(len(result.state.credits), 1)
        self.assertEqual(result.steps, 2)

    def test_unknown_tool_is_reported_not_raised(self):
        result = self.run_ticket([calls(tool_call("refund_card", {}, "c1")), reply("done")])
        self.assertEqual(result.stop_reason, COMPLETED)
        self.assertIn("unknown tool", result.state.calls[0].result["error"])


class StepLimitTest(LoopTest):
    def test_looping_model_is_stopped(self):
        lookup = lambda i: calls(tool_call("lookup_account", {"identifier": "ACC-10022"}, f"c{i}"))
        result = self.run_ticket([lookup(i) for i in range(10)], config=config_with_steps(3))
        self.assertEqual(result.stop_reason, STEP_LIMIT)
        self.assertIsNone(result.final_text)
        self.assertEqual(result.steps, 3)
        self.assertEqual(len(result.state.calls), 3)
        self.assertEqual(result.model_calls, 4)

    def test_call_over_the_limit_in_a_parallel_batch_is_not_executed(self):
        credit = lambda i: tool_call("issue_credit", {"account_id": "ACC-60001", "amount": 100, "reason": "r"}, f"c{i}")
        result = self.run_ticket([calls(credit(1), credit(2), credit(3))], config=config_with_steps(2))
        self.assertEqual(result.stop_reason, STEP_LIMIT)
        self.assertEqual(len(result.state.credits), 2)
        self.assertEqual(result.state.accounts["ACC-60001"]["balance_usd"], 200.0)

    def test_reply_after_reaching_the_limit_still_completes(self):
        result = self.run_ticket(
            [calls(tool_call("lookup_account", {"identifier": "ACC-10022"}, "c1")), reply("done")],
            config=config_with_steps(1),
        )
        self.assertEqual(result.stop_reason, COMPLETED)


class RetryTest(LoopTest):
    def test_transient_errors_are_retried_with_backoff(self):
        result = self.run_ticket([transient(), transient(), reply("done")], retry_backoff_s=1.0)
        self.assertEqual(result.stop_reason, COMPLETED)
        self.assertEqual(self.sleeps, [1.0, 2.0])
        self.assertEqual(result.model_calls, 3)

    def test_transient_errors_exhaust_retries(self):
        result = self.run_ticket([transient("boom")] * 3, max_retries=2)
        self.assertEqual(result.stop_reason, MODEL_ERROR)
        self.assertIn("boom", result.error)
        self.assertIsNone(result.final_text)
        self.assertEqual(result.model_calls, 3)

    def test_incomplete_response_is_retried_and_discarded(self):
        result = self.run_ticket([incomplete(), reply("done")])
        self.assertEqual(result.stop_reason, COMPLETED)
        # The partial output never reaches the conversation.
        self.assertEqual(len(self.model.requests[1]["input"]), 1)
        self.assertEqual(result.total_tokens, 20)

    def test_persistently_incomplete_response(self):
        result = self.run_ticket([incomplete("max_output_tokens")] * 2, max_retries=1)
        self.assertEqual(result.stop_reason, INCOMPLETE)
        self.assertIn("max_output_tokens", result.error)

    def test_failed_status_is_retried(self):
        result = self.run_ticket([ModelResponse(status="failed"), reply("done")])
        self.assertEqual(result.stop_reason, COMPLETED)

    def test_retry_mid_run_keeps_earlier_tool_results(self):
        result = self.run_ticket(
            [calls(tool_call("lookup_account", {"identifier": "ACC-10022"}, "c1")), transient(), reply("done")]
        )
        self.assertEqual(result.stop_reason, COMPLETED)
        self.assertEqual(self.model.requests[1]["input"], self.model.requests[2]["input"])

    def test_non_transient_errors_propagate(self):
        with self.assertRaises(ValueError):
            self.run_ticket([ValueError("bad api key")])


if __name__ == "__main__":
    unittest.main()
