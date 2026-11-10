from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr = SecretStr("")
    bot_username: str = ""
    telegram_use_windows_proxy: bool = False
    public_url: str = "http://localhost:8000"
    admin_ids: str = ""
    app_secret: SecretStr = SecretStr("development-only-change-before-publishing")
    database_path: Path = Path("data/verniqr.sqlite3")
    backup_dir: Path = Path("data/backups")
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    trusted_proxy_ips: str = "127.0.0.1,::1"
    support_contact: str = "Напишите организатору пилота"
    access_price_rub: int = Field(default=50, ge=1)
    report_ip_limit: int = Field(default=10, ge=1)
    report_item_limit: int = Field(default=5, ge=1)
    report_window_seconds: int = Field(default=600, ge=1)

    @field_validator("public_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.rstrip("/")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.netloc
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            raise ValueError("PUBLIC_URL должен быть адресом сайта без пути и параметров")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Для публичного сайта требуется HTTPS")
        return value

    @field_validator("admin_ids")
    @classmethod
    def validate_admin_ids(cls, value: str) -> str:
        for part in value.split(","):
            if part.strip() and (not part.strip().isdigit() or int(part) <= 0):
                raise ValueError("ADMIN_IDS — положительные Telegram ID через запятую")
        return value

    @property
    def admins(self) -> frozenset[int]:
        return frozenset(int(part.strip()) for part in self.admin_ids.split(",") if part.strip())

    def validate_runtime(self, web_only: bool = False) -> None:
        if not web_only and not self.bot_token.get_secret_value():
            raise ValueError("Укажите BOT_TOKEN в .env. Для просмотра сайта: --web-only")
        if self.public_url.startswith("https://"):
            if len(
                self.app_secret.get_secret_value()
            ) < 32 or self.app_secret.get_secret_value().startswith("development-"):
                raise ValueError("Замените APP_SECRET случайной строкой не короче 32 символов")
            if not self.admins and not web_only:
                raise ValueError("Укажите ADMIN_IDS для ручной активации доступа")
