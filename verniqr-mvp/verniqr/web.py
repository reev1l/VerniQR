import hashlib
import hmac
import json
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

import aiosqlite
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import Settings
from .db import Database
from .validation import DomainError

logger = logging.getLogger(__name__)
ROOT = Path(__file__).parent


class ReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(max_length=1100)
    finder_contact: str | None = Field(default=None, max_length=200)


def form_token(settings: Settings, item_id: str) -> str:
    data = f"{int(time.time())}.{secrets.token_hex(16)}"
    signature = hmac.new(
        settings.app_secret.get_secret_value().encode(),
        f"{item_id}.{data}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{data}.{signature}"


def verify_form_token(settings: Settings, item_id: str, token: str) -> str:
    try:
        timestamp, nonce, signature = token.split(".")
        if not 0 <= int(time.time()) - int(timestamp) <= 86400:
            raise ValueError
        if len(nonce) != 32 or len(signature) != 64:
            raise ValueError
        expected = hmac.new(
            settings.app_secret.get_secret_value().encode(),
            f"{item_id}.{timestamp}.{nonce}".encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError
    except (ValueError, AttributeError):
        raise DomainError(
            "Обновите страницу и повторите отправку.", "INVALID_FORM_TOKEN", 403
        ) from None
    return nonce


def valid_item_id(value: str) -> str:
    try:
        parsed = UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError
    except ValueError:
        raise DomainError("Метка неактивна или не найдена.", "INACTIVE_ITEM", 404) from None
    return str(parsed)


def create_web_app(settings: Settings, database: Database, lifespan=None) -> FastAPI:
    app = FastAPI(
        title="ВерниQR", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.database = database
    app.state.settings = settings
    app.state.bot_username = settings.bot_username or None
    templates = Jinja2Templates(directory=ROOT / "templates")
    templates.env.filters["mailto"] = lambda value: "mailto:" + quote(value, safe="@.+-_")
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "Cache-Control": "no-store",
                "X-Robots-Tag": "noindex, nofollow, noarchive",
            }
        )
        if settings.public_url.startswith("https://"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse(
            {"error": error.code, "message": error.message},
            status_code=error.status,
            headers={"Retry-After": str(settings.report_window_seconds)}
            if error.status == 429
            else None,
        )

    @app.exception_handler(aiosqlite.Error)
    async def database_error(request, error):
        # Never log input bodies, contacts, tokens, SQL bindings or Telegram secrets.
        logger.error("Database request failed: %s", type(error).__name__)
        return JSONResponse(
            {
                "error": "SERVER_ERROR",
                "message": "Не удалось сохранить сообщение. Попробуйте позже.",
            },
            status_code=503,
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="home.html",
            context={"bot_username": getattr(app.state, "bot_username", None)},
        )

    @app.get("/demo", response_class=HTMLResponse)
    async def demo(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="item.html",
            context={
                "item": {
                    "id": "demo",
                    "name": "Мой рюкзак",
                    "phone": None,
                    "tg": None,
                    "email": None,
                },
                "token": "",
                "demo": True,
            },
        )

    @app.get("/item/{item_id}", response_class=HTMLResponse)
    async def item_page(request: Request, item_id: str):
        try:
            item_id = valid_item_id(item_id)
            item = await database.public_item(item_id)
        except DomainError:
            item = None
        if not item:
            return templates.TemplateResponse(
                request=request, name="inactive.html", context={}, status_code=404
            )
        return templates.TemplateResponse(
            request=request,
            name="item.html",
            context={"item": item, "token": form_token(settings, item_id), "demo": False},
        )

    @app.post("/api/reports/{item_id}")
    async def submit_report(request: Request, item_id: str):
        item_id = valid_item_id(item_id)
        origin = request.headers.get("origin")
        if origin and origin != settings.public_url:
            raise DomainError("Отправьте сообщение со страницы метки.", "INVALID_ORIGIN", 403)
        key = verify_form_token(settings, item_id, request.headers.get("x-form-token", ""))
        if (
            request.headers.get("content-type", "").split(";")[0].strip().lower()
            != "application/json"
        ):
            raise DomainError("Требуется JSON.", "INVALID_CONTENT_TYPE", 415)
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 8192:
                raise DomainError("Сообщение слишком большое.", "BODY_TOO_LARGE", 413)
        try:
            data = ReportInput.model_validate(json.loads(content))
        except (ValidationError, ValueError, UnicodeError):
            raise DomainError(
                "Проверьте текст сообщения и обратный контакт.", "VALIDATION_ERROR", 400
            ) from None
        ip = request.client.host if request.client else "unknown"
        # A keyed hash is sufficient for short-term rate limiting; raw IP is not stored.
        ip_hash = hmac.new(
            settings.app_secret.get_secret_value().encode(), ip.encode(), hashlib.sha256
        ).hexdigest()
        _, created = await database.create_report(
            item_id,
            data.message,
            data.finder_contact,
            key,
            ip_hash,
            settings.report_ip_limit,
            settings.report_item_limit,
            settings.report_window_seconds,
        )
        return JSONResponse(
            {"status": "saved", "message": "Сообщение сохранено для владельца. Спасибо за помощь!"},
            status_code=201 if created else 200,
        )

    @app.get("/healthz")
    async def health():
        async with database.connect() as db:
            await db.execute("SELECT 1")
        if any(task.done() for task in getattr(app.state, "worker_tasks", [])):
            return JSONResponse({"status": "worker_failed"}, status_code=503)
        return {"status": "ok"}

    @app.get("/robots.txt")
    async def robots():
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    return app
