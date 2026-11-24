"""
アーカイブの保存（06_08）。保存先はリポジトリのjra-archiveブランチ
（開発用ブランチを重くしないため、データ専用の別ブランチに置く）。

    races/YYYYMM.csv              … 1レース1行（レース条件・新馬フラグ等）
    horses/YYYYMM.csv             … 1頭1行（着順・通過順・騎手・単複オッズ等）
    payouts/YYYYMM.csv            … 払戻金（全券種、的中組み合わせ）
    odds_combo/YYYYMM/<race_id>.csv.gz … 枠連〜3連単の全組み合わせ確定オッズ
"""

import csv
import gzip
import io
from pathlib import Path
from typing import Dict, Iterable, List, Set

RACE_COLUMNS = [
    "race_id", "date", "venue", "kai", "day", "race_no", "race_name", "list_name", "grade",
    "category", "class", "is_shinba", "rule", "weight_rule", "surface", "distance",
    "course_detail", "weather", "track_condition", "start_time", "n_runners", "combo_odds", "cname",
]
HORSE_COLUMNS = [
    "race_id", "date", "finish", "waku", "umaban", "horse_name", "horse_id", "horse_cname", "sex_age",
    "weight_carried", "jockey", "jockey_id", "trainer", "trainer_id", "time", "margin",
    "corner_positions", "corner_first", "corner_last", "last_3f", "body_weight",
    "body_weight_diff", "popularity", "tan_odds", "fuku_min", "fuku_max",
]
PAYOUT_COLUMNS = ["race_id", "date", "bet_type", "combo", "payout", "popularity"]
COMBO_COLUMNS = ["bet_type", "combo", "odds", "odds_max"]
PEDIGREE_COLUMNS = [
    "horse_id", "horse_name", "sire", "dam", "broodmare_sire", "dam_dam", "birth_date",
    "sex", "coat", "origin", "breeder", "owner", "market",
]
PEDIGREE_FILE = "pedigree/horses.csv"


def _read(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _write(path: Path, columns: List[str], rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, columns, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})
    tmp.replace(path)


README = """# JRA全レースアーカイブ（06_08）

kb-projectの`scripts/06_pipeline/jra_archive`が自動で更新するデータ専用ブランチです。
2024年11月以降のJRA平地・障害の全レース（重賞・非重賞・新馬戦）を収録します。

| パス | 内容 |
|---|---|
| races/YYYYMM.csv | 1レース1行。race_id（netkeibaと同じ12桁）、クラス、is_shinba（新馬戦=1）、芝/ダート、距離、馬場、頭数、combo_odds（全組み合わせオッズの有無） |
| horses/YYYYMM.csv | 1頭1行。着順、枠、馬番、騎手・調教師（ID付き）、通過順（corner_first=最初のコーナー順位。1なら逃げ）、上り3F、馬体重、人気、単勝・複勝の確定オッズ |
| payouts/YYYYMM.csv | 払戻金（単勝・複勝・枠連・ワイド・馬連・馬単・3連複・3連単の的中組み合わせ、100円あたり） |
| pedigree/horses.csv | 1頭1行（horse_idで結合）。父・母・母の父・母の母・生年月日・産地・生産牧場・馬主 |
| odds_combo/YYYYMM/RACEID.csv.gz | 枠連・馬連・ワイド（下限/上限）・馬単・3連複・3連単の全組み合わせの確定オッズ |

取得方法: `git fetch --depth 1 origin jra-archive && git worktree add ../jra-archive FETCH_HEAD`
"""


class ArchiveStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._pending: Dict[str, Dict[str, list]] = {}
        readme = self.root / "README.md"
        if self.root.exists() and not readme.exists():
            readme.write_text(README, encoding="utf-8")

    def races(self, shinba_only: bool = False) -> List[dict]:
        rows = []
        for p in sorted((self.root / "races").glob("*.csv")):
            rows.extend(r for r in _read(p) if not shinba_only or r.get("is_shinba") == "1")
        return rows

    def horses(self) -> List[dict]:
        rows = []
        for p in sorted((self.root / "horses").glob("*.csv")):
            rows.extend(_read(p))
        return rows

    def pedigree(self) -> Dict[str, dict]:
        return {r["horse_id"]: r for r in _read(self.root / PEDIGREE_FILE)}

    def save_pedigree(self, rows: Dict[str, dict]) -> None:
        _write(self.root / PEDIGREE_FILE, PEDIGREE_COLUMNS,
               sorted(rows.values(), key=lambda r: r["horse_id"]))

    def set_grades(self, grades: Dict[str, str]) -> int:
        """races CSVのgradeが空の行に、race_id→グレードを書き込む。"""
        fixed = 0
        for p in sorted((self.root / "races").glob("*.csv")):
            rows = _read(p)
            changed = False
            for r in rows:
                g = grades.get(r["race_id"])
                if g and not r.get("grade"):
                    r["grade"] = g
                    changed = True
                    fixed += 1
            if changed:
                cols = list(RACE_COLUMNS) + [k for k in rows[0] if k not in RACE_COLUMNS]
                _write(p, cols, rows)
        return fixed

    def done_ids(self) -> Set[str]:
        ids = set()
        for p in (self.root / "races").glob("*.csv"):
            ids.update(r["race_id"] for r in _read(p))
        return ids

    def add(self, race: dict, horses: List[dict], payouts: List[dict],
            combos: List[dict]) -> None:
        month = race["date"][:6]
        bucket = self._pending.setdefault(month, {"races": [], "horses": [], "payouts": []})
        bucket["races"].append(race)
        bucket["horses"].extend(horses)
        bucket["payouts"].extend(payouts)
        if combos:
            path = self.root / "odds_combo" / month / f"{race['race_id']}.csv.gz"
            path.parent.mkdir(parents=True, exist_ok=True)
            buf = io.StringIO()
            w = csv.DictWriter(buf, COMBO_COLUMNS, extrasaction="ignore", lineterminator="\n")
            w.writeheader()
            for r in combos:
                w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in COMBO_COLUMNS})
            # mtime=0で同じ内容なら同じバイト列にする（差分を出さない）
            with gzip.GzipFile(path, "wb", mtime=0) as f:
                f.write(buf.getvalue().encode("utf-8"))

    def flush(self) -> int:
        """溜めたレースを月別CSVへ書き込む。書いたレース数を返す。"""
        n = 0
        for month, b in self._pending.items():
            new_ids = {r["race_id"] for r in b["races"]}
            n += len(new_ids)
            for name, cols, new in (("races", RACE_COLUMNS, b["races"]),
                                    ("horses", HORSE_COLUMNS, b["horses"]),
                                    ("payouts", PAYOUT_COLUMNS, b["payouts"])):
                path = self.root / name / f"{month}.csv"
                rows = [r for r in _read(path) if r["race_id"] not in new_ids] + new
                rows.sort(key=lambda r: (r["race_id"], _sort_key(r)))
                _write(path, cols, rows)
        self._pending = {}
        return n


def _sort_key(r: dict):
    for k in ("umaban", "combo"):
        v = r.get(k)
        if v not in (None, ""):
            try:
                return (0, int(v), "")
            except (TypeError, ValueError):
                return (1, 0, str(v))
    return (2, 0, "")
