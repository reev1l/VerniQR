import asyncio
import logging
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .config import Settings

logger = logging.getLogger(__name__)


def verify_backup(path: Path) -> None:
    if not path.is_file():
        raise ValueError("Файл резервной копии не найден")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Резервная копия повреждена")
        if source.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("Неподдерживаемая версия резервной копии")
        required = {"owners", "items", "reports", "rate_events", "admin_audit"}
        actual = {
            row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not required <= actual or source.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Неверная структура резервной копии")


def make_backup(database_path: Path, backup_dir: Path, *, prefix: str = "verniqr") -> Path:
    if not database_path.is_file():
        raise ValueError("База данных ещё не создана")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_dir / f"{prefix}-{stamp}.sqlite3"
    temporary = target.with_suffix(".tmp")
    try:
        # SQLite backup includes committed WAL pages and is safe while the service runs.
        with closing(
            sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as source:
            with closing(sqlite3.connect(temporary)) as destination:
                source.backup(destination)
        verify_backup(temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    prune_backups(backup_dir)
    return target


def prune_backups(backup_dir: Path) -> None:
    cutoff = time.time() - 7 * 86400
    for candidate in backup_dir.glob("*.sqlite3"):
        if (
            candidate.name.startswith(("verniqr-", "before-restore-"))
            and candidate.stat().st_mtime <= cutoff
        ):
            candidate.unlink()


def restore_backup(source_path: Path, database_path: Path, backup_dir: Path) -> None:
    verify_backup(source_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    if source_path.resolve() == database_path.resolve():
        raise ValueError("Источник и рабочая база не должны совпадать")
    temporary = database_path.parent / f"restore-{uuid4().hex}.sqlite3"
    try:
        with closing(
            sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as source:
            with closing(sqlite3.connect(temporary)) as destination:
                source.backup(destination)
        verify_backup(temporary)
        if database_path.exists():
            make_backup(database_path, backup_dir, prefix="before-restore")
        # Caller must hold the application's exclusive process lock.
        for suffix in ("-wal", "-shm"):
            Path(str(database_path) + suffix).unlink(missing_ok=True)
        temporary.replace(database_path)
    finally:
        temporary.unlink(missing_ok=True)


async def backup_loop(settings: Settings):
    while True:
        try:
            await asyncio.to_thread(prune_backups, settings.backup_dir)
            recent = [p.stat().st_mtime for p in settings.backup_dir.glob("verniqr-*.sqlite3")]
            if not recent or max(recent) < time.time() - 86400:
                await asyncio.to_thread(make_backup, settings.database_path, settings.backup_dir)
        except Exception as error:
            logger.error("Backup failed: %s", type(error).__name__)
        await asyncio.sleep(60)
