"""Offline tests for the Responses <-> Chat Completions translation and the
build_model selector. No SDK calls and no running server."""

from __future__ import annotations

import unittest

from agent.model import (
    ModelResponse,
    build_model,
    chat_response_to_model_response,
    responses_input_to_chat_messages,
    responses_tools_to_chat,
)

# A real Ollama (qwen2.5:7b) reply to a tool-calling prompt, verbatim.
OLLAMA_TOOL_REPLY = {
    "id": "chatcmpl-984",
    "object": "chat.completion",
    "model": "qwen2.5:7b",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_mcjwbqcp",
                        "index": 0,
                        "type": "function",
                        "function": {"name": "lookup_account", "arguments": '{"identifier":"ACC-10022"}'},
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 152, "completion_tokens": 26, "total_tokens": 178},
}

TOOL_SCHEMA = {
    "type": "function",
    "name": "lookup_account",
    "description": "Look up a customer account.",
    "parameters": {"type": "object", "properties": {"identifier": {"type": "string"}}, "required": ["identifier"]},
}


class ToolSchemaTest(unittest.TestCase):
    def test_flat_schema_becomes_nested(self):
        [tool] = responses_tools_to_chat([TOOL_SCHEMA])
        self.assertEqual(tool["type"], "function")
        self.assertEqual(tool["function"]["name"], "lookup_account")
        self.assertEqual(tool["function"]["description"], "Look up a customer account.")
        self.assertEqual(tool["function"]["parameters"], TOOL_SCHEMA["parameters"])


class ResponseToModelResponseTest(unittest.TestCase):
    def test_tool_call_reply(self):
        result = chat_response_to_model_response(OLLAMA_TOOL_REPLY)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.total_tokens, 178)
        self.assertEqual(result.text, "")
        self.assertEqual([c.name for c in result.tool_calls], ["lookup_account"])
        self.assertEqual(result.tool_calls[0].call_id, "call_mcjwbqcp")
        self.assertEqual(result.tool_calls[0].arguments, '{"identifier":"ACC-10022"}')
        # output_items carry only the function_call (content was empty).
        self.assertEqual([i["type"] for i in result.output_items], ["function_call"])

    def test_text_reply(self):
        payload = {
            "choices": [{"message": {"role": "assistant", "content": "All set."}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 40},
        }
        result = chat_response_to_model_response(payload)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.text, "All set.")
        self.assertEqual(result.output_items, [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "All set."}]}])
        self.assertEqual(result.tool_calls, [])

    def test_length_finish_is_incomplete(self):
        payload = {"choices": [{"message": {"role": "assistant", "content": "partial"}, "finish_reason": "length"}], "usage": {}}
        result = chat_response_to_model_response(payload)
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.incomplete_reason, "max_tokens")

    def test_dict_arguments_are_stringified(self):
        payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": {"a": 1}}}],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"total_tokens": 5},
        }
        result = chat_response_to_model_response(payload)
        self.assertEqual(result.tool_calls[0].arguments, '{"a": 1}')


class InputToMessagesTest(unittest.TestCase):
    def test_system_and_user(self):
        messages = responses_input_to_chat_messages("sys prompt", [{"role": "user", "content": "hi"}])
        self.assertEqual(messages, [{"role": "system", "content": "sys prompt"}, {"role": "user", "content": "hi"}])

    def test_full_tool_turn_round_trips(self):
        # Turn 1 output from the adapter, as the loop would append it.
        reply = chat_response_to_model_response(OLLAMA_TOOL_REPLY)
        conversation = [{"role": "user", "content": "look up my account"}]
        conversation.extend(reply.output_items)
        # The loop appends the tool result in Responses shape.
        conversation.append({"type": "function_call_output", "call_id": "call_mcjwbqcp", "output": '{"balance_usd": 15.0}'})

        messages = responses_input_to_chat_messages("sys", conversation)
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "tool"])
        assistant = messages[2]
        self.assertEqual(assistant["tool_calls"][0]["id"], "call_mcjwbqcp")
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "lookup_account")
        # The tool result's id matches the assistant tool_call's id.
        self.assertEqual(messages[3]["tool_call_id"], "call_mcjwbqcp")
        self.assertEqual(messages[3]["content"], '{"balance_usd": 15.0}')

    def test_assistant_text_then_tool_call_merge_into_one_message(self):
        conversation = [
            {"role": "user", "content": "q"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "let me check"}]},
            {"type": "function_call", "call_id": "x1", "name": "lookup_account", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "x1", "output": "{}"},
        ]
        messages = responses_input_to_chat_messages("", conversation)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "tool"])
        self.assertEqual(messages[1]["content"], "let me check")
        self.assertEqual(len(messages[1]["tool_calls"]), 1)

    def test_follow_up_user_message_after_a_reply(self):
        conversation = [
            {"role": "user", "content": "ticket"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Which tx?"}]},
            {"role": "user", "content": "[Customer reply]\n0xa1b2\n"},
        ]
        messages = responses_input_to_chat_messages("", conversation)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])
        self.assertEqual(messages[1]["content"], "Which tx?")
        self.assertEqual(messages[2]["content"], "[Customer reply]\n0xa1b2\n")

    def test_reasoning_items_are_dropped(self):
        messages = responses_input_to_chat_messages("", [{"type": "reasoning", "summary": []}, {"role": "user", "content": "hi"}])
        self.assertEqual(messages, [{"role": "user", "content": "hi"}])


class BuildModelTest(unittest.TestCase):
    def test_unknown_api_raises(self):
        with self.assertRaises(ValueError):
            build_model({"model": {"api": "bogus", "name": "x", "api_key_env": "X"}})


if __name__ == "__main__":
    unittest.main()
