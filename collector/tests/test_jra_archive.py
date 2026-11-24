import csv
import gzip
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jra_archive import parse
from jra_archive.collector import collect, collect_pedigree
from jra_archive.store import ArchiveStore

FIX = Path(__file__).resolve().parent / "fixtures" / "jra_archive"


def _read(name):
    return (FIX / f"{name}.html").read_text(encoding="utf-8")


class FakeClient:
    """srl0の開催日だけを返し、新馬戦(5R)の結果・オッズはfixtureを返す。"""

    def __init__(self):
        self.calls = []

    def fetch_month(self, y, m):
        return _read("month_202411")

    def fetch_latest_month(self):
        return ""

    def fetch(self, page, cname):
        self.calls.append(cname)
        if cname.startswith("pw01srl"):
            return _read("srl0") if cname.startswith("pw01srl1005202405012") else "<html></html>"
        if cname.startswith("pw01dud"):
            return _read("horse0")
        if cname.startswith("pw01sde"):
            return _read("sde4")
        code = cname[4]
        return _read({"1": "o151", "3": "o153", "4": "o154", "5": "o155", "6": "o156",
                      "8": "o158"}.get(code, "o151"))


class ParseTest(unittest.TestCase):
    def test_month_days(self):
        days = parse.parse_month_days(_read("month_202411"))
        self.assertEqual(len(days), 25)
        self.assertEqual(days[0][0], "20241102")

    def test_day_races(self):
        races = parse.parse_day_races(_read("srl0"))
        self.assertEqual(len(races), 12)
        shinba = [r for r in races if parse.is_shinba(r["name"])]
        self.assertEqual([r["race_no"] for r in shinba], [5, 6])
        self.assertEqual(races[0]["race_id"], "202405050101")

    def test_result(self):
        race, horses, payouts, odds = parse.parse_result(_read("sde4"))
        self.assertEqual(race["class"], "新馬")
        self.assertEqual(race["distance"], 2000)
        self.assertEqual(race["surface"], "芝")
        top = horses[0]
        self.assertEqual((top["finish"], top["umaban"], top["jockey"]), ("1", 1, "戸崎 圭太"))
        self.assertEqual(top["corner_first"], 4)
        types = {p["bet_type"] for p in payouts}
        self.assertTrue({"単勝", "複勝", "ワイド", "馬連", "馬単", "3連複", "3連単"} <= types)
        self.assertIn("1", odds)

    def test_graded_result(self):
        race, horses, _, _ = parse.parse_result(_read("sde_graded"))
        self.assertEqual((race["grade"], race["race_name"]), ("G3", "ラジオNIKKEI賞"))
        self.assertEqual(horses[0]["horse_name"], "サノノグレーター")

    def test_odds(self):
        tp = parse.parse_tanpuku(_read("o151"))
        self.assertEqual(tp[1], {"tan_odds": 1.7, "fuku_min": 1.1, "fuku_max": 1.1})
        wide = parse.parse_combo_odds(_read("o155"), "ワイド")
        self.assertEqual(len(wide), 45)
        self.assertEqual((wide[0]["combo"], wide[0]["odds"], wide[0]["odds_max"]), ("1-2", 7.5, 8.7))
        tan3 = parse.parse_combo_odds(_read("o158"), "3連単")
        self.assertEqual(len(tan3), 720)
        self.assertEqual(tan3[0]["combo"], "1-2-3")
        waku = parse.parse_combo_odds(_read("o153"), "枠連")
        self.assertEqual(waku[0]["combo"], "1-2")


class CollectTest(unittest.TestCase):
    def test_collect_shinba_and_resume(self):
        with tempfile.TemporaryDirectory() as d:
            store = ArchiveStore(Path(d))
            client = FakeClient()
            rep = collect(store, date(2024, 11, 2), date(2024, 11, 2), shinba_only=True,
                          client=client)
            self.assertEqual(rep.saved, 2)
            with open(Path(d) / "races" / "202411.csv", encoding="utf-8") as f:
                races = list(csv.DictReader(f))
            self.assertEqual([r["is_shinba"] for r in races], ["1", "1"])
            self.assertEqual(races[0]["combo_odds"], "1")
            with open(Path(d) / "horses" / "202411.csv", encoding="utf-8") as f:
                horses = list(csv.DictReader(f))
            self.assertEqual(horses[0]["tan_odds"], "1.7")
            gz = Path(d) / "odds_combo" / "202411" / f"{races[0]['race_id']}.csv.gz"
            text = gzip.decompress(gz.read_bytes()).decode("utf-8")
            self.assertIn("3連単,1-2-3,438.1,", text)
            # 2回目は取得済みとして何も取りに行かない
            client2 = FakeClient()
            rep2 = collect(store, date(2024, 11, 2), date(2024, 11, 2), shinba_only=True,
                           client=client2)
            self.assertEqual((rep2.saved, rep2.skipped), (0, 2))
            self.assertFalse(any(c.startswith("pw01sde") for c in client2.calls))


    def test_pedigree(self):
        with tempfile.TemporaryDirectory() as d:
            store = ArchiveStore(Path(d))
            collect(store, date(2024, 11, 2), date(2024, 11, 2), shinba_only=True,
                    client=FakeClient(), with_combos=False)
            client = FakeClient()
            rep = collect_pedigree(store, shinba_only=True, client=client)
            self.assertEqual(rep.remaining, 0)
            self.assertEqual(rep.saved, 10)  # sde4は10頭（2レースとも同じfixture）
            ped = store.pedigree()
            row = next(iter(ped.values()))
            self.assertEqual((row["sire"], row["broodmare_sire"]), ("サンダースノー", "ハーツクライ"))
            self.assertEqual(row["dam"], "アルトゥーナ")
            # horse_cnameがあるので結果ページは取り直さない
            self.assertFalse(any(c.startswith("pw01sde") for c in client.calls))
            rep2 = collect_pedigree(store, shinba_only=True, client=FakeClient())
            self.assertEqual(rep2.saved, 0)

    def test_pedigree_since(self):
        with tempfile.TemporaryDirectory() as d:
            store = ArchiveStore(Path(d))
            collect(store, date(2024, 11, 2), date(2024, 11, 2), shinba_only=True,
                    client=FakeClient(), with_combos=False)
            rep = collect_pedigree(store, since="20241103", client=FakeClient())
            self.assertEqual((rep.saved, rep.remaining), (0, 0))

    def test_pedigree_without_cnames(self):
        """古い行（horse_cname・cname無し）は開催日ページから取り直す。"""
        with tempfile.TemporaryDirectory() as d:
            store = ArchiveStore(Path(d))
            collect(store, date(2024, 11, 2), date(2024, 11, 2), shinba_only=True,
                    client=FakeClient(), with_combos=False)
            for name in ("races", "horses"):
                p = Path(d) / name / "202411.csv"
                rows = list(csv.DictReader(open(p, encoding="utf-8")))
                for r in rows:
                    r.pop("cname", None)
                    r.pop("horse_cname", None)
                with open(p, "w", encoding="utf-8", newline="") as f:
                    w = csv.DictWriter(f, list(rows[0].keys()))
                    w.writeheader()
                    w.writerows(rows)
            rep = collect_pedigree(store, shinba_only=True, client=FakeClient())
            self.assertEqual((rep.saved, rep.remaining), (10, 0))



    def test_graded_only(self):
        with tempfile.TemporaryDirectory() as d:
            store = ArchiveStore(Path(d))
            rep = collect(store, date(2024, 11, 2), date(2024, 11, 2), graded_only=True,
                          client=FakeClient(), with_combos=False)
            # 11/2東京は11R京王杯2歳S（G2）のみ
            self.assertEqual(rep.saved, 1)
            self.assertEqual([r["race_id"] for r in store.races()], ["202405050111"])

    def test_set_grades(self):
        with tempfile.TemporaryDirectory() as d:
            store = ArchiveStore(Path(d))
            collect(store, date(2024, 11, 2), date(2024, 11, 2), shinba_only=True,
                    client=FakeClient(), with_combos=False)
            self.assertEqual(store.set_grades({"202405050105": "G3", "999": "G1"}), 1)
            races = {r["race_id"]: r for r in store.races()}
            self.assertEqual(races["202405050105"]["grade"], "G3")
            self.assertEqual(store.set_grades({"202405050105": "G1"}), 0)


class MergeTest(unittest.TestCase):
    def test_merge_csv_union_ours_wins(self):
        from jra_archive.main import merge_csv
        theirs = "horse_id,horse_name,sire\n1,A,X\n2,B,Y\n"
        ours = "horse_id,horse_name,sire\n2,B,Z\n3,C,W\n"
        self.assertEqual(merge_csv(theirs, ours, "pedigree/horses.csv"),
                         "horse_id,horse_name,sire\n1,A,X\n2,B,Z\n3,C,W\n")

    def test_merge_csv_column_union(self):
        from jra_archive.main import merge_csv
        theirs = "race_id,date\n1,20241101\n"
        ours = "race_id,date,cname\n2,20241102,c\n"
        self.assertEqual(merge_csv(theirs, ours, "races/202411.csv"),
                         "race_id,date,cname\n1,20241101,\n2,20241102,c\n")

    def test_commit_push_recovers_from_conflict(self):
        import subprocess
        from jra_archive.main import _git_commit_push
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            run = lambda *a, cwd: subprocess.run(a, cwd=cwd, check=True, capture_output=True)
            run("git", "init", "-q", "--bare", "-b", "jra-archive", str(d / "remote.git"), cwd=d)
            for name in ("a", "b"):
                run("git", "clone", "-q", str(d / "remote.git"), name, cwd=d)
                run("git", "config", "user.email", "t@example.com", cwd=d / name)
                run("git", "config", "user.name", "t", cwd=d / name)
            (d / "a" / "pedigree").mkdir()
            (d / "a" / "pedigree" / "horses.csv").write_text("horse_id,sire\n1,X\n")
            run("git", "add", "-A", cwd=d / "a")
            run("git", "commit", "-qm", "init", cwd=d / "a")
            run("git", "push", "-q", "origin", "HEAD:jra-archive", cwd=d / "a")
            run("git", "pull", "-q", "origin", "jra-archive", cwd=d / "b")
            (d / "a" / "pedigree" / "horses.csv").write_text("horse_id,sire\n1,X\n2,Y\n")
            _git_commit_push(d / "a", "a")
            (d / "b" / "pedigree" / "horses.csv").write_text("horse_id,sire\n1,X\n3,Z\n")
            _git_commit_push(d / "b", "b")
            run("git", "pull", "-q", "origin", "jra-archive", cwd=d / "a")
            self.assertEqual((d / "a" / "pedigree" / "horses.csv").read_text(),
                             "horse_id,sire\n1,X\n2,Y\n3,Z\n")


if __name__ == "__main__":
    unittest.main()
