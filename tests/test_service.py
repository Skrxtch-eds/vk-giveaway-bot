from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
import csv
import sqlite3

from giveaway.config import moment, validate
from giveaway.db import Database
from giveaway.service import Service
from .helpers import BotTestCase


class PromoAndParticipationTests(BotTestCase):
    def test_same_day_returns_same_promo_and_counts_requests(self):
        day = self.campaign.days[0].date
        self.service.import_promos(day, ["FIRST", "SECOND"])
        first = self.service.promo(1, self.now)
        second = self.service.promo(1, self.now)
        self.assertEqual(first, second)
        self.assertIn("FIRST", first["message"])
        self.assertEqual(self.count("active_days"), 1)
        stats = self.service.statistics()
        self.assertEqual(stats["promo_requests"], 2)
        self.assertEqual(stats["promos_issued"], 1)
        self.assertEqual(stats["days"][0]["promos_remaining"], 1)

    def test_existing_promo_returned_even_when_stock_empty(self):
        day = self.campaign.days[0].date
        self.service.import_promos(day, ["LAST"])
        self.service.promo(1, self.now)
        self.assertIn("LAST", self.service.promo(1, self.now)["message"])

    def test_no_promo_does_not_block_registration(self):
        result = self.service.promo(1, self.now)
        self.assertIn("закончились", result["message"])
        self.assertEqual(self.count("active_days"), 0)
        self.service.register(1, self.campaign.days[0].date, True, self.now)
        self.assertEqual(self.count("registrations"), 1)
        self.assertEqual(self.count("active_days"), 1)

    def test_both_actions_give_only_one_chance_in_either_order(self):
        day = self.campaign.days[0].date
        self.service.import_promos(day, ["ONE", "TWO"])
        self.service.promo(1, self.now)
        self.service.register(1, day, True, self.now)
        self.service.register(2, day, True, self.now)
        self.service.promo(2, self.now)
        self.assertEqual(self.count("active_days"), 2)

    def test_failed_checks_never_register_and_retries_are_unlimited(self):
        day = self.campaign.days[0].date
        for _ in range(15):
            self.assertIn("Пока не вижу", self.service.register(1, day, False, self.now)["message"])
        self.assertEqual(self.count("registrations"), 0)
        self.assertEqual(self.count("active_days"), 0)
        self.service.register(1, day, True, self.now)
        self.assertEqual(self.count("registrations"), 1)

    def test_check_error_is_not_reported_as_no_subscription(self):
        result = self.service.register(1, self.campaign.days[0].date, None, self.now)
        self.assertIn("не удалось проверить", result["message"])
        self.assertEqual(self.count("registrations"), 0)

    def test_repeated_registration_never_adds_a_second_entry(self):
        day = self.campaign.days[0].date
        self.service.register(1, day, True, self.now)
        self.assertIn("уже участвуешь", self.service.register(1, day, True, self.now)["message"])
        self.assertEqual(self.count("registrations"), 1)

    def test_midnight_uses_campaign_timezone_and_rejects_old_button(self):
        day1, day2 = self.campaign.days[:2]
        self.service.import_promos(day1.date, ["BEFORE"])
        self.service.import_promos(day2.date, ["AFTER"])
        before = moment("2026-10-21T23:59:59.999999+03:00")
        after = before + timedelta(microseconds=1)
        self.assertIn("BEFORE", self.service.promo(1, before)["message"])
        self.assertIn("AFTER", self.service.promo(1, after)["message"])
        self.assertIn("сменился", self.service.register(1, day1.date, True, after)["message"])
        self.assertEqual(self.count("registrations"), 0)

    def test_ten_days_cap_and_chances_details(self):
        for index, day in enumerate(self.campaign.days):
            self.service.import_promos(day.date, [f"CODE-{index}"])
            self.service.promo(1, self.at_day(index))
            self.service.register(1, day.date, True, self.at_day(index))
        result = self.service.chances(1, self.at_day(10))["message"]
        self.assertIn("10 из 10", result)
        self.assertIn("Осталось доступных дней: 0", result)
        for day in self.campaign.days:
            self.assertIn(day.partner_name, result)
        self.assertEqual(self.count("active_days"), 10)

    def test_concurrent_last_code_has_one_owner(self):
        self.service.import_promos(self.campaign.days[0].date, ["LAST"])
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(lambda uid: self.service.promo(uid, self.now), range(1, 25)))
        self.assertEqual(self.query("SELECT count(*) FROM promo_codes WHERE user_id IS NOT NULL")[0][0], 1)
        self.assertEqual(self.count("active_days"), 1)

    def test_concurrent_requests_from_same_user_have_one_assignment(self):
        self.service.import_promos(self.campaign.days[0].date, [f"C{n}" for n in range(20)])
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(lambda _: self.service.promo(1, self.now), range(20)))
        self.assertTrue(all(reply == replies[0] for reply in replies))
        self.assertEqual(self.service.statistics()["promos_issued"], 1)

    def test_concurrent_registrations_are_idempotent(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.service.register(1, self.campaign.days[0].date, True, self.now), range(20)))
        self.assertEqual(self.count("registrations"), 1)
        self.assertEqual(self.count("active_days"), 1)

    def test_import_is_repeatable_and_deduplicated(self):
        day = self.campaign.days[0].date
        self.assertEqual(self.service.import_promos(day, ["A", "A", "B", ""]), 2)
        self.assertEqual(self.service.import_promos(day, ["A", "B"]), 0)
        with self.assertRaises(ValueError):
            self.service.import_promos(day, ["CODE WITH SPACE"])

    def test_actions_outside_campaign_do_not_add_chances(self):
        now = self.at_day(-1)
        self.service.promo(1, now)
        self.service.register(1, self.campaign.days[0].date, True, now)
        self.assertEqual(self.count("active_days"), 0)

    def test_new_user_statistics_use_local_day_and_do_not_recount(self):
        self.service.touch_user(1, moment("2026-10-21T22:30:00+00:00"))
        self.service.touch_user(1, self.at_day(3))
        stats = self.service.statistics()
        self.assertEqual(stats["users_started"], 1)
        self.assertEqual(stats["new_users_by_day"], {"2026-10-22": 1})


class DrawTests(BotTestCase):
    def test_daily_pool_requires_verified_registration(self):
        self.service.import_promos(self.campaign.days[0].date, ["PROMO-ONLY"])
        self.service.promo(99, self.now)
        key, now = self.daily_draw(4)
        self.assertEqual(self.service.draw_summary(key)["participants"], 4)
        self.assertNotIn(99, [row[0] for row in self.query("SELECT user_id FROM draw_pool")])

    def test_drawing_too_early_is_rejected(self):
        with self.assertRaises(ValueError):
            self.service.draw(f"daily:{self.campaign.days[0].date}", self.now)
        self.assertEqual(self.count("draws"), 0)

    def test_no_notifications_until_approval_and_no_reroll(self):
        key, now = self.daily_draw()
        snapshot = self.service.draw_summary(key)
        self.assertEqual(self.count("outbox"), 0)
        self.assertFalse(self.service.draw(key, now + timedelta(days=1)))
        self.assertEqual(self.service.draw_summary(key), snapshot)
        self.assertTrue(self.service.approve(key, 500, now))
        self.assertEqual(self.count("outbox"), 1)
        self.assertFalse(self.service.approve(key, 500, now))
        self.assertEqual(self.count("outbox"), 1)

    def test_final_is_weighted_and_each_person_wins_at_most_once(self):
        self.register_users(12)
        for index in range(1, 7):
            self.service.register(1, self.campaign.days[index].date, True, self.at_day(index))
        totals = []
        self.service.randbelow = lambda n: totals.append(n) or 0
        now = moment(self.campaign.final_draw_at)
        self.service.draw("final", now)
        result = self.service.draw_summary("final")
        self.assertEqual(result["chances"], 18)
        self.assertEqual(totals[:2], [18, 11])
        self.assertEqual(self.query("SELECT weight FROM draw_pool WHERE draw_id='final' AND user_id=1")[0][0], 7)
        winners = [row["user_id"] for row in result["selected"] if row["role"] == "winner"]
        reserves = [row["user_id"] for row in result["selected"] if row["role"] == "reserve"]
        self.assertEqual(len(set(winners)), 5)
        self.assertEqual(len(set(reserves)), 5)
        self.assertFalse(set(winners) & set(reserves))
        self.service.approve("final", 500, now)
        self.assertEqual(self.count("offers"), 5)
        self.assertTrue(all("два билета" in row[0] for row in self.query("SELECT body FROM outbox")))

    def test_insufficient_participants_never_get_duplicate_prizes(self):
        self.register_users(2)
        self.service.draw("final", moment(self.campaign.final_draw_at))
        result = self.service.draw_summary("final")
        self.assertEqual(len(result["selected"]), 2)
        self.assertEqual(self.count("awards"), 2)

    def test_empty_draw_is_saved_without_fabricating_winners(self):
        self.service.draw("final", moment(self.campaign.final_draw_at))
        self.assertEqual(self.service.draw_summary("final")["selected"], [])

    def test_clock_rollback_cannot_change_a_finished_pool(self):
        key, now = self.daily_draw(2)
        self.service.import_promos(self.campaign.days[0].date, ["LATE"])
        self.service.promo(99, self.now)
        self.service.register(99, self.campaign.days[0].date, True, self.now)
        self.assertEqual(self.service.draw_summary(key)["participants"], 2)
        self.assertEqual(self.count("registrations"), 2)
        self.assertEqual(self.count("active_days"), 2)

    def test_concurrent_draws_and_approvals_do_not_duplicate(self):
        self.register_users()
        now = moment(self.campaign.final_draw_at)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.service.draw("final", now), range(12)))
        self.assertEqual(sum(results), 1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.service.approve("final", 500, now), range(12)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.count("offers"), 5)
        self.assertEqual(self.count("outbox"), 5)

    def test_restart_preserves_results_approvals_and_pending_messages(self):
        key, now = self.daily_draw()
        self.service.approve(key, 500, now)
        snapshot = self.service.draw_summary(key)
        restarted = Service(Database(self.path / "test.sqlite3"), self.campaign)
        self.assertEqual(restarted.draw_summary(key), snapshot)
        self.assertFalse(restarted.draw(key, now))
        self.assertEqual(self.count("outbox"), 1)

    def test_tick_runs_due_draws_once(self):
        self.register_users()
        now = moment(self.campaign.final_draw_at)
        self.assertEqual(len(self.service.tick(now)), 11)
        self.assertEqual(self.service.tick(now), [])
        self.assertEqual(self.count("outbox"), 0)

    def test_export_snapshot_has_participants_weights_and_roles(self):
        key, now = self.daily_draw()
        path = self.path / "results.csv"
        self.assertEqual(self.service.export_draw(key, path), 12)
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter=";"))
        self.assertEqual(len(rows), 12)
        self.assertEqual(sum(row["initial_role"] == "winner" for row in rows), 1)
        self.assertEqual(sum(row["initial_role"] == "reserve" for row in rows), 5)
        self.assertEqual({row["draw_status"] for row in rows}, {"draft"})

    def test_backup_contains_consistent_database(self):
        key, now = self.daily_draw()
        backup = self.path / "backup.sqlite3"
        self.db.backup(backup)
        with closing(sqlite3.connect(backup)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM draws").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_campaign_settings_cannot_change_under_existing_participants(self):
        self.register_users(1)
        with self.assertRaises(ValueError):
            Service(self.db, replace(self.campaign, claim_hours=24))


class ClaimTests(BotTestCase):
    def setUp(self):
        super().setUp()
        self.key, self.offer_time = self.daily_draw()
        self.service.approve(self.key, 500, self.offer_time)
        self.offer = self.query("SELECT * FROM offers")[0]

    def fill_names(self, user_id=1):
        offer_id = self.offer["id"]
        self.service.claim_start(user_id, offer_id, self.offer_time)
        self.service.claim_input(user_id, "Иван", self.offer_time)
        return self.service.claim_input(user_id, "Иванов", self.offer_time)

    def test_name_surname_and_explicit_consent_are_saved(self):
        result = self.fill_names()
        self.assertIn("Тестовый партнер 1", result["message"])
        self.assertEqual(self.query("SELECT status FROM offers")[0][0], "offered")
        self.service.consent(1, self.offer["id"], self.offer_time)
        offer = self.query("SELECT * FROM offers")[0]
        self.assertEqual(offer["status"], "claimed")
        self.assertEqual((offer["first_name"], offer["last_name"]), ("Иван", "Иванов"))
        self.assertEqual(offer["consent_version"], "demo-1")
        self.assertIn("Тестовый партнер 1", offer["consent_text"])
        self.assertTrue(offer["consent_at"])
        self.assertEqual(self.count("claim_sessions"), 0)

    def test_other_user_cannot_claim_or_consent_for_winner(self):
        self.fill_names()
        self.service.claim_start(2, self.offer["id"], self.offer_time)
        self.service.consent(2, self.offer["id"], self.offer_time)
        self.assertEqual(self.query("SELECT status FROM offers")[0][0], "offered")
        self.assertEqual(self.count("claim_sessions"), 1)

    def test_consent_cannot_skip_names(self):
        self.service.consent(1, self.offer["id"], self.offer_time)
        self.assertEqual(self.query("SELECT status FROM offers")[0][0], "offered")

    def test_deadline_is_exactly_48_hours_and_promotes_first_reserve(self):
        deadline = moment(self.offer["deadline"])
        self.assertEqual(deadline - self.offer_time, timedelta(hours=48))
        self.assertEqual(self.service.expire_offers(deadline - timedelta(microseconds=1)), 0)
        self.assertEqual(self.service.expire_offers(deadline), 1)
        offers = self.query("SELECT * FROM offers ORDER BY id")
        self.assertEqual([row["status"] for row in offers], ["expired", "offered"])
        self.assertEqual(offers[1]["user_id"], 2)
        self.assertEqual(moment(offers[1]["deadline"]), deadline + timedelta(hours=48))
        self.assertEqual(self.service.expire_offers(deadline), 0)

    def test_claim_after_deadline_is_rejected_even_before_background_tick(self):
        self.fill_names()
        self.service.consent(1, self.offer["id"], moment(self.offer["deadline"]))
        self.assertEqual(self.query("SELECT status FROM offers")[0][0], "offered")
        self.assertIsNone(self.query("SELECT consent_at FROM offers")[0][0])

    def test_claimed_prize_is_never_reassigned(self):
        self.fill_names()
        self.service.consent(1, self.offer["id"], self.offer_time)
        self.assertEqual(self.service.expire_offers(self.offer_time + timedelta(days=10)), 0)
        self.assertEqual(self.count("offers"), 1)

    def test_reserves_exhaust_without_reusing_previous_winners(self):
        now = self.offer_time
        for _ in range(8):
            now += timedelta(hours=48)
            self.service.expire_offers(now)
        offers = self.query("SELECT user_id, status FROM offers")
        self.assertEqual(len(offers), 6)
        self.assertEqual(len({row[0] for row in offers}), 6)
        self.assertEqual({row[1] for row in offers}, {"expired"})

    def test_cancel_clears_partial_names_and_does_not_confirm(self):
        self.fill_names()
        self.service.cancel_claim(1)
        self.assertEqual(self.count("claim_sessions"), 0)
        self.assertEqual(self.query("SELECT status FROM offers")[0][0], "offered")

    def test_invalid_names_are_not_saved(self):
        self.service.claim_start(1, self.offer["id"], self.offer_time)
        result = self.service.claim_input(1, "=SUM(1+1)", self.offer_time)
        self.assertIn("буквами", result["message"])
        self.assertIsNone(self.query("SELECT first_name FROM claim_sessions")[0][0])

    def test_claim_export_has_only_consented_names(self):
        self.fill_names()
        path = self.path / "before.csv"
        self.service.export_draw(self.key, path)
        self.assertNotIn("Иванов", path.read_text(encoding="utf-8-sig"))
        self.service.consent(1, self.offer["id"], self.offer_time)
        self.service.export_draw(self.key, path)
        self.assertIn("Иванов", path.read_text(encoding="utf-8-sig"))


class ConfigTests(BotTestCase):
    def test_empty_calendar_duplicate_dates_and_early_final_are_rejected(self):
        with self.assertRaises(ValueError):
            validate(replace(self.campaign, days=()))
        repeated = (self.campaign.days[0], replace(self.campaign.days[1], date=self.campaign.days[0].date), *self.campaign.days[2:])
        with self.assertRaises(ValueError):
            validate(replace(self.campaign, days=repeated))
        with self.assertRaises(ValueError):
            validate(replace(self.campaign, final_draw_at=self.now.isoformat()))
