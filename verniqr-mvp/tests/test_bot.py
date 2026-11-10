from datetime import UTC, datetime
from html import escape

from aiogram.methods import EditMessageText, SendDocument, SendMessage
from aiogram.types import Update

from verniqr.bot import create_dispatcher


def update_message(user_id, text, update_id=1):
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                "text": text,
                "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
                if text.startswith("/")
                else [],
            },
        }
    )


def update_callback(user_id, data, update_id=1):
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": str(update_id),
                "chat_instance": "test",
                "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                "data": data,
                "message": {
                    "message_id": 1,
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": user_id, "type": "private"},
                    "text": "Menu",
                },
            },
        }
    )


def texts(session):
    return "\n".join(
        method.text
        for method in session.calls
        if isinstance(method, (SendMessage, EditMessageText))
    )


async def test_bot_full_create_contacts_labels_report_close_flow(db, settings, fake_bot):
    bot, session = fake_bot
    dispatcher = create_dispatcher(db, settings)
    try:
        await dispatcher.feed_update(bot, update_message(100, "/start"))
        await dispatcher.feed_update(bot, update_callback(100, "new", 2))
        await dispatcher.feed_update(bot, update_message(100, "Мой ноутбук", 3))
        item = (await db.items(100))[0]
        await dispatcher.feed_update(bot, update_callback(100, f"contact:phone:{item['id']}", 4))
        await dispatcher.feed_update(bot, update_message(100, "+79001234567", 5))
        assert (await db.item(100, item["id"]))["show_phone"] == 1
        await dispatcher.feed_update(bot, update_callback(100, f"visible:phone:{item['id']}", 6))
        assert (await db.item(100, item["id"]))["show_phone"] == 0
        await dispatcher.feed_update(bot, update_callback(100, f"qr:{item['id']}", 7))
        files = [method for method in session.calls if isinstance(method, SendDocument)]
        assert len(files) == 2
        assert files[0].document.filename.endswith(".png")
        assert files[1].document.filename.endswith(".pdf")
        report_id, _ = await db.create_report(
            item["id"], "Ноутбук оставлен у охраны", "@finder_name", "key", "ip", 10, 5, 600
        )
        await dispatcher.feed_update(bot, update_message(100, "/reports", 8))
        await dispatcher.feed_update(bot, update_callback(100, f"report:{report_id}", 9))
        assert "Ноутбук оставлен у охраны" in texts(session)
        await dispatcher.feed_update(bot, update_callback(100, f"close:returned:{report_id}", 10))
        assert (await db.report(100, report_id))["closed_reason"] == "returned"
    finally:
        await dispatcher.storage.close()


async def test_bot_rejects_forged_ownership_and_admin_commands(db, settings, item, fake_bot):
    bot, session = fake_bot
    dispatcher = create_dispatcher(db, settings)
    try:
        await dispatcher.feed_update(bot, update_message(200, "/activate 200 30"))
        assert (await db.owner(200))["subscription_until"] == 0
        assert "только администратору" in texts(session)
        await dispatcher.feed_update(bot, update_callback(200, f"rename:{item['id']}", 2))
        await dispatcher.feed_update(bot, update_callback(200, f"confirm_delitem:{item['id']}", 3))
        assert (await db.item(100, item["id"]))["name"] == "Мой рюкзак"
        await dispatcher.feed_update(bot, update_message(900, "/activate 200 30", 4))
        assert (await db.owner(200))["subscription_until"] > 0
        await dispatcher.feed_update(bot, update_message(900, "/revoke 200", 5))
        assert (await db.owner(200))["subscription_until"] == 0
    finally:
        await dispatcher.storage.close()


async def test_bot_does_not_issue_labels_for_inactive_subscription(db, settings, item, fake_bot):
    bot, session = fake_bot
    await db.set_access(100, 0, 900)
    dispatcher = create_dispatcher(db, settings)
    try:
        await dispatcher.feed_update(bot, update_callback(100, f"qr:{item['id']}"))
        assert not any(isinstance(method, SendDocument) for method in session.calls)
        assert "Доступ не активен" in texts(session)
    finally:
        await dispatcher.storage.close()


async def test_delete_profile_requires_explicit_confirmation_and_cancel_works(
    db, settings, item, fake_bot
):
    bot, session = fake_bot
    dispatcher = create_dispatcher(db, settings)
    try:
        await dispatcher.feed_update(bot, update_message(100, "/delete_me"))
        await dispatcher.feed_update(bot, update_message(100, "да", 2))
        assert await db.owner(100)
        await dispatcher.feed_update(bot, update_message(100, "/cancel", 3))
        await dispatcher.feed_update(bot, update_message(100, "УДАЛИТЬ", 4))
        assert await db.owner(100)
        await dispatcher.feed_update(bot, update_message(100, "/delete_me", 5))
        await dispatcher.feed_update(bot, update_message(100, "УДАЛИТЬ", 6))
        assert await db.owner(100) is None
    finally:
        await dispatcher.storage.close()


async def test_user_input_is_escaped_in_bot_messages(db, settings, fake_bot):
    bot, session = fake_bot
    dispatcher = create_dispatcher(db, settings)
    try:
        name = '<b>Ключи</b> & "тест"'
        await dispatcher.feed_update(bot, update_message(100, "/new"))
        await dispatcher.feed_update(bot, update_message(100, name, 2))
        assert escape(name) in texts(session)
    finally:
        await dispatcher.storage.close()
