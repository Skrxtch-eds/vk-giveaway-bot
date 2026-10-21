from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from giveaway.config import moment
from giveaway.db import Database
from giveaway.demo import example_campaign
from giveaway.service import Service


class FakeVk:
    def __init__(self):
        self.membership = True
        self.allowed = True
        self.error = None
        self.sent = []
        self.membership_calls = 0
        self.after_send = None

    def is_member(self, day, user_id):
        self.membership_calls += 1
        if self.error:
            raise self.error
        return self.membership

    def messages_allowed(self, user_id):
        if self.error:
            raise self.error
        return self.allowed

    def send(self, user_id, random_id, body):
        if self.error:
            raise self.error
        self.sent.append((user_id, random_id, body))
        if self.after_send:
            self.after_send()


class BotTestCase(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[1] / "data" / "test-tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.campaign = example_campaign(datetime(2026, 10, 21).date())
        self.db = Database(self.path / "test.sqlite3")
        self.service = Service(self.db, self.campaign, randbelow=lambda n: 0)
        self.now = moment("2026-10-21T12:00:00+03:00")
        self.vk = FakeVk()

    def at_day(self, index):
        return self.now + timedelta(days=index)

    def query(self, sql, params=()):
        with self.db.read() as conn:
            return conn.execute(sql, params).fetchall()

    def count(self, table):
        return self.query(f"SELECT count(*) FROM {table}")[0][0]

    def register_users(self, count=12, day_index=0):
        now = self.at_day(day_index)
        day = self.campaign.days[day_index]
        for user_id in range(1, count + 1):
            self.service.register(user_id, day.date, True, now)

    def daily_draw(self, count=12):
        self.register_users(count)
        key = f"daily:{self.campaign.days[0].date}"
        now = moment(self.campaign.days[0].draw_at)
        self.service.draw(key, now)
        return key, now

    def event(self, user_id=1, text="Начать", payload=None, identity="event-1"):
        import json
        return {"group_id": 99, "type": "message_new", "event_id": identity,
                "object": {"message": {"from_id": user_id, "peer_id": user_id,
                                         "text": text, "payload": json.dumps(payload) if payload else None}}}
