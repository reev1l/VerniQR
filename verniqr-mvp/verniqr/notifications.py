import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from .bot import notification_text, report_keyboard
from .db import Database
from .validation import DomainError

logger = logging.getLogger(__name__)


async def deliver_one(database: Database, bot: Bot) -> bool:
    report = await database.claim_notification()
    if not report:
        return False
    try:
        # Recheck access to discard queue rows whose owner or item was deleted.
        report = await database.report(report["owner_id"], report["id"])
        await bot.send_message(
            report["owner_id"], notification_text(report), reply_markup=report_keyboard(report)
        )
    except TelegramRetryAfter as error:
        await database.finish_notification(report["id"], False)
        await asyncio.sleep(min(error.retry_after, 30))
    except TelegramAPIError as error:
        logger.warning("Notification failed: report=%s type=%s", report["id"], type(error).__name__)
        await database.finish_notification(report["id"], False)
    except DomainError:
        # The owner or item was deleted after the queue claim.
        pass
    else:
        await database.finish_notification(report["id"], True)
    return True


async def notification_loop(database: Database, bot: Bot):
    while True:
        try:
            processed = await deliver_one(database, bot)
        except Exception as error:
            logger.error("Notification worker failed: %s", type(error).__name__)
            processed = False
        await asyncio.sleep(0.1 if processed else 2)
