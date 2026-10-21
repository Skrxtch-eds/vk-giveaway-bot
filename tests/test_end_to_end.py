from dataclasses import asdict, replace
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from giveaway.bot import Bot
from giveaway.config import moment
from giveaway.outbox import OutboxWorker
from giveaway.vk import VkClient
from .helpers import BotTestCase


class EndToEndTests(BotTestCase):
    def test_full_ten_day_campaign_through_bot_events(self):
        bot = Bot(self.service, self.vk, 99, [500], clock=lambda: self.now)
        number = 0

        def send(user_id, action, **payload):
            nonlocal number
            number += 1
            return bot.handle(self.event(user_id=user_id, payload={"action": action, **payload}, identity=f"flow-{number}"))

        for index, day in enumerate(self.campaign.days):
            self.now = self.at_day(0) if index == 0 else moment(f"{day.date}T12:00:00+03:00")
            self.service.import_promos(day.date, [f"DAY-{index}-{code}" for code in range(3)])
            for user_id in range(1, 13):
                send(user_id, "start")
                send(user_id, "promo")
                send(user_id, "check", day=day.date)
        self.assertEqual(self.count("registrations"), 120)
        self.assertEqual(self.count("active_days"), 120)
        self.assertEqual(self.service.statistics()["promos_issued"], 30)
        self.now = moment(self.campaign.final_draw_at) + timedelta(seconds=1)
        self.assertEqual(len(self.service.tick(self.now)), 11)
        self.assertEqual(self.query("SELECT count(*) FROM outbox WHERE kind='winner'")[0][0], 0)
        for draw_id in [f"daily:{day.date}" for day in self.campaign.days] + ["final"]:
            self.service.approve(draw_id, 500, self.now)
        summary = self.service.draw_summary("final")
        self.assertEqual(summary["chances"], 120)
        self.assertEqual(len([row for row in summary["selected"] if row["role"] == "winner"]), 5)
        self.assertEqual(self.query("SELECT count(*) FROM outbox WHERE kind='winner'")[0][0], 15)
        final_offer = self.query("SELECT id, user_id FROM offers WHERE draw_id='final' ORDER BY id LIMIT 1")[0]
        send(final_offer["user_id"], "claim", offer_id=final_offer["id"])
        for value in ("Иван", "Иванов"):
            number += 1
            bot.handle(self.event(user_id=final_offer["user_id"], text=value, identity=f"flow-{number}"))
        send(final_offer["user_id"], "consent", offer_id=final_offer["id"])
        self.assertEqual(self.query("SELECT status FROM offers WHERE id=?", (final_offer["id"],))[0][0], "claimed")
        self.now += timedelta(hours=48)
        self.service.expire_offers(self.now)
        self.assertEqual(self.query("SELECT status FROM offers WHERE id=?", (final_offer["id"],))[0][0], "claimed")
        self.assertEqual(self.query("SELECT count(*) FROM offers WHERE draw_id='final' AND status='offered'")[0][0], 4)
        self.service.export_draw("final", self.path / "final.csv")
        self.assertIn("Иванов", (self.path / "final.csv").read_text(encoding="utf-8-sig"))


class CliTests(BotTestCase):
    def cli(self, *args, text=None):
        root = Path(__file__).resolve().parents[1]
        env = {**os.environ, "PYTHONPATH": str(root), "PYTHONUTF8": "1"}
        return subprocess.run([sys.executable, "-X", "utf8", "-m", "giveaway", *args],
                              cwd=self.path, input=text, capture_output=True, text=True,
                              encoding="utf-8", env=env, timeout=60)

    def test_config_generation_validation_and_invalid_live_time(self):
        result = self.cli("init-demo", "--output", "campaign.json", "--start", "2026-10-21")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        checked = self.cli("--config", "campaign.json", "check")
        self.assertEqual(checked.returncode, 0)
        self.assertIn("Дней: 10", checked.stdout)
        path = self.path / "campaign.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["demo"] = False
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        result = self.cli("--config", "campaign.json", "--now", "2026-10-21T12:00:00+03:00", "stats")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Подмена времени запрещена", result.stdout)

    def test_console_demo_can_complete_real_user_flow_without_vk(self):
        text = "\n".join(["Начать", "Получить промокод", "Получить промокод", "Мои шансы",
                           "Участвовать в розыгрыше", "Проверить подписку", ":subscribe", "Проверить подписку",
                           "Мои шансы", ":finish", "/draw final", "/approve final", ":quit", ""])
        result = self.cli("demo", text=text)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Пока не вижу подписку", result.stdout)
        self.assertIn("Ты в лайнапе", result.stdout)
        self.assertIn("Накоплено шансов: 1 из 10", result.stdout)
        self.assertIn("Ты выиграл два билета", result.stdout)
        self.assertIn(":claim", result.stdout)

    def test_cli_export_and_backup(self):
        key, now = self.daily_draw()
        config = self.path / "campaign.json"
        config.write_text(json.dumps(asdict(self.campaign), ensure_ascii=False), encoding="utf-8")
        common = ["--config", str(config), "--db", str(self.path / "test.sqlite3")]
        result = self.cli(*common, "export", key, str(self.path / "results.csv"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.path / "results.csv").exists())
        result = self.cli(*common, "backup", str(self.path / "copy.sqlite3"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.path / "copy.sqlite3").exists())

    def test_demo_restart_keeps_simulated_time(self):
        first = self.cli("demo", text=":day 2\n:quit\n")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        second = self.cli("demo", text=":day 1\n:quit\n")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("Время можно переводить только вперёд", second.stdout)

    @unittest.skipUnless(sys.platform == "win32", "Windows launcher")
    def test_launcher_explains_incomplete_zip_extraction(self):
        source = Path(__file__).resolve().parents[1] / "Запустить демо.cmd"
        launcher = self.path / source.name
        launcher.write_bytes(source.read_bytes())
        result = subprocess.run(["cmd.exe", "/d", "/c", str(launcher)], input=b"x\n",
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Извлечь все", result.stdout.decode("utf-8"))
        self.assertNotIn("start_demo.ps1", result.stderr.decode("utf-8", errors="replace"))


class ExtraVkTests(BotTestCase):
    def test_windows_environment_partner_token_case(self):
        vk = VkClient("main", 99, partner_tokens={"PARTNER_1": "specific"})
        with patch.object(vk, "call", return_value=1) as call:
            vk.is_member(self.campaign.days[0], 1)
        self.assertEqual(call.call_args.kwargs["token"], "specific")

    def test_banner_upload_contract(self):
        image = self.path / "banner.png"
        image.write_bytes(b"test-image")
        vk = VkClient("main", 99)
        with patch.object(vk, "call", side_effect=[{"upload_url": "https://upload.vk.com/test"},
                                                   [{"owner_id": -99, "id": 7, "access_key": "key"}]]) as call:
            with patch.object(vk, "_json", return_value={"photo": "uploaded", "server": 1, "hash": "h"}) as upload:
                attachment = vk.upload_banner(image, 500)
        self.assertEqual(attachment, "photo-99_7_key")
        self.assertIn(b'name="photo"', upload.call_args.args[0].data)
        self.assertEqual(call.call_args_list[1].args[0], "photos.saveMessagesPhoto")

    def test_preflight_checks_all_partners_and_long_poll(self):
        vk = VkClient("main", 99)
        with patch.object(vk, "call", return_value={"is_enabled": True, "events": {"message_new": 1}}):
            with patch.object(vk, "long_poll_server", return_value={}):
                with patch.object(vk, "messages_allowed", return_value=True):
                    with patch.object(vk, "is_member", return_value=False) as member:
                        result = vk.check_connection(self.campaign, 500)
        self.assertEqual(len(result["partners_checked"]), 10)
        self.assertEqual(member.call_count, 10)

    def test_preflight_rejects_disabled_long_poll(self):
        vk = VkClient("main", 99)
        with patch.object(vk, "call", return_value={"is_enabled": False, "events": {"message_new": 1}}):
            with self.assertRaises(ValueError):
                vk.check_connection(self.campaign, 500)
