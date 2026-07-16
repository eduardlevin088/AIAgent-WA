import os
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import aiosqlite


os.environ.setdefault("GPT_KEY", "test-key")
os.environ.setdefault("GPT_MODEL", "gpt-test")
os.environ.setdefault("GPT_SPARE_MODEL", "gpt-test-spare")
os.environ.setdefault("GPT_TRANSCRIPTION_MODEL", "gpt-test-transcription")
os.environ.setdefault("LIMIT_PER_USER", "100000")
os.environ.setdefault("ABSOLUTE_LIMIT", "200000")
os.environ.setdefault("KZ_UTC", "5")
os.environ.setdefault("WAZZUP_CHANNEL_ID", "test-channel")

import bot
import database
from services.wazzup import WazzupClient


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_db = database.db
        database.db = await aiosqlite.connect(":memory:")
        database.db.row_factory = aiosqlite.Row
        await database.create_tables()

    async def asyncTearDown(self):
        await database.db.close()
        database.db = self.previous_db

    async def create_request(self, deal_id: int = 42) -> None:
        await database.create_repair_request(
            user_id="77000000000",
            data={
                "name": "Test Customer",
                "phone": "77000000000",
                "city": "Almaty",
                "service_type": "Repair",
                "product_type": "Suitcase",
                "model": "Test",
                "problem": "Wheel",
            },
            deal_id=deal_id,
        )

    async def test_only_stage_beyond_furthest_is_advanced(self):
        await self.create_request()

        preparation = await database.sync_repair_request_status_by_deal_id(
            42,
            "Диагностика",
            stage_id="C5:PREPARATION",
        )
        self.assertTrue(preparation["stage_advanced"])
        self.assertTrue(preparation["changed"])

        parts = await database.sync_repair_request_status_by_deal_id(
            42,
            "Диагностика",
            stage_id="C5:PREPAYMENT_INVOICE",
        )
        self.assertTrue(parts["stage_advanced"])
        self.assertFalse(parts["changed"])

        executing = await database.sync_repair_request_status_by_deal_id(
            42,
            "В работе",
            stage_id="C5:EXECUTING",
        )
        self.assertTrue(executing["stage_advanced"])

        moved_back = await database.sync_repair_request_status_by_deal_id(
            42,
            "Диагностика",
            stage_id="C5:PREPARATION",
        )
        self.assertFalse(moved_back["stage_advanced"])
        self.assertTrue(moved_back["changed"])

        returned_to_furthest = await database.sync_repair_request_status_by_deal_id(
            42,
            "В работе",
            stage_id="C5:EXECUTING",
        )
        self.assertFalse(returned_to_furthest["stage_advanced"])
        self.assertTrue(returned_to_furthest["changed"])

        beyond_furthest = await database.sync_repair_request_status_by_deal_id(
            42,
            "Готов",
            stage_id="C5:FINAL_INVOICE",
        )
        self.assertTrue(beyond_furthest["stage_advanced"])

    async def test_legacy_request_initializes_checkpoint_without_notification(self):
        await self.create_request()
        await database.db.execute("""
            UPDATE repair_requests
            SET furthest_bitrix_stage_id = NULL,
                furthest_bitrix_stage_rank = NULL
            WHERE deal_id = 42
        """)
        await database.db.commit()

        result = await database.sync_repair_request_status_by_deal_id(
            42,
            "Готов",
            stage_id="C5:FINAL_INVOICE",
        )

        self.assertFalse(result["stage_advanced"])
        self.assertEqual(result["furthest_stage_id"], "C5:FINAL_INVOICE")

    async def test_backward_stage_never_sends_customer_notification(self):
        fake_wazzup = Mock()
        fake_wazzup.send_text = AsyncMock(return_value={})
        original_wazzup = bot.wazzup
        bot.wazzup = fake_wazzup
        try:
            backward_result = await bot.notify_customer_about_bitrix_status(
                {
                    "application": {
                        "user_id": "77000000000",
                        "request_number": 10500,
                    },
                    "changed": True,
                    "stage_advanced": False,
                    "new_status": "Диагностика",
                },
                stage_id="C5:PREPARATION",
            )
            forward_result = await bot.notify_customer_about_bitrix_status(
                {
                    "application": {
                        "user_id": "77000000000",
                        "request_number": 10500,
                    },
                    "changed": False,
                    "stage_advanced": True,
                    "new_status": "Диагностика",
                },
                stage_id="C5:PREPAYMENT_INVOICE",
            )
        finally:
            bot.wazzup = original_wazzup

        self.assertFalse(backward_result)
        self.assertTrue(forward_result)
        fake_wazzup.send_text.assert_awaited_once()

    async def test_manager_first_response_is_recorded_once(self):
        handoff = await database.create_operator_handoff(
            "77000000000",
            "Outside script",
            "Customer asks about product availability",
        )
        requested_at = database.parse_utc_timestamp(handoff["requested_at"])
        responded_at = (requested_at + timedelta(seconds=75)).isoformat()

        await database.set_bot_paused("77000000000", True, "Waiting for manager")
        response = await database.record_operator_message(
            user_id="77000000000",
            manager_message_id="manager-message-1",
            manager_id="crm-user-1",
            manager_name="Manager",
            responded_at=responded_at,
        )
        duplicate = await database.record_operator_message(
            user_id="77000000000",
            manager_message_id="manager-message-1",
            manager_id="crm-user-1",
            manager_name="Manager",
            responded_at=responded_at,
        )
        second_message_at = (requested_at + timedelta(seconds=120)).isoformat()
        second_message = await database.record_operator_message(
            user_id="77000000000",
            manager_message_id="manager-message-2",
            manager_id="crm-user-1",
            manager_name="Manager",
            responded_at=second_message_at,
        )
        not_expired = await database.close_expired_operator_handoffs(
            timeout_minutes=1,
            now=requested_at + timedelta(seconds=179),
        )
        expired = await database.close_expired_operator_handoffs(
            timeout_minutes=1,
            now=requested_at + timedelta(seconds=181),
        )
        stats = await database.get_operator_handoff_stats()

        self.assertIsNotNone(response)
        self.assertEqual(response["status"], "active")
        self.assertEqual(response["response_time_seconds"], 75)
        self.assertTrue(response["first_response_recorded"])
        self.assertIsNone(duplicate)
        self.assertEqual(second_message["last_manager_message_at"], second_message_at)
        self.assertFalse(second_message["first_response_recorded"])
        self.assertEqual(not_expired, [])
        self.assertEqual(len(expired), 1)
        self.assertFalse(await database.is_bot_paused("77000000000"))
        self.assertEqual(stats["answered"], 1)
        self.assertEqual(stats["waiting"], 0)
        self.assertEqual(stats["active"], 0)
        self.assertEqual(stats["average_seconds"], 75.0)

    async def test_manager_can_take_over_without_agent_handoff(self):
        message_at = database.utc_now_iso()
        takeover = await database.record_operator_message(
            user_id="77000000000",
            manager_message_id="manager-takeover-1",
            manager_id="crm-user-1",
            manager_name="Manager",
            responded_at=message_at,
        )

        self.assertEqual(takeover["initiated_by"], "manager")
        self.assertEqual(takeover["status"], "active")
        self.assertEqual(takeover["last_manager_message_at"], message_at)
        self.assertIsNone(takeover["response_time_seconds"])

    async def test_manager_webhook_pauses_chat_and_deduplicates_status_updates(self):
        first_message_at = database.parse_utc_timestamp(database.utc_now_iso())
        original_is_allowed_chat = bot.is_allowed_chat
        bot.is_allowed_chat = lambda _: True
        try:
            message = {
                "messageId": "manager-webhook-1",
                "chatId": "77000000000",
                "type": "text",
                "text": "Я подключился",
                "authorId": "crm-user-1",
                "authorName": "Manager",
                "dateTime": first_message_at.isoformat(),
            }
            await bot.process_manager_outbound_message(message)
            await bot.process_manager_outbound_message({
                **message,
                "dateTime": (first_message_at + timedelta(minutes=5)).isoformat(),
            })

            async with database.db.execute("""
                SELECT COUNT(*) AS total, last_manager_message_at
                FROM operator_handoffs
                WHERE user_id = ?
            """, ("77000000000",)) as cursor:
                handoff = await cursor.fetchone()
        finally:
            bot.is_allowed_chat = original_is_allowed_chat

        self.assertTrue(await database.is_bot_paused("77000000000"))
        self.assertEqual(handoff["total"], 1)
        self.assertEqual(handoff["last_manager_message_at"], first_message_at.isoformat())

    async def test_wazzup_client_registers_api_message_id_before_echo(self):
        class FakeResponse:
            status = 201

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def text(self):
                return '{"messageId":"bot-message-1","chatId":"77000000000"}'

            async def json(self):
                return {
                    "messageId": "bot-message-1",
                    "chatId": "77000000000",
                }

        class FakeSession:
            def post(self, *args, **kwargs):
                return FakeResponse()

        recorder = AsyncMock()
        client = WazzupClient(outbound_message_recorder=recorder)
        client._session = FakeSession()
        client.headers = lambda: {}

        result = await client.send_text("77000000000", "Bot reply")

        self.assertEqual(result["messageId"], "bot-message-1")
        recorder.assert_awaited_once_with("bot-message-1", "77000000000")

    def test_wazzup_echo_identifies_manager_outbound_message(self):
        self.assertTrue(bot.is_manager_outbound_message({
            "isEcho": True,
            "status": "delivered",
            "authorName": "Manager",
        }))
        self.assertFalse(bot.is_manager_outbound_message({
            "isEcho": False,
            "status": "inbound",
        }))
        self.assertFalse(bot.is_manager_outbound_message({
            "isEcho": True,
            "status": "error",
        }))


if __name__ == "__main__":
    unittest.main()
