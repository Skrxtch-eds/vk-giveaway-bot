"""Проверки ограничений и формулировок из технического задания."""
from dataclasses import replace
from datetime import date, timedelta
import json

from giveaway.bot import Bot
from giveaway.config import Prize, moment, validate
from giveaway.service import Service
from .helpers import BotTestCase


class RequirementTests(BotTestCase):
    def use_campaign(self, campaign):
        validate(campaign)
        self.campaign = campaign
        self.service = Service(self.db, campaign, randbelow=lambda n: 0)

    def repeating_partner(self):
        first, second, *others = self.campaign.days
        second = replace(second, partner_id=first.partner_id, partner_name=first.partner_name, group_id=first.group_id)
        self.use_campaign(replace(self.campaign, days=(first, second, *others)))
        return first, second

    def test_calendar_does_not_require_exactly_ten_days(self):
        self.use_campaign(replace(self.campaign, days=self.campaign.days[:3]))
        self.register_users(1)
        self.assertEqual(self.count("registrations"), 1)

    def test_more_than_ten_active_days_still_give_at_most_ten_chances(self):
        last = self.campaign.days[-1]
        extra = replace(last, date=(date.fromisoformat(last.date) + timedelta(days=1)).isoformat(),
                        partner_id="partner_11", partner_name="Партнер 11", group_id=2000,
                        draw_at=(moment(last.draw_at) + timedelta(days=1)).isoformat())
        final_at = moment(extra.draw_at) + timedelta(minutes=5)
        self.use_campaign(replace(self.campaign, days=(*self.campaign.days, extra), final_draw_at=final_at.isoformat()))
        for index, day in enumerate(self.campaign.days):
            result = self.service.register(1, day.date, True, self.at_day(index))
        self.service.register(2, self.campaign.days[0].date, True, self.now)
        self.assertIn("10 из 10", result["message"])
        self.assertIn("10 из 10", self.service.chances(1, final_at)["message"])
        self.assertEqual(self.query("SELECT count(*) FROM active_days WHERE user_id=1")[0][0], 11)
        self.service.draw("final", final_at)
        self.assertEqual(self.query("SELECT weight FROM draw_pool WHERE draw_id='final' AND user_id=1")[0][0], 10)
        self.assertEqual(self.service.draw_summary("final")["chances"], 11)

    def test_repeated_partner_cannot_register_same_user_on_another_day(self):
        first, second = self.repeating_partner()
        self.service.register(1, first.date, True, self.now)
        result = self.service.register(1, second.date, True, self.at_day(1))
        self.assertIn("этого партнера уже зарегистрирована", result["message"])
        self.assertEqual(self.count("registrations"), 1)
        self.assertEqual(self.count("active_days"), 1)
        self.service.import_promos(second.date, ["NEW-DAY-CODE"])
        self.service.promo(1, self.at_day(1))
        self.assertEqual(self.count("active_days"), 2)

    def test_repeated_partner_keeps_participation_message_and_skips_unneeded_api_check(self):
        first, second = self.repeating_partner()
        self.service.register(1, first.date, True, self.now)
        result = self.service.participate(1, self.at_day(1))
        self.assertIn("этого партнера уже зарегистрирована", result["message"])
        bot = Bot(self.service, self.vk, 99, clock=lambda: self.at_day(1))
        bot.handle(self.event(payload={"action": "check", "day": second.date}))
        self.assertEqual(self.vk.membership_calls, 0)

    def test_same_partner_code_cannot_be_loaded_for_two_days(self):
        first, second = self.repeating_partner()
        self.assertEqual(self.service.import_promos(first.date, ["SHARED", "FIRST"]), 2)
        self.assertEqual(self.service.import_promos(second.date, ["SHARED", "SECOND"]), 1)
        self.assertEqual(self.count("promo_codes"), 3)

    def test_different_partners_can_have_the_same_code_text(self):
        first, second = self.campaign.days[:2]
        self.assertEqual(self.service.import_promos(first.date, ["SAME"]), 1)
        self.assertEqual(self.service.import_promos(second.date, ["SAME"]), 1)

    def test_daily_prize_types_and_winner_counts_are_independent_for_each_partner(self):
        first = replace(self.campaign.days[0], prizes=(Prize("Приз А", 2, "Инструкция А"), Prize("Приз Б", 1, "Инструкция Б")))
        self.use_campaign(replace(self.campaign, days=(first, *self.campaign.days[1:])))
        key, now = self.daily_draw()
        selected = self.service.draw_summary(key)["selected"]
        winners = [item["user_id"] for item in selected if item["role"] == "winner"]
        self.assertEqual(len(set(winners)), 3)
        self.service.approve(key, 500, now)
        prizes = [row[0] for row in self.query("SELECT prize FROM awards ORDER BY slot")]
        self.assertEqual(prizes, ["Приз А", "Приз А", "Приз Б"])
        self.assertEqual(self.count("offers"), 3)

    def test_welcome_contains_configured_banner(self):
        self.use_campaign(replace(self.campaign, banner_attachment="photo-99_1"))
        Bot(self.service, self.vk, 99, clock=lambda: self.now).handle(self.event())
        body = json.loads(self.query("SELECT body FROM outbox")[0][0])
        self.assertEqual(body["attachment"], "photo-99_1")
        self.assertEqual(body["message"], "Добро пожаловать в Бот розыгрышей")

    def test_administration_alone_does_not_count_as_user_start(self):
        bot = Bot(self.service, self.vk, 99, admin_ids=[500], clock=lambda: self.now)
        bot.handle(self.event(user_id=500, text="/stats"))
        self.assertEqual(self.service.statistics()["users_started"], 0)
        bot.handle(self.event(user_id=500, text="Начать", identity="start"))
        self.assertEqual(self.service.statistics()["users_started"], 1)

    def test_final_notification_uses_configured_prize(self):
        self.use_campaign(replace(self.campaign, final_prize="два билета на фестиваль"))
        self.register_users(5)
        now = moment(self.campaign.final_draw_at)
        self.service.draw("final", now)
        self.service.approve("final", 500, now)
        text = json.loads(self.query("SELECT body FROM outbox ORDER BY id LIMIT 1")[0][0])["message"]
        self.assertIn("Ты выиграл два билета на фестиваль!", text)
