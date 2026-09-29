import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch


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


USER = bot.ChatUser(id="77000000000", username="77000000000")


def agent_result(text: str) -> dict:
    return {
        "response": text,
        "data to send": None,
        "handoff": None,
        "input": 1,
        "cache": 0,
        "output": 1,
        "response_id": "resp",
    }


class MessageQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            import aiosqlite  # type: ignore
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest("aiosqlite is not installed for local unit-test DB backend") from exc

        self.previous_db = database.db
        database.db = await aiosqlite.connect(":memory:")
        database.db.row_factory = aiosqlite.Row
        # create_tables() uses Postgres-only DDL, so mirror the one table used here.
        await database.db.execute("""
            CREATE TABLE dialog_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                role TEXT,
                message_type TEXT,
                text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT,
                agent_input TEXT,
                agent_context TEXT
            )
        """)

        bot._user_generation_locks.clear()
        bot._user_conversation_epochs.clear()
        self.sent: list[str] = []

        async def send_text(chat_id, text, **kwargs):
            self.sent.append(text)

        self.patches = [
            patch.object(bot, "RESPONSE_DEBOUNCE_SECONDS", 0.05),
            patch.object(bot, "ensure_conversation", AsyncMock(return_value="conv-1")),
            patch.object(bot, "current_token_usage", AsyncMock(return_value={"input": 0, "output": 0})),
            patch.object(bot, "add_token_usage", AsyncMock()),
            patch.object(bot, "is_bot_paused", AsyncMock(return_value=False)),
            patch.object(bot, "log_event", AsyncMock()),
            patch.object(bot.wazzup, "send_text", AsyncMock(side_effect=send_text)),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in self.patches:
            item.stop()
        await database.db.close()
        database.db = self.previous_db

    async def inbound(self, text: str, status: str = "preparing") -> int:
        return await database.append_dialog_message(USER.id, "user", "text", text, status=status)

    async def statuses(self) -> list[str | None]:
        async with database.db.execute(
            "SELECT status FROM dialog_messages WHERE role = 'user' ORDER BY id"
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]

    async def test_burst_is_answered_once_with_all_messages_in_order(self):
        generate = AsyncMock(return_value=agent_result("Ответ"))
        first = await self.inbound("Чемодан")
        second = await self.inbound("Самсонайт")

        with patch.object(bot, "generate_response_with_retry", generate):
            await asyncio.gather(
                bot.enqueue_and_reply(USER, "test-channel", "whatsapp", first, "Чемодан"),
                bot.enqueue_and_reply(USER, "test-channel", "whatsapp", second, "Самсонайт"),
            )

        generate.assert_awaited_once()
        self.assertEqual(generate.await_args.kwargs["user_message"], "Чемодан\nСамсонайт")
        self.assertIn("несколько сообщений подряд", generate.await_args.kwargs["system_message"])
        self.assertEqual(self.sent, ["Ответ"])
        self.assertEqual(await self.statuses(), ["completed", "completed"])

    async def test_message_during_generation_waits_and_is_answered_next(self):
        release_first = asyncio.Event()
        calls: list[str] = []

        async def generate(**kwargs):
            calls.append(kwargs["user_message"])
            if len(calls) == 1:
                await release_first.wait()
            return agent_result(f"Ответ на {kwargs['user_message']}")

        with patch.object(bot, "generate_response_with_retry", AsyncMock(side_effect=generate)):
            first = await self.inbound("Ручка сломалась")
            first_task = asyncio.create_task(
                bot.enqueue_and_reply(USER, "test-channel", "whatsapp", first, "Ручка сломалась")
            )
            while not calls:
                await asyncio.sleep(0.01)

            second = await self.inbound("Переносная")
            second_task = asyncio.create_task(
                bot.enqueue_and_reply(USER, "test-channel", "whatsapp", second, "Переносная")
            )
            await asyncio.sleep(0.1)
            release_first.set()
            await asyncio.gather(first_task, second_task)

        self.assertEqual(calls, ["Ручка сломалась", "Переносная"])
        self.assertEqual(self.sent, ["Ответ на Ручка сломалась", "Ответ на Переносная"])

    async def test_batch_stops_before_a_message_still_being_prepared(self):
        text_before = await self.inbound("Вот фото", status="new")
        photo = await self.inbound("[image]")
        text_after = await self.inbound("Это ручка", status="new")

        batch = await database.claim_new_dialog_messages(USER.id)
        self.assertEqual([row["id"] for row in batch], [text_before])

        await database.queue_dialog_message(photo, None, "Пользователь отправил фото")
        batch = await database.claim_new_dialog_messages(USER.id)
        self.assertEqual([row["id"] for row in batch], [photo, text_after])

    async def test_restart_cancels_queued_messages_and_drops_stale_reply(self):
        generation_started = asyncio.Event()
        release = asyncio.Event()

        async def generate(**kwargs):
            generation_started.set()
            await release.wait()
            return agent_result("Устаревший ответ")

        with (
            patch.object(bot, "generate_response_with_retry", AsyncMock(side_effect=generate)),
            patch.object(bot, "new_conversation", AsyncMock(return_value="conv-2")),
            patch.object(bot, "create_or_update_user", AsyncMock()),
            patch.object(bot, "cancel_open_operator_handoff", AsyncMock()),
            patch.object(bot, "set_bot_paused", AsyncMock()),
        ):
            first = await self.inbound("Колесо")
            task = asyncio.create_task(
                bot.enqueue_and_reply(USER, "test-channel", "whatsapp", first, "Колесо")
            )
            await generation_started.wait()

            queued = await self.inbound("Samsonite", status="new")
            greeting = await self.inbound("Здравствуйте")
            await bot.reset_conversation(USER, "test-channel", "whatsapp", greeting)
            release.set()
            await task

        self.assertEqual(self.sent, [bot.GREETING_TEXT])
        async with database.db.execute(
            "SELECT status FROM dialog_messages WHERE id = ?", (queued,)
        ) as cursor:
            self.assertEqual((await cursor.fetchone())[0], "completed")

    async def test_unfinished_messages_are_requeued_on_startup(self):
        await self.inbound("Колесо", status="processing")
        await self.inbound("[image]", status="preparing")
        await self.inbound("Готово", status="completed")

        await database.requeue_unfinished_dialog_messages()

        self.assertEqual(await self.statuses(), ["new", "completed", "completed"])

    async def test_messages_that_skip_the_agent_are_released(self):
        with (
            patch.object(bot, "is_dedicated_wazzup_channel", return_value=True),
            patch.object(bot, "is_manager_outbound_message", return_value=False),
            patch.object(bot, "is_inbound_customer_message", return_value=True),
            patch.object(bot, "is_allowed_chat", return_value=True),
            patch.object(bot, "user_from_message", return_value=USER),
            patch.object(bot, "mark_message_processed", AsyncMock(return_value=True)),
            patch.object(bot, "save_feedback", AsyncMock()),
        ):
            await bot.process_wazzup_message(
                {"messageId": "m-1", "type": "text", "text": "Оценка 5"}
            )

        self.assertEqual(await self.statuses(), ["completed"])

    async def test_bare_rating_is_saved_only_as_answer_to_rating_request(self):
        await database.append_dialog_message(USER.id, "assistant", "text", bot.FEEDBACK_REQUEST_TEXT)
        await self.inbound("5")
        with patch.object(bot, "save_feedback", AsyncMock()) as save:
            self.assertTrue(await bot.maybe_save_feedback(USER, "5 всё отлично", "c", "whatsapp"))
        save.assert_awaited_once_with(USER.id, 5, "всё отлично")

        await database.append_dialog_message(USER.id, "assistant", "text", bot.GREETING_TEXT)
        with patch.object(bot, "save_feedback", AsyncMock()) as save:
            # After the greeting "5" is menu item 5, not a rating.
            self.assertFalse(await bot.maybe_save_feedback(USER, "5", "c", "whatsapp"))
        save.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
