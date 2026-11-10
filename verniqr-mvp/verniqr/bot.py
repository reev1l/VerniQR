import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from html import escape

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from .config import Settings
from .db import Database, now
from .labels import pdf_label, png_label
from .validation import DomainError

logger = logging.getLogger(__name__)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
CONTACT_NAMES = {"phone": "Телефон", "tg": "Telegram", "email": "Email"}


class Input(StatesGroup):
    name = State()
    rename = State()
    contact = State()
    delete_profile = State()


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def main_keyboard():
    return keyboard(
        [
            [("Мои вещи", "items"), ("Добавить вещь", "new")],
            [("Обращения", "reports:new:0:all")],
            [("Подписка", "subscription"), ("Пример страницы", "demo")],
            [("Помощь", "help")],
        ]
    )


def date_text(timestamp: int) -> str:
    return (
        datetime.fromtimestamp(timestamp, UTC).strftime("%d.%m.%Y %H:%M UTC")
        if timestamp
        else "нет"
    )


def notification_text(report: dict) -> str:
    contact = escape(report["finder_contact"]) if report["finder_contact"] else "не оставлен"
    return (
        f"<b>Сообщение о находке · №{report['id']}</b>\n"
        f"Вещь: <b>{escape(report['item_name'])}</b>\n\n"
        f"{escape(report['message_text'])}\n\n"
        f"Обратный контакт: {contact}\n\n"
        "Договоритесь о возврате по указанному контакту или заберите вещь в указанном месте."
    )


def report_keyboard(report: dict):
    report_id = report["id"]
    rows = []
    if report["status"] == "new":
        rows.extend(
            [
                [("Вещь возвращена", f"close:returned:{report_id}")],
                [("Неактуально", f"close:irrelevant:{report_id}")],
            ]
        )
    rows.append([("Повторить уведомление", f"retry:{report_id}")])
    rows.append([("Все обращения", "reports:new:0:all"), ("Меню", "home")])
    return keyboard(rows)


async def render(event: Message | CallbackQuery, text: str, markup=None):
    if isinstance(event, CallbackQuery):
        if not isinstance(event.message, Message):
            return
        try:
            await event.message.edit_text(
                text, reply_markup=markup, link_preview_options=NO_PREVIEW
            )
        except TelegramBadRequest as error:
            if "message is not modified" not in error.message:
                await event.message.answer(
                    text, reply_markup=markup, link_preview_options=NO_PREVIEW
                )
    else:
        await event.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


class PrivateAndErrors(BaseMiddleware):
    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        if not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE:
            if isinstance(event, CallbackQuery):
                await event.answer("Бот работает в личном чате.", show_alert=True)
            return
        if isinstance(event, CallbackQuery):
            with suppress(TelegramAPIError):
                await event.answer()
        try:
            return await handler(event, data)
        except DomainError as error:
            await message.answer(
                escape(error.message), reply_markup=keyboard([[("В меню", "home")]])
            )
        except (aiosqlite.Error, TelegramAPIError, RuntimeError) as error:
            logger.error("Bot action failed: %s", type(error).__name__)
            with suppress(TelegramAPIError):
                await message.answer(
                    "Не удалось выполнить действие. Попробуйте снова. Ваши сохранённые данные остаются в сервисе."
                )


def create_dispatcher(database: Database, settings: Settings) -> Dispatcher:
    router = Router(name="verniqr")
    router.message.outer_middleware(PrivateAndErrors())
    router.callback_query.outer_middleware(PrivateAndErrors())
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())

    async def show_items(event):
        items = await database.items(event.from_user.id)
        rows = [[(item["name"], f"item:{item['id']}")] for item in items]
        if len(items) < 3:
            rows.append([("Добавить вещь", "new")])
        rows.append([("В меню", "home")])
        await render(
            event,
            f"<b>Мои вещи · {len(items)} из 3</b>\n\nВыберите вещь для настройки контактов и получения метки."
            if items
            else "<b>Пока нет вещей</b>\n\nДобавьте первую вещь. До активации доступа можно подготовить карточку и посмотреть пример страницы.",
            keyboard(rows),
        )

    async def show_item(event, item_id):
        item = await database.item(event.from_user.id, item_id)
        contacts = "\n".join(
            f"{label}: {escape(item[kind] or 'не указан')} · {'показан' if item[f'show_{kind}'] else 'скрыт'}"
            for kind, label in CONTACT_NAMES.items()
        )
        owner = await database.owner(event.from_user.id)
        active = owner and owner["subscription_until"] > now()
        await render(
            event,
            f"<b>{escape(item['name'])}</b>\n\n{contacts}\n\n"
            + (
                f"Страница: {settings.public_url}/item/{item_id}\n"
                if active
                else "Страница связи отключена до активации или продления доступа.\n"
            )
            + "\nQR остаётся прежним при изменении контактов и продлении доступа.",
            keyboard(
                [
                    [("Получить PNG и PDF", f"qr:{item_id}")],
                    [("Настроить контакты", f"contacts:{item_id}")],
                    [("Обращения по вещи", f"reports:new:0:{item_id}")],
                    [("Переименовать", f"rename:{item_id}"), ("Удалить", f"delitem:{item_id}")],
                    [("Мои вещи", "items"), ("В меню", "home")],
                ]
            ),
        )

    async def show_contacts(event, item_id):
        item = await database.item(event.from_user.id, item_id)
        rows = []
        for kind, label in CONTACT_NAMES.items():
            rows.append(
                [
                    (
                        f"{label}: изменить" if item[kind] else f"{label}: добавить",
                        f"contact:{kind}:{item_id}",
                    )
                ]
            )
            if item[kind]:
                rows.append(
                    [
                        (
                            "Скрыть" if item[f"show_{kind}"] else "Показать",
                            f"visible:{kind}:{item_id}",
                        ),
                        ("Удалить контакт", f"clear:{kind}:{item_id}"),
                    ]
                )
        rows.append([("К карточке", f"item:{item_id}")])
        contacts = "\n".join(
            f"{label}: {escape(item[kind] or 'не указан')} · {'показан' if item[f'show_{kind}'] else 'скрыт'}"
            for kind, label in CONTACT_NAMES.items()
        )
        await render(
            event,
            f"<b>Контакты · {escape(item['name'])}</b>\n\n{contacts}\n\nДобавленный контакт сразу становится публичным на активной странице. Можно скрыть все контакты и оставить только форму сообщения. Владение номером или почтой не подтверждается.",
            keyboard(rows),
        )

    async def show_subscription(event):
        owner = await database.owner(event.from_user.id)
        expires = owner["subscription_until"] if owner else 0
        active = expires > now()
        await render(
            event,
            f"<b>Доступ {'активен' if active else 'не активен'}</b>\n"
            + (f"До: {date_text(expires)}\n" if expires else "")
            + f"\n{settings.access_price_rub} ₽ за месяц, до трёх вещей. Включены PNG/PDF, изменение контактов, приём сообщений и учёт обращений.\n\n"
            "В пилоте оплату и срок подтверждает команда вручную. Автоматических списаний нет.\n\n"
            "После окончания доступа контакты и форма отключаются; прежние обращения остаются доступны. После продления те же QR работают снова.\n\n"
            f"Для оплаты и активации: {escape(settings.support_contact)}\nВаш ID: <code>{event.from_user.id}</code>",
            keyboard([[("Мои вещи", "items"), ("В меню", "home")]]),
        )

    async def show_help(event):
        await render(
            event,
            "<b>Как пользоваться ВерниQR</b>\n\n"
            "1. Добавьте вещь и выберите публичные контакты. Можно скрыть все контакты.\n"
            "2. Активируйте доступ через организатора пилота.\n"
            "3. Получите PNG/PDF, распечатайте метку и прикрепите её к вещи. Проверьте сканирование.\n"
            "4. Нашедший откроет страницу без регистрации и отправит сообщение. Оно появится в разделе «Обращения».\n"
            "5. После возврата закройте выбранное обращение. QR можно использовать снова.\n\n"
            "Без обратного контакта нашедшему нельзя ответить через сервис. Фото, чат и геолокация не поддерживаются.\n\n"
            "/items — ваши вещи\n/new — добавить вещь\n/reports — обращения\n/subscription — доступ и оплата\n/demo — пример страницы\n/id — ваш Telegram ID\n/cancel — отменить ввод\n/delete_me — удалить профиль и данные\n\n"
            "Контакты и обращения хранятся до удаления вещи или профиля. Удаление отключает страницы и удаляет данные из рабочей базы. В резервных копиях данные могут оставаться до семи дней.\n\n"
            f"Поддержка: {escape(settings.support_contact)}",
            main_keyboard(),
        )

    async def begin_new(event, state):
        if not await database.owner(event.from_user.id):
            raise DomainError("Сначала запустите бота командой /start.")
        if len(await database.items(event.from_user.id)) >= 3:
            raise DomainError(
                "Можно создать не более трёх карточек. Удалите ненужную, чтобы добавить новую."
            )
        await state.clear()
        await state.set_state(Input.name)
        await render(
            event,
            "<b>Добавить вещь</b>\n\nОтправьте название от 1 до 100 символов, например «Мой рюкзак».\n/cancel — отменить.",
        )

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        await database.ensure_owner(message.from_user.id)
        await message.answer(
            "<b>ВерниQR</b>\n\nСоздайте метку вещи, чтобы нашедший мог связаться с вами. Каждой вещи — свой QR. Сообщения о находках приходят сюда.\n\nДобавьте первую вещь или посмотрите пример страницы. Публичная связь работает при действующем доступе.",
            reply_markup=main_keyboard(),
        )

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Ввод отменён.", reply_markup=main_keyboard())

    @router.message(Command("id"))
    async def user_id(message: Message):
        await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")

    @router.message(Command("help", "terms"))
    async def help_command(message: Message, state: FSMContext):
        await state.clear()
        await show_help(message)

    @router.message(Command("items"))
    async def items_command(message: Message, state: FSMContext):
        await state.clear()
        await show_items(message)

    @router.message(Command("new"))
    async def new_command(message: Message, state: FSMContext):
        await begin_new(message, state)

    @router.message(Command("subscription"))
    async def subscription_command(message: Message, state: FSMContext):
        await state.clear()
        await show_subscription(message)

    @router.message(Command("demo"))
    async def demo_command(message: Message, state: FSMContext):
        await state.clear()
        await render(
            message,
            f"<b>Пример страницы</b>\n\n{settings.public_url}/demo\n\nПример можно проверить без оплаты. Он не создаёт настоящие обращения.",
            main_keyboard(),
        )

    async def show_reports(event, status="new", offset=0, item_id=None):
        if item_id:
            item = await database.item(event.from_user.id, item_id)
            title = f" · {escape(item['name'])}"
        else:
            title = ""
        rows, total = await database.reports(event.from_user.id, status, offset, item_id)
        scope = item_id or "all"
        # A deleted/closed last entry may leave an old page empty.
        if offset and not rows:
            offset = max(0, ((total - 1) // 5) * 5)
            rows, total = await database.reports(event.from_user.id, status, offset, item_id)
        buttons = [
            [(f"№{row['id']} · {row['item_name'][:45]}", f"report:{row['id']}")] for row in rows
        ]
        nav = []
        if offset:
            nav.append(("← Назад", f"reports:{status}:{max(0, offset - 5)}:{scope}"))
        if offset + 5 < total:
            nav.append(("Далее →", f"reports:{status}:{offset + 5}:{scope}"))
        if nav:
            buttons.append(nav)
        buttons.extend(
            [
                [("Новые", f"reports:new:0:{scope}"), ("Завершённые", f"reports:closed:0:{scope}")],
                [("В меню", "home")],
            ]
        )
        await render(
            event,
            f"<b>{'Новые' if status == 'new' else 'Завершённые'} обращения{title}</b>\nВсего: {total}\n\n"
            + (
                "Выберите обращение, чтобы прочитать сообщение и отметить результат."
                if rows
                else "В этом списке пока нет обращений."
            ),
            keyboard(buttons),
        )

    @router.message(Command("reports"))
    async def reports_command(message: Message, state: FSMContext):
        await state.clear()
        await show_reports(message)

    async def begin_delete(event, state):
        await state.clear()
        await state.set_state(Input.delete_profile)
        await render(
            event,
            "<b>Удаление профиля</b>\n\nВсе ваши страницы отключатся. Карточки, контакты и обращения будут удалены из рабочей базы. Оплаченный доступ также будет удалён. В резервных копиях данные могут оставаться до семи дней.\n\nДля подтверждения отправьте слово <b>УДАЛИТЬ</b>.\n/cancel — отменить.",
        )

    @router.message(Command("delete_me"))
    async def delete_command(message: Message, state: FSMContext):
        await begin_delete(message, state)

    async def require_admin(user_id: int):
        if user_id not in settings.admins:
            raise DomainError("Команда доступна только администратору.", "FORBIDDEN", 403)

    @router.message(Command("admin"))
    async def admin_command(message: Message):
        await require_admin(message.from_user.id)
        await message.answer(
            "<b>Управление пилотом</b>\n\n/owners — владельцы\n/stats — статистика\n/activate ID ДНИ — продлить доступ, например /activate 123456789 30\n/revoke ID — отключить доступ\n\nАктивация выполняется после ручной проверки оплаты. Продление добавляется к оставшемуся сроку."
        )

    @router.message(Command("activate", "revoke"))
    async def access_command(message: Message, bot: Bot):
        await require_admin(message.from_user.id)
        parts = (message.text or "").split()
        command = parts[0].split("@")[0]
        try:
            if command == "/activate" and len(parts) == 3:
                owner_id, days = int(parts[1]), int(parts[2])
                if not 1 <= days <= 366:
                    raise ValueError
            elif command == "/revoke" and len(parts) == 2:
                owner_id, days = int(parts[1]), 0
            else:
                raise ValueError
            if owner_id <= 0:
                raise ValueError
        except ValueError:
            raise DomainError("Формат: /activate ID ДНИ (1–366) или /revoke ID.") from None
        expires = await database.set_access(owner_id, days, message.from_user.id)
        await message.answer(
            f"Доступ владельца <code>{owner_id}</code>: "
            + (f"до {date_text(expires)}." if days else "отключён.")
        )
        try:
            await bot.send_message(
                owner_id,
                f"Доступ ВерниQR продлён до {date_text(expires)}. Прежние QR продолжают работать."
                if days
                else "Доступ ВерниQR отключён. Ранее полученные обращения доступны в боте. Для возобновления страниц продлите доступ.",
                reply_markup=main_keyboard(),
            )
        except TelegramAPIError:
            await message.answer(
                "Срок сохранён, но уведомить владельца не удалось. Он увидит статус в разделе «Подписка»."
            )

    @router.message(Command("stats"))
    async def stats_command(message: Message):
        await require_admin(message.from_user.id)
        stats = await database.stats()
        await message.answer(
            f"<b>Статистика пилота</b>\n\nВладельцев: {stats['owners']}\nАктивных доступов: {stats['active']}\nКарточек: {stats['items']}\nОбращений: {stats['reports']}\nОтметок возврата: {stats['returned']}\nОшибок уведомлений: {stats['failed']}\n\nОтметки возврата подтверждены владельцами вручную. Учебные проверки проводите в отдельной базе."
        )

    async def show_owners(event, offset=0):
        await require_admin(event.from_user.id)
        rows = await database.owners(offset)
        text = "<b>Владельцы</b>\n\n" + "\n".join(
            f"<code>{row['telegram_id']}</code> · вещей: {row['item_count']} · {'до ' + date_text(row['subscription_until']) if row['subscription_until'] > now() else 'нет доступа'}"
            for row in rows
        )
        buttons = []
        if offset:
            buttons.append(("← Назад", f"owners:{max(0, offset - 10)}"))
        if len(rows) == 10:
            buttons.append(("Далее →", f"owners:{offset + 10}"))
        await render(
            event,
            text if rows else text + "Пока нет владельцев.",
            keyboard([buttons, [("Меню", "home")]]) if buttons else main_keyboard(),
        )

    @router.message(Command("owners"))
    async def owners_command(message: Message):
        await show_owners(message)

    @router.message(Input.name)
    async def input_name(message: Message, state: FSMContext):
        if not message.text or message.text.startswith("/"):
            raise DomainError("Отправьте название текстом или /cancel.")
        item = await database.create_item(message.from_user.id, message.text)
        await state.clear()
        await show_item(message, item["id"])

    @router.message(Input.rename)
    async def input_rename(message: Message, state: FSMContext):
        if not message.text or message.text.startswith("/"):
            raise DomainError("Отправьте новое название текстом или /cancel.")
        data = await state.get_data()
        await database.rename_item(message.from_user.id, data["item_id"], message.text)
        await state.clear()
        await show_item(message, data["item_id"])

    @router.message(Input.contact)
    async def input_contact(message: Message, state: FSMContext):
        if not message.text or message.text.startswith("/"):
            raise DomainError("Отправьте контакт текстом или /cancel.")
        data = await state.get_data()
        await database.set_contact(
            message.from_user.id, data["item_id"], data["kind"], message.text
        )
        await state.clear()
        await show_contacts(message, data["item_id"])

    @router.message(Input.delete_profile)
    async def confirm_profile_delete(message: Message, state: FSMContext):
        if message.text != "УДАЛИТЬ":
            raise DomainError("Для удаления отправьте УДАЛИТЬ или /cancel для отмены.")
        await database.delete_owner(message.from_user.id)
        await state.clear()
        await message.answer(
            "Профиль и связанные данные удалены из рабочей базы. Страницы отключены. Для нового профиля используйте /start.\n\nСообщения и файлы, ранее отправленные в Telegram, могут оставаться в истории вашего чата. Их можно удалить средствами Telegram."
        )

    @router.callback_query()
    async def callbacks(call: CallbackQuery, state: FSMContext, bot: Bot):
        parts = (call.data or "").split(":")
        action = parts[0]
        # Menu actions cancel unfinished dialogs, preventing a later input from editing another card.
        await state.clear()
        if action == "home":
            await render(call, "<b>ВерниQR</b>\n\nВыберите действие.", main_keyboard())
        elif action == "items":
            await show_items(call)
        elif action == "new":
            await begin_new(call, state)
        elif action == "subscription":
            await show_subscription(call)
        elif action == "help":
            await show_help(call)
        elif action == "demo":
            await render(
                call,
                f"<b>Пример страницы</b>\n\n{settings.public_url}/demo\n\nТест примера не создаёт настоящее обращение.",
                main_keyboard(),
            )
        elif action == "item" and len(parts) == 2:
            await show_item(call, parts[1])
        elif action == "contacts" and len(parts) == 2:
            await show_contacts(call, parts[1])
        elif action in {"contact", "visible", "clear"} and len(parts) == 3:
            kind, item_id = parts[1:]
            await database.item(call.from_user.id, item_id)
            if kind not in CONTACT_NAMES:
                raise DomainError("Неизвестный тип контакта.")
            if action == "contact":
                await state.set_state(Input.contact)
                await state.update_data(item_id=item_id, kind=kind)
                example = {"phone": "+79001234567", "tg": "@username", "email": "name@example.ru"}[
                    kind
                ]
                await render(
                    call,
                    f"<b>{CONTACT_NAMES[kind]}</b>\n\nОтправьте контакт текстом, например {example}. Он будет показан на активной странице этой вещи.\n/cancel — отменить.",
                )
            else:
                if action == "visible":
                    await database.toggle_contact(call.from_user.id, item_id, kind)
                else:
                    await database.set_contact(call.from_user.id, item_id, kind, None)
                await show_contacts(call, item_id)
        elif action == "rename" and len(parts) == 2:
            await database.item(call.from_user.id, parts[1])
            await state.set_state(Input.rename)
            await state.update_data(item_id=parts[1])
            await render(
                call, "Отправьте новое название вещи, от 1 до 100 символов.\n/cancel — отменить."
            )
        elif action == "qr" and len(parts) == 2:
            item = await database.item(call.from_user.id, parts[1])
            owner = await database.owner(call.from_user.id)
            if not owner or owner["subscription_until"] <= now():
                await show_subscription(call)
                return
            url = f"{settings.public_url}/item/{item['id']}"
            png, pdf = await asyncio.to_thread(
                lambda: (png_label(url), pdf_label(url, item["name"]))
            )
            await bot.send_document(
                call.from_user.id,
                BufferedInputFile(png, filename=f"verniqr-{item['id']}.png"),
                caption=f"QR для вещи «{escape(item['name'])}». Печатайте с белыми полями и проверьте сканирование.",
            )
            await bot.send_document(
                call.from_user.id,
                BufferedInputFile(pdf, filename=f"verniqr-{item['id']}.pdf"),
                caption="Готовый макет A4. Печатайте в масштабе 100%, вырежьте по рамке и прикрепите к вещи. Связь по метке работает при действующем доступе.",
            )
        elif action in {"delitem", "confirm_delitem"} and len(parts) == 2:
            item = await database.item(call.from_user.id, parts[1])
            if action == "delitem":
                await render(
                    call,
                    f"Удалить «{escape(item['name'])}»?\n\nМетка перестанет работать. Контакты и все обращения по этой вещи будут удалены из рабочей базы.",
                    keyboard(
                        [
                            [("Удалить вещь", f"confirm_delitem:{item['id']}")],
                            [("Отмена", f"item:{item['id']}")],
                        ]
                    ),
                )
            else:
                await database.delete_item(call.from_user.id, item["id"])
                await show_items(call)
        elif action == "reports" and len(parts) == 4:
            try:
                offset = int(parts[2])
            except ValueError:
                raise DomainError("Некорректная страница.") from None
            await show_reports(call, parts[1], offset, None if parts[3] == "all" else parts[3])
        elif action in {"report", "retry"} and len(parts) == 2 and parts[1].isdigit():
            report = await database.report(call.from_user.id, int(parts[1]))
            if action == "retry":
                queued = await database.retry_notification(call.from_user.id, report["id"])
                await render(
                    call,
                    "Уведомление поставлено в очередь повторной отправки."
                    if queued
                    else "Уведомление уже в очереди. Откройте список обращений, чтобы прочитать сообщение.",
                    report_keyboard(report),
                )
            else:
                status = (
                    "Новое"
                    if report["status"] == "new"
                    else "Закрыто: "
                    + (
                        "вещь возвращена"
                        if report["closed_reason"] == "returned"
                        else "неактуально"
                    )
                )
                delivery = {
                    "pending": "в очереди",
                    "sending": "отправляется",
                    "sent": "доставлено в Telegram",
                    "failed": "не отправлено; можно повторить",
                }[report["notification_status"]]
                await render(
                    call,
                    notification_text(report)
                    + f"\n\nСоздано: {date_text(report['created_at'])}\nСтатус: {status}\nУведомление: {delivery}"
                    + (
                        f"\nЗакрыто: {date_text(report['closed_at'])}"
                        if report["closed_at"]
                        else ""
                    ),
                    report_keyboard(report),
                )
        elif action == "close" and len(parts) == 3 and parts[2].isdigit():
            await database.close_report(call.from_user.id, int(parts[2]), parts[1])
            await render(
                call,
                "Обращение закрыто: "
                + ("вещь возвращена." if parts[1] == "returned" else "неактуально.")
                + "\n\nДругие обращения и QR этой вещи продолжают работать.",
                keyboard([[("Новые обращения", "reports:new:0:all"), ("Меню", "home")]]),
            )
        elif action == "owners" and len(parts) == 2 and parts[1].isdigit():
            await show_owners(call, int(parts[1]))
        else:
            raise DomainError("Кнопка устарела. Откройте меню командой /start.")

    @router.message()
    async def fallback(message: Message):
        await message.answer(
            "Выберите действие в меню. Фото и голосовые сообщения в MVP не принимаются. /cancel — отменить ввод.",
            reply_markup=main_keyboard(),
        )

    dispatcher.include_router(router)
    return dispatcher


async def configure_commands(bot: Bot):
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="items", description="Мои вещи"),
            BotCommand(command="new", description="Добавить вещь"),
            BotCommand(command="reports", description="Сообщения о находках"),
            BotCommand(command="subscription", description="Доступ и оплата"),
            BotCommand(command="demo", description="Пример страницы"),
            BotCommand(command="help", description="Помощь и данные"),
            BotCommand(command="id", description="Мой Telegram ID"),
            BotCommand(command="cancel", description="Отменить ввод"),
            BotCommand(command="delete_me", description="Удалить профиль"),
        ]
    )
