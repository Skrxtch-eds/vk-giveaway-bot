"""SQLite с короткими транзакциями. Каждому потоку — своё соединение."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import threading


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY CHECK(user_id > 0),
    started_at TEXT,
    reachable INTEGER NOT NULL DEFAULT 1 CHECK(reachable IN (0, 1))
);
CREATE TABLE IF NOT EXISTS incoming_events (
    event_key TEXT PRIMARY KEY, processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promo_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL, code TEXT NOT NULL,
    user_id INTEGER REFERENCES users(user_id), issued_at TEXT,
    UNIQUE(day, code), UNIQUE(day, user_id)
);
CREATE TABLE IF NOT EXISTS promo_requests (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(user_id),
    day TEXT, requested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS registrations (
    day TEXT NOT NULL, partner_id TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(user_id), registered_at TEXT NOT NULL,
    PRIMARY KEY(day, user_id), UNIQUE(partner_id, user_id)
);
CREATE TABLE IF NOT EXISTS active_days (
    day TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(user_id),
    activated_at TEXT NOT NULL, PRIMARY KEY(day, user_id)
);
CREATE TABLE IF NOT EXISTS draws (
    draw_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('daily','final')),
    day TEXT, drawn_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','approved')),
    approved_at TEXT, approved_by INTEGER
);
CREATE TABLE IF NOT EXISTS draw_pool (
    draw_id TEXT NOT NULL REFERENCES draws(draw_id), user_id INTEGER NOT NULL,
    weight INTEGER NOT NULL CHECK(weight > 0), registered_at TEXT,
    PRIMARY KEY(draw_id, user_id)
);
CREATE TABLE IF NOT EXISTS draw_order (
    draw_id TEXT NOT NULL REFERENCES draws(draw_id), user_id INTEGER NOT NULL,
    position INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('winner','reserve')),
    PRIMARY KEY(draw_id, user_id), UNIQUE(draw_id, position)
);
CREATE TABLE IF NOT EXISTS awards (
    id INTEGER PRIMARY KEY AUTOINCREMENT, draw_id TEXT NOT NULL REFERENCES draws(draw_id),
    slot INTEGER NOT NULL, prize TEXT NOT NULL, instructions TEXT NOT NULL,
    UNIQUE(draw_id, slot)
);
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, award_id INTEGER NOT NULL REFERENCES awards(id),
    draw_id TEXT NOT NULL REFERENCES draws(draw_id), user_id INTEGER NOT NULL REFERENCES users(user_id),
    status TEXT NOT NULL CHECK(status IN ('offered','claimed','expired')),
    offered_at TEXT NOT NULL, deadline TEXT NOT NULL,
    first_name TEXT, last_name TEXT, consent_text TEXT, consent_version TEXT, consent_at TEXT,
    UNIQUE(draw_id, user_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_offer_per_award
    ON offers(award_id) WHERE status IN ('offered','claimed');
CREATE TABLE IF NOT EXISTS claim_sessions (
    user_id INTEGER PRIMARY KEY REFERENCES users(user_id),
    offer_id INTEGER NOT NULL REFERENCES offers(id),
    step TEXT NOT NULL, first_name TEXT, last_name TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(user_id),
    random_id INTEGER NOT NULL UNIQUE,
    body TEXT NOT NULL, kind TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sending','sent','skipped','failed')),
    created_at TEXT NOT NULL, next_attempt_at TEXT NOT NULL,
    lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT, error_code TEXT
);
CREATE INDEX IF NOT EXISTS outbox_ready ON outbox(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS broadcasts (
    campaign_key TEXT PRIMARY KEY, text TEXT NOT NULL, created_at TEXT NOT NULL,
    created_by INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Версия базы новее приложения")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA user_version=1")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def read(self):
        active = getattr(self._local, "conn", None)
        if active is not None:
            yield active
        else:
            with self.connect() as conn:
                yield conn

    @contextmanager
    def transaction(self):
        active = getattr(self._local, "conn", None)
        if active is not None:
            # Вложенные вызовы сервиса участвуют в одной транзакции входящего события.
            yield active
            return
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._local.conn = conn
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                self._local.conn = None

    def backup(self, destination: str | Path) -> None:
        target = Path(destination)
        if target.resolve() == self.path.resolve():
            raise ValueError("Резервная копия должна быть отдельным файлом")
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source:
            with closing(sqlite3.connect(target)) as copy:
                source.backup(copy)
