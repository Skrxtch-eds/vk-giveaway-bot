"""Точка входа: python -m giveaway --help."""
import argparse
from datetime import date
import json
import logging
import os
from pathlib import Path
import signal
import sys

from .bot import Bot
from .config import load_campaign, moment
from .db import Database
from .demo import interactive, write_example
from .outbox import OutboxWorker
from .runner import run
from .service import Service, utcnow
from .vk import TransportError, VkClient, VkError


def load_env(path: str = ".env") -> None:
    source = Path(path)
    if not source.exists():
        return
    for line in source.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip().replace("_", "").isalnum():
            raise ValueError("Некорректная строка в .env")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="VK-бот промокодов и розыгрышей")
    result.add_argument("--config", default="config.local.json")
    result.add_argument("--db", default=None)
    result.add_argument("--now", help="Время симуляции ISO 8601; только для demo=true")
    commands = result.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("init-demo", help="Создать пример настроек на 10 дней")
    demo.add_argument("--output", default="config.demo.json")
    demo.add_argument("--start", type=date.fromisoformat)
    commands.add_parser("demo", help="Интерактивный демонстрационный режим")
    commands.add_parser("check", help="Проверить настройки")
    commands.add_parser("check-vk", help="Проверить ключ, Long Poll и доступность проверки партнёров")
    banner = commands.add_parser("upload-banner", help="Загрузить PNG/JPEG в VK и получить banner_attachment")
    banner.add_argument("file")
    banner.add_argument("--peer-id", type=int, required=True, help="ID администратора, разрешившего сообщения")
    commands.add_parser("run", help="Запустить VK Long Poll, розыгрыши и очередь")
    promos = commands.add_parser("import-promos", help="Загрузить промокоды: один код в строке")
    promos.add_argument("day")
    promos.add_argument("file")
    commands.add_parser("stats")
    commands.add_parser("draws")
    for name in ("draw", "show-draw", "approve"):
        command = commands.add_parser(name)
        command.add_argument("draw_id", help="daily:ГГГГ-ММ-ДД или final")
        if name == "approve":
            command.add_argument("--admin-id", type=int, required=True)
    commands.add_parser("tick", help="Обработать наступившие сроки без отправки сообщений")
    export = commands.add_parser("export")
    export.add_argument("draw_id")
    export.add_argument("file")
    backup = commands.add_parser("backup")
    backup.add_argument("file")
    broadcast = commands.add_parser("broadcast")
    broadcast.add_argument("key")
    broadcast.add_argument("file", help="UTF-8 файл с текстом рассылки")
    broadcast.add_argument("--admin-id", type=int, required=True)
    commands.add_parser("retry-failed", help="Вернуть неотправленные сообщения в очередь")
    return result


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "init-demo":
            write_example(args.output, args.start)
            print(f"Пример создан: {args.output}")
            return 0
        if args.command == "demo":
            interactive(args.db or "data/demo.sqlite3")
            return 0
        load_env()
        campaign = load_campaign(args.config)
        if args.now and not campaign.demo:
            raise ValueError("Подмена времени запрещена для рабочей акции")
        now = moment(args.now) if args.now else utcnow()
        admin_ids = [int(value.strip()) for value in os.getenv("VK_ADMIN_IDS", "").split(",") if value.strip()]
        if any(value <= 0 for value in admin_ids):
            raise ValueError("ID администраторов должны быть положительными")
        if hasattr(args, "admin_id"):
            if args.admin_id <= 0 or (not campaign.demo and args.admin_id not in admin_ids):
                raise ValueError("Укажите ID администратора из VK_ADMIN_IDS")
        if args.command == "check":
            print(f"Настройки корректны. Дней: {len(campaign.days)}. Резерв: {campaign.reserve_count}. Ответ: {campaign.claim_hours} ч. Режим: {'демо' if campaign.demo else 'рабочий'}.")
            return 0
        if args.command in ("check-vk", "upload-banner"):
            if campaign.demo:
                raise ValueError("Для подключения к VK нужен рабочий конфиг с demo=false")
            group_id = int(os.getenv("VK_GROUP_ID", "0"))
            partner_tokens = {key[len("VK_PARTNER_TOKEN_"):]: value for key, value in os.environ.items() if key.upper().startswith("VK_PARTNER_TOKEN_")}
            vk = VkClient(os.getenv("VK_GROUP_TOKEN", ""), group_id, os.getenv("VK_API_VERSION", "5.199"), partner_tokens)
            if args.command == "upload-banner":
                if args.peer_id not in admin_ids:
                    raise ValueError("peer-id должен быть администратором из VK_ADMIN_IDS")
                print(vk.upload_banner(args.file, args.peer_id))
            else:
                if not admin_ids:
                    raise ValueError("Заполните VK_ADMIN_IDS")
                print(json.dumps(vk.check_connection(campaign, admin_ids[0]), ensure_ascii=False, indent=2))
            return 0
        service = Service(Database(args.db or "data/bot.sqlite3"), campaign)
        if args.command == "import-promos":
            codes = Path(args.file).read_text(encoding="utf-8-sig").splitlines()
            print(f"Загружено новых промокодов: {service.import_promos(args.day, codes)}")
        elif args.command == "stats":
            print(json.dumps(service.statistics(), ensure_ascii=False, indent=2))
        elif args.command == "draws":
            with service.db.read() as conn:
                print(json.dumps([dict(row) for row in conn.execute("SELECT * FROM draws ORDER BY drawn_at, draw_id")], ensure_ascii=False, indent=2))
        elif args.command == "draw":
            print("Результаты подготовлены." if service.draw(args.draw_id, now) else "Розыгрыш уже проведён. Результаты сохранены.")
        elif args.command == "show-draw":
            print(json.dumps(service.draw_summary(args.draw_id), ensure_ascii=False, indent=2))
        elif args.command == "approve":
            print("Подтверждено. Уведомления поставлены в очередь." if service.approve(args.draw_id, args.admin_id, now) else "Уже подтверждено.")
        elif args.command == "tick":
            print(json.dumps({"new_draws": service.tick(now)}, ensure_ascii=False))
        elif args.command == "export":
            print(f"Выгружено участников: {service.export_draw(args.draw_id, args.file)}")
        elif args.command == "backup":
            service.db.backup(args.file)
            print("Резервная копия сохранена.")
        elif args.command == "broadcast":
            body = Path(args.file).read_text(encoding="utf-8-sig").strip()
            print(f"В очередь добавлено: {service.broadcast(args.key, body, args.admin_id, now)}")
        elif args.command == "retry-failed":
            print(f"В очередь возвращено: {OutboxWorker(service, None).retry_failed()}")
        elif args.command == "run":
            if campaign.demo:
                raise ValueError("Рабочее подключение к VK запрещено для demo=true. Для демонстрации: python -m giveaway demo")
            if not admin_ids:
                raise ValueError("Заполните VK_ADMIN_IDS для подтверждения результатов")
            if not campaign.banner_attachment:
                raise ValueError("Загрузите приветственный баннер в VK и заполните banner_attachment")
            group_id = int(os.getenv("VK_GROUP_ID", "0"))
            partner_tokens = {key[len("VK_PARTNER_TOKEN_"):]: value for key, value in os.environ.items() if key.upper().startswith("VK_PARTNER_TOKEN_")}
            vk = VkClient(os.getenv("VK_GROUP_TOKEN", ""), group_id, os.getenv("VK_API_VERSION", "5.199"), partner_tokens)
            vk.check_connection(campaign, admin_ids[0])
            def stop_on_signal(signum, frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, stop_on_signal)
            run(Bot(service, vk, group_id, admin_ids), service, vk, admin_ids)
        return 0
    except (ValueError, OSError, KeyError, TypeError, TransportError, VkError) as error:
        print(f"Не удалось выполнить команду: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
