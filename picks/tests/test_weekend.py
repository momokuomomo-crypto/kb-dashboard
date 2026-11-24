import csv
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import weekend  # noqa: E402

FIX = (Path(__file__).parent / "fixtures" / "o151.html").read_text(encoding="utf-8", errors="ignore")


class FakeClient:
    def __init__(self, page):
        self.page = page
        self.calls = []

    def fetch(self, page, cname):
        self.calls.append(cname)
        return self.page


def _archive(root: Path, n=80):
    (root / "horses").mkdir(parents=True)
    (root / "payouts").mkdir()
    odds = [2.0, 4.0, 8.0, 15.0, 30.0, 60.0, 90.0, 120.0]
    with open(root / "horses/202501.csv", "w", encoding="utf-8", newline="") as fh, \
            open(root / "payouts/202501.csv", "w", encoding="utf-8", newline="") as fp:
        w = csv.writer(fh); p = csv.writer(fp)
        w.writerow(["race_id", "date", "finish", "umaban", "tan_odds", "fuku_min", "fuku_max", "jockey"])
        p.writerow(["race_id", "date", "bet_type", "combo", "payout", "popularity"])
        for i in range(n):
            rid, date = f"2025{i:08d}", f"202501{1 + i % 28:02d}"
            for no, o in enumerate(odds, 1):
                w.writerow([rid, date, str(no), str(no), o, round(o / 4, 1), round(o / 2, 1), "J"])
            for no, pay in ((2, 200), (3, 300), (5, 900)):  # 5番（単勝30倍）が毎回3着内
                p.writerow([rid, date, "複勝", str(no), str(pay), ""])


class TestParse(unittest.TestCase):
    def test_parse_odds_page(self):
        rows = weekend.parse_odds_page(FIX)
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["jockey"], "戸崎 圭太")
        self.assertEqual(rows[0]["win_odds"], "1.7")

    def test_odds_cnames_give_netkeiba_race_id(self):
        races = weekend.odds_cnames(FIX)
        self.assertEqual(races["202405050105"]["venue"], "東京")
        self.assertEqual(races["202405050105"]["race_no"], 5)
        self.assertEqual(races["202405050105"]["date"], "20241102")

    def test_jockey_group(self):
        self.assertEqual(weekend.jockey_group("C.ルメール"), "bet")
        self.assertEqual(weekend.jockey_group("武 豊"), "watch")
        self.assertIsNone(weekend.jockey_group("A.ルメートル"))

    def test_weekend_key_is_saturday(self):
        self.assertEqual(weekend.weekend_key("20261128"), "20261128")
        self.assertEqual(weekend.weekend_key("20261129"), "20261128")


class TestSelect(unittest.TestCase):
    def test_budget_and_min_place_odds(self):
        cands = [dict(group="bet", place_odds_min="2.0", expected_value=1.0 + i / 10) for i in range(7)]
        cands.append(dict(group="bet", place_odds_min="1.1", expected_value=9.0))
        cands.append(dict(group="watch", place_odds_min="3.0", expected_value=5.0))
        weekend.select(cands, already=1)
        self.assertEqual(sum(c["stake"] for c in cands), 4000)
        self.assertEqual(cands[-1]["stake"], 0)
        self.assertEqual(cands[-2]["stake"], 0)
        self.assertEqual([c["stake"] for c in cands[3:7]], [1000] * 4)


class TestRun(unittest.TestCase):
    def test_run_writes_json_and_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _archive(root / "archive")
            client = FakeClient(FIX)
            store = weekend.run(root / "archive", root / "out", ["20241102"], client,
                                now=datetime(2024, 11, 1, 20, 0, tzinfo=weekend.JST))
            self.assertTrue((root / "out/picks.html").exists())
            data = json.loads((root / "out/picks.json").read_text(encoding="utf-8"))
            self.assertEqual(data, store)
            self.assertIn("20241102", store)
            html = (root / "out/picks.html").read_text(encoding="utf-8")
            self.assertIn("週末の買い目", html)


if __name__ == "__main__":
    unittest.main()
