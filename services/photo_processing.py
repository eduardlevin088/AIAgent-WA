import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI

from config import GPT_KEY, PHOTO_PROCESSING_INSTRUCTIONS_PATH, PHOTO_PROCESSING_MODEL
from services.wazzup import DownloadedContent, WazzupClient


logger = logging.getLogger(__name__)

client = OpenAI(api_key=GPT_KEY)


class PhotoProcessingInstructionsMissing(FileNotFoundError):
    pass


@dataclass
class PhotoProcessingResult:
    message_id: str | None
    content_uri: str
    filename: str | None
    content_type: str | None
    size_bytes: int
    model: str
    raw_text: str
    data: dict[str, Any] | list[Any] | None

    def as_agent_context(self) -> str:
        if isinstance(self.data, dict):
            # Prices come only from the diagnostic rules in the agent prompt.
            data = {key: value for key, value in self.data.items() if "cost" not in key and "price" not in key}
            payload = json.dumps(data, ensure_ascii=False)
        elif self.data is not None:
            payload = json.dumps(self.data, ensure_ascii=False)
        else:
            payload = self.raw_text
        return (
            "Анализ фото повреждения (внутренняя подсказка: не пересказывай её клиенту дословно, "
            "не проси фото деталей, которых нет у изделия клиента, цены бери только из правил "
            f"диагностики):\n{payload}"
        )


def load_photo_processing_instructions(path: Path = PHOTO_PROCESSING_INSTRUCTIONS_PATH) -> str:
    if not path.exists():
        raise PhotoProcessingInstructionsMissing(f"Photo processing instruction file is missing: {path}")

    instructions = path.read_text(encoding="utf-8").strip()
    if not instructions:
        raise PhotoProcessingInstructionsMissing(f"Photo processing instruction file is empty: {path}")
    return instructions


def image_data_url(data: bytes, content_type: str | None) -> str:
    media_type = content_type or "image/jpeg"
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def parse_json_if_possible(text: str) -> dict[str, Any] | list[Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None

    return parsed if isinstance(parsed, (dict, list)) else None


def analyze_photo_bytes(
    data: bytes,
    content_type: str | None,
    instructions: str,
    model: str = PHOTO_PROCESSING_MODEL,
    dialog_context: str | None = None,
) -> str:
    text = (
        f"{instructions}\n\n"
        "Если в инструкции не указан формат ответа, верни компактный JSON. "
        "Не добавляй markdown вокруг JSON."
    )
    if dialog_context:
        text += f"\n\nПоследние сообщения переписки с клиентом:\n{dialog_context}"
    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": text,
                    },
                    {
                        "type": "input_image",
                        "image_url": image_data_url(data, content_type),
                    },
                ],
            }
        ],
    )
    return response.output_text


async def process_incoming_photo(
    message: dict[str, Any],
    wazzup: WazzupClient,
    downloaded_content: DownloadedContent | None = None,
    instruction_path: Path = PHOTO_PROCESSING_INSTRUCTIONS_PATH,
    model: str = PHOTO_PROCESSING_MODEL,
    dialog_context: str | None = None,
) -> PhotoProcessingResult:
    content_uri = str(message.get("contentUri") or "").strip()
    if not content_uri:
        raise ValueError("Incoming Wazzup image message does not contain contentUri")

    instructions = load_photo_processing_instructions(instruction_path)
    content = downloaded_content or await wazzup.download_content(content_uri)
    raw_text = await asyncio.to_thread(
        analyze_photo_bytes,
        content.data,
        content.content_type,
        instructions,
        model,
        dialog_context,
    )

    return PhotoProcessingResult(
        message_id=message.get("messageId"),
        content_uri=content_uri,
        filename=content.filename,
        content_type=content.content_type,
        size_bytes=len(content.data),
        model=model,
        raw_text=raw_text,
        data=parse_json_if_possible(raw_text),
    )


async def maybe_process_incoming_photo(
    message: dict[str, Any],
    wazzup: WazzupClient,
    downloaded_content: DownloadedContent | None = None,
    dialog_context: str | None = None,
) -> PhotoProcessingResult | None:
    try:
        return await process_incoming_photo(
            message=message,
            wazzup=wazzup,
            downloaded_content=downloaded_content,
            dialog_context=dialog_context,
        )
    except PhotoProcessingInstructionsMissing:
        return None
    except Exception:
        logger.exception("Failed to process incoming photo %s", message.get("messageId"))
        return None
