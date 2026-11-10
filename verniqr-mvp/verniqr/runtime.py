import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from urllib import request

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramServerError, TelegramUnauthorizedError
from aiogram.types import LinkPreviewOptions
from filelock import FileLock

from .backups import backup_loop
from .bot import configure_commands, create_dispatcher
from .config import Settings
from .db import Database
from .notifications import notification_loop
from .web import create_web_app

logger = logging.getLogger(__name__)


def telegram_session(settings: Settings) -> AiohttpSession:
    if not settings.telegram_use_windows_proxy:
        return AiohttpSession()

    read_proxy = getattr(request, "getproxies_registry", None)
    bypass_proxy = getattr(request, "proxy_bypass_registry", None)
    proxy = read_proxy().get("https") if read_proxy else None
    if not proxy or (bypass_proxy and bypass_proxy("api.telegram.org")):
        raise ValueError(
            "Для Telegram включён прокси Windows, но в системе нет активного HTTPS-прокси "
            "для api.telegram.org. Включите его или уберите TELEGRAM_USE_WINDOWS_PROXY из .env."
        )
    try:
        return AiohttpSession(proxy=proxy)
    except (TypeError, ValueError):
        raise ValueError("Не удалось прочитать адрес HTTPS-прокси Windows.") from None


async def connect_telegram(bot: Bot, expected_username: str):
    for attempt in range(1, 4):
        try:
            # Bound startup requests without shortening the timeout for long polling.
            async with asyncio.timeout(10):
                identity = await bot.get_me()
                if (
                    expected_username
                    and (identity.username or "").lower() != expected_username.lstrip("@").lower()
                ):
                    raise ValueError(
                        "BOT_TOKEN относится к другому боту. Проверьте BOT_USERNAME и токен."
                    )
                await configure_commands(bot)
                await bot.delete_webhook(drop_pending_updates=False)
                return identity
        except TelegramUnauthorizedError:
            raise ValueError("Telegram отклонил BOT_TOKEN. Проверьте токен в .env.") from None
        except (TelegramNetworkError, TelegramServerError, TimeoutError):
            if attempt == 3:
                raise RuntimeError(
                    "Не удалось подключиться к Telegram Bot API (api.telegram.org:443). "
                    "Проверьте доступ к этому адресу с компьютера, попробуйте другую сеть "
                    "или запустите бота на сервере с доступом к Telegram. "
                    "Для просмотра сайта без Telegram: start-windows.cmd --web-only."
                ) from None
            logger.warning(
                "Telegram Bot API недоступен, повторяем подключение (%s из 3).", attempt + 1
            )
            await asyncio.sleep(2)


def build_app(settings: Settings, web_only: bool = False):
    settings.validate_runtime(web_only)
    database = Database(settings.database_path)

    @asynccontextmanager
    async def lifespan(app):
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(settings.database_path) + ".lock")
        lock.acquire(timeout=0)
        bot = None
        dispatcher = None
        tasks = []
        try:
            await database.initialize()
            if not web_only:
                bot = Bot(
                    settings.bot_token.get_secret_value(),
                    session=telegram_session(settings),
                    default=DefaultBotProperties(
                        parse_mode=ParseMode.HTML, link_preview=LinkPreviewOptions(is_disabled=True)
                    ),
                )
                identity = await connect_telegram(bot, settings.bot_username)
                app.state.bot_username = identity.username
                dispatcher = create_dispatcher(database, settings)
                tasks.extend(
                    [
                        asyncio.create_task(
                            dispatcher.start_polling(
                                bot,
                                handle_signals=False,
                                close_bot_session=False,
                                tasks_concurrency_limit=20,
                            ),
                            name="telegram",
                        ),
                        asyncio.create_task(notification_loop(database, bot), name="notifications"),
                    ]
                )
                logger.info("Bot connected: @%s", identity.username)
            else:
                logger.warning("Web preview only: no Telegram polling or notification delivery")
            tasks.append(asyncio.create_task(backup_loop(settings), name="backups"))
            app.state.worker_tasks = tasks
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError, Exception):
                    await task
            if dispatcher:
                await dispatcher.storage.close()
            if bot:
                await bot.session.close()
            lock.release()

    return create_web_app(settings, database, lifespan)
