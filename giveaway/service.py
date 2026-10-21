"""Правила акции. Этот модуль не зависит от VK и проверяется без сети."""
import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets

from .config import Campaign, moment
from .db import Database
from . import messages


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(now: datetime) -> str:
    if now.tzinfo is None:
        raise ValueError("Нужно время с часовым поясом")
    return now.astimezone(timezone.utc).isoformat(timespec="microseconds")


class Service:
    def __init__(self, db: Database, campaign: Campaign, randbelow=None):
        self.db, self.campaign = db, campaign
        self.randbelow = randbelow or secrets.randbelow
        with db.transaction() as conn:
            stored = conn.execute("SELECT value FROM settings WHERE key='campaign'").fetchone()
            if stored and stored[0] != campaign.as_json():
                used = conn.execute("SELECT (SELECT count(*) FROM users) + (SELECT count(*) FROM promo_codes)").fetchone()[0]
                if used:
                    raise ValueError("Настройки этой акции уже закреплены в базе. Для другой акции используйте новую базу.")
            conn.execute("INSERT OR REPLACE INTO settings VALUES ('campaign', ?)", (campaign.as_json(),))

    def touch_user(self, user_id: int, now: datetime, started: bool = True) -> None:
        if type(user_id) is not int or user_id <= 0:
            raise ValueError("Некорректный ID пользователя")
        with self.db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO users(user_id, started_at) VALUES (?, ?)",
                         (user_id, stamp(now) if started else None))
            if started:
                conn.execute("UPDATE users SET started_at=COALESCE(started_at, ?), reachable=1 WHERE user_id=?",
                             (stamp(now), user_id))

    def set_reachable(self, user_id: int, allowed: bool, now: datetime) -> None:
        with self.db.transaction() as conn:
            self.touch_user(user_id, now, started=False)
            conn.execute("UPDATE users SET reachable=? WHERE user_id=?", (int(allowed), user_id))

    def enqueue(self, user_id: int, body: dict, key: str, now: datetime, kind: str = "reply") -> None:
        # Разбиваем длинные правила, сохраняя один устойчивый ключ на каждый фрагмент.
        text = body["message"]
        parts = [text[i:i + 3500] for i in range(0, len(text), 3500)] or [""]
        with self.db.transaction() as conn:
            for index, part in enumerate(parts):
                payload = {**body, "message": part}
                dedupe = f"{key}:{index}"
                if conn.execute("SELECT 1 FROM outbox WHERE dedupe_key=?", (dedupe,)).fetchone():
                    continue
                while True:
                    random_id = secrets.randbelow(2**31 - 1) + 1
                    if not conn.execute("SELECT 1 FROM outbox WHERE random_id=?", (random_id,)).fetchone():
                        break
                conn.execute("""INSERT INTO outbox
                    (user_id, random_id, body, kind, dedupe_key, created_at, next_attempt_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, random_id, json.dumps(payload, ensure_ascii=False), kind, dedupe, stamp(now), stamp(now)))

    def import_promos(self, day_key: str, codes: list[str]) -> int:
        day = self.campaign.day(day_key)
        cleaned = [code.strip() for code in codes if code.strip()]
        if any(len(code) > 200 or any(char.isspace() for char in code) for code in cleaned):
            raise ValueError("Промокод должен быть одной строкой без пробелов, не более 200 символов")
        with self.db.transaction() as conn:
            # Один код партнёра не должен повторно попасть в запас другого его дня.
            partner_days = [item.date for item in self.campaign.days if item.partner_id == day.partner_id]
            placeholders = ",".join("?" for _ in partner_days)
            known = {row[0] for row in conn.execute(
                f"SELECT code FROM promo_codes WHERE day IN ({placeholders})", partner_days)}
            cleaned = [code for code in dict.fromkeys(cleaned) if code not in known]
            before = conn.total_changes
            conn.executemany("INSERT OR IGNORE INTO promo_codes(day, code) VALUES (?, ?)",
                             [(day_key, code) for code in cleaned])
            return conn.total_changes - before

    def _activate(self, conn, user_id: int, day_key: str, now: datetime) -> None:
        conn.execute("INSERT OR IGNORE INTO active_days VALUES (?, ?, ?)", (day_key, user_id, stamp(now)))

    def promo(self, user_id: int, now: datetime) -> dict:
        day = self.campaign.current_day(now)
        with self.db.transaction() as conn:
            self.touch_user(user_id, now)
            conn.execute("INSERT INTO promo_requests(user_id, day, requested_at) VALUES (?, ?, ?)",
                         (user_id, day.date if day else None, stamp(now)))
            if not day:
                return messages.reply("Сегодня нет активного партнёрского дня. Посмотри расписание в правилах акции.")
            if conn.execute("SELECT 1 FROM draws WHERE draw_id IN (?, 'final')", (f"daily:{day.date}",)).fetchone():
                return messages.reply("Приём действий за этот партнёрский день завершён. Результаты уже сохранены.")
            assigned = conn.execute("SELECT code FROM promo_codes WHERE day=? AND user_id=?", (day.date, user_id)).fetchone()
            if assigned:
                code = assigned[0]
            else:
                free = conn.execute("SELECT id, code FROM promo_codes WHERE day=? AND user_id IS NULL ORDER BY id LIMIT 1",
                                    (day.date,)).fetchone()
                if not free:
                    return messages.reply(
                        f"Промокоды от {day.partner_name} уже закончились — их разобрали быстрее всех 🔥\n\n"
                        "Но ты все еще можешь подписаться на партнера и принять участие в розыгрыше призов дня.",
                        {"inline": True, "buttons": [[messages.button("Участвовать в розыгрыше", "participate")]]})
                code = free["code"]
                conn.execute("UPDATE promo_codes SET user_id=?, issued_at=? WHERE id=?", (user_id, stamp(now), free["id"]))
                self._activate(conn, user_id, day.date, now)
            return messages.reply(
                f"Твой фестивальный бонус от {day.partner_name} готов 🎁\n\nПромокод: {code}\n\n"
                f"Используй его до {day.promo_expires} на {day.promo_where}.\n\n"
                "Теперь можешь принять участие в розыгрыше призов дня.")

    def registration_day(self, user_id: int, partner_id: str) -> str | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT day FROM registrations WHERE partner_id=? AND user_id=?", (partner_id, user_id)).fetchone()
        return row[0] if row else None

    def participate(self, user_id: int, now: datetime) -> dict:
        day = self.campaign.current_day(now)
        if not day:
            return messages.reply("Сегодня нет активного партнёрского дня.")
        previous_day = self.registration_day(user_id, day.partner_id)
        if previous_day:
            return self.already_registered() if previous_day == day.date else self.already_registered_partner()
        return messages.reply(f"Чтобы попасть в розыгрыш призов от {day.partner_name}, подпишись на аккаунт партнера.",
                              messages.participation(day))

    @staticmethod
    def already_registered() -> dict:
        return messages.reply("Ты уже участвуешь в сегодняшнем розыгрыше ✅\nРезультаты сообщим после подведения итогов.")

    @staticmethod
    def already_registered_partner() -> dict:
        return messages.reply("Твоя заявка на розыгрыш этого партнера уже зарегистрирована ✅\n"
                              "Один пользователь может получить только одну заявку в розыгрыше конкретного партнера.")

    def register(self, user_id: int, day_key: str, subscribed: bool | None, now: datetime) -> dict:
        day = self.campaign.current_day(now)
        if not day or day.date != day_key:
            return messages.reply("Партнёрский день уже сменился. Нажми «Участвовать в розыгрыше» для текущего дня.")
        with self.db.transaction() as conn:
            self.touch_user(user_id, now)
            previous_day = self.registration_day(user_id, day.partner_id)
            if previous_day:
                return self.already_registered() if previous_day == day.date else self.already_registered_partner()
            if conn.execute("SELECT 1 FROM draws WHERE draw_id IN (?, 'final')", (f"daily:{day.date}",)).fetchone():
                return messages.reply("Приём заявок за этот партнёрский день завершён. Результаты уже сохранены.")
            if subscribed is None:
                return messages.reply("Сейчас не удалось проверить подписку. Попробуй еще раз чуть позже.", messages.participation(day))
            if not subscribed:
                keyboard = messages.participation(day)
                keyboard["buttons"][0][0]["action"]["label"] = "Подписаться"
                keyboard["buttons"][1][0]["action"]["label"] = "Проверить еще раз"
                return messages.reply(f"Пока не вижу подписку 👀\n\nПодпишись на аккаунт {day.partner_name}, вернись в бот и попробуй еще раз.", keyboard)
            conn.execute("INSERT INTO registrations VALUES (?, ?, ?, ?)", (day.date, day.partner_id, user_id, stamp(now)))
            self._activate(conn, user_id, day.date, now)
            chances = min(10, conn.execute("SELECT count(*) FROM active_days WHERE user_id=?", (user_id,)).fetchone()[0])
            return messages.reply(f"Ты в лайнапе ✅\n\nЗаявка на розыгрыш призов от {day.partner_name} зарегистрирована.\n\n"
                                  f"Накоплено шансов: {chances} из 10.\n\nВозвращайся завтра — на сцену выйдет новый партнер.")

    def chances(self, user_id: int, now: datetime) -> dict:
        with self.db.read() as conn:
            rows = conn.execute("SELECT day FROM active_days WHERE user_id=? ORDER BY day", (user_id,)).fetchall()
        visited = [f"{row[0]} — {self.campaign.day(row[0]).partner_name}" for row in rows]
        today = self.campaign.local_date(now)
        active = {row[0] for row in rows}
        remaining = sum(day.date >= today and day.date not in active for day in self.campaign.days)
        return messages.reply(
            f"Твой фестивальный пропуск\nНакоплено шансов: {min(10, len(rows))} из 10\n\n"
            "Чем больше дней розыгрыша ты посетишь, тем выше вероятность выиграть главный приз.\n\n"
            f"Пройденные дни и партнеры:\n{chr(10).join(visited) if visited else 'Пока нет активных дней.'}\n\n"
            f"Осталось доступных дней: {remaining}\nГлавный приз: {self.campaign.final_prize}\n"
            "В финале — 5 победителей, каждый получает 2 билета.")

    def draw(self, draw_id: str, now: datetime) -> bool:
        final = draw_id == "final"
        day = None if final else self.campaign.day(draw_id.removeprefix("daily:"))
        if not final and draw_id != f"daily:{day.date}":
            raise ValueError("ID розыгрыша должен быть final или daily:ГГГГ-ММ-ДД")
        if now < moment(self.campaign.final_draw_at if final else day.draw_at):
            raise ValueError("Время розыгрыша еще не наступило")
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM draws WHERE draw_id=?", (draw_id,)).fetchone():
                return False  # Повторный запуск не перевыбирает победителей.
            conn.execute("INSERT INTO draws(draw_id, kind, day, drawn_at) VALUES (?, ?, ?, ?)",
                         (draw_id, "final" if final else "daily", None if final else day.date, stamp(now)))
            if final:
                pool = conn.execute("SELECT user_id, min(count(*), 10) AS weight, min(activated_at) AS registered_at FROM active_days GROUP BY user_id ORDER BY user_id").fetchall()
                prizes = [(self.campaign.final_prize, self.campaign.final_instructions)] * 5
            else:
                pool = conn.execute("SELECT user_id, 1 AS weight, registered_at FROM registrations WHERE day=? ORDER BY user_id", (day.date,)).fetchall()
                prizes = [(prize.name, prize.instructions) for prize in day.prizes for _ in range(prize.count)]
            conn.executemany("INSERT INTO draw_pool VALUES (?, ?, ?, ?)",
                             [(draw_id, row["user_id"], row["weight"], row["registered_at"]) for row in pool])
            # Выбор пропорционален весам. После выбора все шансы человека удаляются.
            # Поэтому один человек не может занять два места в одном розыгрыше.
            candidates = [(row["user_id"], row["weight"]) for row in pool]
            winner_count = min(len(prizes), len(candidates))
            for position in range(min(len(candidates), winner_count + self.campaign.reserve_count)):
                ticket = self.randbelow(sum(weight for _, weight in candidates))
                for index, (user_id, weight) in enumerate(candidates):
                    ticket -= weight
                    if ticket < 0:
                        candidates.pop(index)
                        break
                role = "winner" if position < winner_count else "reserve"
                conn.execute("INSERT INTO draw_order VALUES (?, ?, ?, ?)", (draw_id, user_id, position, role))
                if role == "winner":
                    prize, instructions = prizes[position]
                    conn.execute("INSERT INTO awards(draw_id, slot, prize, instructions) VALUES (?, ?, ?, ?)",
                                 (draw_id, position, prize, instructions))
            return True

    def _offer(self, conn, award, user_id: int, now: datetime) -> None:
        deadline = now + timedelta(hours=self.campaign.claim_hours)
        offer_id = conn.execute("""INSERT INTO offers(award_id, draw_id, user_id, status, offered_at, deadline)
            VALUES (?, ?, ?, 'offered', ?, ?)""", (award["id"], award["draw_id"], user_id, stamp(now), stamp(deadline))).lastrowid
        if award["draw_id"] == "final":
            text = (f"Твой сентябрь выходит на главную сцену 🔥\nТы выиграл {award['prize']}!\n\n"
                    "Чтобы подтвердить получение приза, нажми кнопку ниже и оставь необходимые контактные данные.")
        else:
            day = self.campaign.day(award["draw_id"].removeprefix("daily:"))
            text = (f"Ты в числе хедлайнеров дня 🔥\n\nТы выиграл {award['prize']} от {day.partner_name}.\n\n"
                    f"Чтобы получить приз, {award['instructions']}\nНажми «Получить приз» для подтверждения.")
        deadline_local = deadline.astimezone(self.campaign.tz).strftime("%d.%m.%Y %H:%M %z")
        text += f"\n\nПодтверди получение до {deadline_local}. Срок ответа — {self.campaign.claim_hours} часов с момента предложения приза."
        self.enqueue(user_id, messages.reply(text, messages.claim_button(offer_id)), f"offer:{offer_id}", now, "winner")

    def approve(self, draw_id: str, admin_id: int, now: datetime) -> bool:
        with self.db.transaction() as conn:
            draw = conn.execute("SELECT * FROM draws WHERE draw_id=?", (draw_id,)).fetchone()
            if not draw:
                raise ValueError("Розыгрыш еще не проведён")
            if draw["status"] == "approved":
                return False
            conn.execute("UPDATE draws SET status='approved', approved_at=?, approved_by=? WHERE draw_id=?",
                         (stamp(now), admin_id, draw_id))
            awards = conn.execute("SELECT * FROM awards WHERE draw_id=? ORDER BY slot", (draw_id,)).fetchall()
            for award in awards:
                winner = conn.execute("SELECT user_id FROM draw_order WHERE draw_id=? AND position=?",
                                      (draw_id, award["slot"])).fetchone()[0]
                self._offer(conn, award, winner, now)
            return True

    def expire_offers(self, now: datetime) -> int:
        count = 0
        with self.db.transaction() as conn:
            expired = conn.execute("SELECT * FROM offers WHERE status='offered' AND deadline<=? ORDER BY id", (stamp(now),)).fetchall()
            for offer in expired:
                conn.execute("UPDATE offers SET status='expired' WHERE id=?", (offer["id"],))
                conn.execute("DELETE FROM claim_sessions WHERE offer_id=?", (offer["id"],))
                # Не отправляем предложение, срок которого уже истёк, после длительного простоя.
                conn.execute("UPDATE outbox SET status='skipped', error_code='offer_expired' WHERE dedupe_key LIKE ? AND status='pending'",
                             (f"offer:{offer['id']}:%",))
                reserve = conn.execute("""SELECT o.user_id FROM draw_order o
                    WHERE o.draw_id=? AND o.role='reserve' AND NOT EXISTS
                    (SELECT 1 FROM offers f WHERE f.draw_id=o.draw_id AND f.user_id=o.user_id)
                    ORDER BY o.position LIMIT 1""", (offer["draw_id"],)).fetchone()
                if reserve:
                    award = conn.execute("SELECT * FROM awards WHERE id=?", (offer["award_id"],)).fetchone()
                    self._offer(conn, award, reserve[0], now)
                count += 1
        return count

    def _valid_offer(self, conn, user_id: int, offer_id: int, now: datetime):
        return conn.execute("SELECT * FROM offers WHERE id=? AND user_id=? AND status='offered' AND deadline>?",
                            (offer_id, user_id, stamp(now))).fetchone()

    def claim_start(self, user_id: int, offer_id: int, now: datetime) -> dict:
        with self.db.transaction() as conn:
            if not self._valid_offer(conn, user_id, offer_id, now):
                claimed = conn.execute("SELECT 1 FROM offers WHERE id=? AND user_id=? AND status='claimed'", (offer_id, user_id)).fetchone()
                return messages.reply("Получение этого приза уже подтверждено ✅" if claimed else "Это предложение приза недоступно или срок ответа истёк.")
            conn.execute("INSERT OR REPLACE INTO claim_sessions(user_id, offer_id, step) VALUES (?, ?, 'first_name')", (user_id, offer_id))
            return messages.reply("Как тебя зовут? Напиши имя. Для выхода напиши «Отмена».")

    def claim_input(self, user_id: int, text: str, now: datetime) -> dict | None:
        with self.db.transaction() as conn:
            session = conn.execute("SELECT * FROM claim_sessions WHERE user_id=?", (user_id,)).fetchone()
            if not session:
                return None
            if not self._valid_offer(conn, user_id, session["offer_id"], now):
                conn.execute("DELETE FROM claim_sessions WHERE user_id=?", (user_id,))
                return messages.reply("Срок подтверждения приза истёк.")
            name = text.strip()
            if session["step"] == "consent":
                return self.consent_message(user_id, session["offer_id"])
            if not 1 <= len(name) <= 80 or not any(char.isalpha() for char in name) or any(not (char.isalpha() or char in " -'’") for char in name):
                return messages.reply("Напиши имя или фамилию буквами, не более 80 символов.")
            if session["step"] == "first_name":
                conn.execute("UPDATE claim_sessions SET first_name=?, step='last_name' WHERE user_id=?", (name, user_id))
                return messages.reply("Теперь напиши фамилию.")
            conn.execute("UPDATE claim_sessions SET last_name=?, step='consent' WHERE user_id=?", (name, user_id))
            return self.consent_message(user_id, session["offer_id"])

    def consent_message(self, user_id: int, offer_id: int) -> dict:
        with self.db.read() as conn:
            offer = conn.execute("SELECT draw_id FROM offers WHERE id=? AND user_id=?", (offer_id, user_id)).fetchone()
        recipient = self.campaign.final_recipient if offer[0] == "final" else self.campaign.day(offer[0].removeprefix("daily:")).partner_name
        text = self.campaign.consent_text.replace("{recipient}", recipient)
        return messages.reply(text, messages.consent_buttons(offer_id))

    def consent(self, user_id: int, offer_id: int, now: datetime) -> dict:
        with self.db.transaction() as conn:
            session = conn.execute("SELECT * FROM claim_sessions WHERE user_id=? AND offer_id=? AND step='consent'", (user_id, offer_id)).fetchone()
            offer = self._valid_offer(conn, user_id, offer_id, now)
            if not session or not offer:
                return messages.reply("Сначала нажми «Получить приз» и заполни имя и фамилию. Срок предложения должен быть действующим.")
            recipient = self.campaign.final_recipient if offer["draw_id"] == "final" else self.campaign.day(offer["draw_id"].removeprefix("daily:")).partner_name
            text = self.campaign.consent_text.replace("{recipient}", recipient)
            conn.execute("""UPDATE offers SET status='claimed', first_name=?, last_name=?, consent_text=?, consent_version=?, consent_at=? WHERE id=?""",
                         (session["first_name"], session["last_name"], text, self.campaign.consent_version, stamp(now), offer_id))
            conn.execute("DELETE FROM claim_sessions WHERE user_id=?", (user_id,))
            instructions = conn.execute("SELECT instructions FROM awards WHERE id=?", (offer["award_id"],)).fetchone()[0]
            return messages.reply(f"Получение приза подтверждено ✅\nСогласие сохранено.\n\n{instructions}")

    def cancel_claim(self, user_id: int) -> dict:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM claim_sessions WHERE user_id=?", (user_id,))
        return messages.reply("Ввод данных отменён. Подтвердить приз можно до истечения срока предложения.")

    def broadcast(self, key: str, text: str, admin_id: int, now: datetime) -> int:
        if not key or len(key) > 80 or not text.strip() or len(text) > 3500:
            raise ValueError("У рассылки должен быть уникальный ключ до 80 символов и текст до 3500 символов")
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT text FROM broadcasts WHERE campaign_key=?", (key,)).fetchone()
            if existing:
                if existing[0] != text:
                    raise ValueError("Этот ключ уже использован для другого текста")
                return 0
            conn.execute("INSERT INTO broadcasts VALUES (?, ?, ?, ?)", (key, text, stamp(now), admin_id))
            users = conn.execute("SELECT user_id FROM users WHERE started_at IS NOT NULL AND reachable=1").fetchall()
            for user in users:
                self.enqueue(user[0], messages.reply(text), f"broadcast:{key}:{user[0]}", now, "broadcast")
            return len(users)

    def tick(self, now: datetime) -> list[str]:
        created = []
        for day in self.campaign.days:
            if now >= moment(day.draw_at) and self.draw(f"daily:{day.date}", now):
                created.append(f"daily:{day.date}")
        if now >= moment(self.campaign.final_draw_at) and self.draw("final", now):
            created.append("final")
        self.expire_offers(now)
        return created

    def draw_summary(self, draw_id: str) -> dict:
        with self.db.read() as conn:
            draw = conn.execute("SELECT * FROM draws WHERE draw_id=?", (draw_id,)).fetchone()
            if not draw:
                raise ValueError("Розыгрыш не найден")
            pool = conn.execute("SELECT count(*), coalesce(sum(weight),0) FROM draw_pool WHERE draw_id=?", (draw_id,)).fetchone()
            selected = [dict(row) for row in conn.execute("SELECT user_id, position, role FROM draw_order WHERE draw_id=? ORDER BY position", (draw_id,))]
            offers = [dict(row) for row in conn.execute("SELECT id, user_id, status, deadline FROM offers WHERE draw_id=? ORDER BY id", (draw_id,))]
        expected = 5 if draw_id == "final" else sum(prize.count for prize in self.campaign.day(draw["day"]).prizes)
        winners_count = sum(row["role"] == "winner" for row in selected)
        return {**dict(draw), "participants": pool[0], "chances": pool[1], "expected_winners": expected,
                "unawarded_prizes": expected - winners_count, "selected": selected, "offers": offers}

    def statistics(self) -> dict:
        with self.db.read() as conn:
            users = conn.execute("SELECT started_at FROM users WHERE started_at IS NOT NULL").fetchall()
            new = {}
            for user in users:
                day = self.campaign.local_date(moment(user[0]))
                new[day] = new.get(day, 0) + 1
            daily = []
            for day in self.campaign.days:
                total, issued = conn.execute("SELECT count(*), count(user_id) FROM promo_codes WHERE day=?", (day.date,)).fetchone()
                requests = conn.execute("SELECT count(*) FROM promo_requests WHERE day=?", (day.date,)).fetchone()[0]
                participants = conn.execute("SELECT count(*) FROM registrations WHERE day=?", (day.date,)).fetchone()[0]
                daily.append({"day": day.date, "partner": day.partner_name, "promo_requests": requests,
                              "promos_issued": issued, "promos_remaining": total - issued, "participants": participants})
            statuses = {row[0]: row[1] for row in conn.execute("SELECT status, count(*) FROM outbox GROUP BY status")}
            total_requests = conn.execute("SELECT count(*) FROM promo_requests").fetchone()[0]
        return {"users_started": len(users), "new_users_by_day": new, "promo_requests": total_requests,
                "promos_issued": sum(day["promos_issued"] for day in daily), "days": daily, "messages": statuses}

    def export_draw(self, draw_id: str, path: str | Path) -> int:
        self.draw_summary(draw_id)
        with self.db.read() as conn:
            rows = conn.execute("""SELECT p.user_id, p.weight, p.registered_at, coalesce(o.role,'participant') AS initial_role,
                o.position, f.status, f.deadline,
                CASE WHEN f.status='claimed' THEN f.first_name ELSE '' END AS first_name,
                CASE WHEN f.status='claimed' THEN f.last_name ELSE '' END AS last_name,
                f.consent_at, f.consent_version, f.consent_text, coalesce(a.prize, initial_award.prize) AS prize,
                d.drawn_at, d.status AS draw_status, d.approved_at, d.approved_by
                FROM draw_pool p JOIN draws d ON d.draw_id=p.draw_id
                LEFT JOIN draw_order o ON o.draw_id=p.draw_id AND o.user_id=p.user_id
                LEFT JOIN offers f ON f.draw_id=p.draw_id AND f.user_id=p.user_id
                LEFT JOIN awards a ON a.id=f.award_id
                LEFT JOIN awards initial_award ON initial_award.draw_id=p.draw_id AND initial_award.slot=o.position
                WHERE p.draw_id=? ORDER BY p.user_id""", (draw_id,)).fetchall()
        headers = ["user_id", "weight", "registered_at", "initial_role", "position", "status", "deadline",
                   "first_name", "last_name", "consent_at", "consent_version", "consent_text", "prize",
                   "drawn_at", "draw_status", "approved_at", "approved_by"]
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(headers)
            for row in rows:
                writer.writerow([self._csv_value(value) for value in row])
        return len(rows)

    @staticmethod
    def _csv_value(value):
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return "" if value is None else value
