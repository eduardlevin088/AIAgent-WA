import os
import unittest
from unittest.mock import AsyncMock


os.environ.setdefault("GPT_KEY", "test-key")
os.environ.setdefault("GPT_MODEL", "gpt-test")
os.environ.setdefault("GPT_SPARE_MODEL", "gpt-test-spare")
os.environ.setdefault("GPT_TRANSCRIPTION_MODEL", "gpt-test-transcription")
os.environ.setdefault("LIMIT_PER_USER", "100000")
os.environ.setdefault("ABSOLUTE_LIMIT", "200000")
os.environ.setdefault("KZ_UTC", "5")
os.environ.setdefault("WAZZUP_CHANNEL_ID", "test-channel")

import bot


class GreetingTests(unittest.IsolatedAsyncioTestCase):
    async def test_greeting_only_restarts_dialogue_and_commands_still_work(self):
        user = bot.ChatUser(
            id="77000000000",
            username="77000000000",
            first_name="Customer",
        )
        original_reset = bot.reset_conversation
        original_run_agent = bot.run_agent_and_reply
        bot.reset_conversation = AsyncMock()
        bot.run_agent_and_reply = AsyncMock()
        try:
            await bot.process_text_message(
                user,
                {"text": "Здравствуйте!"},
                "test-channel",
                "whatsapp",
                1,
            )
            bot.reset_conversation.assert_awaited_once()
            bot.run_agent_and_reply.assert_not_awaited()

            bot.reset_conversation.reset_mock()
            await bot.process_text_message(
                user,
                {"text": "/start"},
                "test-channel",
                "whatsapp",
                2,
            )
            bot.reset_conversation.assert_awaited_once()
            bot.run_agent_and_reply.assert_not_awaited()

            bot.reset_conversation.reset_mock()
            await bot.process_text_message(
                user,
                {"text": "Здравствуйте, сломался замок"},
                "test-channel",
                "whatsapp",
                3,
            )
            bot.reset_conversation.assert_not_awaited()
            self.assertEqual(
                bot.run_agent_and_reply.await_args.kwargs["user_message"],
                "Здравствуйте, сломался замок",
            )
        finally:
            bot.reset_conversation = original_reset
            bot.run_agent_and_reply = original_run_agent

    async def test_reset_conversation_sends_locked_greeting_without_llm(self):
        user = bot.ChatUser(
            id="77000000000",
            username="77000000000",
            first_name="Customer",
        )
        with (
            unittest.mock.patch.object(bot, "new_conversation", AsyncMock(return_value="conv-1")),
            unittest.mock.patch.object(bot, "create_or_update_user", AsyncMock()),
            unittest.mock.patch.object(bot, "cancel_open_operator_handoff", AsyncMock()),
            unittest.mock.patch.object(bot, "set_bot_paused", AsyncMock()),
            unittest.mock.patch.object(bot, "is_latest_activity", AsyncMock(return_value=True)),
            unittest.mock.patch.object(bot, "append_dialog_message", AsyncMock()) as append_message,
            unittest.mock.patch.object(bot, "generate_response_serialized", AsyncMock()) as generate,
            unittest.mock.patch.object(bot.wazzup, "send_text", AsyncMock()) as send_text,
        ):
            await bot.reset_conversation(
                user,
                "test-channel",
                "whatsapp",
                activity_version=1,
            )

        generate.assert_not_awaited()
        send_text.assert_awaited_once_with(
            user.id,
            bot.GREETING_TEXT,
            channel_id="test-channel",
            chat_type="whatsapp",
        )
        append_message.assert_awaited_once_with(
            user.id,
            "assistant",
            "text",
            bot.GREETING_TEXT,
        )


if __name__ == "__main__":
    unittest.main()
