"""Prepare a local .env without overwriting credentials or printing the secret."""

import secrets
from pathlib import Path

root = Path(__file__).resolve().parent.parent
target = root / ".env"
if target.exists():
    print("Файл .env уже существует; сохранён без изменений.")
else:
    template = (root / ".env.example").read_text(encoding="utf-8")
    target.write_text(
        template.replace("development-only-change-before-publishing", secrets.token_hex(32)),
        encoding="utf-8",
    )
    print("Файл .env подготовлен. Заполните BOT_TOKEN, ADMIN_IDS и PUBLIC_URL.")
