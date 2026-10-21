"""Демонстрационный режим с локальной обработкой событий."""
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

from .bot import Bot
from .config import Campaign, PartnerDay, Prize, load_campaign, moment
from .db import Database
from .outbox import OutboxWorker
from .service import Service


def example_campaign(start: date | None = None) -> Campaign:
    tz = timezone(timedelta(hours=3))
    start = start or datetime.now(tz).date()
    days = []
    for index in range(10):
        day = start + timedelta(days=index)
        draw_at = datetime.combine(day + timedelta(days=1), datetime.min.time(), tz) + timedelta(minutes=5)
        days.append(PartnerDay(
            date=day.isoformat(), partner_id=f"partner_{index + 1}",
            partner_name=f"Тестовый партнер {index + 1}", group_id=1000 + index,
            promo_expires=(day + timedelta(days=30)).isoformat(), promo_where="сайте тестового партнера",
            draw_at=draw_at.isoformat(),
            prizes=(Prize("Подарок партнера", 1, "дождись сообщения организатора в VK после подтверждения данных."),)))
    return Campaign(
        title="Фестивальный бот розыгрышей", demo=True, utc_offset_hours=3,
        rules="ДЕМОНСТРАЦИОННЫЕ ПРАВИЛА. Каждый день можно получить промокод и участвовать в розыгрыше партнера после проверки подписки. "
              "Одно или оба действия дают один шанс в финале, максимум 10. Финал: 5 разных победителей по 2 билета. "
              "Результаты подтверждает администратор. На ответ победителю — 48 часов с момента предложения приза. "
              "Затем приз переходит резервному победителю. Доступно до 5 резервных победителей на розыгрыш. "
              "Это тестовые данные. Для реальной акции организатор должен предоставить правила, даты и призы.",
        consent_text="ДЕМОНСТРАЦИОННОЕ СОГЛАСИЕ. Я согласен на передачу моего имени, фамилии и ID VK получателю «{recipient}» "
                     "для организации выдачи приза. Нажимая кнопку ниже, подтверждаю это согласие. "
                     "Для реальной акции этот текст должен заменить организатор.",
        consent_version="demo-1", final_prize="два билета на Канье Уэста",
        final_instructions="После подтверждения данных организатор свяжется с тобой в VK для передачи двух билетов.",
        final_recipient="Тестовый билетный оператор", final_draw_at=(moment(days[-1].draw_at) + timedelta(minutes=5)).isoformat(),
        days=tuple(days), reserve_count=5, claim_hours=48)


def write_example(path: str | Path, start: date | None = None) -> None:
    target = Path(path)
    if target.exists():
        raise ValueError(f"Файл уже существует: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(asdict(example_campaign(start)), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class DemoVk:
    def __init__(self):
        self.members = set()
        self.sent = set()

    def is_member(self, day, user_id):
        return (day.group_id, user_id) in self.members

    def messages_allowed(self, user_id):
        return True

    def send(self, user_id, random_id, body):
        if random_id in self.sent:
            return
        self.sent.add(random_id)
        print(f"\nБот → пользователь {user_id}:\n{body['message']}")
        labels = []
        for row in body["keyboard"]["buttons"]:
            for item in row:
                action = item["action"]
                label = action["label"]
                payload = json.loads(action.get("payload", "{}"))
                if payload.get("action") in ("claim", "consent"):
                    label += f" (:{payload['action']} {payload['offer_id']})"
                labels.append(label)
        print("Кнопки: " + " | ".join(labels))
        if body.get("attachment"):
            print("Баннер: " + body["attachment"])


def interactive(db_path: str = "data/demo.sqlite3") -> None:
    config_path = Path("config.demo.json")
    if not config_path.exists():
        write_example(config_path)
    campaign = load_campaign(config_path)
    if not campaign.demo:
        raise ValueError("Демонстрация разрешена только с demo=true")
    service = Service(Database(db_path), campaign)
    for day in campaign.days:
        service.import_promos(day.date, [f"DEMO-{day.partner_id}-{index}" for index in range(1, 4)])
    now = datetime.combine(date.fromisoformat(campaign.days[0].date), datetime.min.time(), campaign.tz) + timedelta(hours=12)
    with service.db.read() as conn:
        saved_time = conn.execute("SELECT value FROM settings WHERE key='demo_now'").fetchone()
    if saved_time:
        now = moment(saved_time[0]).astimezone(campaign.tz)
    clock = lambda: now
    vk = DemoVk()
    user_id = 101
    bot = Bot(service, vk, 1, admin_ids=[101], clock=clock)
    worker = OutboxWorker(service, vk, clock)
    sequence = 0
    print("ДЕМОНСТРАЦИОННЫЙ РЕЖИМ. Ответы бота отображаются в консоли. Для проверки используйте вымышленные имена.")
    print("Вводи подписи кнопок: Начать, Получить промокод, Участвовать в розыгрыше, Проверить подписку, Мои шансы.")
    print(":subscribe / :unsubscribe — тестовая подписка; :user ID — сменить пользователя (админ 101)")
    print(f":day N — перейти к дню 1–{len(campaign.days)}; :finish — подвести все итоги; :advance ЧАСЫ — перевести часы вперёд")
    print(":claim ID / :consent ID — кнопки получения и согласия; /admin — команды администратора; :quit — выход")
    while True:
        try:
            text = input(f"\n[{now.strftime('%d.%m %H:%M')} • {user_id}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        try:
            payload = None
            if text == ":quit":
                break
            if text.startswith(":user "):
                user_id = int(text.split()[1])
                if user_id <= 0:
                    raise ValueError("ID должен быть положительным")
                continue
            if text in (":subscribe", ":unsubscribe"):
                day = campaign.current_day(now)
                if not day:
                    raise ValueError("Сейчас нет активного дня")
                if text == ":subscribe":
                    vk.members.add((day.group_id, user_id))
                else:
                    vk.members.discard((day.group_id, user_id))
                print("Тестовая подписка обновлена.")
                continue
            if text.startswith(":day "):
                index = int(text.split()[1]) - 1
                if not 0 <= index < len(campaign.days):
                    raise ValueError(f"Номер дня: от 1 до {len(campaign.days)}")
                target = datetime.combine(date.fromisoformat(campaign.days[index].date), datetime.min.time(), campaign.tz) + timedelta(hours=12)
                if target < now:
                    raise ValueError("Время можно переводить только вперёд")
                now = target
            elif text == ":finish":
                now = max(now, moment(campaign.final_draw_at) + timedelta(minutes=1))
            elif text.startswith(":advance "):
                hours = float(text.split()[1])
                if hours < 0:
                    raise ValueError("Время можно переводить только вперёд")
                now += timedelta(hours=hours)
            else:
                if text.startswith((":claim ", ":consent ")):
                    command, value = text.split()
                    payload = {"action": command[1:], "offer_id": int(value)}
                elif text.casefold() in ("проверить подписку", "проверить еще раз"):
                    day = campaign.current_day(now)
                    payload = {"action": "check", "day": day.date if day else ""}
                sequence += 1
                # После перезапуска номер берём из времени: он не пересекается с прошлыми событиями.
                import time
                identity = f"demo:{time.time_ns()}:{sequence}"
                bot.handle({"group_id": 1, "type": "message_new", "event_id": identity,
                            "object": {"message": {"from_id": user_id, "peer_id": user_id, "text": text,
                                                     "payload": json.dumps(payload) if payload else None}}})
            for draw_id in service.tick(now):
                print(f"\nПодготовлены результаты: {draw_id}. Посмотреть: /draw {draw_id}; подтвердить: /approve {draw_id}")
            with service.db.transaction() as conn:
                conn.execute("INSERT OR REPLACE INTO settings VALUES ('demo_now', ?)", (now.isoformat(),))
            for _ in range(1000):
                if not worker.send_one():
                    break
        except (ValueError, IndexError) as error:
            print(f"Ошибка ввода: {error}")
