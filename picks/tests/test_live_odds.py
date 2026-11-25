import csv
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import live_odds  # noqa: E402
from weekend import JST  # noqa: E402

FIX = (Path(__file__).parent / "fixtures" / "o151.html").read_text(encoding="utf-8", errors="ignore")


class FakeClient:
    def __init__(self):
        self.calls = 0

    def fetch(self, page, cname):
        self.calls += 1
        return FIX


class FakeClock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    def sleep(self, sec):
        self.t += timedelta(seconds=sec)


class TestLiveOdds(unittest.TestCase):
    def test_parse_post_time(self):
        post = live_odds.parse_post_time(FIX, "20241102")
        self.assertEqual(post, datetime(2024, 11, 2, 12, 15, tzinfo=JST))

    def test_due_snapshots(self):
        post = datetime(2024, 11, 2, 12, 15, tzinfo=JST)
        at = lambda h, m: datetime(2024, 11, 2, h, m, tzinfo=JST)
        self.assertEqual(live_odds.due_snapshots(post, at(8, 30), set()), [])
        self.assertEqual(live_odds.due_snapshots(post, at(9, 0), set()), ["09:00"])
        self.assertEqual(live_odds.due_snapshots(post, at(9, 40), set()), [])  # 9時の分は30分遅れまで
        self.assertEqual(live_odds.due_snapshots(post, at(11, 15), {"09:00"}), ["60"])
        self.assertEqual(live_odds.due_snapshots(post, at(12, 0), {"09:00", "60", "30"}), ["12:00"])
        self.assertEqual(live_odds.due_snapshots(post, at(12, 6), {"09:00", "60", "30", "12:00"}), ["10"])
        self.assertEqual(live_odds.due_snapshots(post, at(12, 11), {"09:00", "60", "30", "12:00"}), ["5"])  # 10分前を逃した
        self.assertEqual(live_odds.due_snapshots(post, at(12, 15), set()), [])  # 発走後
        early = datetime(2024, 11, 2, 11, 50, tzinfo=JST)
        self.assertNotIn("12:00", live_odds.due_snapshots(early, at(12, 0), set()))  # 12時より前に発走

    def test_run_takes_three_snapshots_per_race(self):
        clock = FakeClock(datetime(2024, 11, 2, 8, 30, tzinfo=JST))
        commits = []
        with tempfile.TemporaryDirectory() as d:
            until = datetime(2024, 11, 2, 17, 30, tzinfo=JST)
            taken = live_odds.run(Path(d), "20241102", until, client=FakeClient(), clock=clock,
                                  sleep=clock.sleep, on_commit=commits.append)
            self.assertTrue(taken)
            want = {"09:00", "12:00", "60", "30", "10", "5", "3"}
            self.assertTrue(all(v == want for v in taken.values()))
            rows = list(csv.DictReader(open(Path(d) / "odds_live/20241102.csv", encoding="utf-8")))
        self.assertEqual(len(rows), 70 * len(taken))  # 10頭×7回×レース数
        self.assertEqual({r["point"] for r in rows}, want)
        self.assertEqual(rows[0]["post_time"], "12:15")
        self.assertTrue(commits)


if __name__ == "__main__":
    unittest.main()


class TestResume(unittest.TestCase):
    def test_afternoon_job_skips_points_already_taken(self):
        with tempfile.TemporaryDirectory() as d:
            until = datetime(2024, 11, 2, 17, 30, tzinfo=JST)
            clock = FakeClock(datetime(2024, 11, 2, 11, 0, tzinfo=JST))
            live_odds.run(Path(d), "20241102", datetime(2024, 11, 2, 11, 40, tzinfo=JST),
                          client=FakeClient(), clock=clock, sleep=clock.sleep)
            clock = FakeClock(datetime(2024, 11, 2, 11, 40, tzinfo=JST))
            taken = live_odds.run(Path(d), "20241102", until, client=FakeClient(), clock=clock, sleep=clock.sleep)
            rows = list(csv.DictReader(open(Path(d) / "odds_live/20241102.csv", encoding="utf-8")))
        one = [r for r in rows if r["race_id"] == sorted(taken)[0]]
        points = [r["point"] for r in one[::10]]
        self.assertEqual(points, ["60", "30", "12:00", "10", "5", "3"])  # 重複なし
