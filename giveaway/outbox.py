"""Устойчивая очередь отправки. Один random_id сохраняется для всех повторных попыток."""
from datetime import timedelta
import json

from .service import stamp, utcnow
from .vk import TransportError, VkError


class OutboxWorker:
    def __init__(self, service, vk, clock=utcnow):
        self.service, self.vk, self.clock = service, vk, clock

    def send_one(self) -> bool:
        now = self.clock()
        with self.service.db.transaction() as conn:
            # Возвращаем задания, прерванные аварийным завершением процесса.
            conn.execute("UPDATE outbox SET status='pending' WHERE status='sending' AND lease_until<=?", (stamp(now),))
            item = conn.execute("SELECT * FROM outbox WHERE status='pending' AND next_attempt_at<=? ORDER BY id LIMIT 1", (stamp(now),)).fetchone()
            if not item:
                return False
            if item["kind"] == "winner":
                offer_id = int(item["dedupe_key"].split(":")[1])
                offered = conn.execute("SELECT 1 FROM offers WHERE id=? AND status='offered' AND deadline>?", (offer_id, stamp(now))).fetchone()
                if not offered:
                    conn.execute("UPDATE outbox SET status='skipped', error_code='offer_inactive' WHERE id=?", (item["id"],))
                    return True
            conn.execute("UPDATE outbox SET status='sending', attempts=attempts+1, lease_until=? WHERE id=?",
                         (stamp(now + timedelta(minutes=2)), item["id"]))
        # Сетевых вызовов внутри транзакции нет: пользовательские действия не ждут отправки.
        try:
            if item["kind"] in ("winner", "broadcast") and not self.vk.messages_allowed(item["user_id"]):
                self._finish(item["id"], "skipped", "messages_denied", reachable=False)
                return True
            self.vk.send(item["user_id"], item["random_id"], json.loads(item["body"]))
        except VkError as error:
            if error.code in (7, 15, 18, 900, 901, 902):
                self._finish(item["id"], "skipped", str(error.code), reachable=False)
            elif error.code in (5, 27, 28, 100, 911, 914):
                self._finish(item["id"], "failed", str(error.code))
            else:
                self._retry(item, str(error.code))
        except TransportError:
            self._retry(item, "network")
        else:
            # Если процесс упадёт до этой записи, повторная попытка использует тот же random_id.
            self._finish(item["id"], "sent")
        return True

    def _finish(self, item_id: int, status: str, error: str | None = None, reachable: bool | None = None):
        with self.service.db.transaction() as conn:
            conn.execute("UPDATE outbox SET status=?, delivered_at=?, error_code=?, lease_until=NULL WHERE id=?",
                         (status, stamp(self.clock()) if status == "sent" else None, error, item_id))
            if reachable is not None:
                conn.execute("UPDATE users SET reachable=? WHERE user_id=(SELECT user_id FROM outbox WHERE id=?)",
                             (int(reachable), item_id))

    def _retry(self, item, reason: str):
        attempts = item["attempts"] + 1
        delay = min(3600, 5 * 2 ** min(attempts, 10))
        if reason in ("9", "29"):
            delay = max(60, delay)
        with self.service.db.transaction() as conn:
            conn.execute("""UPDATE outbox SET status=?, next_attempt_at=?, lease_until=NULL, error_code=? WHERE id=?""",
                         ("failed" if attempts >= 12 else "pending", stamp(self.clock() + timedelta(seconds=delay)), reason, item["id"]))

    def retry_failed(self) -> int:
        with self.service.db.transaction() as conn:
            cursor = conn.execute("UPDATE outbox SET status='pending', attempts=0, next_attempt_at=?, error_code=NULL WHERE status='failed'",
                                  (stamp(self.clock()),))
            return cursor.rowcount
