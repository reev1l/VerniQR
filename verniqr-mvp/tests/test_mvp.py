import asyncio
import re
from io import BytesIO
from uuid import uuid4

import aiosqlite
import pytest
import zxingcpp
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from PIL import Image
from pypdf import PdfReader

from verniqr.backups import make_backup, restore_backup, verify_backup
from verniqr.db import Database
from verniqr.labels import pdf_label, png_label
from verniqr.notifications import deliver_one
from verniqr.validation import DomainError
from verniqr.web import form_token


async def report(db, item, text="Рюкзак оставлен у охраны корпуса Б", contact=None, key=None):
    return await db.create_report(
        item["id"], text, contact, key or uuid4().hex, "test-ip", 100, 100, 600
    )


async def submit(
    client, settings, item, message="Рюкзак оставлен у охраны корпуса Б", contact=None, token=None
):
    return await client.post(
        f"/api/reports/{item['id']}",
        json={"message": message, "finder_contact": contact},
        headers={"X-Form-Token": token or form_token(settings, item["id"])},
    )


async def test_a1_three_distinct_items_and_atomic_limit(db):
    outcomes = await asyncio.gather(
        *(db.create_item(100, f"Вещь {i}") for i in range(4)), return_exceptions=True
    )
    created = [value for value in outcomes if isinstance(value, dict)]
    assert len(created) == 3
    assert len({value["id"] for value in created}) == 3
    assert sum(isinstance(value, DomainError) for value in outcomes) == 1


async def test_a2_hidden_contacts_never_leave_public_layer(db, item, client):
    await db.set_contact(100, item["id"], "phone", "+7 (900) 123-45-67")
    await db.set_contact(100, item["id"], "email", "owner@example.ru")
    await db.toggle_contact(100, item["id"], "email")
    page = await client.get(f"/item/{item['id']}")
    assert page.status_code == 200
    assert "+79001234567" in page.text
    assert "owner@example.ru" not in page.text
    assert "owner_id" not in page.text
    await db.set_contact(100, item["id"], "phone", "+79990000000")
    assert "+79990000000" in (await client.get(f"/item/{item['id']}")).text
    await db.toggle_contact(100, item["id"], "phone")
    assert "+79990000000" not in (await client.get(f"/item/{item['id']}")).text


def test_a3_png_and_pdf_decode_to_same_url_and_have_cyrillic():
    url = f"https://verniqr.example/item/{uuid4()}"
    png = png_label(url)
    assert zxingcpp.read_barcode(Image.open(BytesIO(png))).text == url
    pdf = PdfReader(BytesIO(pdf_label(url, "Мой рюкзак")))
    assert len(pdf.pages) == 1
    assert "Мой рюкзак" in pdf.pages[0].extract_text()
    assert "Нашли вещь?" in pdf.pages[0].extract_text()
    images = list(pdf.pages[0].images)
    assert len(images) == 1
    assert zxingcpp.read_barcode(images[0].image).text == url


@pytest.mark.parametrize("contact", [None, "@finder_name"])
async def test_a4_report_saved_with_optional_contact(db, client, settings, item, contact):
    result = await submit(client, settings, item, contact=contact)
    assert result.status_code == 201
    rows, total = await db.reports(100)
    assert total == 1 and rows[0]["finder_contact"] == contact
    assert rows[0]["item_id"] == item["id"]


async def test_a5_telegram_failure_keeps_report_and_manual_retry(db, item, fake_bot):
    report_id, _ = await report(db, item)
    bot, session = fake_bot
    session.fail_send = TelegramForbiddenError(
        method=SendMessage(chat_id=100, text="test"), message="Forbidden"
    )
    assert await deliver_one(db, bot)
    saved = await db.report(100, report_id)
    assert saved["notification_status"] == "failed" and saved["status"] == "new"
    assert await db.retry_notification(100, report_id)
    session.fail_send = None
    assert await deliver_one(db, bot)
    saved = await db.report(100, report_id)
    assert saved["notification_status"] == "sent" and saved["notification_attempts"] == 2


async def test_a6_database_failure_never_confirms_success(db, client, settings, item, monkeypatch):
    async def fail(*args, **kwargs):
        raise aiosqlite.OperationalError("simulated disk failure")

    monkeypatch.setattr(db, "create_report", fail)
    response = await submit(client, settings, item)
    assert response.status_code == 503
    assert response.json()["error"] == "SERVER_ERROR"
    assert "status" not in response.json()
    assert (await db.reports(100))[1] == 0


async def test_a7_repeated_request_is_idempotent_even_at_rate_limit(client, settings, db, item):
    settings.report_ip_limit = 1
    token = form_token(settings, item["id"])
    first, second = await asyncio.gather(
        submit(client, settings, item, token=token), submit(client, settings, item, token=token)
    )
    assert sorted([first.status_code, second.status_code]) == [200, 201]
    assert (await db.reports(100))[1] == 1
    limited = await submit(client, settings, item)
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers
    conflict = await submit(
        client, settings, item, message="Другое сообщение о найденной вещи", token=token
    )
    assert conflict.status_code == 409


async def test_a8_owner_cannot_access_or_modify_foreign_data(db, item):
    report_id, _ = await report(db, item, contact="private-contact")
    actions = [
        db.item(200, item["id"]),
        db.rename_item(200, item["id"], "Чужое имя"),
        db.set_contact(200, item["id"], "phone", "+79001234567"),
        db.toggle_contact(200, item["id"], "phone"),
        db.report(200, report_id),
        db.close_report(200, report_id, "returned"),
        db.retry_notification(200, report_id),
    ]
    for action in actions:
        with pytest.raises(DomainError):
            await action
    await db.delete_item(200, item["id"])
    assert (await db.item(100, item["id"]))["name"] == "Мой рюкзак"
    assert (await db.reports(200))[1] == 0


@pytest.mark.parametrize("reason", ["returned", "irrelevant"])
async def test_a9_only_selected_report_closes(db, item, reason):
    selected, _ = await report(db, item)
    other, _ = await report(db, item)
    await db.close_report(100, selected, reason)
    assert (await db.report(100, selected))["closed_reason"] == reason
    assert (await db.report(100, selected))["closed_at"]
    assert (await db.report(100, other))["status"] == "new"
    with pytest.raises(DomainError):
        await db.close_report(100, selected, "irrelevant")


async def test_a10_qr_remains_usable_after_return(db, item, client, settings):
    selected, _ = await report(db, item)
    await db.close_report(100, selected, "returned")
    assert (await client.get(f"/item/{item['id']}")).status_code == 200
    assert (await submit(client, settings, item)).status_code == 201
    assert (await db.reports(100, "closed"))[1] == 1
    assert (await db.reports(100, "new"))[1] == 1


async def test_a11_expiration_and_renewal_preserve_qr_and_history(
    db, item, client, settings, monkeypatch
):
    import verniqr.db as db_module

    timestamp = 2000000000
    monkeypatch.setattr(db_module, "now", lambda: timestamp)
    await db.set_access(100, 0, 900)
    await db.set_access(100, 1, 900)
    selected, _ = await report(db, item)
    timestamp += 86400
    assert (await client.get(f"/item/{item['id']}")).status_code == 404
    assert (await submit(client, settings, item)).status_code == 404
    assert (await db.report(100, selected))["id"] == selected
    await db.set_access(100, 30, 900)
    assert (await client.get(f"/item/{item['id']}")).status_code == 200
    assert (await submit(client, settings, item)).status_code == 201


async def test_a12_backup_restore_recovers_contacts_and_statuses(db, item, settings, tmp_path):
    await db.set_contact(100, item["id"], "tg", "@owner_name")
    selected, _ = await report(db, item)
    await db.close_report(100, selected, "returned")
    backup = await asyncio.to_thread(make_backup, settings.database_path, settings.backup_dir)
    verify_backup(backup)
    await db.delete_owner(100)
    restored_path = tmp_path / "restored.sqlite3"
    restore_backup(backup, restored_path, settings.backup_dir)
    restored = Database(restored_path)
    await restored.initialize()
    assert (await restored.item(100, item["id"]))["tg"] == "owner_name"
    assert (await restored.report(100, selected))["closed_reason"] == "returned"
    assert (await restored.owner(100))["subscription_until"] > 0


async def test_profile_deletion_cascades_and_deactivates_pages(db, item, client):
    await report(db, item, contact="sensitive-contact")
    await db.delete_owner(100)
    assert await db.owner(100) is None
    assert await db.items(100) == []
    assert (await db.reports(100))[1] == 0
    assert (await client.get(f"/item/{item['id']}")).status_code == 404
    async with db.connect() as connection:
        assert (await (await connection.execute("SELECT count(*) FROM rate_events")).fetchone())[
            0
        ] == 0
        assert (await (await connection.execute("SELECT count(*) FROM admin_audit")).fetchone())[
            0
        ] == 0


@pytest.mark.parametrize(
    "message,status", [("short", 400), (" " * 20, 400), ("я" * 1000, 201), ("я" * 1001, 400)]
)
async def test_message_boundaries(client, settings, item, message, status):
    assert (await submit(client, settings, item, message=message)).status_code == status


async def test_form_token_is_required_signed_and_bound_to_item(client, settings, db, item):
    path = f"/api/reports/{item['id']}"
    assert (await client.post(path, json={"message": "Рюкзак у охраны"})).status_code == 403
    other = await db.create_item(100, "Ключи")
    assert (
        await submit(client, settings, other, token=form_token(settings, item["id"]))
    ).status_code == 403
    token = form_token(settings, item["id"])
    assert (
        await submit(client, settings, item, token=token[:-1] + ("a" if token[-1] != "a" else "b"))
    ).status_code == 403


async def test_public_page_escapes_html_and_private_fields(db, client, item):
    await db.rename_item(100, item["id"], '<script>alert("x")</script>')
    await report(db, item, contact="PRIVATE-FINDER")
    response = await client.get(f"/item/{item['id']}")
    assert '<script>alert("x")</script>' not in response.text
    assert "&lt;script&gt;" in response.text
    assert "PRIVATE-FINDER" not in response.text
    assert "no-store" == response.headers["cache-control"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


async def test_unknown_inactive_and_invalid_items_have_neutral_page(client):
    for value in [str(uuid4()), "not-a-uuid"]:
        response = await client.get(f"/item/{value}")
        assert response.status_code == 404
        assert "Мой рюкзак" not in response.text
        assert "report-form" not in response.text


async def test_page_issues_working_token(client, settings, item):
    response = await client.get(f"/item/{item['id']}")
    token = re.search('data-token="([^"]+)"', response.text).group(1)
    assert (await submit(client, settings, item, token=token)).status_code == 201


async def test_two_delivery_workers_do_not_claim_same_report(db, item):
    await report(db, item)
    outcomes = await asyncio.gather(db.claim_notification(), db.claim_notification())
    assert sum(value is not None for value in outcomes) == 1


async def test_abandoned_notification_claim_recovers(db, item, monkeypatch):
    import verniqr.db as db_module

    await report(db, item)
    selected = await db.claim_notification()
    original_now = db_module.now()
    monkeypatch.setattr(db_module, "now", lambda: original_now + 121)
    reclaimed = await db.claim_notification()
    assert reclaimed["id"] == selected["id"]


async def test_invalid_json_and_oversized_body(client, settings, item):
    headers = {"X-Form-Token": form_token(settings, item["id"]), "Content-Type": "application/json"}
    path = f"/api/reports/{item['id']}"
    assert (await client.post(path, content="{", headers=headers)).status_code == 400
    assert (await client.post(path, content="x" * 9000, headers=headers)).status_code == 413
    assert (await client.post(path, json={"message": 123}, headers=headers)).status_code == 400


async def test_untrusted_origin_cannot_submit(client, settings, item):
    response = await client.post(
        f"/api/reports/{item['id']}",
        json={"message": "Рюкзак у охраны"},
        headers={
            "Origin": "https://evil.example",
            "X-Form-Token": form_token(settings, item["id"]),
        },
    )
    assert response.status_code == 403
