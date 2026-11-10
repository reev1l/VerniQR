import hashlib
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import aiosqlite

from .validation import DomainError, clean_text, validate_contact

SCHEMA = """
CREATE TABLE IF NOT EXISTS owners (
    telegram_id INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL,
    subscription_until INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES owners(telegram_id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 100),
    phone TEXT, tg TEXT, email TEXT,
    show_phone INTEGER NOT NULL DEFAULT 0 CHECK(show_phone IN (0,1)),
    show_tg INTEGER NOT NULL DEFAULT 0 CHECK(show_tg IN (0,1)),
    show_email INTEGER NOT NULL DEFAULT 0 CHECK(show_email IN (0,1)),
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS items_owner ON items(owner_id);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    message_text TEXT NOT NULL CHECK(length(message_text) BETWEEN 10 AND 1000),
    finder_contact TEXT,
    status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','closed')),
    closed_reason TEXT CHECK(closed_reason IN ('returned','irrelevant')),
    created_at INTEGER NOT NULL,
    closed_at INTEGER,
    request_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    notification_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(notification_status IN ('pending','sending','sent','failed')),
    notification_attempts INTEGER NOT NULL DEFAULT 0,
    notification_claim_until INTEGER NOT NULL DEFAULT 0,
    delivered_at INTEGER,
    UNIQUE(item_id, request_key)
);
CREATE INDEX IF NOT EXISTS reports_item ON reports(item_id, id DESC);
CREATE INDEX IF NOT EXISTS reports_delivery ON reports(notification_status, notification_claim_until);
CREATE TABLE IF NOT EXISTS rate_events (
    id INTEGER PRIMARY KEY,
    ip_hash TEXT NOT NULL,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_events_ip ON rate_events(ip_hash, created_at);
CREATE INDEX IF NOT EXISTS rate_events_item ON rate_events(item_id, created_at);
CREATE TABLE IF NOT EXISTS admin_audit (
    id INTEGER PRIMARY KEY,
    admin_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL REFERENCES owners(telegram_id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
PRAGMA user_version = 1;
"""


def now() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    @asynccontextmanager
    async def connect(self, write: bool = False):
        async with aiosqlite.connect(self.path, timeout=15) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA busy_timeout=15000")
            if write:
                await db.execute("PRAGMA secure_delete=ON")
                await db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                if write:
                    await db.commit()
            except BaseException:
                if write:
                    await db.rollback()
                raise

    async def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as db:
            version = (await (await db.execute("PRAGMA user_version")).fetchone())[0]
            if version > 1:
                raise RuntimeError("База создана более новой версией приложения")
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA secure_delete=ON")
            await db.executescript(SCHEMA)

    async def ensure_owner(self, owner_id: int):
        async with self.connect(write=True) as db:
            await db.execute(
                "INSERT OR IGNORE INTO owners(telegram_id,created_at) VALUES (?,?)",
                (owner_id, now()),
            )

    async def owner(self, owner_id: int):
        async with self.connect() as db:
            row = await (
                await db.execute("SELECT * FROM owners WHERE telegram_id=?", (owner_id,))
            ).fetchone()
            return dict(row) if row else None

    async def set_access(self, owner_id: int, days: int, admin_id: int):
        if not 0 <= days <= 366:
            raise DomainError("Срок доступа: от 1 до 366 дней; 0 означает отключение.")
        async with self.connect(write=True) as db:
            row = await (
                await db.execute("SELECT * FROM owners WHERE telegram_id=?", (owner_id,))
            ).fetchone()
            if not row:
                raise DomainError("Владелец должен сначала запустить бота.", "NOT_FOUND", 404)
            expires = max(now(), row["subscription_until"]) + days * 86400 if days else 0
            await db.execute(
                "UPDATE owners SET subscription_until=? WHERE telegram_id=?", (expires, owner_id)
            )
            await db.execute(
                "INSERT INTO admin_audit(admin_id,owner_id,action,created_at,expires_at) VALUES (?,?,?,?,?)",
                (admin_id, owner_id, "extend" if days else "revoke", now(), expires),
            )
            return expires

    async def items(self, owner_id: int):
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT * FROM items WHERE owner_id=? ORDER BY created_at,id", (owner_id,)
                )
            ).fetchall()
            return [dict(row) for row in rows]

    async def item(self, owner_id: int, item_id: str):
        async with self.connect() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM items WHERE id=? AND owner_id=?", (item_id, owner_id)
                )
            ).fetchone()
            if not row:
                raise DomainError("Карточка не найдена или недоступна.", "NOT_FOUND", 404)
            return dict(row)

    async def public_item(self, item_id: str):
        async with self.connect() as db:
            row = await (
                await db.execute(
                    "SELECT i.* FROM items i JOIN owners o ON o.telegram_id=i.owner_id WHERE i.id=? AND o.subscription_until>?",
                    (item_id, now()),
                )
            ).fetchone()
            if not row:
                return None
            # Public data is whitelisted; hidden values never leave this layer.
            return {
                "id": row["id"],
                "name": row["name"],
                **{
                    kind: row[kind] if row[f"show_{kind}"] else None
                    for kind in ("phone", "tg", "email")
                },
            }

    async def create_item(self, owner_id: int, name: str):
        name = clean_text(name, 1, 100, "Название")
        async with self.connect(write=True) as db:
            owner = await (
                await db.execute("SELECT telegram_id FROM owners WHERE telegram_id=?", (owner_id,))
            ).fetchone()
            if not owner:
                raise DomainError("Сначала запустите бота командой /start.")
            count = (
                await (
                    await db.execute("SELECT count(*) FROM items WHERE owner_id=?", (owner_id,))
                ).fetchone()
            )[0]
            if count >= 3:
                raise DomainError("В MVP можно создать не более трёх карточек.", "ITEM_LIMIT", 409)
            item_id = str(uuid4())
            await db.execute(
                "INSERT INTO items(id,owner_id,name,created_at) VALUES (?,?,?,?)",
                (item_id, owner_id, name, now()),
            )
        return await self.item(owner_id, item_id)

    async def rename_item(self, owner_id: int, item_id: str, name: str):
        name = clean_text(name, 1, 100, "Название")
        async with self.connect(write=True) as db:
            cur = await db.execute(
                "UPDATE items SET name=? WHERE id=? AND owner_id=?", (name, item_id, owner_id)
            )
            if not cur.rowcount:
                raise DomainError("Карточка недоступна.", "NOT_FOUND", 404)

    async def set_contact(self, owner_id: int, item_id: str, kind: str, value: str | None):
        if kind not in {"phone", "tg", "email"}:
            raise DomainError("Неизвестный тип контакта.")
        if value is not None:
            value = validate_contact(kind, value)
        async with self.connect(write=True) as db:
            cur = await db.execute(
                f"UPDATE items SET {kind}=?,show_{kind}=? WHERE id=? AND owner_id=?",
                (value, int(value is not None), item_id, owner_id),
            )
            if not cur.rowcount:
                raise DomainError("Карточка недоступна.", "NOT_FOUND", 404)

    async def toggle_contact(self, owner_id: int, item_id: str, kind: str):
        if kind not in {"phone", "tg", "email"}:
            raise DomainError("Неизвестный тип контакта.")
        async with self.connect(write=True) as db:
            row = await (
                await db.execute(
                    "SELECT * FROM items WHERE id=? AND owner_id=?", (item_id, owner_id)
                )
            ).fetchone()
            if not row:
                raise DomainError("Карточка недоступна.", "NOT_FOUND", 404)
            if not row[kind]:
                raise DomainError("Сначала добавьте контакт.")
            await db.execute(
                f"UPDATE items SET show_{kind}=1-show_{kind} WHERE id=? AND owner_id=?",
                (item_id, owner_id),
            )

    async def delete_item(self, owner_id: int, item_id: str):
        async with self.connect(write=True) as db:
            await db.execute("DELETE FROM items WHERE id=? AND owner_id=?", (item_id, owner_id))

    async def delete_owner(self, owner_id: int):
        async with self.connect(write=True) as db:
            await db.execute("PRAGMA secure_delete=ON")
            await db.execute("DELETE FROM owners WHERE telegram_id=?", (owner_id,))

    async def create_report(
        self,
        item_id: str,
        message: str,
        contact: str | None,
        key: str,
        ip_hash: str,
        ip_limit: int,
        item_limit: int,
        window: int,
    ):
        message = clean_text(message, 10, 1000, "Сообщение")
        contact = clean_text(contact, 0, 200, "Обратный контакт") or None if contact else None
        digest = hashlib.sha256((message + "\0" + (contact or "")).encode()).hexdigest()
        timestamp = now()
        async with self.connect(write=True) as db:
            # Check activation inside the same write transaction as report creation.
            row = await (
                await db.execute(
                    "SELECT i.id FROM items i JOIN owners o ON o.telegram_id=i.owner_id WHERE i.id=? AND o.subscription_until>?",
                    (item_id, timestamp),
                )
            ).fetchone()
            if not row:
                raise DomainError("Метка неактивна или не найдена.", "INACTIVE_ITEM", 404)
            existing = await (
                await db.execute(
                    "SELECT id,payload_hash FROM reports WHERE item_id=? AND request_key=?",
                    (item_id, key),
                )
            ).fetchone()
            if existing:
                if existing["payload_hash"] != digest:
                    raise DomainError(
                        "Этот запрос уже использован. Обновите страницу для нового сообщения.",
                        "IDEMPOTENCY_CONFLICT",
                        409,
                    )
                return existing["id"], False
            await db.execute("DELETE FROM rate_events WHERE created_at<=?", (timestamp - window,))
            ip_count = (
                await (
                    await db.execute("SELECT count(*) FROM rate_events WHERE ip_hash=?", (ip_hash,))
                ).fetchone()
            )[0]
            item_count = (
                await (
                    await db.execute("SELECT count(*) FROM rate_events WHERE item_id=?", (item_id,))
                ).fetchone()
            )[0]
            if ip_count >= ip_limit or item_count >= item_limit:
                raise DomainError("Слишком много сообщений. Попробуйте позже.", "RATE_LIMIT", 429)
            cursor = await db.execute(
                "INSERT INTO reports(item_id,message_text,finder_contact,created_at,request_key,payload_hash) VALUES (?,?,?,?,?,?)",
                (item_id, message, contact, timestamp, key, digest),
            )
            await db.execute(
                "INSERT INTO rate_events(ip_hash,item_id,created_at) VALUES (?,?,?)",
                (ip_hash, item_id, timestamp),
            )
            return cursor.lastrowid, True

    async def reports(
        self, owner_id: int, status: str = "new", offset: int = 0, item_id: str | None = None
    ):
        if status not in {"new", "closed"} or offset < 0:
            raise DomainError("Некорректный фильтр обращений.")
        async with self.connect() as db:
            query = (
                "FROM reports r JOIN items i ON i.id=r.item_id WHERE i.owner_id=? AND r.status=?"
            )
            params = [owner_id, status]
            if item_id:
                query += " AND i.id=?"
                params.append(item_id)
            total = (await (await db.execute("SELECT count(*) " + query, params)).fetchone())[0]
            rows = await (
                await db.execute(
                    "SELECT r.*,i.name AS item_name "
                    + query
                    + " ORDER BY r.id DESC LIMIT 5 OFFSET ?",
                    [*params, offset],
                )
            ).fetchall()
            return [dict(row) for row in rows], total

    async def report(self, owner_id: int, report_id: int):
        async with self.connect() as db:
            row = await (
                await db.execute(
                    "SELECT r.*,i.name AS item_name,i.owner_id FROM reports r JOIN items i ON i.id=r.item_id WHERE r.id=? AND i.owner_id=?",
                    (report_id, owner_id),
                )
            ).fetchone()
            if not row:
                raise DomainError("Обращение не найдено или недоступно.", "NOT_FOUND", 404)
            return dict(row)

    async def close_report(self, owner_id: int, report_id: int, reason: str):
        if reason not in {"returned", "irrelevant"}:
            raise DomainError("Неизвестная причина закрытия.")
        async with self.connect(write=True) as db:
            cur = await db.execute(
                "UPDATE reports SET status='closed',closed_reason=?,closed_at=? WHERE id=? AND status='new' AND item_id IN (SELECT id FROM items WHERE owner_id=?)",
                (reason, now(), report_id, owner_id),
            )
        if not cur.rowcount:
            await self.report(owner_id, report_id)
            raise DomainError("Это обращение уже закрыто.", "ALREADY_CLOSED", 409)

    async def retry_notification(self, owner_id: int, report_id: int):
        await self.report(owner_id, report_id)
        async with self.connect(write=True) as db:
            cur = await db.execute(
                "UPDATE reports SET notification_status='pending',notification_claim_until=0 WHERE id=? AND notification_status IN ('sent','failed') AND item_id IN (SELECT id FROM items WHERE owner_id=?)",
                (report_id, owner_id),
            )
            return bool(cur.rowcount)

    async def claim_notification(self):
        async with self.connect(write=True) as db:
            row = await (
                await db.execute(
                    "SELECT r.*,i.name AS item_name,i.owner_id FROM reports r JOIN items i ON i.id=r.item_id WHERE r.notification_status='pending' OR (r.notification_status='sending' AND r.notification_claim_until<?) ORDER BY r.id LIMIT 1",
                    (now(),),
                )
            ).fetchone()
            if not row:
                return None
            await db.execute(
                "UPDATE reports SET notification_status='sending',notification_attempts=notification_attempts+1,notification_claim_until=? WHERE id=?",
                (now() + 120, row["id"]),
            )
            return dict(row)

    async def finish_notification(self, report_id: int, success: bool):
        async with self.connect(write=True) as db:
            await db.execute(
                "UPDATE reports SET notification_status=?,notification_claim_until=0,delivered_at=? WHERE id=?",
                ("sent" if success else "failed", now() if success else None, report_id),
            )

    async def stats(self):
        async with self.connect() as db:
            owners = (await (await db.execute("SELECT count(*) FROM owners")).fetchone())[0]
            active = (
                await (
                    await db.execute(
                        "SELECT count(*) FROM owners WHERE subscription_until>?", (now(),)
                    )
                ).fetchone()
            )[0]
            items = (await (await db.execute("SELECT count(*) FROM items")).fetchone())[0]
            reports = (await (await db.execute("SELECT count(*) FROM reports")).fetchone())[0]
            returned = (
                await (
                    await db.execute("SELECT count(*) FROM reports WHERE closed_reason='returned'")
                ).fetchone()
            )[0]
            failed = (
                await (
                    await db.execute(
                        "SELECT count(*) FROM reports WHERE notification_status='failed'"
                    )
                ).fetchone()
            )[0]
            return dict(
                owners=owners,
                active=active,
                items=items,
                reports=reports,
                returned=returned,
                failed=failed,
            )

    async def owners(self, offset: int = 0):
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT o.telegram_id,o.subscription_until,count(i.id) AS item_count FROM owners o LEFT JOIN items i ON i.owner_id=o.telegram_id GROUP BY o.telegram_id ORDER BY o.created_at DESC,o.telegram_id LIMIT 10 OFFSET ?",
                    (max(offset, 0),),
                )
            ).fetchall()
            return [dict(row) for row in rows]
