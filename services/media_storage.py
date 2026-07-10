import mimetypes
import re
import uuid
from pathlib import Path

from config import MEDIA_DIR


def _safe_path_part(value: object) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value)).strip("._")
    return cleaned or "unknown"


def _extension(filename: str | None, content_type: str | None) -> str:
    if filename:
        suffix = Path(filename).suffix.lower()
        if suffix:
            return suffix

    if content_type:
        return mimetypes.guess_extension(content_type) or ".bin"

    return ".bin"


def store_media_bytes(
    user_id: object,
    data: bytes,
    filename: str | None = None,
    content_type: str | None = None,
) -> Path:
    user_dir = MEDIA_DIR / _safe_path_part(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)

    stored_name = f"{uuid.uuid4().hex}{_extension(filename, content_type)}"
    path = user_dir / stored_name
    path.write_bytes(data)
    return path
