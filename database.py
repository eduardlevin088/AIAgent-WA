import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

import asyncpg

from config import BITRIX_BOT_STAGE_ID, BITRIX_STAGE_RANKS, DATABASE_URL
from config import MANAGER_WORKING_DAYS, MANAGER_WORKING_HOURS_ENABLED
from config import MANAGER_WORKING_HOURS_END, MANAGER_WORKING_HOURS_START
from config import MANAGER_LOG_RETENTION_DAYS
from prettytable import PrettyTable


logger = logging.getLogger(__name__)

_QUESTION_MARK_RE = re.compile(r"\?")
_INSERT_OR_IGNORE_RE = re.compile(r"(?i)^\s*INSERT\s+OR\s+IGNORE\s+INTO")


class _CompatCursor:
    def __init__(
        self,
        rows: list,
        rowcount: int = 0,
        lastrowid: int | None = None,
    ):
        self._rows = rows
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def fetchone(self):
        if not self._rows:
            return None
        return self._rows.pop(0)

    async def fetchall(self):
        rows = self._rows
        self._rows = []
        return rows


def _normalize_sql(sql: str) -> str:
    had_insert_or_ignore = bool(_INSERT_OR_IGNORE_RE.match(sql))
    normalized = _INSERT_OR_IGNORE_RE.sub("INSERT INTO", sql.strip(), count=1)
    if had_insert_or_ignore and "ON CONFLICT" not in normalized.upper():
        base = normalized.rstrip()
        if not base.endswith("ON CONFLICT DO NOTHING"):
            normalized = f"{base} ON CONFLICT DO NOTHING"
    return normalized


def _sqlite_to_pg(sql: str) -> str:
    sql = _normalize_sql(sql)

    i = 0
    def repl(_: re.Match[str]) -> str:
        nonlocal i
        i += 1
        return f"${i}"

    return _QUESTION_MARK_RE.sub(repl, sql)


def _parse_rowcount(status: str) -> int:
    if not status:
        return 0
    parts = status.split()
    if not parts:
        return 0
    if parts[0] == "INSERT" and len(parts) >= 3 and parts[-1].isdigit():
        return int(parts[-1])
    if len(parts) == 1:
        return 0
    return int(parts[-1]) if parts[-1].isdigit() else 0


class _AsyncDatabase:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def close(self) -> None:
        await self._pool.close()

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def execute(self, query: str, params: tuple | list | Sequence = ()) -> _CompatCursor:
        if params is None:
            params = ()
        normalized = _sqlite_to_pg(query)
        async with self._pool.acquire() as conn:
            upper = normalized.lstrip().upper()
            if " RETURNING " in upper or upper.startswith("SELECT") or upper.startswith("WITH"):
                rows = await conn.fetch(normalized, *params)
                rowcount = len(rows)
                lastrowid = None
                if rows:
                    first = rows[0]
                    if "id" in first.keys():
                        try:
                            lastrowid = int(first["id"])
                        except (TypeError, ValueError):
                            lastrowid = None
                return _CompatCursor(rows=list(rows), rowcount=rowcount, lastrowid=lastrowid)

            status = await conn.execute(normalized, *params)
            return _CompatCursor([], rowcount=_parse_rowcount(status), lastrowid=None)

    async def executemany(self, query: str, seq: Sequence[Sequence]) -> _CompatCursor:
        rowcount = 0
        for params in seq:
            cursor = await self.execute(query, params)
            rowcount += cursor.rowcount
        return _CompatCursor([], rowcount=rowcount)


db: Optional[_AsyncDatabase] = None
_handoff_operation_lock = asyncio.Lock()

DEFAULT_NOTIFICATION_TEMPLATES = (
    {
        "stage_id": "C5:PREPARATION",
        "stage_name": "Передан в сервисный центр",
        "text": (
            "Здравствуйте!\n"
            "Информируем вас об изменении статуса вашей заявки\n"
            "Статус заявки: передана мастеру сервисного центра для осмотра"
        ),
    },
    {
        "stage_id": "C5:PREPAYMENT_INVOICE",
        "stage_name": "Заказ запчастей",
        "text": (
            "Здравствуйте!\n"
            "Статус заявки: запчасть заказана из Бельгии. Ориентировочный срок поставки составляет 2–3 месяца. "
            "По поступлении запчасти мы сразу свяжемся с вами"
        ),
    },
    {
        "stage_id": "C5:EXECUTING",
        "stage_name": "Чемодан в ремонте",
        "text": (
            "Здравствуйте!\n"
            "Статус заявки: ваше изделие находится в ремонте и обслуживается в порядке очереди. "
            "По готовности мы обязательно сообщим вам."
        ),
    },
    {
        "stage_id": "C5:FINAL_INVOICE",
        "stage_name": "Готов к выдаче",
        "text": (
            "Здравствуйте!\n"
            "Благодарим за обращение в наш сервисный центр. Информируем вас об изменении статуса вашей заявки\n"
            "Статус заявки: ваше изделие поступило в бутик и готово к выдаче. "
            "Стоимость ремонта вы можете уточнить в данном диалоге."
        ),
    },
)


async def init_db():
    global db
    try:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not configured")
        pool = await asyncpg.create_pool(DATABASE_URL)
        db = _AsyncDatabase(pool)
        
        await create_tables()
        logger.info("Database initialized: %s", DATABASE_URL)
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        raise


async def create_tables():
    if db is None:
        raise RuntimeError("Database not initialized")
    
    # Users
    await db.execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            conversation TEXT,
            bitrix_id INTEGER,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    
    # Admins
    await db.execute(f"""
        CREATE TABLE IF NOT EXISTS admin (
            user_id TEXT PRIMARY KEY,
            username TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # Web admin users
    await db.execute("""
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            display_name TEXT,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'operator',
            is_active INTEGER NOT NULL DEFAULT 1,
            whatsapp_id TEXT,
            receives_handoffs INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await ensure_column("admin_users", "display_name", "display_name TEXT")
    await ensure_column("admin_users", "whatsapp_id", "whatsapp_id TEXT")
    await ensure_column(
        "admin_users",
        "receives_handoffs",
        "receives_handoffs INTEGER NOT NULL DEFAULT 0",
    )

    # Editable customer notifications for Bitrix stage changes.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS notification_templates (
            stage_id TEXT PRIMARY KEY,
            stage_name TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await db.executemany("""
        INSERT INTO notification_templates (stage_id, stage_name, text)
        VALUES (?, ?, ?)
        ON CONFLICT(stage_id) DO UPDATE SET
            stage_name = excluded.stage_name
    """, [
        (item["stage_id"], item["stage_name"], item["text"])
        for item in DEFAULT_NOTIFICATION_TEMPLATES
    ])

    # Media
    await db.execute("""
        CREATE TABLE IF NOT EXISTS media (
            user_id TEXT,
            file_id TEXT,
            file_path TEXT,
            filename TEXT,
            content_type TEXT,
            media_type TEXT,
            source_message_id TEXT,
            content_uri TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await ensure_column("media", "file_path", "file_path TEXT")
    await ensure_column("media", "filename", "filename TEXT")
    await ensure_column("media", "content_type", "content_type TEXT")
    await ensure_column("media", "media_type", "media_type TEXT")
    await ensure_column("media", "source_message_id", "source_message_id TEXT")
    await ensure_column("media", "content_uri", "content_uri TEXT")

    # Token usage
    await db.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            user_id TEXT,
            input INTEGER DEFAULT 0,
            output INTEGER DEFAULT 0
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id TEXT PRIMARY KEY,
            user_id TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            user_id TEXT PRIMARY KEY,
            is_paused INTEGER DEFAULT 0,
            reason TEXT,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL
        )
    """)
    await db.executemany("""
        INSERT INTO app_settings (setting_key, setting_value)
        VALUES (?, ?)
        ON CONFLICT(setting_key) DO NOTHING
    """, [
        (
            "MANAGER_WORKING_HOURS_ENABLED",
            "1" if MANAGER_WORKING_HOURS_ENABLED else "0",
        ),
        (
            "MANAGER_WORKING_HOURS_START",
            MANAGER_WORKING_HOURS_START,
        ),
        (
            "MANAGER_WORKING_HOURS_END",
            MANAGER_WORKING_HOURS_END,
        ),
        (
            "MANAGER_WORKING_DAYS",
            ",".join(str(day) for day in sorted(MANAGER_WORKING_DAYS)),
        ),
    ])

    await db.execute("""
        CREATE TABLE IF NOT EXISTS dialog_messages (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            user_id TEXT,
            role TEXT,
            message_type TEXT,
            text TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            user_id TEXT,
            rating INTEGER,
            comment TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS analytics_events (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            user_id TEXT,
            event_type TEXT,
            payload TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS operator_handoffs (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            user_id TEXT NOT NULL,
            reason TEXT,
            summary TEXT,
            initiated_by TEXT NOT NULL DEFAULT 'agent',
            status TEXT NOT NULL DEFAULT 'waiting',
            requested_at TIMESTAMP NOT NULL,
            first_manager_response_at TIMESTAMP,
            last_manager_message_at TIMESTAMP,
            response_time_seconds INTEGER,
            manager_message_id TEXT UNIQUE,
            manager_id TEXT,
            manager_name TEXT,
            closed_at TIMESTAMPTZ,
            closed_reason TEXT
        )
    """)
    await ensure_column(
        "operator_handoffs",
        "initiated_by",
        "initiated_by TEXT NOT NULL DEFAULT 'agent'",
    )
    await ensure_column(
        "operator_handoffs",
        "last_manager_message_at",
        "last_manager_message_at TIMESTAMP",
    )
    await ensure_column(
        "operator_handoffs",
        "closed_reason",
        "closed_reason TEXT",
    )
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_operator_handoffs_user_status
        ON operator_handoffs (user_id, status, requested_at DESC)
    """)
    await db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_operator_handoffs_one_open_per_user
        ON operator_handoffs (user_id)
        WHERE status IN ('waiting', 'active')
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS repair_requests (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            request_number INTEGER UNIQUE,
            user_id TEXT,
            deal_id INTEGER,
            bitrix_contact_id INTEGER,
            status TEXT DEFAULT 'Принят',
            furthest_bitrix_stage_id TEXT,
            furthest_bitrix_stage_rank INTEGER,
            service_type TEXT,
            name TEXT,
            phone TEXT,
            city TEXT,
            product_type TEXT,
            brand TEXT,
            model TEXT,
            article TEXT,
            problem TEXT,
            diagnostic_summary TEXT,
            estimated_price_range TEXT,
            warranty_context TEXT,
            convenient_time TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await ensure_column(
        "repair_requests",
        "furthest_bitrix_stage_id",
        "furthest_bitrix_stage_id TEXT",
    )
    await ensure_column(
        "repair_requests",
        "furthest_bitrix_stage_rank",
        "furthest_bitrix_stage_rank INTEGER",
    )

    await db.execute("""
        CREATE TABLE IF NOT EXISTS customer_segments (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            selections_json TEXT NOT NULL,
            stage_rows_json TEXT,
            phone_text TEXT,
            phone_count INTEGER NOT NULL DEFAULT 0,
            contact_count INTEGER NOT NULL DEFAULT 0,
            deal_count INTEGER NOT NULL DEFAULT 0,
            max_phone_count INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            created_by INTEGER,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await ensure_column("customer_segments", "name", "name TEXT NOT NULL DEFAULT 'Сегмент'")
    await ensure_column("customer_segments", "status", "status TEXT NOT NULL DEFAULT 'pending'")
    await ensure_column("customer_segments", "selections_json", "selections_json TEXT NOT NULL DEFAULT '[]'")
    await ensure_column("customer_segments", "stage_rows_json", "stage_rows_json TEXT")
    await ensure_column("customer_segments", "phone_text", "phone_text TEXT")
    await ensure_column("customer_segments", "phone_count", "phone_count INTEGER NOT NULL DEFAULT 0")
    await ensure_column("customer_segments", "contact_count", "contact_count INTEGER NOT NULL DEFAULT 0")
    await ensure_column("customer_segments", "deal_count", "deal_count INTEGER NOT NULL DEFAULT 0")
    await ensure_column(
        "customer_segments",
        "max_phone_count",
        "max_phone_count INTEGER NOT NULL DEFAULT 0",
    )
    await ensure_column("customer_segments", "error", "error TEXT")
    await ensure_column("customer_segments", "created_by", "created_by INTEGER")
    await ensure_column("customer_segments", "started_at", "started_at TIMESTAMP")
    await ensure_column("customer_segments", "completed_at", "completed_at TIMESTAMP")
    await ensure_column(
        "customer_segments",
        "updated_at",
        "updated_at TIMESTAMP DEFAULT NOW()",
    )
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_customer_segments_created_at
        ON customer_segments (created_at DESC)
    """)
    
    logger.info("Database tables created successfully")

    await db.commit()


async def ensure_column(table: str, column: str, definition: str):
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = ?
        """,
        (table,),
    ) as cursor:
        rows = await cursor.fetchall()

    columns = {row["column_name"] for row in rows}
    if column not in columns:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


async def close_db():
    global db
    if db:
        await db.close()
        db = None
        logger.info("Database connection closed")


async def create_or_update_user(user_id: str, username: Optional[str] = None,
                                first_name: Optional[str] = None,
                                last_name: Optional[str] = None,
                                conversation: Optional[str] = None):
    if db is None:
        raise RuntimeError("Database not initialized")

    await db.execute("""
        INSERT INTO users (user_id, username, first_name, last_name, conversation)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            conversation = excluded.conversation,
            updated_at = NOW()
    """, (user_id, username, first_name, last_name, conversation))
    await db.commit()

    await db.execute("""
        INSERT INTO tokens (user_id)
        SELECT ?
        WHERE NOT EXISTS (
            SELECT 1 FROM tokens WHERE user_id = ?
        )
    """, (user_id, user_id))
    await db.commit()


async def get_user_conversation(user_id: str) -> Optional[str]:
    if db is None:
        raise RuntimeError("Database not initialized")
    
    async with db.execute(
        "SELECT conversation FROM users WHERE user_id = ?",
        (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
        return row[0] if row and row[0] else None


async def set_user_conversation(user_id: str, conversation: str):
    if db is None:
        raise RuntimeError("Database not initialized")
    
    await db.execute("""
        UPDATE users SET conversation = ?, updated_at = NOW()
        WHERE user_id = ?
    """, (conversation, user_id))
    await db.commit()


async def create_admin(user_id: str):
    if db is None:
        raise RuntimeError("Database not initialized")
    
    await db.execute("""
        INSERT INTO admin (user_id, created_at)
        VALUES (?, NOW())
        ON CONFLICT(user_id) DO UPDATE SET
            created_at = NOW()
    """, (user_id,))
    await db.commit()


async def delete_admin(user_id: str):
    if db is None:
        raise RuntimeError("Database not initialized")

    await db.execute("""
        DELETE FROM admin WHERE user_id = ?
    """, (user_id,))
    await db.commit()


async def get_admin_ids() -> list[str]:
    if db is None:
        raise RuntimeError("Database not initialized")
    
    async with db.execute(
        "SELECT user_id FROM admin"
    ) as cursor:
        rows = await cursor.fetchall()
    return [row["user_id"] for row in rows]


async def get_app_setting(key: str, default: str | None = None) -> str | None:
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute("""
        SELECT setting_value
        FROM app_settings
        WHERE setting_key = ?
    """, (key,)) as cursor:
        row = await cursor.fetchone()
    if row:
        return row["setting_value"]
    return default


async def set_app_setting(key: str, value: str) -> None:
    if db is None:
        raise RuntimeError("Database not initialized")

    await db.execute("""
        INSERT INTO app_settings (setting_key, setting_value)
        VALUES (?, ?)
        ON CONFLICT(setting_key) DO UPDATE SET
            setting_value = excluded.setting_value
    """, (key, value))
    await db.commit()


async def get_manager_working_hours_settings() -> dict[str, str | bool | list[int]]:
    if db is None:
        raise RuntimeError("Database not initialized")

    enabled = await get_app_setting(
        "MANAGER_WORKING_HOURS_ENABLED",
        "1" if MANAGER_WORKING_HOURS_ENABLED else "0",
    )
    start = await get_app_setting("MANAGER_WORKING_HOURS_START", MANAGER_WORKING_HOURS_START)
    end = await get_app_setting("MANAGER_WORKING_HOURS_END", MANAGER_WORKING_HOURS_END)
    days_raw = await get_app_setting(
        "MANAGER_WORKING_DAYS",
        ",".join(str(day) for day in sorted(MANAGER_WORKING_DAYS)),
    )

    try:
        working_days = [
            int(item)
            for item in (days_raw or "").split(",")
            if item.strip().isdigit() and 0 <= int(item.strip()) <= 6
        ]
    except Exception:
        working_days = sorted(MANAGER_WORKING_DAYS)

    return {
        "enabled": str(enabled).lower() in {"1", "true", "yes", "on"},
        "start": (start or MANAGER_WORKING_HOURS_START),
        "end": (end or MANAGER_WORKING_HOURS_END),
        "days": sorted(set(working_days)),
    }


async def set_manager_working_hours_settings(
    *,
    enabled: bool,
    start: str,
    end: str,
    working_days: list[int],
) -> None:
    if db is None:
        raise RuntimeError("Database not initialized")

    await set_app_setting("MANAGER_WORKING_HOURS_ENABLED", "1" if enabled else "0")
    await set_app_setting("MANAGER_WORKING_HOURS_START", start)
    await set_app_setting("MANAGER_WORKING_HOURS_END", end)
    await set_app_setting(
        "MANAGER_WORKING_DAYS",
        ",".join(str(day) for day in sorted(set(working_days))),
    )


async def upsert_admin_user(username: str, password_hash: str, role: str = "superadmin") -> None:
    if db is None:
        raise RuntimeError("Database not initialized")

    await db.execute("""
        INSERT INTO admin_users (username, display_name, password_hash, role, is_active)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(username) DO UPDATE SET
            password_hash = excluded.password_hash,
            role = excluded.role,
            display_name = COALESCE(admin_users.display_name, excluded.display_name),
            is_active = 1,
            updated_at = NOW()
    """, (username, username, password_hash, role))
    await db.commit()


async def create_admin_user(
    username: str,
    password_hash: str,
    display_name: str,
    role: str = "operator",
    whatsapp_id: str = "",
) -> int:
    if db is None:
        raise RuntimeError("Database not initialized")

    try:
        cursor = await db.execute("""
            INSERT INTO admin_users (
                username, display_name, password_hash, role, is_active, whatsapp_id
            )
            VALUES (?, ?, ?, ?, 1, ?)
            RETURNING id
        """, (username, display_name.strip(), password_hash, role, whatsapp_id.strip()))
        await db.commit()
    except asyncpg.UniqueViolationError as exc:
        raise ValueError("Пользователь с таким логином уже существует") from exc
    return int(cursor.lastrowid)


async def update_admin_user(
    admin_id: int,
    username: str,
    display_name: str,
    role: str,
    whatsapp_id: str | None,
    is_active: bool,
    password_hash: str | None = None,
) -> bool:
    if db is None:
        raise RuntimeError("Database not initialized")

    password_update = ", password_hash = ?" if password_hash else ""
    params: list[object] = [
        username,
        display_name.strip(),
        role,
        whatsapp_id or None,
        int(is_active),
    ]
    if password_hash:
        params.append(password_hash)
    params.append(admin_id)
    try:
        cursor = await db.execute(f"""
            UPDATE admin_users
            SET username = ?, display_name = ?, role = ?, whatsapp_id = ?, is_active = ?,
                updated_at = NOW(){password_update}
            WHERE id = ?
        """, params)
        if not is_active or not whatsapp_id:
            await db.execute(
                "UPDATE admin_users SET receives_handoffs = 0 WHERE id = ?",
                (admin_id,),
            )
        await db.commit()
    except asyncpg.UniqueViolationError as exc:
        raise ValueError("Пользователь с таким логином уже существует") from exc
    return cursor.rowcount > 0


async def list_admin_users() -> list[dict]:
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute("""
        SELECT id, username, display_name, role, is_active, whatsapp_id, receives_handoffs,
               created_at, updated_at
        FROM admin_users
        ORDER BY LOWER(username), id
    """) as cursor:
        rows = await cursor.fetchall()

    users = [dict(row) for row in rows]
    for user in users:
        user["display_name"] = (user.get("display_name") or user["username"]).strip()
        user["is_active"] = bool(user["is_active"])
        user["receives_handoffs"] = bool(user["receives_handoffs"])
    return users


async def set_handoff_recipients(admin_ids: Sequence[int]) -> None:
    if db is None:
        raise RuntimeError("Database not initialized")

    selected_ids = sorted(set(admin_ids))
    await db.execute(
        "UPDATE admin_users SET receives_handoffs = 0, updated_at = NOW()"
    )
    if selected_ids:
        placeholders = ", ".join("?" for _ in selected_ids)
        await db.execute(f"""
            UPDATE admin_users
            SET receives_handoffs = 1, updated_at = NOW()
            WHERE id IN ({placeholders})
              AND is_active = 1
              AND whatsapp_id IS NOT NULL
              AND TRIM(whatsapp_id) != ''
        """, selected_ids)
    await db.commit()


async def get_handoff_recipient_ids() -> list[str]:
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute("""
        SELECT whatsapp_id
        FROM admin_users
        WHERE receives_handoffs = 1
          AND is_active = 1
          AND whatsapp_id IS NOT NULL
          AND TRIM(whatsapp_id) != ''
        ORDER BY LOWER(username), id
    """) as cursor:
        rows = await cursor.fetchall()
    return [str(row["whatsapp_id"]).strip() for row in rows]


async def get_admin_user_by_username(username: str) -> dict | None:
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute("""
        SELECT id, username, display_name, password_hash, role, is_active, whatsapp_id,
               receives_handoffs, created_at, updated_at
        FROM admin_users
        WHERE username = ?
    """, (username,)) as cursor:
        row = await cursor.fetchone()

    if not row:
        return None
    user = dict(row)
    user["display_name"] = (user.get("display_name") or user["username"]).strip()
    return user


async def get_admin_user_by_id(admin_id: int) -> dict | None:
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute("""
        SELECT id, username, display_name, password_hash, role, is_active, whatsapp_id,
               receives_handoffs, created_at, updated_at
        FROM admin_users
        WHERE id = ?
    """, (admin_id,)) as cursor:
        row = await cursor.fetchone()

    if not row:
        return None
    user = dict(row)
    user["display_name"] = (user.get("display_name") or user["username"]).strip()
    return user


async def count_active_superadmins() -> int:
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute("""
        SELECT COUNT(*) AS count
        FROM admin_users
        WHERE role = 'superadmin' AND is_active = 1
    """) as cursor:
        row = await cursor.fetchone()
    return int(row["count"])


async def delete_admin_user_by_id(admin_id: int) -> bool:
    if db is None:
        raise RuntimeError("Database not initialized")

    cursor = await db.execute(
        "DELETE FROM admin_users WHERE id = ?",
        (admin_id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def save_file_id(user_id: str, file_id: str):
    await save_media_file(user_id=user_id, file_id=file_id)


async def save_media_file(
    user_id: str,
    file_id: Optional[str] = None,
    file_path: Optional[str] = None,
    filename: Optional[str] = None,
    content_type: Optional[str] = None,
    media_type: Optional[str] = None,
    source_message_id: Optional[str] = None,
    content_uri: Optional[str] = None,
):
    if db is None:
        raise RuntimeError("Database not initialized")
    
    await db.execute("""
        INSERT INTO media (
            user_id, file_id, file_path, filename, content_type,
            media_type, source_message_id, content_uri
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id, file_id, file_path, filename, content_type,
        media_type, source_message_id, content_uri
    ))
    await db.commit()


async def get_file_ids(user_id: str) -> list[str]:
    if db is None:
        raise RuntimeError("Database not initialized")
    
    async with db.execute("""
        SELECT file_id FROM media
        WHERE user_id = ?
    """, (user_id,)) as cursor:
        rows = await cursor.fetchall()

    file_ids = [row[0] for row in rows]

    await db.execute("""
        DELETE FROM media
        WHERE user_id = ?
    """, (user_id,))
    await db.commit()

    return file_ids


async def get_media_files(user_id: str, media_types: Sequence[str] | None = None) -> list[dict]:
    if db is None:
        raise RuntimeError("Database not initialized")

    params: list[str] = [user_id]
    type_filter = ""
    if media_types:
        placeholders = ", ".join("?" for _ in media_types)
        type_filter = f" AND media_type IN ({placeholders})"
        params.extend(media_types)

    async with db.execute(f"""
        SELECT file_id, file_path, filename, content_type, media_type, source_message_id, content_uri
        FROM media
        WHERE user_id = ?{type_filter}
    """, params) as cursor:
        rows = await cursor.fetchall()

    media_files = [dict(row) for row in rows]

    await db.execute(f"""
        DELETE FROM media
        WHERE user_id = ?{type_filter}
    """, params)
    await db.commit()

    return media_files


async def count_media_files(user_id: str, media_types: Sequence[str] | None = None) -> int:
    if db is None:
        raise RuntimeError("Database not initialized")

    params: list[str] = [user_id]
    type_filter = ""
    if media_types:
        placeholders = ", ".join("?" for _ in media_types)
        type_filter = f" AND media_type IN ({placeholders})"
        params.extend(media_types)

    async with db.execute(f"""
        SELECT COUNT(*) AS count
        FROM media
        WHERE user_id = ?{type_filter}
    """, params) as cursor:
        row = await cursor.fetchone()

    return row["count"] if row else 0


async def get_users():
    if db is None:
        raise RuntimeError("Database not initialized")
    
    async with db.execute("""
        SELECT username, user_id
        FROM users
    """) as cursor:
        rows = await cursor.fetchall()
        users = [ (row["username"], row["user_id"]) for row in rows]
        return users


async def get_token_usage(user_id: str) -> dict | None:
    if db is None:
        raise RuntimeError("Database not initialized")
    
    async with db.execute("""
        SELECT input, output
        FROM tokens
        WHERE user_id = ?
    """, (user_id,)) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def add_token_usage(user_id: str, input: int, output: int) -> None:
    if db is None:
        raise RuntimeError("Database not initialized")
    
    await db.execute("""
        UPDATE tokens
        SET input = input + ?, output = output + ?
        WHERE user_id = ?
    """, (input, output, user_id))
    await db.commit()


async def execute_query(query: str) -> str:
    if db is None:
        raise RuntimeError("Database not initialized")

    try:
        cursor = await db.execute(query)

        if "SELECT" in query.upper().split():
            rows = await cursor.fetchall()

            if not rows:
                return "No rows returned."

            columns = rows[0].keys()
            table = PrettyTable(columns)
            
            for row in rows:
                table.add_row([row[col] for col in columns])
            
            return str(table)

        else:
            await db.commit()
            return f"Rows affected: {cursor.rowcount}"

    except Exception as e:
        return f"SQL Error: {e}"


async def create_token_usage(user_id: str):
    if db is None:
        raise RuntimeError("Database not initialized")
    
    await db.execute("""
        INSERT INTO tokens (user_id)
        VALUES (?)
    """, (user_id,))
    await db.commit()


async def set_bitrix_id(user_id: str, bitrix_id: int):
    if db is None:
        raise RuntimeError("Database not initialized")
    
    await db.execute("""
        UPDATE users SET bitrix_id = ?
        WHERE user_id = ?
    """, (bitrix_id, user_id))
    await db.commit()


async def get_bitrix_id(user_id: str) -> Optional[int]:
    if db is None:
        raise RuntimeError("Database not initialized")
    
    async with db.execute("""
        SELECT bitrix_id FROM users
        WHERE user_id = ?
    """, (user_id,)) as cursor:
        row = await cursor.fetchone()
        return row[0] if row and row[0] else None


async def mark_message_processed(message_id: str, user_id: str) -> bool:
    if db is None:
        raise RuntimeError("Database not initialized")

    cursor = await db.execute("""
        INSERT INTO processed_messages (message_id, user_id)
        VALUES (?, ?)
        ON CONFLICT(message_id) DO NOTHING
    """, (message_id, user_id))
    await db.commit()
    return cursor.rowcount == 1


async def is_bot_paused(user_id: str) -> bool:
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute("""
        SELECT is_paused FROM bot_state
        WHERE user_id = ?
    """, (user_id,)) as cursor:
        row = await cursor.fetchone()
        return bool(row["is_paused"]) if row else False


async def set_bot_paused(user_id: str, is_paused: bool, reason: str | None = None) -> None:
    if db is None:
        raise RuntimeError("Database not initialized")

    await db.execute("""
        INSERT INTO bot_state (user_id, is_paused, reason, updated_at)
        VALUES (?, ?, ?, NOW())
        ON CONFLICT(user_id) DO UPDATE SET
            is_paused = excluded.is_paused,
            reason = excluded.reason,
            updated_at = NOW()
    """, (user_id, int(is_paused), reason))
    await db.commit()


async def append_dialog_message(
    user_id: str,
    role: str,
    message_type: str,
    text: str | None = None,
) -> None:
    if db is None:
        raise RuntimeError("Database not initialized")

    await db.execute("""
        INSERT INTO dialog_messages (user_id, role, message_type, text)
        VALUES (?, ?, ?, ?)
    """, (user_id, role, message_type, text))
    await db.commit()


async def get_recent_dialog(user_id: str, limit: int = 20) -> list[dict]:
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute("""
        SELECT role, message_type, text, created_at
        FROM dialog_messages
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (user_id, limit)) as cursor:
        rows = await cursor.fetchall()

    return list(reversed([dict(row) for row in rows]))


async def save_feedback(user_id: str, rating: int, comment: str | None = None) -> None:
    if db is None:
        raise RuntimeError("Database not initialized")

    await db.execute("""
        INSERT INTO feedback (user_id, rating, comment)
        VALUES (?, ?, ?)
    """, (user_id, rating, comment))
    await db.commit()


async def log_event(user_id: str | None, event_type: str, payload: str | None = None) -> None:
    if db is None:
        raise RuntimeError("Database not initialized")

    await db.execute("""
        INSERT INTO analytics_events (user_id, event_type, payload)
        VALUES (?, ?, ?)
    """, (user_id, event_type, payload))
    await db.commit()


async def cleanup_expired_manager_logs(retention_days: int | None = None) -> dict[str, int]:
    if db is None:
        raise RuntimeError("Database not initialized")

    retained_days = max(1, int(retention_days if retention_days is not None else MANAGER_LOG_RETENTION_DAYS))

    cutoff_dt = (datetime.now(timezone.utc) - timedelta(days=retained_days)).isoformat()

    dialog_cursor = await db.execute(
        "DELETE FROM dialog_messages WHERE created_at <= ?",
        (cutoff_dt,),
    )
    feedback_cursor = await db.execute(
        "DELETE FROM feedback WHERE created_at <= ?",
        (cutoff_dt,),
    )
    analytics_cursor = await db.execute(
        "DELETE FROM analytics_events WHERE created_at <= ?",
        (cutoff_dt,),
    )
    handoff_cursor = await db.execute(
        """
        DELETE FROM operator_handoffs
        WHERE status IN ('closed', 'cancelled')
          AND requested_at <= ?
        """,
        (cutoff_dt,),
    )
    await db.commit()

    return {
        "retained_days": retained_days,
        "dialog_messages_deleted": int(dialog_cursor.rowcount),
        "feedback_deleted": int(feedback_cursor.rowcount),
        "analytics_events_deleted": int(analytics_cursor.rowcount),
        "operator_handoffs_deleted": int(handoff_cursor.rowcount),
    }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_utc_timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)

    normalized = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.now(timezone.utc)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def create_operator_handoff(
    user_id: str,
    reason: str | None,
    summary: str | None,
) -> dict:
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute("""
        SELECT *
        FROM operator_handoffs
        WHERE user_id = ? AND status IN ('waiting', 'active')
        ORDER BY requested_at DESC, id DESC
        LIMIT 1
    """, (user_id,)) as cursor:
        existing = await cursor.fetchone()

    if existing:
        return dict(existing)

    requested_at = utc_now_iso()
    cursor = await db.execute("""
        INSERT INTO operator_handoffs (user_id, reason, summary, requested_at)
        VALUES (?, ?, ?, ?)
        RETURNING id
    """, (user_id, reason, summary, requested_at))
    await db.commit()

    return {
        "id": cursor.lastrowid,
        "user_id": user_id,
        "reason": reason,
        "summary": summary,
        "status": "waiting",
        "requested_at": requested_at,
    }


async def record_operator_message(
    user_id: str,
    manager_message_id: str | None,
    manager_id: str | None,
    manager_name: str | None,
    responded_at: str | None,
) -> dict | None:
    async with _handoff_operation_lock:
        return await _record_operator_message(
            user_id=user_id,
            manager_message_id=manager_message_id,
            manager_id=manager_id,
            manager_name=manager_name,
            responded_at=responded_at,
        )


async def _pause_bot_for_manager(user_id: str) -> None:
    await db.execute("""
        INSERT INTO bot_state (user_id, is_paused, reason, updated_at)
        VALUES (?, 1, 'Менеджер ведет диалог', NOW())
        ON CONFLICT(user_id) DO UPDATE SET
            is_paused = 1,
            reason = 'Менеджер ведет диалог',
            updated_at = NOW()
    """, (user_id,))


async def _record_operator_message(
    user_id: str,
    manager_message_id: str | None,
    manager_id: str | None,
    manager_name: str | None,
    responded_at: str | None,
) -> dict | None:
    if db is None:
        raise RuntimeError("Database not initialized")

    if manager_message_id:
        async with db.execute("""
            SELECT id FROM operator_handoffs WHERE manager_message_id = ?
        """, (manager_message_id,)) as cursor:
            if await cursor.fetchone():
                return None

    async with db.execute("""
        SELECT *
        FROM operator_handoffs
        WHERE user_id = ? AND status IN ('waiting', 'active')
        ORDER BY requested_at DESC, id DESC
        LIMIT 1
    """, (user_id,)) as cursor:
        handoff = await cursor.fetchone()

    responded_dt = parse_utc_timestamp(responded_at)
    normalized_responded_at = responded_dt.isoformat()

    if not handoff:
        cursor = await db.execute("""
            INSERT INTO operator_handoffs (
                user_id, reason, summary, initiated_by, status,
                requested_at, first_manager_response_at, last_manager_message_at,
                manager_message_id, manager_id, manager_name
            )
            VALUES (?, ?, ?, 'manager', 'active', ?, ?, ?, ?, ?, ?)
            RETURNING id
        """, (
            user_id,
            "Менеджер подключился к диалогу",
            "Менеджер прервал автоматический диалог",
            normalized_responded_at,
            normalized_responded_at,
            normalized_responded_at,
            manager_message_id,
            manager_id,
            manager_name,
        ))
        await _pause_bot_for_manager(user_id)
        await db.commit()
        return {
            "id": cursor.lastrowid,
            "user_id": user_id,
            "initiated_by": "manager",
            "status": "active",
            "requested_at": normalized_responded_at,
            "first_manager_response_at": normalized_responded_at,
            "last_manager_message_at": normalized_responded_at,
            "response_time_seconds": None,
            "manager_message_id": manager_message_id,
            "manager_id": manager_id,
            "manager_name": manager_name,
            "first_response_recorded": False,
        }

    handoff_data = dict(handoff)
    if handoff_data["status"] == "waiting":
        requested_dt = parse_utc_timestamp(handoff_data["requested_at"])
        if responded_dt < requested_dt:
            return None

        response_time_seconds = max(0, round((responded_dt - requested_dt).total_seconds()))
        cursor = await db.execute("""
            UPDATE operator_handoffs
            SET status = 'active',
                first_manager_response_at = ?,
                last_manager_message_at = ?,
                response_time_seconds = ?,
                manager_message_id = ?,
                manager_id = ?,
                manager_name = ?
            WHERE id = ? AND status = 'waiting'
        """, (
            normalized_responded_at,
            normalized_responded_at,
            response_time_seconds,
            manager_message_id,
            manager_id,
            manager_name,
            handoff_data["id"],
        ))
        if cursor.rowcount <= 0:
            await db.rollback()
            return None
        await _pause_bot_for_manager(user_id)
        await db.commit()

        handoff_data.update({
            "status": "active",
            "first_manager_response_at": normalized_responded_at,
            "last_manager_message_at": normalized_responded_at,
            "response_time_seconds": response_time_seconds,
            "manager_message_id": manager_message_id,
            "manager_id": manager_id,
            "manager_name": manager_name,
            "first_response_recorded": True,
        })
        return handoff_data

    previous_message_dt = parse_utc_timestamp(handoff_data.get("last_manager_message_at"))
    last_manager_message_at = max(previous_message_dt, responded_dt).isoformat()
    cursor = await db.execute("""
        UPDATE operator_handoffs
        SET last_manager_message_at = ?,
            manager_id = COALESCE(?, manager_id),
            manager_name = COALESCE(?, manager_name)
        WHERE id = ? AND status = 'active'
    """, (
        last_manager_message_at,
        manager_id,
        manager_name,
        handoff_data["id"],
    ))
    if cursor.rowcount <= 0:
        await db.rollback()
        return None
    await _pause_bot_for_manager(user_id)
    await db.commit()

    handoff_data.update({
        "last_manager_message_at": last_manager_message_at,
        "manager_id": manager_id or handoff_data.get("manager_id"),
        "manager_name": manager_name or handoff_data.get("manager_name"),
        "first_response_recorded": False,
    })
    return handoff_data


async def close_expired_operator_handoffs(
    timeout_minutes: int,
    now: datetime | None = None,
) -> list[dict]:
    async with _handoff_operation_lock:
        return await _close_expired_operator_handoffs(timeout_minutes, now)


async def _close_expired_operator_handoffs(
    timeout_minutes: int,
    now: datetime | None = None,
) -> list[dict]:
    if db is None:
        raise RuntimeError("Database not initialized")

    current_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = (current_dt - timedelta(minutes=timeout_minutes)).isoformat()
    closed_at = current_dt.isoformat()
    async with db.execute("""
        SELECT id, user_id, last_manager_message_at
        FROM operator_handoffs
        WHERE status = 'active'
          AND last_manager_message_at IS NOT NULL
          AND last_manager_message_at <= ?
        ORDER BY id
    """, (cutoff,)) as cursor:
        candidates = await cursor.fetchall()

    closed: list[dict] = []
    for candidate in candidates:
        cursor = await db.execute("""
            UPDATE operator_handoffs
            SET status = 'closed',
                closed_at = ?,
                closed_reason = 'manager_inactivity_timeout'
            WHERE id = ?
              AND status = 'active'
              AND last_manager_message_at <= ?
        """, (closed_at, candidate["id"], cutoff))
        if cursor.rowcount <= 0:
            continue

        await db.execute("""
            INSERT INTO bot_state (user_id, is_paused, reason, updated_at)
            VALUES (?, 0, NULL, NOW())
            ON CONFLICT(user_id) DO UPDATE SET
                is_paused = 0,
                reason = NULL,
                updated_at = NOW()
        """, (candidate["user_id"],))
        closed.append({
            "id": candidate["id"],
            "user_id": candidate["user_id"],
            "last_manager_message_at": candidate["last_manager_message_at"],
            "closed_at": closed_at,
        })

    await db.commit()
    return closed


async def cancel_open_operator_handoff(user_id: str) -> int:
    if db is None:
        raise RuntimeError("Database not initialized")

    cursor = await db.execute("""
        UPDATE operator_handoffs
        SET status = 'cancelled',
            closed_at = ?,
            closed_reason = 'manual_resume'
        WHERE user_id = ? AND status IN ('waiting', 'active')
    """, (utc_now_iso(), user_id))
    await db.commit()
    return cursor.rowcount


async def get_operator_handoff_stats() -> dict[str, int | float | None]:
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'waiting' THEN 1 ELSE 0 END) AS waiting,
            SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
            SUM(CASE WHEN response_time_seconds IS NOT NULL THEN 1 ELSE 0 END) AS answered,
            AVG(response_time_seconds) AS average_seconds,
            MAX(response_time_seconds) AS maximum_seconds
        FROM operator_handoffs
    """) as cursor:
        row = await cursor.fetchone()

    return {
        "total": int(row["total"] or 0),
        "waiting": int(row["waiting"] or 0),
        "active": int(row["active"] or 0),
        "answered": int(row["answered"] or 0),
        "average_seconds": round(float(row["average_seconds"]), 1)
        if row["average_seconds"] is not None else None,
        "maximum_seconds": int(row["maximum_seconds"])
        if row["maximum_seconds"] is not None else None,
    }


async def create_repair_request(
    user_id: str,
    data: dict,
    deal_id: int | None = None,
    bitrix_contact_id: int | None = None,
) -> int:
    if db is None:
        raise RuntimeError("Database not initialized")

    cursor = await db.execute("""
        INSERT INTO repair_requests (
            user_id, deal_id, bitrix_contact_id,
            furthest_bitrix_stage_id, furthest_bitrix_stage_rank,
            service_type, name, phone, city,
            product_type, brand, model, article, problem, diagnostic_summary,
            estimated_price_range, warranty_context, convenient_time
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
    """, (
        user_id,
        deal_id,
        bitrix_contact_id,
        BITRIX_BOT_STAGE_ID,
        BITRIX_STAGE_RANKS.get(BITRIX_BOT_STAGE_ID.strip().upper()),
        data.get("service_type"),
        data.get("name"),
        data.get("phone"),
        data.get("city"),
        data.get("product_type"),
        data.get("brand"),
        data.get("model"),
        data.get("article"),
        data.get("problem"),
        data.get("diagnostic_summary"),
        data.get("estimated_price_range"),
        data.get("warranty_context"),
        data.get("convenient_time"),
    ))
    request_id = cursor.lastrowid
    request_number = 10499 + int(request_id)

    await db.execute("""
        UPDATE repair_requests
        SET request_number = ?, updated_at = NOW()
        WHERE id = ?
    """, (request_number, request_id))
    await db.commit()
    return request_number


REPAIR_REQUEST_STATUSES = (
    "Принят",
    "Диагностика",
    "В работе",
    "Передан в бутик",
    "Готов",
    "Выдан",
    "Передан на утилизацию",
)


async def list_repair_requests(
    status: str | None = None,
    q: str | None = None,
    limit: int = 100,
) -> list[dict]:
    if db is None:
        raise RuntimeError("Database not initialized")

    conditions: list[str] = []
    params: list[object] = []

    if status:
        conditions.append("status = ?")
        params.append(status)

    if q:
        like = f"%{q}%"
        conditions.append("""
            (
                CAST(request_number AS TEXT) LIKE ?
                OR user_id LIKE ?
                OR COALESCE(name, '') LIKE ?
                OR COALESCE(phone, '') LIKE ?
                OR COALESCE(city, '') LIKE ?
                OR COALESCE(service_type, '') LIKE ?
                OR COALESCE(product_type, '') LIKE ?
                OR COALESCE(brand, '') LIKE ?
                OR COALESCE(model, '') LIKE ?
                OR COALESCE(article, '') LIKE ?
                OR COALESCE(problem, '') LIKE ?
            )
        """)
        params.extend([like] * 11)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    normalized_limit = max(1, min(int(limit), 500))
    params.append(normalized_limit)

    async with db.execute(f"""
        SELECT
            id, request_number, user_id, deal_id, bitrix_contact_id, status,
            furthest_bitrix_stage_id, furthest_bitrix_stage_rank,
            service_type, name, phone, city, product_type, brand, model, article,
            problem, diagnostic_summary, estimated_price_range, warranty_context,
            convenient_time, created_at, updated_at
        FROM repair_requests
        {where_clause}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
    """, params) as cursor:
        rows = await cursor.fetchall()

    return [dict(row) for row in rows]


async def list_customers(q: str | None = None, limit: int = 100) -> list[dict]:
    if db is None:
        raise RuntimeError("Database not initialized")

    conditions: list[str] = []
    params: list[object] = []

    if q:
        like = f"%{q}%"
        conditions.append("""
            (
                u.user_id LIKE ?
                OR COALESCE(u.first_name, '') LIKE ?
                OR COALESCE(u.last_name, '') LIKE ?
                OR COALESCE(r.name, '') LIKE ?
                OR COALESCE(r.phone, '') LIKE ?
                OR COALESCE(r.city, '') LIKE ?
            )
        """)
        params.extend([like] * 6)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    normalized_limit = max(1, min(int(limit), 500))
    params.append(normalized_limit)

    async with db.execute(f"""
        SELECT
            u.user_id,
            u.username,
            u.first_name,
            u.last_name,
            u.bitrix_id,
            u.created_at,
            u.updated_at,
            COALESCE(latest.name, u.first_name, u.username, u.user_id) AS display_name,
            COALESCE(latest.phone, u.user_id) AS phone,
            latest.city,
            latest.request_number AS last_request_number,
            latest.status AS last_status,
            latest.deal_id AS last_deal_id,
            latest.created_at AS last_request_at,
            COUNT(r.id) AS requests_count
        FROM users u
        LEFT JOIN repair_requests r ON r.user_id = u.user_id
        LEFT JOIN repair_requests latest ON latest.id = (
            SELECT rr.id
            FROM repair_requests rr
            WHERE rr.user_id = u.user_id
            ORDER BY rr.created_at DESC, rr.id DESC
            LIMIT 1
        )
        {where_clause}
        GROUP BY u.user_id
        ORDER BY COALESCE(latest.created_at, u.updated_at, u.created_at) DESC
        LIMIT ?
    """, params) as cursor:
        rows = await cursor.fetchall()

    return [dict(row) for row in rows]


async def get_repair_request_stats() -> dict[str, int]:
    if db is None:
        raise RuntimeError("Database not initialized")

    stats = {status: 0 for status in REPAIR_REQUEST_STATUSES}

    async with db.execute("""
        SELECT status, COUNT(*) AS count
        FROM repair_requests
        GROUP BY status
    """) as cursor:
        rows = await cursor.fetchall()

    total = 0
    for row in rows:
        status = row["status"] or "Без статуса"
        count = int(row["count"] or 0)
        stats[status] = count
        total += count

    stats["Все"] = total
    return stats


async def get_repair_request_group_stats(group_by: str) -> list[dict[str, int | str]]:
    if db is None:
        raise RuntimeError("Database not initialized")
    if group_by not in {"status", "city"}:
        raise ValueError("Unsupported repair request grouping")

    expression = (
        "COALESCE(NULLIF(TRIM(status), ''), 'Без статуса')"
        if group_by == "status"
        else "COALESCE(NULLIF(TRIM(city), ''), 'Не указан')"
    )
    async with db.execute(f"""
        SELECT {expression} AS label, COUNT(*) AS count
        FROM repair_requests
        GROUP BY {expression}
        ORDER BY count DESC, LOWER(label)
    """) as cursor:
        rows = await cursor.fetchall()

    counts = {str(row["label"]): int(row["count"] or 0) for row in rows}
    if group_by == "city":
        return [
            {"label": label, "count": count}
            for label, count in counts.items()
        ]

    result = [
        {"label": status, "count": counts.pop(status, 0)}
        for status in REPAIR_REQUEST_STATUSES
    ]
    result.extend(
        {"label": label, "count": count}
        for label, count in counts.items()
    )
    return result


async def list_notification_templates() -> list[dict[str, str]]:
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute("""
        SELECT stage_id, stage_name, text
        FROM notification_templates
        ORDER BY stage_id
    """) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_notification_template_text(stage_id: str | None) -> str | None:
    if db is None:
        raise RuntimeError("Database not initialized")
    if not stage_id:
        return None

    async with db.execute("""
        SELECT text
        FROM notification_templates
        WHERE stage_id = ?
    """, (stage_id.strip().upper(),)) as cursor:
        row = await cursor.fetchone()
    return str(row["text"]) if row else None


async def update_notification_templates(template_texts: dict[str, str]) -> None:
    if db is None:
        raise RuntimeError("Database not initialized")

    known_templates = await list_notification_templates()
    known_stage_ids = {item["stage_id"] for item in known_templates}
    if set(template_texts) != known_stage_ids:
        raise ValueError("Template set does not match configured Bitrix stages")

    await db.executemany("""
        UPDATE notification_templates
        SET text = ?, updated_at = NOW()
        WHERE stage_id = ?
    """, [
        (text, stage_id)
        for stage_id, text in template_texts.items()
    ])
    await db.commit()


async def create_customer_segment_job(
    name: str,
    selections: list[dict],
    max_phone_count: int | None,
    created_by: int | None,
) -> int:
    if db is None:
        raise RuntimeError("Database not initialized")

    cursor = await db.execute("""
        INSERT INTO customer_segments (
            name, status, selections_json, max_phone_count, created_by
        )
        VALUES (?, 'pending', ?, ?, ?)
        RETURNING id
    """, (
        name,
        json.dumps(selections, ensure_ascii=False),
        int(max_phone_count or 0),
        created_by,
    ))
    await db.commit()
    return int(cursor.lastrowid)


async def mark_customer_segment_running(segment_id: int) -> None:
    if db is None:
        raise RuntimeError("Database not initialized")

    await db.execute("""
        UPDATE customer_segments
        SET status = 'running',
            started_at = NOW(),
            updated_at = NOW(),
            error = NULL
        WHERE id = ?
    """, (segment_id,))
    await db.commit()


async def complete_customer_segment_job(segment_id: int, result: dict) -> None:
    if db is None:
        raise RuntimeError("Database not initialized")

    await db.execute("""
        UPDATE customer_segments
        SET status = 'completed',
            stage_rows_json = ?,
            phone_text = ?,
            phone_count = ?,
            contact_count = ?,
            deal_count = ?,
            max_phone_count = COALESCE(max_phone_count, 0),
            error = NULL,
            completed_at = NOW(),
            updated_at = NOW()
        WHERE id = ?
    """, (
        json.dumps(result.get("stage_rows") or [], ensure_ascii=False),
        result.get("phone_text") or "",
        int(result.get("unique_phones") or 0),
        int(result.get("unique_contacts") or 0),
        int(result.get("total_deals") or 0),
        segment_id,
    ))
    await db.commit()


async def fail_customer_segment_job(segment_id: int, error: str) -> None:
    if db is None:
        raise RuntimeError("Database not initialized")

    await db.execute("""
        UPDATE customer_segments
        SET status = 'failed',
            error = ?,
            completed_at = NOW(),
            updated_at = NOW()
        WHERE id = ?
    """, (error[:2000], segment_id))
    await db.commit()


def _segment_row_to_dict(row) -> dict:
    item = dict(row)
    try:
        item["selections"] = json.loads(item.get("selections_json") or "[]")
    except json.JSONDecodeError:
        item["selections"] = []
    try:
        item["stage_rows"] = json.loads(item.get("stage_rows_json") or "[]")
    except json.JSONDecodeError:
        item["stage_rows"] = []
    item["can_download"] = item.get("status") == "completed" and bool(item.get("phone_text"))
    return item


async def list_customer_segments(limit: int = 20) -> list[dict]:
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute("""
        SELECT
            id, name, status, selections_json, stage_rows_json, phone_text,
            phone_count, contact_count, deal_count, error, created_by,
            max_phone_count,
            created_at, started_at, completed_at, updated_at
        FROM customer_segments
        ORDER BY datetime(created_at) DESC, id DESC
        LIMIT ?
    """, (limit,)) as cursor:
        rows = await cursor.fetchall()
    return [_segment_row_to_dict(row) for row in rows]


async def get_customer_segment(segment_id: int) -> dict | None:
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute("""
        SELECT
            id, name, status, selections_json, stage_rows_json, phone_text,
            phone_count, contact_count, deal_count, error, created_by,
            max_phone_count,
            created_at, started_at, completed_at, updated_at
        FROM customer_segments
        WHERE id = ?
    """, (segment_id,)) as cursor:
        row = await cursor.fetchone()
    return _segment_row_to_dict(row) if row else None


async def update_repair_request_status_by_deal_id(deal_id: int, status: str) -> int:
    if db is None:
        raise RuntimeError("Database not initialized")

    cursor = await db.execute("""
        UPDATE repair_requests
        SET status = ?, updated_at = NOW()
        WHERE deal_id = ?
    """, (status, deal_id))
    await db.commit()
    return cursor.rowcount


async def sync_repair_request_status_by_deal_id(
    deal_id: int,
    status: str,
    stage_id: str | None = None,
) -> dict:
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute("""
        SELECT
            id, request_number, user_id, deal_id, status, name, phone,
            service_type, furthest_bitrix_stage_id, furthest_bitrix_stage_rank,
            created_at, updated_at
        FROM repair_requests
        WHERE deal_id = ?
        ORDER BY id DESC
        LIMIT 1
    """, (deal_id,)) as cursor:
        row = await cursor.fetchone()

    if not row:
        return {
            "application": None,
            "changed": False,
            "updated": 0,
            "old_status": None,
            "new_status": status,
            "stage_advanced": False,
            "furthest_stage_id": None,
            "furthest_stage_rank": None,
        }

    application = dict(row)
    old_status = application.get("status")
    normalized_stage_id = stage_id.strip().upper() if stage_id else None
    stage_rank = BITRIX_STAGE_RANKS.get(normalized_stage_id) if normalized_stage_id else None
    furthest_stage_id = application.get("furthest_bitrix_stage_id")
    furthest_stage_rank = application.get("furthest_bitrix_stage_rank")
    tracking_initialized = furthest_stage_rank is not None
    stage_advanced = bool(
        tracking_initialized
        and stage_rank is not None
        and stage_rank > int(furthest_stage_rank)
    )
    initialize_tracking = not tracking_initialized and stage_rank is not None
    status_changed = old_status != status

    if stage_advanced or initialize_tracking:
        furthest_stage_id = normalized_stage_id
        furthest_stage_rank = stage_rank

    if not status_changed and not stage_advanced and not initialize_tracking:
        return {
            "application": application,
            "changed": False,
            "updated": 0,
            "old_status": old_status,
            "new_status": status,
            "stage_advanced": False,
            "furthest_stage_id": furthest_stage_id,
            "furthest_stage_rank": furthest_stage_rank,
        }

    cursor = await db.execute("""
        UPDATE repair_requests
        SET status = ?,
            furthest_bitrix_stage_id = ?,
            furthest_bitrix_stage_rank = ?,
            updated_at = NOW()
        WHERE id = ?
    """, (
        status,
        furthest_stage_id,
        furthest_stage_rank,
        application["id"],
    ))
    await db.commit()

    application["status"] = status
    application["furthest_bitrix_stage_id"] = furthest_stage_id
    application["furthest_bitrix_stage_rank"] = furthest_stage_rank
    return {
        "application": application,
        "changed": status_changed and cursor.rowcount > 0,
        "updated": cursor.rowcount,
        "old_status": old_status,
        "new_status": status,
        "stage_advanced": stage_advanced,
        "furthest_stage_id": furthest_stage_id,
        "furthest_stage_rank": furthest_stage_rank,
    }


async def get_analytics_summary() -> str:
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute("""
        SELECT event_type, COUNT(*) AS count
        FROM analytics_events
        GROUP BY event_type
        ORDER BY count DESC, event_type
    """) as cursor:
        events = await cursor.fetchall()

    async with db.execute("""
        SELECT COUNT(*) AS count FROM dialog_messages
    """) as cursor:
        messages = await cursor.fetchone()

    async with db.execute("""
        SELECT AVG(rating) AS avg_rating, COUNT(*) AS count FROM feedback
    """) as cursor:
        feedback_row = await cursor.fetchone()

    lines = [
        f"Messages logged: {messages['count'] if messages else 0}",
        f"Feedback count: {feedback_row['count'] if feedback_row else 0}",
        f"Average rating: {round(feedback_row['avg_rating'], 2) if feedback_row and feedback_row['avg_rating'] else 'n/a'}",
        "",
        "Events:",
    ]
    lines.extend([f"{row['event_type']}: {row['count']}" for row in events])
    return "\n".join(lines)
