import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("GPT_KEY", "test-key")
os.environ.setdefault("GPT_MODEL", "gpt-test")

from services import photo_processing


def result(data):
    return photo_processing.PhotoProcessingResult(
        message_id="m-1",
        content_uri="uri",
        filename="photo.jpg",
        content_type="image/jpeg",
        size_bytes=1,
        model="gpt-test",
        raw_text="",
        data=data,
    )


class PhotoProcessingTests(unittest.TestCase):
    def test_agent_context_drops_price_fields(self):
        context = result({
            "damage_parts": ["шов"],
            "preliminary_cost_range": "1000-2000 тг",
            "price": "500",
        }).as_agent_context()
        self.assertIn("шов", context)
        self.assertNotIn("1000-2000", context)
        self.assertNotIn("500", context)

    def test_dialog_context_is_sent_with_the_photo(self):
        requests = []
        fake_client = SimpleNamespace(responses=SimpleNamespace(
            create=lambda **kwargs: requests.append(kwargs) or SimpleNamespace(output_text="{}")
        ))
        with patch.object(photo_processing, "client", fake_client):
            photo_processing.analyze_photo_bytes(
                b"img", "image/jpeg", "instructions",
                dialog_context="Клиент: Рюкзак самсонайт\nКлиент: Шов разошелся",
            )
        text = requests[0]["input"][0]["content"][0]["text"]
        self.assertIn("Рюкзак самсонайт", text)


if __name__ == "__main__":
    unittest.main()
