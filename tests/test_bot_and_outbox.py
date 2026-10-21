from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from unittest.mock import patch

from giveaway.bot import Bot
from giveaway.outbox import OutboxWorker
from giveaway.service import Service, stamp
from giveaway.vk import TransportError, VkClient, VkError
from .helpers import BotTestCase, FakeVk


class EventTests(BotTestCase):
    def setUp(self):
        super().setUp()
        self.bot = Bot(self.service, self.vk, 99, [500], clock=lambda: self.now)

    def test_start_shows_required_menu(self):
        self.bot.handle(self.event())
        body = json.loads(self.query("SELECT body FROM outbox")[0][0])
        self.assertEqual(body["message"], "Добро пожаловать в Бот розыгрышей")
        labels = [button["action"]["label"] for row in body["keyboard"]["buttons"] for button in row]
        self.assertEqual(labels, ["Получить промокод", "Участвовать в розыгрыше", "Как участвовать", "Мои шансы", "Правила акции"])

    def test_duplicate_events_are_processed_once_even_after_restart(self):
        self.service.import_promos(self.campaign.days[0].date, ["FIRST", "SECOND"])
        event = self.event(payload={"action": "promo"})
        self.assertTrue(self.bot.handle(event))
        restarted = Bot(Service(self.db, self.campaign), self.vk, 99, clock=lambda: self.now)
        self.assertFalse(restarted.handle(event))
        self.assertEqual(self.count("promo_requests"), 1)
        self.assertEqual(self.count("outbox"), 1)

    def test_concurrent_duplicate_events_have_one_effect(self):
        event = self.event(payload={"action": "promo"})
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.bot.handle(event), range(16)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.count("promo_requests"), 1)
        self.assertEqual(self.count("outbox"), 1)

    def test_atomic_event_processing_rolls_back_promo_if_reply_is_not_saved(self):
        self.service.import_promos(self.campaign.days[0].date, ["CODE"])
        event = self.event(payload={"action": "promo"})
        with patch.object(self.service, "enqueue", side_effect=RuntimeError("simulated crash")):
            with self.assertRaises(RuntimeError):
                self.bot.handle(event)
        self.assertEqual(self.count("incoming_events"), 0)
        self.assertEqual(self.count("promo_requests"), 0)
        self.assertIsNone(self.query("SELECT user_id FROM promo_codes")[0][0])
        self.assertTrue(self.bot.handle(event))
        self.assertEqual(self.service.statistics()["promos_issued"], 1)

    def test_subscription_is_checked_again_after_user_retries(self):
        day = self.campaign.days[0]
        self.vk.membership = False
        self.bot.handle(self.event(payload={"action": "check", "day": day.date}))
        self.assertEqual(self.count("registrations"), 0)
        self.vk.membership = True
        self.bot.handle(self.event(payload={"action": "check", "day": day.date}, identity="retry"))
        self.assertEqual(self.count("registrations"), 1)
        self.assertEqual(self.vk.membership_calls, 2)

    def test_subscription_network_failure_does_not_award_chance(self):
        self.vk.error = TransportError("test")
        self.bot.handle(self.event(payload={"action": "check", "day": self.campaign.days[0].date}))
        body = json.loads(self.query("SELECT body FROM outbox")[0][0])
        self.assertIn("не удалось проверить", body["message"])
        self.assertEqual(self.count("active_days"), 0)

    def test_day_is_rechecked_after_network_request_crosses_midnight(self):
        before = self.at_day(0).replace(hour=20, minute=59, second=59)
        after = before + timedelta(seconds=2)  # 00:00:01 московского времени.
        times = iter([before, after])
        self.bot.clock = lambda: next(times)
        self.bot.handle(self.event(payload={"action": "check", "day": self.campaign.days[0].date}))
        self.assertEqual(self.count("registrations"), 0)

    def test_repeated_registered_check_does_not_call_vk_again(self):
        day = self.campaign.days[0].date
        self.service.register(1, day, True, self.now)
        self.bot.handle(self.event(payload={"action": "check", "day": day}))
        self.assertEqual(self.vk.membership_calls, 0)

    def test_admin_privileges_are_checked_on_each_command(self):
        key, now = self.daily_draw()
        self.now = now
        self.bot.handle(self.event(text=f"/approve {key}"))
        self.assertEqual(self.query("SELECT status FROM draws")[0][0], "draft")
        self.bot.handle(self.event(user_id=500, text=f"/approve {key}", identity="admin"))
        self.assertEqual(self.query("SELECT status FROM draws")[0][0], "approved")

    def test_foreign_community_group_chat_and_negative_sender_are_ignored(self):
        foreign = self.event()
        foreign["group_id"] = 100
        self.assertFalse(self.bot.handle(foreign))
        chat = self.event()
        chat["object"]["message"]["peer_id"] = 2000000001
        self.assertFalse(self.bot.handle(chat))
        self.assertFalse(self.bot.handle(self.event(user_id=-1)))
        self.assertEqual(self.count("users"), 0)

    def test_message_permission_event_does_not_count_as_started_user(self):
        self.bot.handle({"group_id": 99, "event_id": "allow", "type": "message_allow", "object": {"user_id": 1}})
        self.assertEqual(self.service.statistics()["users_started"], 0)
        self.bot.handle({"group_id": 99, "event_id": "deny", "type": "message_deny", "object": {"user_id": 1}})
        self.assertEqual(self.query("SELECT reachable FROM users")[0][0], 0)

    def test_invalid_payload_is_safe(self):
        event = self.event()
        event["object"]["message"]["payload"] = "{invalid json"
        self.assertTrue(self.bot.handle(event))


class OutboxTests(BotTestCase):
    def setUp(self):
        super().setUp()
        self.service.touch_user(1, self.now)
        self.worker = OutboxWorker(self.service, self.vk, clock=lambda: self.now)

    def enqueue(self, kind="reply", key="test"):
        self.service.enqueue(1, {"message": "test", "keyboard": {"buttons": []}}, key, self.now, kind)

    def test_successful_message_is_not_sent_twice(self):
        self.enqueue()
        self.assertTrue(self.worker.send_one())
        self.assertFalse(self.worker.send_one())
        self.assertEqual(len(self.vk.sent), 1)
        self.assertEqual(self.query("SELECT status FROM outbox")[0][0], "sent")

    def test_uncertain_network_delivery_retries_with_same_random_id(self):
        self.enqueue()
        self.vk.error = TransportError("network")
        self.worker.send_one()
        row = self.query("SELECT * FROM outbox")[0]
        self.assertEqual(row["status"], "pending")
        self.assertFalse(self.worker.send_one())
        self.now += timedelta(seconds=11)
        self.vk.error = None
        self.worker.send_one()
        self.assertEqual(self.vk.sent[0][1], row["random_id"])
        self.assertEqual(self.query("SELECT attempts FROM outbox")[0][0], 2)

    def test_crash_after_delivery_recovers_lease_and_keeps_random_id(self):
        self.enqueue()
        with patch.object(self.worker, "_finish", side_effect=RuntimeError("crash before acknowledgement")):
            with self.assertRaises(RuntimeError):
                self.worker.send_one()
        first = self.vk.sent[0]
        self.assertEqual(self.query("SELECT status FROM outbox")[0][0], "sending")
        self.now += timedelta(minutes=3)
        restarted = OutboxWorker(Service(self.db, self.campaign), self.vk, clock=lambda: self.now)
        restarted.send_one()
        self.assertEqual(self.vk.sent[-1][1], first[1])
        self.assertEqual(self.query("SELECT status FROM outbox")[0][0], "sent")

    def test_permission_revoked_skips_campaign_message(self):
        self.enqueue(kind="broadcast")
        self.vk.allowed = False
        self.worker.send_one()
        self.assertEqual(len(self.vk.sent), 0)
        self.assertEqual(self.query("SELECT status FROM outbox")[0][0], "skipped")
        self.assertEqual(self.query("SELECT reachable FROM users WHERE user_id=1")[0][0], 0)

    def test_vk_rate_limit_reschedules_without_losing_message(self):
        self.enqueue()
        self.vk.error = VkError(6)
        self.worker.send_one()
        self.assertEqual(self.query("SELECT status FROM outbox")[0][0], "pending")

    def test_permanent_token_error_requires_manual_retry(self):
        self.enqueue()
        self.vk.error = VkError(5)
        self.worker.send_one()
        self.assertEqual(self.query("SELECT status FROM outbox")[0][0], "failed")
        self.assertEqual(self.worker.retry_failed(), 1)
        self.vk.error = None
        self.worker.send_one()
        self.assertEqual(self.query("SELECT status FROM outbox")[0][0], "sent")

    def test_expired_winner_message_is_not_sent(self):
        key, now = self.daily_draw()
        self.service.approve(key, 500, now)
        self.now = now + timedelta(hours=49)
        self.worker.send_one()
        self.assertEqual(len(self.vk.sent), 0)
        self.assertEqual(self.query("SELECT status FROM outbox")[0][0], "skipped")

    def test_multiple_workers_do_not_claim_same_message(self):
        self.enqueue()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.worker.send_one(), range(16)))
        self.assertEqual(len(self.vk.sent), 1)

    def test_broadcast_has_stable_key_and_excludes_denied_and_unstarted(self):
        self.service.touch_user(2, self.now)
        self.service.set_reachable(2, False, self.now)
        self.service.set_reachable(3, True, self.now)
        self.assertEqual(self.service.broadcast("festival", "hello", 500, self.now), 1)
        self.assertEqual(self.service.broadcast("festival", "hello", 500, self.now), 0)
        self.assertEqual(self.count("outbox"), 1)
        with self.assertRaises(ValueError):
            self.service.broadcast("festival", "changed", 500, self.now)

    def test_long_rules_are_split_into_vk_sized_messages(self):
        self.service.enqueue(1, {"message": "я" * 9000, "keyboard": {"buttons": []}}, "rules", self.now)
        bodies = [json.loads(row[0]) for row in self.query("SELECT body FROM outbox ORDER BY id")]
        self.assertEqual(len(bodies), 3)
        self.assertTrue(all(len(body["message"]) <= 3500 for body in bodies))
        self.assertEqual(sum(len(body["message"]) for body in bodies), 9000)


class VkAdapterTests(BotTestCase):
    def test_membership_response_formats_and_errors(self):
        vk = VkClient("test-token", 99)
        for result in (1, {"member": 1}):
            with patch.object(vk, "call", return_value=result):
                self.assertTrue(vk.is_member(self.campaign.days[0], 1))
        for result in (0, {"member": 0}):
            with patch.object(vk, "call", return_value=result):
                self.assertFalse(vk.is_member(self.campaign.days[0], 1))
        with patch.object(vk, "call", return_value={}):
            with self.assertRaises(TransportError):
                vk.is_member(self.campaign.days[0], 1)

    def test_partner_specific_token_is_used(self):
        vk = VkClient("main-token", 99, partner_tokens={"partner_1": "partner-token"})
        with patch.object(vk, "call", return_value=1) as call:
            vk.is_member(self.campaign.days[0], 1)
        self.assertEqual(call.call_args.kwargs["token"], "partner-token")

    def test_error_does_not_expose_token_or_request_parameters(self):
        vk = VkClient("very-secret-token", 99)
        with patch.object(vk, "_json", return_value={"error": {"error_code": 5, "request_params": ["very-secret-token"]}}):
            with self.assertRaises(VkError) as caught:
                vk.call("groups.isMember")
        self.assertNotIn("very-secret-token", str(caught.exception))

    def test_csv_formula_injection_is_escaped(self):
        for value in ("=SUM(1+1)", "+cmd", "-cmd", "@cmd", "  =formula"):
            self.assertTrue(Service._csv_value(value).startswith("'"))
