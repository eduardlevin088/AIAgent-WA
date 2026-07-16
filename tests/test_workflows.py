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

        response = await database.record_operator_response(
            user_id="77000000000",
            manager_message_id="manager-message-1",
            manager_id="crm-user-1",
            manager_name="Manager",
            responded_at=responded_at,
        )
        duplicate = await database.record_operator_response(
            user_id="77000000000",
            manager_message_id="manager-message-1",
            manager_id="crm-user-1",
            manager_name="Manager",
            responded_at=responded_at,
        )
        stats = await database.get_operator_handoff_stats()

        self.assertIsNotNone(response)
        self.assertEqual(response["response_time_seconds"], 75)
        self.assertIsNone(duplicate)
        self.assertEqual(stats["answered"], 1)
        self.assertEqual(stats["waiting"], 0)
        self.assertEqual(stats["average_seconds"], 75.0)

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
