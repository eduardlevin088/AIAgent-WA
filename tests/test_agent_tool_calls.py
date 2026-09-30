import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("GPT_KEY", "test-key")
os.environ.setdefault("GPT_MODEL", "gpt-test")
os.environ.setdefault("GPT_SPARE_MODEL", "gpt-test-spare")
os.environ.setdefault("GPT_TRANSCRIPTION_MODEL", "gpt-test-transcription")
os.environ.setdefault("LIMIT_PER_USER", "100000")
os.environ.setdefault("ABSOLUTE_LIMIT", "200000")
os.environ.setdefault("KZ_UTC", "5")
os.environ.setdefault("WAZZUP_CHANNEL_ID", "test-channel")

from services import agent, integrations


def usage():
    return SimpleNamespace(
        input_tokens=1,
        output_tokens=1,
        input_tokens_details=SimpleNamespace(cached_tokens=0),
    )


def function_call(call_id, name="handoff_to_operator", arguments=None):
    return SimpleNamespace(
        type="function_call",
        name=name,
        call_id=call_id,
        arguments=json.dumps(arguments or {"reason": "r", "summary": "s", "force": True}),
    )


def message(text="ок"):
    return SimpleNamespace(type="message", content=text)


def response(output, text="", response_id="resp_1"):
    return SimpleNamespace(id=response_id, output=output, output_text=text, usage=usage())


class FakeResponses:
    """Replays a queued list of responses and records every request."""

    def __init__(self, queue):
        self.queue = list(queue)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.queue.pop(0) if self.queue else response([], "конец")

    def answered_call_ids(self):
        ids = set()
        for request in self.requests:
            for item in request.get("input") or []:
                if isinstance(item, dict) and item.get("type") == "function_call_output":
                    ids.add(item["call_id"])
        return ids


class ToolCallAnsweringTests(unittest.TestCase):
    """Every function_call must get a function_call_output.

    The OpenAI `conversation` is stateful, so a dangling call makes every later
    request on it fail with 400 "No tool output found for function call ...".
    """

    def run_agent(self, queue, should_continue=None):
        fake = FakeResponses(queue)
        with patch.object(agent, "client", SimpleNamespace(responses=fake)):
            result = agent.generate_response(
                user_message="Нет",
                conversation="conv_test",
                username="tester",
                user_id="77000000000",
                should_continue=should_continue,
            )
        return fake, result

    def test_chained_function_calls_are_all_answered(self):
        fake, _ = self.run_agent([
            response([function_call("call_a")]),
            response([function_call("call_b")]),
            response([message()], "готово"),
        ])
        self.assertEqual({"call_a", "call_b"}, fake.answered_call_ids())

    def test_parallel_function_calls_are_answered_in_one_request(self):
        fake, _ = self.run_agent([
            response([function_call("call_a"), function_call("call_b")]),
            response([message()], "готово"),
        ])
        self.assertEqual({"call_a", "call_b"}, fake.answered_call_ids())
        follow_up = fake.requests[1]["input"]
        self.assertEqual(2, len(follow_up))

    def test_failing_tool_still_answers_the_call(self):
        with patch.object(agent, "send_contact_details", side_effect=RuntimeError("bitrix down")):
            fake, result = self.run_agent([
                response([function_call("call_a", "send_contact_details", {"name": "n"})]),
                response([message()], "извините"),
            ])
        self.assertEqual({"call_a"}, fake.answered_call_ids())
        self.assertIsNone(result["data to send"])

    def test_unknown_tool_still_answers_the_call(self):
        fake, _ = self.run_agent([
            response([function_call("call_a", "does_not_exist", {})]),
            response([message()], "ок"),
        ])
        self.assertEqual({"call_a"}, fake.answered_call_ids())

    def test_superseded_turn_still_answers_the_call(self):
        calls = {"n": 0}

        def should_continue():
            calls["n"] += 1
            return calls["n"] <= 1  # goes stale right after the first response

        fake, _ = self.run_agent(
            [
                response([function_call("call_a")]),
                response([message()], "ок"),
            ],
            should_continue=should_continue,
        )
        self.assertEqual({"call_a"}, fake.answered_call_ids())

    def test_runaway_tool_loop_terminates_with_every_call_answered(self):
        queue = [response([function_call(f"call_{i}")]) for i in range(agent.MAX_TOOL_ROUNDS + 3)]
        fake, _ = self.run_agent(queue)

        self.assertEqual(agent.MAX_TOOL_ROUNDS + 1, len(fake.requests))
        # The final round is sent without tools, so the model cannot emit a
        # function_call we would then leave unanswered.
        self.assertEqual([], fake.requests[-1]["tools"])
        expected = {f"call_{i}" for i in range(agent.MAX_TOOL_ROUNDS)}
        self.assertEqual(expected, fake.answered_call_ids())


class GetClientApplicationsTests(unittest.TestCase):
    DEALS = [
        {"deal_id": 3, "stage_id": "C5:FINAL_INVOICE", "status": "Готов",
         "created": "2026-08-23", "number": None},
        {"deal_id": 2, "stage_id": "C5:EXECUTING", "status": "В работе",
         "created": "2026-06-10", "number": "11560"},
        {"deal_id": 1, "stage_id": "C5:WON", "status": "Выдан",
         "created": "2026-05-01", "number": "11557"},
    ]

    def run_tool(self, arguments, deals=None):
        fake = FakeResponses([
            response([function_call("call_a", "get_client_applications", arguments)]),
            response([message()], "ок"),
        ])
        with patch.object(agent, "client", SimpleNamespace(responses=fake)), \
                patch.object(agent, "find_bitrix_client_deals",
                             side_effect=agent.find_bitrix_client_deals if deals is None
                             else lambda phone: deals) as find:
            agent.generate_response(
                user_message="Статус заказа",
                conversation="conv_test",
                username="tester",
                user_id="77000000000",
            )
        output = json.loads(fake.requests[1]["input"][0]["output"])["func_response"]
        return find, output

    def test_defaults_to_whatsapp_number(self):
        find, _ = self.run_tool({}, deals=[])
        find.assert_called_once_with("+77000000000")

    def test_uses_explicit_phone(self):
        find, output = self.run_tool({"phone": "+77071759248"}, deals=[])
        find.assert_called_once_with("+77071759248")
        self.assertIn("не найдены", output)

    def test_returns_statuses_with_numbers(self):
        _, output = self.run_tool({}, deals=self.DEALS)
        applications = json.loads(output)["applications"]
        # Only the Bitrix number reaches the customer, even when it is empty.
        self.assertEqual([None, "11560"], [a["number"] for a in applications])
        self.assertEqual(["Готов", "В работе"], [a["status"] for a in applications])
        self.assertNotIn("description", applications[0])

    def test_skips_issued_and_closed_applications(self):
        deals = self.DEALS + [
            {"deal_id": 0, "stage_id": "C5:LOSE", "status": "Передан на утилизацию",
             "created": "2026-01-01", "number": "11500"},
        ]
        _, output = self.run_tool({}, deals=deals)
        numbers = [a["number"] for a in json.loads(output)["applications"]]
        self.assertNotIn("11557", numbers)
        self.assertNotIn("11500", numbers)

    def test_reports_no_active_applications_when_all_are_closed(self):
        _, output = self.run_tool({}, deals=[self.DEALS[-1]])
        self.assertIn("нет активных заявок", output)

    def test_accepts_a_number_written_with_a_leading_eight(self):
        find, _ = self.run_tool({"phone": "87071759248"}, deals=[])
        find.assert_called_once_with("87071759248")

    def test_rejects_malformed_phone(self):
        _, output = self.run_tool({"phone": "707-12-34"})
        self.assertIn("+7XXXXXXXXXX", output)


class RequestConfirmationTests(unittest.TestCase):
    """A request is created only after the customer confirmed the summary."""

    DATA = {
        "name": "Нурбол", "phone": "87762015818", "city": "Астана",
        "service_type": "Ремонт фурнитуры", "product_type": "Чемодан",
        "model": "Не указана", "problem": "Сломано колесо",
    }

    def setUp(self):
        agent._summary_requested_conversations.clear()

    def turn(self, *calls, conversation="conv_test"):
        fake = FakeResponses([
            response([
                function_call(f"call_{i}", "send_contact_details", {**self.DATA, "confirmed": confirmed})
                for i, confirmed in enumerate(calls)
            ]),
            response([message()], "ок"),
        ])
        with patch.object(agent, "client", SimpleNamespace(responses=fake)), \
                patch.object(agent, "send_contact_details",
                             return_value=("Заявка создана в CRM. Номер заявки: 10509", {"deal_id": 1})) as create:
            result = agent.generate_response(
                user_message="Да",
                conversation=conversation,
                username="tester",
                user_id="77000000000",
            )
        output = json.loads(fake.requests[1]["input"][0]["output"])["func_response"]
        return create, result, output

    def test_first_call_only_asks_for_the_summary(self):
        create, result, output = self.turn(False)
        create.assert_not_called()
        self.assertIsNone(result["data to send"])
        self.assertIn("НЕ создана", output)

    def test_confirmation_without_a_shown_summary_is_rejected(self):
        create, result, _ = self.turn(True)
        create.assert_not_called()
        self.assertIsNone(result["data to send"])

    def test_summary_and_confirmation_in_the_same_turn_are_rejected(self):
        create, _, _ = self.turn(False, True)
        create.assert_not_called()

    def test_confirmation_on_a_later_turn_creates_the_request(self):
        self.turn(False)
        create, result, output = self.turn(True)
        create.assert_called_once()
        self.assertNotIn("confirmed", create.call_args.kwargs["data"])
        self.assertEqual({"deal_id": 1}, result["data to send"])

    def test_summary_from_another_conversation_does_not_count(self):
        self.turn(False, conversation="conv_old")
        create, _, _ = self.turn(True, conversation="conv_new")
        create.assert_not_called()


class ComplaintTitleTests(unittest.TestCase):
    def test_regular_application_title(self):
        self.assertEqual("ТЕСТ Заявка на ремонт", integrations.repair_request_title({"complaint": False}))

    def test_complaint_title(self):
        self.assertEqual("ТЕСТ Жалоба", integrations.repair_request_title({"complaint": True}))


class SendContactDetailsTests(unittest.TestCase):
    def test_no_application_number_is_generated_or_announced(self):
        with patch.object(agent, "create_bitrix_lead",
                          return_value={"deal_id": 5, "bitrix_id": 7}), \
                patch.object(agent, "run_coro_on_db_loop",
                             side_effect=lambda coro: coro.close() or 42):
            message, data = agent.send_contact_details(
                data={"complaint": False}, username="tester", user_id="77000000000"
            )
        # The application number is assigned in Bitrix, not by the bot.
        self.assertNotIn("request_number", data)
        self.assertNotIn("42", message)
        self.assertEqual(5, data["deal_id"])


if __name__ == "__main__":
    unittest.main()
