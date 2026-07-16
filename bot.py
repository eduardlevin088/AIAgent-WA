import asyncio
import base64
import csv
import io
import json
import logging
import re
import threading
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from config import ABSOLUTE_LIMIT, ADMIN_IDS, ADMIN_PASSWORD, ADMIN_SESSION_SECRET
from config import ALLOWED_CHAT_IDS, ENABLE_CHAT_ALLOWLIST
from config import ADMIN_USERNAME, BITRIX_APPLICATION_TOKEN, BITRIX_STAGE_STATUS_MAP
from config import GPT_MODEL, INTERNAL_API_KEY, LIMIT_PER_USER
from config import MANAGER_HANDOFF_POLL_SECONDS, MANAGER_HANDOFF_TIMEOUT_MINUTES
from config import SUPERADMIN_ID, WAZZUP_CHANNEL_ID, WAZZUP_CHAT_TYPE
from database import add_token_usage, append_dialog_message, cancel_open_operator_handoff
from database import close_db, close_expired_operator_handoffs, count_media_files
from database import create_admin, create_operator_handoff
from database import create_or_update_user, delete_admin, execute_query, get_admin_ids
from database import get_admin_user_by_id, get_admin_user_by_username, get_analytics_summary
from database import get_media_files, get_operator_handoff_stats, get_recent_dialog
from database import get_repair_request_stats
from database import get_token_usage, get_user_conversation, get_users, init_db
from database import is_bot_paused, list_customers, list_repair_requests, log_event, mark_message_processed
from database import record_operator_message, REPAIR_REQUEST_STATUSES, save_feedback
from database import save_media_file, set_bot_paused
from database import sync_repair_request_status_by_deal_id, update_repair_request_status
from database import upsert_admin_user
from services.agent import generate_response, transcribe
from services.admin_auth import hash_password, sign_session, verify_password, verify_session
from services.integrations import get_bitrix_deal_stage_id, upload_files_to_bitrix
from services.media_storage import store_media_bytes
from services.miscellaneous import format_repair_text_minimal as format_message
from services.new_conv import new_conversation
from services.photo_processing import maybe_process_incoming_photo
from services.wazzup import DownloadedContent, WazzupClient


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

wazzup = WazzupClient(outbound_message_recorder=mark_message_processed)
REQUEST_MEDIA_LIMIT = 5
RESPONSE_DEBOUNCE_SECONDS = 1.0
GENERATION_BUSY_RETRY_ATTEMPTS = 3
GENERATION_BUSY_RETRY_DELAY_SECONDS = 0.75
_user_activity_versions: dict[str, int] = {}
_user_activity_lock = threading.Lock()
_user_generation_locks: dict[str, asyncio.Lock] = {}


@dataclass
class ChatUser:
    id: str
    username: str
    first_name: str | None = None
    last_name: str | None = None


class SendMessageRequest(BaseModel):
    chat_id: str = Field(alias="chatId")
    text: str
    channel_id: str | None = Field(default=None, alias="channelId")
    chat_type: str | None = Field(default=None, alias="chatType")


class StatusNotificationRequest(BaseModel):
    chat_id: str = Field(alias="chatId")
    status: str
    request_number: str | None = Field(default=None, alias="requestNumber")
    text: str | None = None
    channel_id: str | None = Field(default=None, alias="channelId")
    chat_type: str | None = Field(default=None, alias="chatType")


async def manager_handoff_timeout_worker() -> None:
    while True:
        try:
            closed_handoffs = await close_expired_operator_handoffs(
                MANAGER_HANDOFF_TIMEOUT_MINUTES,
            )
            for handoff in closed_handoffs:
                await log_event(
                    handoff["user_id"],
                    "operator_handoff_timeout",
                    json.dumps({
                        "handoff_id": handoff["id"],
                        "last_manager_message_at": handoff["last_manager_message_at"],
                        "closed_at": handoff["closed_at"],
                        "timeout_minutes": MANAGER_HANDOFF_TIMEOUT_MINUTES,
                    }),
                )
                logger.info(
                    "Manager handoff expired: chat_id=%s handoff_id=%s timeout_minutes=%s",
                    handoff["user_id"],
                    handoff["id"],
                    MANAGER_HANDOFF_TIMEOUT_MINUTES,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to close expired manager handoffs")

        await asyncio.sleep(MANAGER_HANDOFF_POLL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting WhatsApp webhook service...")
    await init_db()
    await wazzup.start()
    if ADMIN_USERNAME and ADMIN_PASSWORD:
        await upsert_admin_user(ADMIN_USERNAME, hash_password(ADMIN_PASSWORD), role="superadmin")
    for admin_id in ADMIN_IDS:
        await create_admin(admin_id)
    handoff_timeout_task = asyncio.create_task(manager_handoff_timeout_worker())
    try:
        yield
    finally:
        handoff_timeout_task.cancel()
        with suppress(asyncio.CancelledError):
            await handoff_timeout_task
        await close_db()
        await wazzup.close()


app = FastAPI(title="Samsonite WhatsApp Bot", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
ADMIN_COOKIE_NAME = "samsonite_admin_session"
ADMIN_SECTIONS = [
    {"id": "applications", "label": "Заявки", "href": "/admin/applications"},
    {"id": "templates", "label": "Шаблоны", "href": "/admin/templates"},
    {"id": "statistics", "label": "Статистика", "href": "/admin/statistics"},
    {"id": "customers", "label": "Клиенты", "href": "/admin/customers"},
    {"id": "payments", "label": "Платежи", "href": "/admin/payments", "status": "В разработке"},
    {"id": "settings", "label": "Настройки", "href": "/admin/settings", "status": "В разработке"},
    {"id": "users", "label": "Пользователи", "href": "/admin/users", "status": "В разработке"},
]
BITRIX_NOTIFICATION_TEMPLATE_STAGES = [
    {"stage_id": "C5:PREPARATION", "stage_name": "Передан в сервисный центр"},
    {"stage_id": "C5:PREPAYMENT_INVOICE", "stage_name": "Заказ запчастей"},
    {"stage_id": "C5:EXECUTING", "stage_name": "Чемодан в ремонте"},
    {"stage_id": "C5:FINAL_INVOICE", "stage_name": "Готов к выдаче"},
]


def require_internal_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if INTERNAL_API_KEY and x_api_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


async def parse_urlencoded_form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


async def parse_incoming_post_payload(request: Request) -> dict[str, Any]:
    body = await request.body()
    content_type = request.headers.get("content-type", "").lower()

    if "application/json" in content_type:
        try:
            parsed = json.loads(body.decode("utf-8") or "{}")
            return parsed if isinstance(parsed, dict) else {"payload": parsed}
        except json.JSONDecodeError:
            return {"raw": body.decode("utf-8", errors="replace")}

    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    try:
        parsed = json.loads(body.decode("utf-8") or "{}")
        return parsed if isinstance(parsed, dict) else {"payload": parsed}
    except json.JSONDecodeError:
        parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        if parsed:
            return {key: values[-1] for key, values in parsed.items()}
        return {"raw": body.decode("utf-8", errors="replace")}


def is_secure_request(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
    return request.url.scheme == "https" or forwarded_proto == "https"


def normalize_admin_next(next_url: str | None) -> str:
    if next_url and next_url.startswith("/admin/"):
        return next_url
    return "/admin/applications"


async def current_admin(request: Request) -> dict[str, Any] | None:
    session = request.cookies.get(ADMIN_COOKIE_NAME)
    payload = verify_session(session, ADMIN_SESSION_SECRET)
    if not payload:
        return None

    try:
        admin_id = int(payload["admin_id"])
    except (KeyError, TypeError, ValueError):
        return None

    admin = await get_admin_user_by_id(admin_id)
    if not admin or not int(admin["is_active"]):
        return None
    return admin


def admin_template_context(request: Request, admin: dict[str, Any], active_section: str) -> dict[str, Any]:
    return {
        "request": request,
        "admin": admin,
        "admin_sections": ADMIN_SECTIONS,
        "active_section": active_section,
    }


def admin_login_redirect(request: Request) -> RedirectResponse:
    next_url = request.url.path
    if request.url.query:
        next_url += f"?{request.url.query}"
    return RedirectResponse(
        f"/admin/login?next={quote(next_url, safe='')}",
        status_code=303,
    )


def user_from_message(message: dict[str, Any]) -> ChatUser:
    chat_id = str(message["chatId"])
    contact = message.get("contact") or {}
    name = contact.get("name") or chat_id
    return ChatUser(
        id=chat_id,
        username=chat_id,
        first_name=name,
    )


def build_photo_caption(user: ChatUser) -> str:
    name = f"{user.first_name or ''} {user.last_name or ''}".strip() or user.username
    return f"Фото от {name}"


def is_inbound_customer_message(message: dict[str, Any]) -> bool:
    return message.get("status") == "inbound" and not message.get("isEcho")


def is_manager_outbound_message(message: dict[str, Any]) -> bool:
    return (
        bool(message.get("isEcho"))
        and message.get("status") in {"sent", "delivered", "read"}
        and not message.get("isDeleted")
        and not message.get("isEdited")
    )


def is_dedicated_wazzup_channel(message: dict[str, Any]) -> bool:
    expected_channel_id = (WAZZUP_CHANNEL_ID or "").strip()
    incoming_channel_id = str(message.get("channelId") or "").strip()

    if not expected_channel_id:
        logger.error("WAZZUP_CHANNEL_ID is not configured; ignoring inbound Wazzup message")
        return False

    if incoming_channel_id != expected_channel_id:
        logger.info(
            "Ignoring Wazzup message %s from channel %s; expected channel %s",
            message.get("messageId"),
            incoming_channel_id or "missing",
            expected_channel_id,
        )
        return False

    return True


def normalized_chat_id(chat_id: str) -> str:
    return re.sub(r"\D", "", chat_id)


def is_allowed_chat(user_id: str) -> bool:
    if not ENABLE_CHAT_ALLOWLIST:
        return True

    allowed_chat_ids = {normalized_chat_id(chat_id) for chat_id in ALLOWED_CHAT_IDS}
    allowed_chat_ids.discard("")
    if not allowed_chat_ids:
        logger.error("ENABLE_CHAT_ALLOWLIST is enabled but ALLOWED_CHAT_IDS is empty; ignoring inbound message")
        return False

    normalized_user_id = normalized_chat_id(user_id)
    if normalized_user_id not in allowed_chat_ids:
        logger.info("Ignoring Wazzup message from chat %s; chat allowlist is enabled", normalized_user_id or user_id)
        return False

    return True


def is_superadmin(user_id: str) -> bool:
    return bool(SUPERADMIN_ID) and user_id == SUPERADMIN_ID


def deep_get(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def first_payload_value(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def extract_bitrix_deal_id(payload: dict[str, Any]) -> int | None:
    value = (
        deep_get(payload, ("data", "FIELDS", "ID"))
        or deep_get(payload, ("data", "fields", "ID"))
        or deep_get(payload, ("data", "fields", "id"))
        or deep_get(payload, ("FIELDS", "ID"))
        or deep_get(payload, ("fields", "ID"))
        or deep_get(payload, ("fields", "id"))
        or first_payload_value(
            payload,
            (
                "data[FIELDS][ID]",
                "data[fields][ID]",
                "data[fields][id]",
                "FIELDS[ID]",
                "fields[ID]",
                "fields[id]",
                "ID",
                "id",
                "dealId",
                "deal_id",
                "entityId",
                "ENTITY_ID",
            ),
        )
    )
    if value not in (None, ""):
        digits = re.sub(r"\D", "", str(value))
        return int(digits) if digits else None

    for possible_value in payload.values():
        if isinstance(possible_value, str):
            match = re.search(r"\bDEAL_(\d+)\b", possible_value)
            if match:
                return int(match.group(1))

    return None


def extract_bitrix_stage_id(payload: dict[str, Any]) -> str | None:
    value = (
        deep_get(payload, ("data", "FIELDS", "STAGE_ID"))
        or deep_get(payload, ("data", "fields", "STAGE_ID"))
        or deep_get(payload, ("data", "fields", "stageId"))
        or deep_get(payload, ("FIELDS", "STAGE_ID"))
        or deep_get(payload, ("fields", "STAGE_ID"))
        or deep_get(payload, ("fields", "stageId"))
        or first_payload_value(
            payload,
            (
                "data[FIELDS][STAGE_ID]",
                "data[fields][STAGE_ID]",
                "data[fields][stageId]",
                "FIELDS[STAGE_ID]",
                "fields[STAGE_ID]",
                "fields[stageId]",
                "STAGE_ID",
                "stageId",
                "stage_id",
                "newStageId",
                "new_stage_id",
                "properties[StageId]",
            ),
        )
    )
    return str(value).strip() if value not in (None, "") else None


def extract_bitrix_application_token(payload: dict[str, Any]) -> str | None:
    value = (
        deep_get(payload, ("auth", "application_token"))
        or first_payload_value(payload, ("auth[application_token]", "application_token"))
    )
    return str(value).strip() if value not in (None, "") else None


def is_valid_bitrix_webhook(payload: dict[str, Any]) -> bool:
    if not BITRIX_APPLICATION_TOKEN:
        return True
    return extract_bitrix_application_token(payload) == BITRIX_APPLICATION_TOKEN


def repair_status_from_bitrix_stage(stage_id: str | None) -> str | None:
    if not stage_id:
        return None
    normalized_stage_id = stage_id.strip()
    return (
        BITRIX_STAGE_STATUS_MAP.get(normalized_stage_id)
        or BITRIX_STAGE_STATUS_MAP.get(normalized_stage_id.upper())
    )


async def notify_customer_about_bitrix_status(
    sync_result: dict,
    stage_id: str | None = None,
    channel_id: str | None = None,
) -> bool:
    application = sync_result.get("application")
    if not application or not sync_result.get("stage_advanced"):
        return False

    user_id = application.get("user_id")
    new_status = sync_result.get("new_status")
    if not user_id or not new_status:
        return False

    request_number = application.get("request_number")
    text = bitrix_stage_notification_text(stage_id)
    if not text:
        return False
    try:
        await wazzup.send_text(
            chat_id=str(user_id),
            text=text,
            channel_id=channel_id or WAZZUP_CHANNEL_ID,
            chat_type=WAZZUP_CHAT_TYPE,
        )
    except Exception:
        logger.exception(
            "Failed to send Bitrix status notification for request %s to %s",
            request_number,
            user_id,
        )
        return False

    await append_dialog_message(str(user_id), "assistant", "status", text)
    await log_event(str(user_id), "bitrix_status_notification_sent", str(request_number or ""))
    return True


async def record_user_activity(user_id: str) -> int:
    with _user_activity_lock:
        version = _user_activity_versions.get(user_id, 0) + 1
        _user_activity_versions[user_id] = version
        return version


def is_latest_activity_sync(user_id: str, version: int) -> bool:
    with _user_activity_lock:
        return _user_activity_versions.get(user_id) == version


async def is_latest_activity(user_id: str, version: int) -> bool:
    return is_latest_activity_sync(user_id, version)


def generation_lock_for_user(user_id: str) -> asyncio.Lock:
    lock = _user_generation_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_generation_locks[user_id] = lock
    return lock


def is_openai_conversation_busy_error(error: Exception) -> bool:
    message = str(error).lower()
    return (
        "conversation" in message
        and (
            "operat" in message
            or "active" in message
            or "already" in message
            or "in progress" in message
            or "locked" in message
        )
    )


async def generate_response_serialized(
    user: ChatUser,
    conversation: str,
    user_message: str | None,
    activity_version: int,
    system_message: str | None = None,
    exceeded: bool = False,
) -> dict[str, Any] | None:
    lock = generation_lock_for_user(user.id)

    async with lock:
        if not await is_latest_activity(user.id, activity_version):
            await log_event(user.id, "stale_after_generation_lock", None)
            return None

        def should_continue_generation() -> bool:
            return is_latest_activity_sync(user.id, activity_version)

        for attempt in range(1, GENERATION_BUSY_RETRY_ATTEMPTS + 1):
            if not is_latest_activity_sync(user.id, activity_version):
                await log_event(user.id, "stale_before_generation_attempt", str(attempt))
                return None

            try:
                return await asyncio.to_thread(
                    generate_response,
                    user_message=user_message,
                    conversation=conversation,
                    username=user.username,
                    user_id=user.id,
                    system_message=system_message,
                    exceeded=exceeded,
                    should_continue=should_continue_generation,
                )
            except Exception as error:
                if (
                    attempt < GENERATION_BUSY_RETRY_ATTEMPTS
                    and is_openai_conversation_busy_error(error)
                    and is_latest_activity_sync(user.id, activity_version)
                ):
                    await log_event(
                        user.id,
                        "openai_conversation_busy_retry",
                        f"attempt={attempt}; error={str(error)[:400]}",
                    )
                    await asyncio.sleep(GENERATION_BUSY_RETRY_DELAY_SECONDS * attempt)
                    continue
                raise

    return None


def status_notification_text(status: str, request_number: str | None = None) -> str:
    prefix = f"Заявка {request_number}: " if request_number else "Ваша заявка: "
    normalized = status.strip().lower()
    mapping = {
        "принят": "принята сервисным центром.",
        "принята": "принята сервисным центром.",
        "диагностика": "передана на диагностику.",
        "в работе": "находится в работе.",
        "готов": "готова к выдаче. Менеджер уточнит детали получения.",
        "готово": "готова к выдаче. Менеджер уточнит детали получения.",
        "выдан": "выдана клиенту. Спасибо за обращение.",
        "выдано": "выдана клиенту. Спасибо за обращение.",
    }
    return prefix + mapping.get(normalized, f"статус изменен: {status}.")


def bitrix_stage_notification_text(stage_id: str | None) -> str | None:
    if not stage_id:
        return None

    mapping = {
        "C5:PREPARATION": (
            "Здравствуйте!\n"
            "Информируем вас об изменении статуса вашей заявки\n"
            "Статус заявки: передана мастеру сервисного центра для осмотра"
        ),
        "C5:PREPAYMENT_INVOICE": (
            "Здравствуйте!\n"
            "Статус заявки: запчасть заказана из Бельгии. Ориентировочный срок поставки составляет 2–3 месяца. "
            "По поступлении запчасти мы сразу свяжемся с вами"
        ),
        "C5:EXECUTING": (
            "Здравствуйте!\n"
            "Статус заявки: ваше изделие находится в ремонте и обслуживается в порядке очереди. "
            "По готовности мы обязательно сообщим вам."
        ),
        "C5:FINAL_INVOICE": (
            "Здравствуйте!\n"
            "Благодарим за обращение в наш сервисный центр. Информируем вас об изменении статуса вашей заявки\n"
            "Статус заявки: ваше изделие поступило в бутик и готово к выдаче. "
            "Стоимость ремонта вы можете уточнить в данном диалоге."
        ),
    }
    return mapping.get(stage_id.strip())


def bitrix_notification_template_rows() -> list[dict[str, str]]:
    return [
        {
            **stage,
            "text": bitrix_stage_notification_text(stage["stage_id"]) or "",
        }
        for stage in BITRIX_NOTIFICATION_TEMPLATE_STAGES
    ]


def repair_status_stat_rows(stats: dict[str, int]) -> list[dict[str, Any]]:
    total = max(stats.get("Все", 0), 1)
    return [
        {
            "status": status,
            "count": stats.get(status, 0),
            "percent": round((stats.get(status, 0) / total) * 100, 1),
        }
        for status in REPAIR_REQUEST_STATUSES
    ]


def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "Нет данных"

    total_seconds = max(0, round(float(seconds)))
    if total_seconds < 60:
        return f"{total_seconds} сек"

    minutes, remaining_seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {remaining_seconds} сек"

    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours} ч {remaining_minutes} мин"


def repair_request_columns(applications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {status: [] for status in REPAIR_REQUEST_STATUSES}
    unknown_statuses: dict[str, list[dict[str, Any]]] = {}

    for application in applications:
        status = (application.get("status") or "Без статуса").strip() or "Без статуса"
        if status in grouped:
            grouped[status].append(application)
        else:
            unknown_statuses.setdefault(status, []).append(application)

    columns = [
        {
            "status": status,
            "applications": grouped[status],
            "count": len(grouped[status]),
        }
        for status in REPAIR_REQUEST_STATUSES
    ]
    columns.extend(
        {
            "status": status,
            "applications": items,
            "count": len(items),
        }
        for status, items in unknown_statuses.items()
    )
    return columns


async def ensure_conversation(user: ChatUser) -> str:
    conversation = await get_user_conversation(user.id)
    if conversation:
        return conversation

    conversation = await new_conversation()
    await create_or_update_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        conversation=conversation,
    )
    return conversation


async def current_token_usage(user_id: str) -> dict[str, int]:
    usage = await get_token_usage(user_id)
    return usage or {"input": 0, "output": 0}


async def media_bytes_from_record(media: dict[str, Any]) -> bytes | None:
    file_path = media.get("file_path")
    if file_path and Path(file_path).exists():
        return Path(file_path).read_bytes()

    content_uri = media.get("content_uri")
    if content_uri:
        return (await wazzup.download_content(content_uri)).data

    return None


async def store_wazzup_content(
    user: ChatUser,
    message: dict[str, Any],
    media_type: str,
) -> DownloadedContent | None:
    content_uri = message.get("contentUri")
    if not content_uri:
        return None

    content = await wazzup.download_content(content_uri)
    file_path = store_media_bytes(
        user_id=user.id,
        data=content.data,
        filename=content.filename,
        content_type=content.content_type,
    )
    await save_media_file(
        user_id=user.id,
        file_path=str(file_path),
        filename=file_path.name,
        content_type=content.content_type,
        media_type=media_type,
        source_message_id=message.get("messageId"),
        content_uri=content_uri,
    )
    return content


async def stored_request_media_count(user_id: str) -> int:
    return await count_media_files(user_id, media_types=["image", "video"])


async def handle_completed_request(
    user: ChatUser,
    data_to_send: dict[str, Any],
    channel_id: str,
    chat_type: str,
) -> None:
    admin_ids = await get_admin_ids()
    media_files = await get_media_files(user.id, media_types=["image", "video"])

    admin_text = format_message(data_to_send)
    if media_files:
        admin_text += f"\n\nФото: {len(media_files)} файл(а) прикреплены к сделке Bitrix."

    for admin_id in admin_ids:
        try:
            await wazzup.send_text(
                chat_id=str(admin_id),
                text=admin_text,
                channel_id=channel_id,
                chat_type=chat_type,
            )
        except Exception:
            logger.exception("Failed to notify admin %s", admin_id)

    files_to_upload = []
    for media in media_files:
        data = await media_bytes_from_record(media)
        if data:
            files_to_upload.append({
                "filename": media.get("filename") or "attachment.bin",
                "content": base64.b64encode(data).decode("utf-8"),
            })

    if files_to_upload and data_to_send.get("deal_id"):
        await asyncio.to_thread(upload_files_to_bitrix, data_to_send["deal_id"], files_to_upload)


async def notify_admins(
    text: str,
    channel_id: str,
    chat_type: str,
) -> None:
    for admin_id in await get_admin_ids():
        try:
            await wazzup.send_text(
                chat_id=str(admin_id),
                text=text,
                channel_id=channel_id,
                chat_type=chat_type,
            )
        except Exception:
            logger.exception("Failed to notify admin %s", admin_id)


def format_recent_dialog(dialog: list[dict[str, Any]]) -> str:
    lines = []
    for item in dialog:
        text = item.get("text") or ""
        if len(text) > 500:
            text = text[:497] + "..."
        lines.append(f"{item['created_at']} {item['role']} ({item['message_type']}): {text}")
    return "\n".join(lines) or "История пуста"


async def handle_handoff(
    user: ChatUser,
    handoff: dict[str, Any],
    channel_id: str,
    chat_type: str,
) -> None:
    await set_bot_paused(user.id, True, handoff.get("reason"))
    handoff_record = await create_operator_handoff(
        user.id,
        handoff.get("reason"),
        handoff.get("summary"),
    )
    await log_event(
        user.id,
        "handoff",
        json.dumps({
            "handoff_id": handoff_record.get("id"),
            "reason": handoff.get("reason"),
        }, ensure_ascii=False),
    )

    dialog = await get_recent_dialog(user.id, limit=20)
    admin_text = (
        "Требуется оператор.\n\n"
        f"Передача: #{handoff_record.get('id')}\n"
        f"Клиент: {user.first_name or user.username}\n"
        f"WhatsApp: {user.id}\n"
        f"Причина: {handoff.get('reason')}\n"
        f"Кратко: {handoff.get('summary')}\n\n"
        "Последние сообщения:\n"
        f"{format_recent_dialog(dialog)}"
    )
    await notify_admins(admin_text, channel_id, chat_type)


async def maybe_save_feedback(user: ChatUser, text: str, channel_id: str, chat_type: str) -> bool:
    stripped = text.strip()
    match = re.match(r"^/(?:feedback|review)\s+([1-5])(?:\s+(.*))?$", stripped, re.IGNORECASE)
    if not match:
        match = re.match(r"^(?:оценка|отзыв)\s*[:\-]?\s*([1-5])(?:\s+(.*))?$", stripped, re.IGNORECASE)

    if not match:
        return False

    rating = int(match.group(1))
    comment = match.group(2)
    await save_feedback(user.id, rating, comment)
    await log_event(user.id, "feedback", str(rating))
    await wazzup.send_text(
        user.id,
        "Спасибо за оценку. Отзыв зафиксирован.",
        channel_id=channel_id,
        chat_type=chat_type,
    )
    await append_dialog_message(user.id, "assistant", "text", "Спасибо за оценку. Отзыв зафиксирован.")
    return True


async def run_agent_and_reply(
    user: ChatUser,
    channel_id: str,
    chat_type: str,
    user_message: str | None,
    activity_version: int,
    system_message: str | None = None,
) -> None:
    conversation = await ensure_conversation(user)

    token_usage = await current_token_usage(user.id)
    token_sum = sum(token_usage.values())
    if token_sum > ABSOLUTE_LIMIT:
        return

    await asyncio.sleep(RESPONSE_DEBOUNCE_SECONDS)
    if (
        not await is_latest_activity(user.id, activity_version)
        or await is_bot_paused(user.id)
    ):
        await log_event(user.id, "stale_before_generation", None)
        return

    exceeded = token_sum > LIMIT_PER_USER

    result = await generate_response_serialized(
        user=user,
        conversation=conversation,
        user_message=user_message,
        activity_version=activity_version,
        system_message=system_message,
        exceeded=exceeded,
    )
    if result is None:
        return

    await add_token_usage(user.id, result["input"], result["output"])

    if (
        not await is_latest_activity(user.id, activity_version)
        or await is_bot_paused(user.id)
    ):
        await log_event(user.id, "stale_after_generation", result.get("response_id"))
        return

    if result["response"]:
        await wazzup.send_text(
            chat_id=user.id,
            text=result["response"],
            channel_id=channel_id,
            chat_type=chat_type,
        )
        await append_dialog_message(user.id, "assistant", "text", result["response"])

    if result["data to send"]:
        await handle_completed_request(user, result["data to send"], channel_id, chat_type)
        await log_event(user.id, "lead_created", str(result["data to send"].get("deal_id")))
        feedback_text = "Оцените, пожалуйста, консультацию от 1 до 5. Можно написать: оценка 5"
        await wazzup.send_text(user.id, feedback_text, channel_id=channel_id, chat_type=chat_type)
        await append_dialog_message(user.id, "assistant", "text", feedback_text)

    if result.get("handoff"):
        await handle_handoff(user, result["handoff"], channel_id, chat_type)


async def reset_conversation(user: ChatUser, channel_id: str, chat_type: str, activity_version: int) -> None:
    conversation = await new_conversation()
    await create_or_update_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        conversation=conversation,
    )
    await cancel_open_operator_handoff(user.id)
    await set_bot_paused(user.id, False)

    system_message = "Пользователь начал диалог. Поприветствуй и скажи куда он обратился на казахском и русском."

    result = await generate_response_serialized(
        user=user,
        conversation=conversation,
        user_message=None,
        activity_version=activity_version,
        system_message=system_message,
    )
    if result is None:
        return

    await add_token_usage(user.id, result["input"], result["output"])

    if not await is_latest_activity(user.id, activity_version):
        await log_event(user.id, "stale_reset_response", result.get("response_id"))
        return

    if result["response"]:
        await wazzup.send_text(user.id, result["response"], channel_id=channel_id, chat_type=chat_type)
        await append_dialog_message(user.id, "assistant", "text", result["response"])


async def handle_command(
    user: ChatUser,
    channel_id: str,
    chat_type: str,
    text: str,
    activity_version: int,
) -> bool:
    parts = text.strip().split(maxsplit=1)
    command = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if command == "/start":
        await reset_conversation(user, channel_id, chat_type, activity_version)
        return True

    if command == "/resume":
        target_user_id = args.strip() or user.id
        if target_user_id != user.id and not is_superadmin(user.id):
            await wazzup.send_text(user.id, "Insufficient rights", channel_id=channel_id, chat_type=chat_type)
            return True
        await cancel_open_operator_handoff(target_user_id)
        await set_bot_paused(target_user_id, False)
        await wazzup.send_text(user.id, f"Bot resumed for {target_user_id}", channel_id=channel_id, chat_type=chat_type)
        return True

    if command in {"/operator", "/manager"}:
        handoff = {
            "reason": "Клиент запросил оператора",
            "summary": args or "Клиент хочет продолжить диалог с менеджером",
        }
        await handle_handoff(user, handoff, channel_id, chat_type)
        await wazzup.send_text(
            user.id,
            "Передал диалог менеджеру. Специалист подключится и продолжит консультацию.",
            channel_id=channel_id,
            chat_type=chat_type,
        )
        return True

    if command == "/help":
        await wazzup.send_text(
            user.id,
            "Доступные команды:\n/start - Перезапустить бота\n/operator - Позвать менеджера\n/resume - Включить бота снова\n/help - Показать это сообщение\n/about - О боте\n/admin - Подать заявку на админа\n/feedback 5 текст - Оставить оценку",
            channel_id=channel_id,
            chat_type=chat_type,
        )
        return True

    if command == "/about":
        await wazzup.send_text(
            user.id,
            "Этот бот собирает заявки на ремонт\nТех поддержка - @levineduard",
            channel_id=channel_id,
            chat_type=chat_type,
        )
        return True

    if command == "/admin":
        if SUPERADMIN_ID:
            await wazzup.send_text(
                SUPERADMIN_ID,
                f"Admin request {user.id}",
                channel_id=channel_id,
                chat_type=chat_type,
            )
        await wazzup.send_text(user.id, "Request sent", channel_id=channel_id, chat_type=chat_type)
        return True

    if command == "/usage":
        usage = await current_token_usage(user.id)
        await wazzup.send_text(
            user.id,
            f'{user.id}:\n\ninput: {usage["input"]}\noutput: {usage["output"]}\n\nGPT model: {GPT_MODEL}',
            channel_id=channel_id,
            chat_type=chat_type,
        )
        return True

    if command not in {"/users", "/admins", "/newadmin", "/deladmin", "/query", "/analytics"}:
        return False

    if not is_superadmin(user.id):
        await wazzup.send_text(user.id, "Insufficient rights", channel_id=channel_id, chat_type=chat_type)
        return True

    if command == "/users":
        users = await get_users()
        users_list = "\n".join([f"{item[0]} - {item[1]}" for item in users]) or "No users"
        await wazzup.send_text(user.id, users_list, channel_id=channel_id, chat_type=chat_type)
        return True

    if command == "/admins":
        admins = await get_admin_ids()
        admins_list = "\n".join(map(str, admins)) or "No admins"
        await wazzup.send_text(user.id, admins_list, channel_id=channel_id, chat_type=chat_type)
        return True

    if command == "/newadmin":
        if not args:
            await wazzup.send_text(user.id, "Usage: /newadmin 77000000000", channel_id=channel_id, chat_type=chat_type)
            return True
        await create_admin(args.strip())
        await wazzup.send_text(user.id, f"Admin id {args.strip()} added", channel_id=channel_id, chat_type=chat_type)
        return True

    if command == "/deladmin":
        if not args:
            await wazzup.send_text(user.id, "Usage: /deladmin 77000000000", channel_id=channel_id, chat_type=chat_type)
            return True
        await delete_admin(args.strip())
        await wazzup.send_text(user.id, f"Admin id {args.strip()} deleted", channel_id=channel_id, chat_type=chat_type)
        return True

    if command == "/query":
        result = await execute_query(args)
        await wazzup.send_text(user.id, result, channel_id=channel_id, chat_type=chat_type)
        return True

    if command == "/analytics":
        result = await get_analytics_summary()
        await wazzup.send_text(user.id, result, channel_id=channel_id, chat_type=chat_type)
        return True

    return False


async def process_text_message(
    user: ChatUser,
    message: dict[str, Any],
    channel_id: str,
    chat_type: str,
    activity_version: int,
) -> None:
    text = message.get("text") or ""
    if await maybe_save_feedback(user, text, channel_id, chat_type):
        return

    if text.startswith("/") and await handle_command(user, channel_id, chat_type, text, activity_version):
        return

    await run_agent_and_reply(
        user=user,
        channel_id=channel_id,
        chat_type=chat_type,
        user_message=text,
        activity_version=activity_version,
    )


async def process_image_message(
    user: ChatUser,
    message: dict[str, Any],
    channel_id: str,
    chat_type: str,
    activity_version: int,
) -> None:
    if await stored_request_media_count(user.id) >= REQUEST_MEDIA_LIMIT:
        await wazzup.send_text(
            user.id,
            "Файл не добавлен: к заявке можно приложить до 5 фото или видео.",
            channel_id=channel_id,
            chat_type=chat_type,
        )
        return

    content = await store_wazzup_content(user, message, media_type="image")
    photo_analysis = None
    if content:
        photo_analysis = await maybe_process_incoming_photo(
            message=message,
            wazzup=wazzup,
            downloaded_content=content,
        )

    system_message = (
        "Пользователь только что отправил фото повреждения. "
        "Фото успешно получено и будет прикреплено к заявке. "
        "Спроси клиента: будет ли он отправлять ещё фотографии? "
        "Если клиент говорит что больше фото не будет — вызови send_contact_details. "
        "НЕ упоминай техническую сторону (что фото 'направится автоматически' и т.п.) — "
        "просто подтверди получение и спроси про дополнительные фото."
    )
    if photo_analysis:
        system_message += f"\n\n{photo_analysis.as_agent_context()}"

    await run_agent_and_reply(
        user=user,
        channel_id=channel_id,
        chat_type=chat_type,
        user_message=message.get("text"),
        activity_version=activity_version,
        system_message=system_message,
    )


async def process_video_message(
    user: ChatUser,
    message: dict[str, Any],
    channel_id: str,
    chat_type: str,
    activity_version: int,
) -> None:
    if await stored_request_media_count(user.id) >= REQUEST_MEDIA_LIMIT:
        await wazzup.send_text(
            user.id,
            "Файл не добавлен: к заявке можно приложить до 5 фото или видео.",
            channel_id=channel_id,
            chat_type=chat_type,
        )
        return

    await store_wazzup_content(user, message, media_type="video")
    system_message = (
        "Пользователь только что отправил видео повреждения. "
        "Видео успешно получено и будет прикреплено к заявке. "
        "Подтверди получение и спроси, будет ли клиент отправлять ещё фото или видео."
    )
    await run_agent_and_reply(
        user=user,
        channel_id=channel_id,
        chat_type=chat_type,
        user_message=message.get("text"),
        activity_version=activity_version,
        system_message=system_message,
    )


async def process_audio_message(
    user: ChatUser,
    message: dict[str, Any],
    channel_id: str,
    chat_type: str,
    activity_version: int,
) -> None:
    content = await store_wazzup_content(user, message, media_type="audio")
    text = None
    if content:
        voice_buffer = io.BytesIO(content.data)
        voice_buffer.name = content.filename or "voice.ogg"
        try:
            text = await asyncio.to_thread(transcribe, voice_buffer)
        except Exception:
            logger.exception("Failed to transcribe voice message %s", message.get("messageId"))

    system_message = None
    if not text:
        system_message = "Пользователь отправил голосовое сообщение, но его не удалось расшифровать. Попроси отправить аудио ещё раз либо написать текстом"

    await run_agent_and_reply(
        user=user,
        channel_id=channel_id,
        chat_type=chat_type,
        user_message=text,
        activity_version=activity_version,
        system_message=system_message,
    )


async def process_manager_outbound_message(message: dict[str, Any]) -> None:
    chat_id = str(message.get("chatId") or "").strip()
    if not chat_id or not is_allowed_chat(chat_id):
        return

    message_id = str(message.get("messageId") or "").strip() or None
    if message_id and not await mark_message_processed(message_id, chat_id):
        return

    handoff = await record_operator_message(
        user_id=chat_id,
        manager_message_id=message_id,
        manager_id=str(message.get("authorId") or "").strip() or None,
        manager_name=str(message.get("authorName") or "").strip() or None,
        responded_at=str(message.get("dateTime") or "").strip() or None,
    )
    if not handoff:
        return

    await record_user_activity(chat_id)

    message_type = str(message.get("type") or "unknown")
    text = message.get("text") or f"[{message_type}]"
    await append_dialog_message(chat_id, "operator", message_type, text)
    manager_takeover_created = (
        handoff.get("initiated_by") == "manager"
        and handoff.get("manager_message_id") == message_id
    )
    if manager_takeover_created:
        event_type = "operator_takeover"
    elif handoff.get("first_response_recorded"):
        event_type = "operator_first_response"
    else:
        event_type = "operator_message"
    event_payload = {
        "handoff_id": handoff.get("id"),
        "response_time_seconds": handoff.get("response_time_seconds"),
        "last_manager_message_at": handoff.get("last_manager_message_at"),
        "manager_id": handoff.get("manager_id"),
        "manager_name": handoff.get("manager_name"),
        "message_id": message_id,
    }
    await log_event(
        chat_id,
        event_type,
        json.dumps(event_payload, ensure_ascii=False),
    )
    logger.info(
        "Manager message recorded: chat_id=%s handoff_id=%s event=%s response_time_seconds=%s manager=%s",
        chat_id,
        handoff.get("id"),
        event_type,
        handoff.get("response_time_seconds"),
        handoff.get("manager_name") or handoff.get("manager_id") or "unknown",
    )


async def process_wazzup_message(message: dict[str, Any]) -> None:
    if not is_dedicated_wazzup_channel(message):
        return

    if is_manager_outbound_message(message):
        await process_manager_outbound_message(message)
        return

    if not is_inbound_customer_message(message):
        return

    user = user_from_message(message)
    if not is_allowed_chat(user.id):
        return

    channel_id = message.get("channelId") or WAZZUP_CHANNEL_ID
    chat_type = message.get("chatType") or WAZZUP_CHAT_TYPE
    message_type = message.get("type")
    message_id = message.get("messageId")

    try:
        if message_id:
            is_new = await mark_message_processed(message_id, user.id)
            if not is_new:
                return

        await log_event(user.id, f"inbound_{message_type}", message_id)
        await append_dialog_message(
            user.id,
            "user",
            message_type or "unknown",
            message.get("text") or f"[{message_type}]",
        )
        activity_version = await record_user_activity(user.id)

        if message_type == "text":
            text = message.get("text") or ""
            if await is_bot_paused(user.id) and not text.startswith(("/start", "/resume")):
                await log_event(user.id, "message_while_paused", message_id)
                return
            await process_text_message(user, message, channel_id, chat_type, activity_version)
        elif message_type == "image":
            if await is_bot_paused(user.id):
                await log_event(user.id, "message_while_paused", message_id)
                return
            await process_image_message(user, message, channel_id, chat_type, activity_version)
        elif message_type == "video":
            if await is_bot_paused(user.id):
                await log_event(user.id, "message_while_paused", message_id)
                return
            await process_video_message(user, message, channel_id, chat_type, activity_version)
        elif message_type == "audio":
            if await is_bot_paused(user.id):
                await log_event(user.id, "message_while_paused", message_id)
                return
            await process_audio_message(user, message, channel_id, chat_type, activity_version)
        else:
            await wazzup.send_text(
                user.id,
                "Пожалуйста, отправьте сообщение текстом, голосом или фото.",
                channel_id=channel_id,
                chat_type=chat_type,
            )
    except Exception:
        logger.exception("Failed to process Wazzup message %s", message.get("messageId"))


@app.get("/admin")
async def admin_index() -> RedirectResponse:
    return RedirectResponse("/admin/applications", status_code=303)


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    if await current_admin(request):
        return RedirectResponse("/admin/applications", status_code=303)
    return templates.TemplateResponse(
        "admin_login.html",
        {
            "request": request,
            "error": None,
            "username": "",
            "next_url": request.query_params.get("next") or "",
        },
    )


@app.post("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request):
    form = await parse_urlencoded_form(request)
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    next_url = normalize_admin_next(form.get("next"))

    admin = await get_admin_user_by_username(username)
    if not admin or not int(admin["is_active"]) or not verify_password(password, admin["password_hash"]):
        return templates.TemplateResponse(
            "admin_login.html",
            {
                "request": request,
                "error": "Неверный логин или пароль",
                "username": username,
                "next_url": next_url,
            },
            status_code=401,
        )

    response = RedirectResponse(next_url, status_code=303)
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        sign_session(int(admin["id"]), ADMIN_SESSION_SECRET),
        httponly=True,
        secure=is_secure_request(request),
        samesite="lax",
        max_age=60 * 60 * 12,
    )
    return response


@app.post("/admin/logout")
async def admin_logout() -> RedirectResponse:
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(ADMIN_COOKIE_NAME)
    return response


@app.get("/admin/applications", response_class=HTMLResponse)
async def admin_applications(request: Request):
    admin = await current_admin(request)
    if not admin:
        return admin_login_redirect(request)

    q = (request.query_params.get("q") or "").strip()

    applications = await list_repair_requests(q=q or None)
    stats = await get_repair_request_stats()
    context = admin_template_context(request, admin, "applications")
    context.update(
        {
            "applications": applications,
            "application_columns": repair_request_columns(applications),
            "stats": stats,
            "statuses": REPAIR_REQUEST_STATUSES,
            "q": q,
            "current_path": str(request.url.path),
            "current_query": str(request.url.query),
        }
    )
    return templates.TemplateResponse(
        "admin_applications.html",
        context,
    )


@app.post("/admin/applications/{request_id}/status")
async def admin_application_status(request: Request, request_id: int) -> RedirectResponse:
    admin = await current_admin(request)
    if not admin:
        return RedirectResponse("/admin/login", status_code=303)

    form = await parse_urlencoded_form(request)
    status = (form.get("status") or "").strip()
    if status not in REPAIR_REQUEST_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid repair request status")

    await update_repair_request_status(request_id, status)
    next_url = normalize_admin_next(form.get("next"))
    return RedirectResponse(next_url, status_code=303)


@app.get("/admin/templates", response_class=HTMLResponse)
async def admin_templates_page(request: Request):
    admin = await current_admin(request)
    if not admin:
        return admin_login_redirect(request)

    context = admin_template_context(request, admin, "templates")
    context["notification_templates"] = bitrix_notification_template_rows()
    return templates.TemplateResponse("admin_templates.html", context)


@app.get("/admin/statistics", response_class=HTMLResponse)
async def admin_statistics(request: Request):
    admin = await current_admin(request)
    if not admin:
        return admin_login_redirect(request)

    stats = await get_repair_request_stats()
    handoff_stats = await get_operator_handoff_stats()
    analytics_summary = await get_analytics_summary()
    context = admin_template_context(request, admin, "statistics")
    context.update(
        {
            "stats": stats,
            "status_rows": repair_status_stat_rows(stats),
            "handoff_stats": handoff_stats,
            "average_manager_response": format_duration(handoff_stats.get("average_seconds")),
            "maximum_manager_response": format_duration(handoff_stats.get("maximum_seconds")),
            "analytics_summary": analytics_summary,
        }
    )
    return templates.TemplateResponse("admin_statistics.html", context)


@app.get("/admin/statistics/export.csv")
async def admin_statistics_export(request: Request) -> Response:
    admin = await current_admin(request)
    if not admin:
        return admin_login_redirect(request)

    applications = await list_repair_requests()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "request_number",
        "status",
        "name",
        "phone",
        "city",
        "service_type",
        "product_type",
        "brand",
        "model",
        "article",
        "problem",
        "estimated_price_range",
        "deal_id",
        "created_at",
        "updated_at",
    ])
    for application in applications:
        writer.writerow([
            application.get("request_number") or application.get("id"),
            application.get("status") or "",
            application.get("name") or "",
            application.get("phone") or "",
            application.get("city") or "",
            application.get("service_type") or "",
            application.get("product_type") or "",
            application.get("brand") or "",
            application.get("model") or "",
            application.get("article") or "",
            application.get("problem") or "",
            application.get("estimated_price_range") or "",
            application.get("deal_id") or "",
            application.get("created_at") or "",
            application.get("updated_at") or "",
        ])

    return Response(
        "\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="repair-applications.csv"'},
    )


async def render_admin_placeholder(
    request: Request,
    section_id: str,
    title: str,
    description: str,
    planned_items: list[str],
):
    admin = await current_admin(request)
    if not admin:
        return admin_login_redirect(request)

    context = admin_template_context(request, admin, section_id)
    context.update(
        {
            "title": title,
            "description": description,
            "planned_items": planned_items,
        }
    )
    return templates.TemplateResponse("admin_placeholder.html", context)


@app.get("/admin/customers", response_class=HTMLResponse)
async def admin_customers(request: Request):
    admin = await current_admin(request)
    if not admin:
        return admin_login_redirect(request)

    q = (request.query_params.get("q") or "").strip()
    customers = await list_customers(q=q or None)
    context = admin_template_context(request, admin, "customers")
    context.update(
        {
            "customers": customers,
            "q": q,
        }
    )
    return templates.TemplateResponse("admin_customers.html", context)


@app.get("/admin/payments", response_class=HTMLResponse)
async def admin_payments(request: Request):
    return await render_admin_placeholder(
        request,
        "payments",
        "Платежи",
        "Kaspi QR, счета и статусы оплат по заявкам.",
        ["создание QR", "уведомления о платеже", "связь платежа с заявкой"],
    )


@app.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings(request: Request):
    return await render_admin_placeholder(
        request,
        "settings",
        "Настройки",
        "Рабочие параметры бота и интеграций без хранения секретов в браузере.",
        ["рабочие часы", "режим тестовых номеров", "проверка Wazzup, Bitrix и Kaspi"],
    )


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request):
    return await render_admin_placeholder(
        request,
        "users",
        "Пользователи",
        "Операторы, администраторы и роли доступа.",
        ["создание операторов", "активация и блокировка", "роли и права"],
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def handle_wazzup_webhook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("test") is True:
        return {"ok": True}

    messages = payload.get("messages") or []
    for message in messages:
        asyncio.create_task(process_wazzup_message(message))

    return {"ok": True, "accepted": len(messages)}


@app.post("/webhook/wazzup")
async def wazzup_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    return await handle_wazzup_webhook_payload(payload)


@app.post("/webhook")
async def webhook(payload: dict[str, Any]) -> dict[str, Any]:
    return await handle_wazzup_webhook_payload(payload)


@app.post("/webhooks")
async def webhooks_alias(payload: dict[str, Any]) -> dict[str, Any]:
    return await handle_wazzup_webhook_payload(payload)


@app.post("/webhook/bitrix")
async def bitrix_webhook(request: Request) -> dict[str, Any]:
    payload = await parse_incoming_post_payload(request)
    if not is_valid_bitrix_webhook(payload):
        logger.warning("Rejected Bitrix webhook with invalid application token")
        raise HTTPException(status_code=403, detail="Invalid Bitrix application token")

    event_name = first_payload_value(payload, ("event", "EVENT", "eventName", "event_name"))
    deal_id = extract_bitrix_deal_id(payload)
    stage_id = extract_bitrix_stage_id(payload)
    stage_id_source = "payload" if stage_id else None
    if deal_id and not stage_id:
        stage_id = await asyncio.to_thread(get_bitrix_deal_stage_id, deal_id)
        stage_id_source = "bitrix_fetch" if stage_id else None
    repair_status = repair_status_from_bitrix_stage(stage_id)

    sync_result = {
        "application": None,
        "changed": False,
        "updated": 0,
        "old_status": None,
        "new_status": repair_status,
        "stage_advanced": False,
        "furthest_stage_id": None,
        "furthest_stage_rank": None,
    }
    notification_sent = False
    if deal_id and repair_status:
        sync_result = await sync_repair_request_status_by_deal_id(
            deal_id,
            repair_status,
            stage_id=stage_id,
        )
        notification_sent = await notify_customer_about_bitrix_status(sync_result, stage_id=stage_id)

    application = sync_result.get("application")

    log_payload = {
        "event": event_name,
        "deal_id": deal_id,
        "stage_id": stage_id,
        "stage_id_source": stage_id_source,
        "mapped_status": repair_status,
        "old_status": sync_result.get("old_status"),
        "changed": sync_result.get("changed"),
        "stage_advanced": sync_result.get("stage_advanced"),
        "furthest_stage_id": sync_result.get("furthest_stage_id"),
        "furthest_stage_rank": sync_result.get("furthest_stage_rank"),
        "updated": sync_result.get("updated"),
        "notification_sent": notification_sent,
        "request_number": application.get("request_number") if application else None,
        "user_id": application.get("user_id") if application else None,
        "payload_keys": sorted(payload.keys()),
    }
    await log_event(None, "bitrix_stage_webhook", json.dumps(log_payload, ensure_ascii=False))
    logger.info(
        "Bitrix stage webhook received: deal_id=%s stage_id=%s source=%s mapped_status=%s changed=%s advanced=%s updated=%s notification_sent=%s",
        deal_id,
        stage_id,
        stage_id_source,
        repair_status,
        sync_result.get("changed"),
        sync_result.get("stage_advanced"),
        sync_result.get("updated"),
        notification_sent,
    )

    return {
        "ok": True,
        "dealId": deal_id,
        "stageId": stage_id,
        "stageIdSource": stage_id_source,
        "mappedStatus": repair_status,
        "oldStatus": sync_result.get("old_status"),
        "changed": sync_result.get("changed"),
        "stageAdvanced": sync_result.get("stage_advanced"),
        "furthestStageId": sync_result.get("furthest_stage_id"),
        "updated": sync_result.get("updated"),
        "notificationSent": notification_sent,
        "requestNumber": application.get("request_number") if application else None,
    }


@app.post("/send", dependencies=[Depends(require_internal_api_key)])
async def send_message(request: SendMessageRequest) -> dict[str, Any]:
    return await wazzup.send_text(
        chat_id=request.chat_id,
        text=request.text,
        channel_id=request.channel_id,
        chat_type=request.chat_type,
    )


@app.post("/crm/status", dependencies=[Depends(require_internal_api_key)])
async def crm_status(request: StatusNotificationRequest) -> dict[str, Any]:
    text = request.text or status_notification_text(request.status, request.request_number)
    await log_event(request.chat_id, "status_notification", request.status)
    await append_dialog_message(request.chat_id, "assistant", "status", text)
    return await wazzup.send_text(
        chat_id=request.chat_id,
        text=text,
        channel_id=request.channel_id,
        chat_type=request.chat_type,
    )
