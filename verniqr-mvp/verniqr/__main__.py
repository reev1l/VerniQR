import argparse
import asyncio
import logging
from pathlib import Path

import uvicorn
from filelock import FileLock, Timeout

from .backups import make_backup, restore_backup
from .config import Settings
from .db import Database


def main():
    parser = argparse.ArgumentParser(description="ВерниQR — бот и страницы возврата вещей")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="Запустить бота и сайт")
    run.add_argument(
        "--web-only", action="store_true", help="Локальный просмотр сайта без Telegram"
    )
    sub.add_parser("init", help="Создать базу данных")
    sub.add_parser("backup", help="Создать проверенную резервную копию")
    restore = sub.add_parser("restore", help="Восстановить базу при остановленном сервисе")
    restore.add_argument("source", type=Path)
    restore.add_argument("--confirm", action="store_true", help="Подтвердить замену рабочей базы")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = Settings()
        if args.command in (None, "run"):
            from .runtime import build_app

            app = build_app(settings, web_only=getattr(args, "web_only", False))
            # Trust forwarding only from a local reverse proxy. UUID paths are not access-logged.
            uvicorn.run(
                app,
                host=settings.host,
                port=settings.port,
                workers=1,
                proxy_headers=True,
                forwarded_allow_ips=settings.trusted_proxy_ips,
                access_log=False,
            )
        elif args.command == "backup":
            print(f"Резервная копия: {make_backup(settings.database_path, settings.backup_dir)}")
        else:
            settings.database_path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(str(settings.database_path) + ".lock", timeout=0):
                if args.command == "init":
                    asyncio.run(Database(settings.database_path).initialize())
                    print("База данных готова")
                elif args.command == "restore":
                    if not args.confirm:
                        parser.error(
                            "Для восстановления добавьте --confirm. Сначала остановите сервис."
                        )
                    restore_backup(args.source, settings.database_path, settings.backup_dir)
                    print("База восстановлена; предыдущая версия сохранена в резервной копии")
    except Timeout:
        parser.exit(1, "Сервис уже работает. Остановите его перед этой операцией.\n")
    except Exception as error:
        # Configuration/validation text is useful; framework/network exceptions may contain secrets.
        if isinstance(error, ValueError) and type(error) is ValueError:
            parser.exit(1, f"Ошибка: {error}\n")
        parser.exit(
            1,
            f"Не удалось выполнить действие ({type(error).__name__}). Проверьте .env и доступность сервисов.\n",
        )


if __name__ == "__main__":
    main()
