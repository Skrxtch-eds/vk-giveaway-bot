"""Обработка VK-событий и команд. Бизнес-правила остаются в service.py."""
import json
import logging

from . import messages
from .service import stamp, utcnow
from .vk import TransportError, VkError


logger = logging.getLogger(__name__)
TEXT_ACTIONS = {label.casefold(): action for row in messages.MENU for label, action in row}
TEXT_ACTIONS.update({"начать": "start", "start": "start", "/start": "start", "отмена": "cancel_claim"})


class Bot:
    def __init__(self, service, vk, group_id: int, admin_ids=(), clock=utcnow):
        self.service, self.vk, self.group_id = service, vk, group_id
        self.admin_ids, self.clock = set(admin_ids), clock

    def handle(self, event: dict) -> bool:
        if event.get("group_id") != self.group_id:
            return False
        kind = event.get("type")
        if kind not in ("message_new", "message_allow", "message_deny"):
            return False
        obj = event.get("object", {})
        if kind == "message_new":
            message = obj.get("message", {})
            user_id = message.get("from_id")
            if type(user_id) is not int or user_id <= 0 or message.get("peer_id") != user_id or message.get("out"):
                return False  # Обрабатываем личный диалог с человеком, не беседу.
            identity = event.get("event_id") or f"message:{user_id}:{message.get('conversation_message_id', message.get('id'))}"
            if not event.get("event_id") and not (message.get("conversation_message_id") or message.get("id")):
                return False
        else:
            user_id = obj.get("user_id")
            identity = event.get("event_id")
            if type(user_id) is not int or user_id <= 0 or not identity:
                return False
            message = {}
        event_key = f"{self.group_id}:{kind}:{identity}"
        with self.service.db.read() as conn:
            if conn.execute("SELECT 1 FROM incoming_events WHERE event_key=?", (event_key,)).fetchone():
                return False
        text = message.get("text", "").strip()
        payload = self._payload(message.get("payload"))
        action = payload.get("action") or TEXT_ACTIONS.get(text.casefold(), "input")
        subscribed = None
        checked_day = None
        # Проверка подписки выполняется до открытия транзакции. Затем заново сверяем день.
        if kind == "message_new" and action == "check":
            checked_day = self.service.campaign.current_day(self.clock())
            if checked_day and payload.get("day") == checked_day.date and not self.service.registration_day(user_id, checked_day.partner_id):
                try:
                    subscribed = self.vk.is_member(checked_day, user_id)
                except (VkError, TransportError):
                    logger.warning("Проверка подписки временно недоступна")
        now = self.clock()
        with self.service.db.transaction() as conn:
            if not conn.execute("INSERT OR IGNORE INTO incoming_events VALUES (?, ?)", (event_key, stamp(now))).rowcount:
                return False
            if kind != "message_new":
                self.service.set_reachable(user_id, kind == "message_allow", now)
                return True
            admin_command = text.startswith("/") and text.split()[0].casefold() != "/start"
            self.service.touch_user(user_id, now, started=not admin_command)
            response = self._route(user_id, text, action, payload, subscribed, now)
            self.service.enqueue(user_id, response, f"reply:{event_key}", now)
        return True

    @staticmethod
    def _payload(raw) -> dict:
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
            return value if isinstance(value, dict) else {}
        except (ValueError, TypeError):
            return {}

    def _route(self, user_id, text, action, payload, subscribed, now) -> dict:
        if text.startswith("/") and text.split()[0].casefold() != "/start":
            return self._admin(user_id, text, now)
        if action == "promo":
            return self.service.promo(user_id, now)
        if action == "participate":
            return self.service.participate(user_id, now)
        if action == "check":
            return self.service.register(user_id, str(payload.get("day", "")), subscribed, now)
        if action == "chances":
            return self.service.chances(user_id, now)
        if action == "rules":
            return messages.reply(self.service.campaign.rules)
        if action == "help":
            return messages.reply("Каждый день — новый партнер. Получи промокод или подпишись на партнера и проверь подписку для участия в розыгрыше дня.\n\n"
                                  "Одно из этих действий дает один шанс в финале. Оба действия в один день тоже дают один шанс. Всего — до 10 шансов. "
                                  "Отсутствие промокодов не мешает участию в розыгрыше дня.\n\n"
                                  "В финале 5 победителей получат по 2 билета. Подробности — в разделе «Правила акции».")
        if action in ("claim", "consent"):
            offer_id = payload.get("offer_id")
            if type(offer_id) is not int or offer_id <= 0:
                return messages.reply("Некорректная кнопка получения приза.")
            if action == "claim":
                return self.service.claim_start(user_id, offer_id, now)
            return self.service.consent(user_id, offer_id, now)
        if action == "cancel_claim":
            return self.service.cancel_claim(user_id)
        if action == "input":
            response = self.service.claim_input(user_id, text, now)
            if response:
                return response
        return messages.reply("Добро пожаловать в Бот розыгрышей", attachment=self.service.campaign.banner_attachment)

    def _admin(self, user_id: int, text: str, now) -> dict:
        # Проверка прав выполняется для КАЖДОЙ команды, а не только для открытия меню.
        if user_id not in self.admin_ids:
            return messages.reply("Эта команда доступна только администратору.")
        command, _, argument = text.partition(" ")
        command, argument = command.casefold(), argument.strip()
        try:
            if command == "/admin":
                return messages.reply("Команды администратора:\n/stats — статистика\n/draws — список розыгрышей\n"
                                      "/draw ID — участники и результаты\n/approve ID — подтвердить результаты и поставить уведомления в очередь\n"
                                      "/broadcast КЛЮЧ ТЕКСТ — отправить уведомление всем доступным участникам\n"
                                      "Выгрузки и загрузка промокодов выполняются через консоль администратора.")
            if command == "/stats":
                stats = self.service.statistics()
                lines = [f"Пользователей: {stats['users_started']}", f"Запросов промокода: {stats['promo_requests']}",
                         f"Выдано промокодов: {stats['promos_issued']}", ""]
                for day in stats["days"]:
                    lines.append(f"{day['day']} • {day['partner']}: запросов {day['promo_requests']}, выдано {day['promos_issued']}, осталось {day['promos_remaining']}, участников {day['participants']}")
                lines += ["", f"Новые пользователи по дням: {stats['new_users_by_day']}", f"Очередь сообщений: {stats['messages']}"]
                return messages.reply("\n".join(lines))
            if command == "/draws":
                with self.service.db.read() as conn:
                    draws = conn.execute("SELECT draw_id, status FROM draws ORDER BY drawn_at, draw_id").fetchall()
                return messages.reply("\n".join(f"{row[0]} — {row[1]}" for row in draws) or "Розыгрыши еще не проводились.")
            if command == "/draw":
                result = self.service.draw_summary(argument)
                return messages.reply(json.dumps(result, ensure_ascii=False, indent=2))
            if command == "/approve":
                done = self.service.approve(argument, user_id, now)
                return messages.reply("Результаты подтверждены. Уведомления поставлены в очередь." if done else "Результаты уже подтверждены. Повторные уведомления не созданы.")
            if command == "/broadcast":
                key, _, body = argument.partition(" ")
                count = self.service.broadcast(key, body, user_id, now)
                return messages.reply(f"В очередь добавлено сообщений: {count}.")
        except ValueError as error:
            return messages.reply(str(error))
        return messages.reply("Неизвестная команда. Список команд: /admin")
