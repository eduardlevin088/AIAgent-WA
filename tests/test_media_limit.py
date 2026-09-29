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


USER = bot.ChatUser(id="77000000000", username="77000000000")


class MediaLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_photos_beyond_five_are_accepted_in_chat(self):
        with (
            patch.object(bot, "store_wazzup_content", AsyncMock(return_value=None)) as store,
            patch.object(bot, "enqueue_and_reply", AsyncMock()) as enqueue,
            patch.object(bot.wazzup, "send_text", AsyncMock()) as send_text,
        ):
            for index in range(8):
                await bot.process_image_message(
                    USER, {"messageId": f"m-{index}", "contentUri": "uri"}, "c", "whatsapp", index
                )

        self.assertEqual(8, store.await_count)
        self.assertEqual(8, enqueue.await_count)
        send_text.assert_not_awaited()

    async def test_only_first_five_files_go_to_bitrix(self):
        media = [{"filename": f"photo-{index}.jpg"} for index in range(7)]
        sent: list[str] = []
        with (
            patch.object(bot, "get_admin_ids", AsyncMock(return_value=["admin"])),
            patch.object(bot, "get_media_files", AsyncMock(return_value=media)),
            patch.object(bot, "media_bytes_from_record", AsyncMock(return_value=b"img")),
            patch.object(bot, "format_message", return_value="Заявка"),
            patch.object(bot, "upload_files_to_bitrix") as upload,
            patch.object(bot.wazzup, "send_text", AsyncMock(side_effect=lambda **kw: sent.append(kw["text"]))),
        ):
            await bot.handle_completed_request(USER, {"deal_id": 42}, "c", "whatsapp")

        deal_id, files = upload.call_args.args
        self.assertEqual(42, deal_id)
        self.assertEqual([f"photo-{index}.jpg" for index in range(5)], [f["filename"] for f in files])
        self.assertIn("клиент прислал 7", sent[0])


if __name__ == "__main__":
    unittest.main()
