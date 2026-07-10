import json
import os
from pathlib import Path
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent

load_dotenv()


SUPERADMIN_ID = (os.getenv("SUPERADMIN_ID") or "").strip()

GPT_KEY = os.getenv("GPT_KEY")
GPT_MODEL = os.getenv("GPT_MODEL")
GPT_SPARE_MODEL = os.getenv("GPT_SPARE_MODEL")
GPT_TRANSCRIPTION_MODEL = os.getenv("GPT_TRANSCRIPTION_MODEL")
LIMIT_PER_USER = int(os.getenv("LIMIT_PER_USER"))
ABSOLUTE_LIMIT = int(os.getenv("ABSOLUTE_LIMIT"))

KZ_UTC = int(os.getenv("KZ_UTC"))

_admin_ids_raw = (os.getenv("ADMIN_IDS") or "").strip()
ADMIN_IDS = [
    x.strip()
    for x in _admin_ids_raw.split(",")
    if x.strip()
]


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


ENABLE_CHAT_ALLOWLIST = _env_bool("ENABLE_CHAT_ALLOWLIST")
_allowed_chat_ids_raw = (os.getenv("ALLOWED_CHAT_IDS") or "").strip()
ALLOWED_CHAT_IDS = [
    x.strip()
    for x in _allowed_chat_ids_raw.split(",")
    if x.strip()
]


STATIC_DIR = BASE_DIR / "static"
AGENT_PROMPT_MAIN_PATH = STATIC_DIR / "agent_prompt_main.txt"
PHOTO_PROCESSING_INSTRUCTIONS_PATH = Path(
    os.getenv("PHOTO_PROCESSING_INSTRUCTIONS_PATH") or STATIC_DIR / "photo_processing_instructions.txt"
)
PHOTO_PROCESSING_MODEL = os.getenv("PHOTO_PROCESSING_MODEL") or GPT_MODEL

DATA_DIR = Path(os.getenv("DATA_DIR") or BASE_DIR / "data").expanduser()
DB_PATH = Path(os.getenv("DB_PATH") or DATA_DIR / "database.db").expanduser()
DB_DIR = DB_PATH.parent
MEDIA_DIR = Path(os.getenv("MEDIA_DIR") or DATA_DIR / "media")

BITRIX_WEBHOOK_URL = os.getenv("BITRIX_WEBHOOK_URL")
BITRIX_APPLICATION_TOKEN = (os.getenv("BITRIX_APPLICATION_TOKEN") or "").strip()
BITRIX_SERVICE_CATEGORY_ID = int(os.getenv("BITRIX_SERVICE_CATEGORY_ID") or 5)
BITRIX_DEAL_ENTITY_TYPE_ID = int(os.getenv("BITRIX_DEAL_ENTITY_TYPE_ID") or 2)
BITRIX_BOT_STAGE_ID = os.getenv("BITRIX_BOT_STAGE_ID") or "C5:UC_SRW3R8"
DEFAULT_BITRIX_STAGE_STATUS_MAP = {
    "C5:NEW": "Принят",
    "C5:UC_SRW3R8": "Принят",
    "C5:UC_KG8OHE": "Принят",
    "C5:PREPARATION": "Диагностика",
    "C5:PREPAYMENT_INVOICE": "Диагностика",
    "C5:EXECUTING": "В работе",
    "C5:UC_QACO2C": "В работе",
    "C5:UC_0CWJKY": "Передан в бутик",
    "C5:FINAL_INVOICE": "Готов",
    "C5:WON": "Выдан",
    "C5:LOSE": "Передан на утилизацию",
}
try:
    BITRIX_STAGE_STATUS_MAP = {
        **DEFAULT_BITRIX_STAGE_STATUS_MAP,
        **json.loads(os.getenv("BITRIX_STAGE_STATUS_MAP") or "{}"),
    }
except json.JSONDecodeError:
    BITRIX_STAGE_STATUS_MAP = DEFAULT_BITRIX_STAGE_STATUS_MAP

WAZZUP_API_KEY = os.getenv("WAZZUP_API_KEY")
WAZZUP_API_URL = os.getenv("WAZZUP_API_URL", "https://api.wazzup24.com").rstrip("/")
WAZZUP_CHANNEL_ID = os.getenv("WAZZUP_CHANNEL_ID")
WAZZUP_CHAT_TYPE = os.getenv("WAZZUP_CHAT_TYPE", "whatsapp")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")
ADMIN_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET") or INTERNAL_API_KEY or GPT_KEY or "dev-admin-session-secret"
ADMIN_USERNAME = (os.getenv("ADMIN_USERNAME") or "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") or ""
