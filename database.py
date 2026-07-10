import logging
from typing import Optional, Sequence

import aiosqlite

from config import DB_PATH
from prettytable import PrettyTable


logger = logging.getLogger(__name__)

db: Optional[aiosqlite.Connection] = None


async def init_db():
    global db
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        
        await create_tables()
        logger.info(f"Database initialized: {DB_PATH}")
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Admins
    await db.execute(f"""
        CREATE TABLE IF NOT EXISTS admin (
            user_id TEXT PRIMARY KEY,
            username TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Web admin users
    await db.execute("""
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'operator',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            user_id TEXT PRIMARY KEY,
            is_paused INTEGER DEFAULT 0,
            reason TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS dialog_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            role TEXT,
            message_type TEXT,
            text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            rating INTEGER,
            comment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS analytics_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            event_type TEXT,
            payload TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS repair_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_number INTEGER UNIQUE,
            user_id TEXT,
            deal_id INTEGER,
            bitrix_contact_id INTEGER,
            status TEXT DEFAULT 'Принят',
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    logger.info("Database tables created successfully")

    await db.commit()


async def ensure_column(table: str, column: str, definition: str):
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        rows = await cursor.fetchall()

    columns = {row["name"] for row in rows}
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
            updated_at = CURRENT_TIMESTAMP
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
        UPDATE users SET conversation = ?, updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
    """, (conversation, user_id))
    await db.commit()


async def create_admin(user_id: str):
    if db is None:
        raise RuntimeError("Database not initialized")
    
    await db.execute("""
        INSERT INTO admin (user_id, created_at)
        VALUES (?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            created_at = CURRENT_TIMESTAMP
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


async def upsert_admin_user(username: str, password_hash: str, role: str = "superadmin") -> None:
    if db is None:
        raise RuntimeError("Database not initialized")

    await db.execute("""
        INSERT INTO admin_users (username, password_hash, role, is_active)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(username) DO UPDATE SET
            password_hash = excluded.password_hash,
            role = excluded.role,
            is_active = 1,
            updated_at = CURRENT_TIMESTAMP
    """, (username, password_hash, role))
    await db.commit()


async def get_admin_user_by_username(username: str) -> dict | None:
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute("""
        SELECT id, username, password_hash, role, is_active, created_at, updated_at
        FROM admin_users
        WHERE username = ?
    """, (username,)) as cursor:
        row = await cursor.fetchone()

    return dict(row) if row else None


async def get_admin_user_by_id(admin_id: int) -> dict | None:
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute("""
        SELECT id, username, password_hash, role, is_active, created_at, updated_at
        FROM admin_users
        WHERE id = ?
    """, (admin_id,)) as cursor:
        row = await cursor.fetchone()

    return dict(row) if row else None


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
        INSERT OR IGNORE INTO processed_messages (message_id, user_id)
        VALUES (?, ?)
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
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            is_paused = excluded.is_paused,
            reason = excluded.reason,
            updated_at = CURRENT_TIMESTAMP
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
            user_id, deal_id, bitrix_contact_id, service_type, name, phone, city,
            product_type, brand, model, article, problem, diagnostic_summary,
            estimated_price_range, warranty_context, convenient_time
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        deal_id,
        bitrix_contact_id,
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
        SET request_number = ?, updated_at = CURRENT_TIMESTAMP
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


async def update_repair_request_status(request_id: int, status: str) -> None:
    if db is None:
        raise RuntimeError("Database not initialized")

    await db.execute("""
        UPDATE repair_requests
        SET status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (status, request_id))
    await db.commit()


async def update_repair_request_status_by_deal_id(deal_id: int, status: str) -> int:
    if db is None:
        raise RuntimeError("Database not initialized")

    cursor = await db.execute("""
        UPDATE repair_requests
        SET status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE deal_id = ?
    """, (status, deal_id))
    await db.commit()
    return cursor.rowcount


async def sync_repair_request_status_by_deal_id(deal_id: int, status: str) -> dict:
    if db is None:
        raise RuntimeError("Database not initialized")

    async with db.execute("""
        SELECT
            id, request_number, user_id, deal_id, status, name, phone,
            service_type, created_at, updated_at
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
        }

    application = dict(row)
    old_status = application.get("status")
    if old_status == status:
        return {
            "application": application,
            "changed": False,
            "updated": 0,
            "old_status": old_status,
            "new_status": status,
        }

    cursor = await db.execute("""
        UPDATE repair_requests
        SET status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (status, application["id"]))
    await db.commit()

    application["status"] = status
    return {
        "application": application,
        "changed": cursor.rowcount > 0,
        "updated": cursor.rowcount,
        "old_status": old_status,
        "new_status": status,
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
