import logging
from dataclasses import dataclass
from email.message import Message
from urllib.parse import parse_qs, urlparse

import aiohttp

from config import WAZZUP_API_KEY, WAZZUP_API_URL, WAZZUP_CHANNEL_ID, WAZZUP_CHAT_TYPE


logger = logging.getLogger(__name__)


@dataclass
class DownloadedContent:
    data: bytes
    filename: str | None
    content_type: str | None


class WazzupClient:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if not self._session:
            self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if not self._session:
            raise RuntimeError("Wazzup client is not initialized")
        return self._session

    def headers(self) -> dict[str, str]:
        if not WAZZUP_API_KEY:
            raise RuntimeError("WAZZUP_API_KEY is not configured")

        return {
            "Authorization": f"Bearer {WAZZUP_API_KEY}",
            "Content-Type": "application/json",
        }

    async def send_text(
        self,
        chat_id: str,
        text: str,
        channel_id: str | None = None,
        chat_type: str | None = None,
    ) -> dict:
        payload = {
            "channelId": channel_id or WAZZUP_CHANNEL_ID,
            "chatType": chat_type or WAZZUP_CHAT_TYPE,
            "chatId": chat_id,
            "text": text,
        }
        if not payload["channelId"]:
            raise RuntimeError("Wazzup channelId is missing")

        async with self.session.post(
            f"{WAZZUP_API_URL}/v3/message",
            headers=self.headers(),
            json=payload,
        ) as response:
            response_text = await response.text()
            if response.status >= 400:
                logger.error("Wazzup send failed: %s %s", response.status, response_text)
                response.raise_for_status()
            if not response_text:
                return {}
            try:
                return await response.json()
            except aiohttp.ContentTypeError:
                return {"raw": response_text}

    async def download_content(self, content_uri: str) -> DownloadedContent:
        async with self.session.get(content_uri) as response:
            response.raise_for_status()
            data = await response.read()
            content_type = response.headers.get("Content-Type")
            filename = filename_from_response(content_uri, response.headers.get("Content-Disposition"))
            return DownloadedContent(data=data, filename=filename, content_type=content_type)


def filename_from_response(content_uri: str, content_disposition: str | None) -> str | None:
    if content_disposition:
        message = Message()
        message["Content-Disposition"] = content_disposition
        filename = message.get_filename()
        if filename:
            return filename

    query = parse_qs(urlparse(content_uri).query)
    values = query.get("filename")
    return values[0] if values else None
