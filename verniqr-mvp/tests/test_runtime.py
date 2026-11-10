import asyncio
import os
import sqlite3
import time
from pathlib import Path

import httpx
import pytest
from filelock import FileLock, Timeout
from pydantic import ValidationError

from verniqr.backups import make_backup, prune_backups, restore_backup
from verniqr.config import Settings
from verniqr.runtime import build_app
from verniqr.validation import DomainError, validate_contact


async def test_web_preview_lifecycle_holds_and_releases_process_lock(settings):
    app = build_app(settings, web_only=True)
    lock = FileLock(str(settings.database_path) + ".lock", timeout=0)
    async with app.router.lifespan_context(app):
        with pytest.raises(Timeout):
            lock.acquire()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=settings.public_url
        ) as client:
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.get("/demo")).status_code == 200
            assert "пример" in (await client.get("/demo")).text.lower()
    with lock:
        assert lock.is_locked


def test_missing_bot_credentials_fail_before_connecting(settings):
    with pytest.raises(ValueError, match="BOT_TOKEN"):
        build_app(settings)


def test_public_http_and_malformed_admin_ids_are_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, public_url="http://public.example")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, admin_ids="@username")


async def test_health_reports_dead_worker(client):
    app = client._transport.app
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    app.state.worker_tasks = [task]
    assert (await client.get("/healthz")).status_code == 503


@pytest.mark.parametrize(
    "kind,value",
    [
        ("phone", "12345"),
        ("email", "not-an-email"),
        ("tg", "@ab"),
        ("phone", "javascript:alert(1)"),
    ],
)
def test_invalid_owner_contacts(kind, value):
    with pytest.raises(DomainError):
        validate_contact(kind, value)


async def test_mailto_contact_encodes_query_characters(db, item, client):
    await db.set_contact(100, item["id"], "email", "user?subject=test@example.ru")
    page = await client.get(f"/item/{item['id']}")
    assert 'href="mailto:user%3Fsubject%3Dtest@example.ru"' in page.text


async def test_restore_existing_database_preserves_previous_version(db, settings):
    backup = make_backup(settings.database_path, settings.backup_dir)
    await db.delete_owner(100)
    with FileLock(str(settings.database_path) + ".lock", timeout=0):
        restore_backup(backup, settings.database_path, settings.backup_dir)
    assert await db.owner(100)
    assert len(list(settings.backup_dir.glob("before-restore-*.sqlite3"))) == 1


def test_retention_removes_only_managed_old_copies(tmp_path):
    old = tmp_path / "verniqr-old.sqlite3"
    unrelated = tmp_path / "unrelated.sqlite3"
    fresh = tmp_path / "verniqr-fresh.sqlite3"
    for path in (old, unrelated, fresh):
        path.write_bytes(b"placeholder")
    for path in (old, unrelated):
        os.utime(path, (time.time() - 8 * 86400,) * 2)
    prune_backups(tmp_path)
    assert not old.exists()
    assert unrelated.exists() and fresh.exists()


def test_invalid_backup_never_replaces_database(tmp_path):
    invalid = tmp_path / "invalid.sqlite3"
    invalid.write_bytes(b"not a database")
    target = tmp_path / "target.sqlite3"
    target.write_bytes(b"original")
    with pytest.raises(sqlite3.DatabaseError):
        restore_backup(invalid, target, Path(tmp_path / "backups"))
    assert target.read_bytes() == b"original"
