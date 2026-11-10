from datetime import UTC, datetime

import httpx
import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendDocument, SendMessage
from aiogram.types import Chat, Message
from pydantic import SecretStr

from verniqr.config import Settings
from verniqr.db import Database
from verniqr.web import create_web_app


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        public_url="https://verniqr.example",
        app_secret=SecretStr("a" * 64),
        admin_ids="900",
        database_path=tmp_path / "db.sqlite3",
        backup_dir=tmp_path / "backups",
    )


@pytest.fixture
async def db(settings):
    database = Database(settings.database_path)
    await database.initialize()
    await database.ensure_owner(100)
    await database.ensure_owner(200)
    await database.set_access(100, 30, 900)
    return database


@pytest.fixture
async def item(db):
    return await db.create_item(100, "Мой рюкзак")


@pytest.fixture
async def client(settings, db):
    app = create_web_app(settings, db)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.public_url
    ) as client:
        yield client


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail_send = None

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendDocument)) and self.fail_send:
            raise self.fail_send
        if isinstance(method, (SendMessage, EditMessageText, SendDocument)):
            return Message(
                message_id=len(self.calls),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=getattr(method, "text", None),
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        return True

    async def stream_content(
        self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True
    ):
        yield b""


@pytest.fixture
async def fake_bot():
    session = FakeSession()
    bot = Bot(
        "123456789:TEST_ONLY_NOT_A_REAL_TOKEN",
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    yield bot, session
    await bot.session.close()
