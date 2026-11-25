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
        self.assertEqual(live_odds.due_snapshots(post, at(12, 0), set()), [])
        self.assertEqual(live_odds.due_snapshots(post, at(12, 6), set()), [10])
        self.assertEqual(live_odds.due_snapshots(post, at(12, 11), set()), [5])  # 10分前を逃した
        self.assertEqual(live_odds.due_snapshots(post, at(12, 11), {10, 5}), [])
        self.assertEqual(live_odds.due_snapshots(post, at(12, 15), set()), [])  # 発走後

    def test_run_takes_three_snapshots_per_race(self):
        clock = FakeClock(datetime(2024, 11, 2, 11, 50, tzinfo=JST))
        commits = []
        with tempfile.TemporaryDirectory() as d:
            until = datetime(2024, 11, 2, 17, 30, tzinfo=JST)
            taken = live_odds.run(Path(d), "20241102", until, client=FakeClient(), clock=clock,
                                  sleep=clock.sleep, on_commit=commits.append)
            self.assertTrue(taken)
            self.assertTrue(all(v == {10, 5, 3} for v in taken.values()))
            rows = list(csv.DictReader(open(Path(d) / "odds_live/20241102.csv", encoding="utf-8")))
        self.assertEqual(len(rows), 30 * len(taken))  # 10頭×3回×レース数
        self.assertEqual(sorted({r["minutes_before"] for r in rows}), ["10", "3", "5"])
        self.assertEqual(rows[0]["post_time"], "12:15")
        self.assertTrue(commits)


if __name__ == "__main__":
    unittest.main()
