#!/usr/bin/env python3
"""Offline tests for reading stats: session write funnel in put_progress
(forward-only, 30-min idle cap, no baseline on first progress), /api/stats
aggregation (minutes, streak incl. gap/today cases, finished count, top books).

Run: .venv/bin/python scripts/test_stats.py
"""
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="bookplate-stats-test-"))
import app.db as db  # noqa: E402

db.DATA_DIR = _TMP
db.DB_PATH = _TMP / "ebook.db"
import os  # noqa: E402

os.environ["BOOKPLATE_ADMIN_USER"] = "rootadmin"
os.environ["BOOKPLATE_ADMIN_PASS"] = "bootpass1"

from app import main  # noqa: E402
from app.auth import hash_password  # noqa: E402


def mk_user(username):
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO users(username, password_hash, role, status) VALUES(?,?,?,?)",
            (username, hash_password("secret6"), "user", "active"))
        return cur.lastrowid


def mk_book(title, owner_id, sha):
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO books(sha256, ext, size, title, added_by) VALUES(?,?,?,?,?)",
            (sha, "epub", 1000, title, owner_id))
        bid = cur.lastrowid
        con.execute("INSERT INTO user_books(user_id, book_id) VALUES(?,?)", (owner_id, bid))
        return bid


def backdate_progress(user_id, book_id, minutes_ago, pct):
    """Seed a baseline progress row `minutes_ago` minutes in the past."""
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
    with db.conn() as con:
        con.execute(
            "INSERT INTO reading_progress(user_id, book_id, cfi, pct, updated_at) VALUES(?,?,?,?,?)",
            (user_id, book_id, "c", pct, ts))


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.u1 = mk_user("alice")
        self.b1 = mk_book("Book One", self.u1, f"sha-{self.u1}-1")

    def tearDown(self):
        with db.conn() as con:
            for t in ("reading_sessions", "reading_progress", "user_books", "books", "users"):
                con.execute(f"DELETE FROM {t}")

    def test_first_progress_no_session(self):
        main.put_progress(self.b1, main.ProgressReq(cfi="c", pct=10), {"id": self.u1})
        with db.conn() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) c FROM reading_sessions").fetchone()["c"], 0)

    def test_forward_progress_logs_session(self):
        backdate_progress(self.u1, self.b1, minutes_ago=10, pct=10)
        main.put_progress(self.b1, main.ProgressReq(cfi="c", pct=20), {"id": self.u1})
        with db.conn() as con:
            row = con.execute("SELECT * FROM reading_sessions").fetchone()
        self.assertEqual(row["pct_from"], 10)
        self.assertEqual(row["pct_to"], 20)
        self.assertAlmostEqual(row["minutes"], 10.0, delta=0.2)

    def test_backward_progress_no_session(self):
        backdate_progress(self.u1, self.b1, minutes_ago=5, pct=50)
        main.put_progress(self.b1, main.ProgressReq(cfi="c", pct=20), {"id": self.u1})
        with db.conn() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) c FROM reading_sessions").fetchone()["c"], 0)

    def test_idle_cap_30_minutes(self):
        backdate_progress(self.u1, self.b1, minutes_ago=200, pct=5)
        main.put_progress(self.b1, main.ProgressReq(cfi="c", pct=6), {"id": self.u1})
        with db.conn() as con:
            row = con.execute("SELECT minutes FROM reading_sessions").fetchone()
        self.assertLessEqual(row["minutes"], 30.0)

    def test_streak_today_only(self):
        with db.conn() as con:
            con.execute("INSERT INTO reading_sessions(user_id, book_id, minutes, ts) VALUES(?,?,?,datetime('now'))",
                        (self.u1, self.b1, 5))
        self.assertEqual(main.get_stats({"id": self.u1})["streak_days"], 1)

    def test_streak_counts_back_through_yesterday(self):
        with db.conn() as con:
            for days_ago in (0, 1, 2):
                con.execute(
                    "INSERT INTO reading_sessions(user_id, book_id, minutes, ts) VALUES(?,?,?,datetime('now', ?))",
                    (self.u1, self.b1, 5, f'-{days_ago} days'))
        self.assertEqual(main.get_stats({"id": self.u1})["streak_days"], 3)

    def test_streak_broken_by_gap(self):
        with db.conn() as con:
            for days_ago in (0, 1, 3, 4):  # gap at 2 days ago
                con.execute(
                    "INSERT INTO reading_sessions(user_id, book_id, minutes, ts) VALUES(?,?,?,datetime('now', ?))",
                    (self.u1, self.b1, 5, f'-{days_ago} days'))
        self.assertEqual(main.get_stats({"id": self.u1})["streak_days"], 2)

    def test_stats_shape_and_minutes(self):
        backdate_progress(self.u1, self.b1, minutes_ago=12, pct=0)
        main.put_progress(self.b1, main.ProgressReq(cfi="c", pct=30), {"id": self.u1})
        s = main.get_stats({"id": self.u1})
        self.assertEqual(set(s.keys()), {"minutes_month", "minutes_total", "streak_days",
                                         "finished_year", "days", "top_books"})
        self.assertEqual(len(s["days"]), 30)
        self.assertGreaterEqual(s["minutes_month"], 11)
        self.assertEqual(s["top_books"][0]["title"], "Book One")

    def test_finished_year_counts_100pct(self):
        backdate_progress(self.u1, self.b1, minutes_ago=3, pct=95)
        main.put_progress(self.b1, main.ProgressReq(cfi="c", pct=100), {"id": self.u1})
        self.assertEqual(main.get_stats({"id": self.u1})["finished_year"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
