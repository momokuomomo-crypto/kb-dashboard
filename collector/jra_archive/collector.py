"""
全レース（重賞・非重賞）の結果・払戻金・確定オッズを収集する（06_08）。

JRAへの負荷を抑えるため、通信はrace_collector.jra_client.JraClient（1秒間隔・
リトライ付き）を使い、取得済みのレースは再取得しない（再開可能）。
"""

import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, List, Optional

from race_collector.jra_client import JraClient, JraFetchError

from . import parse
from .store import ArchiveStore


@dataclass
class Report:
    saved: int = 0
    skipped: int = 0
    remaining: int = 0
    errors: List[str] = field(default_factory=list)


def months_between(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def discover(client: JraClient, start: date, end: date) -> List[dict]:
    """期間内の全レース（開催日ページ経由）を列挙する。"""
    lo, hi = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    days = {}
    for y, m in months_between(start, end):
        try:
            html = client.fetch_month(y, m)
        except JraFetchError:
            continue  # 当月は月別CNAMEで開けない（下の検索トップで補う）
        days.update({c: d for d, c in parse.parse_month_days(html)})
    if end >= date.today().replace(day=1) or not days:
        days.update({c: d for d, c in parse.parse_month_days(client.fetch_latest_month())})
    races = []
    for cname, d in sorted(days.items(), key=lambda x: (x[1], x[0])):
        if not (lo <= d <= hi):
            continue
        try:
            races.extend(parse.parse_day_races(client.fetch("accessS.html", cname)))
        except JraFetchError as e:
            print(f"[失敗] 開催日ページ {d} {cname}: {e}")
    return races


def fetch_race(client: JraClient, item: dict, with_combos: bool = True):
    html = client.fetch("accessS.html", item["cname"])
    race, horses, payouts, odds = parse.parse_result(html)
    rid, d = item["race_id"], item["date"]
    race.update({
        "race_id": rid, "date": d, "venue": item["venue"], "kai": int(rid[6:8]),
        "day": int(rid[8:10]), "race_no": item["race_no"], "list_name": item["name"],
        "is_shinba": int(race.get("class") == "新馬" or parse.is_shinba(item["name"])),
        "n_runners": sum(1 for h in horses if h["umaban"] is not None),
        "cname": item["cname"],
    })
    tanpuku, combos = {}, []
    if "1" in odds:
        o151 = client.fetch("accessO.html", odds["1"])
        tanpuku = parse.parse_tanpuku(o151)
        if with_combos:
            for code, cname in sorted(parse.find_odds_cnames(o151, rid).items()):
                if code == "1":
                    continue
                combos.extend(parse.parse_combo_odds(client.fetch("accessO.html", cname),
                                                     parse.ODDS_PAGES[code]))
    race["combo_odds"] = int(bool(combos))
    for h in horses:
        h.update({"race_id": rid, "date": d})
        h.update(tanpuku.get(h["umaban"], {}))
    for p in payouts:
        p.update({"race_id": rid, "date": d})
    return race, horses, payouts, combos


def graded_ids(client: JraClient, start: date, end: date) -> dict:
    """月別結果ページの重賞欄から {race_id: グレード}（G1〜G3・障害重賞）を作る。"""
    from race_collector import discovery as graded

    out = {}
    pages = []
    for y, m in months_between(start, end):
        try:
            pages.append(client.fetch_month(y, m))
        except JraFetchError:
            pass
    pages.append(client.fetch_latest_month())
    for html in pages:
        for r in graded.parse_month_graded_races(html):
            out[r.key.netkeiba_race_id] = r.grade
    return out


def collect(store: ArchiveStore, start: date, end: date, *, shinba_only: bool = False,
            graded_only: bool = False,
            with_combos: bool = True, client: Optional[JraClient] = None,
            deadline: Optional[float] = None, commit_every: int = 40,
            on_commit: Callable[[str], None] = lambda msg: None) -> Report:
    client = client or JraClient()
    report = Report()
    done = store.done_ids()
    races = [r for r in discover(client, start, end)
             if not shinba_only or parse.is_shinba(r["name"])]
    if graded_only:
        g = graded_ids(client, start, end)
        races = [r for r in races if g.get(r["race_id"]) in ("G1", "G2", "G3")]
    todo = [r for r in races if r["race_id"] not in done]
    report.skipped = len(races) - len(todo)
    print(f"対象 {len(races)} レース（取得済み {report.skipped}、今回 {len(todo)}）")
    since_commit = 0
    for i, item in enumerate(todo):
        if deadline is not None and time.monotonic() > deadline:
            report.remaining = len(todo) - i
            print(f"[時間切れ] 残り {report.remaining} レースは次回に回します")
            break
        try:
            store.add(*fetch_race(client, item, with_combos))
            report.saved += 1
            since_commit += 1
        except Exception as e:  # 1件の失敗で全体を止めない
            report.errors.append(f"{item['race_id']} {item['name']}: {e}")
            print(f"[失敗] {item['race_id']} {item['name']}: {e}")
        if since_commit >= commit_every:
            store.flush()
            on_commit(f"data(06_08): {item['date']}まで{report.saved}レース")
            since_commit = 0
    store.flush()
    on_commit(f"data(06_08): {start}〜{end} 計{report.saved}レース")
    return report


def collect_pedigree(store: ArchiveStore, *, shinba_only: bool = False, since: str = "",
                     client: Optional[JraClient] = None, deadline: Optional[float] = None,
                     commit_every: int = 200,
                     on_commit: Callable[[str], None] = lambda msg: None) -> Report:
    """
    アーカイブ済みレースの出走馬について、未取得の血統（競走馬ページ）を集める。
    競走馬ページのCNAMEはhorses CSVのhorse_cname、無い古い行は結果ページを
    取り直して得る（1レース1リクエストで全頭分）。
    """
    client = client or JraClient()
    report = Report()
    known = store.pedigree()
    races = {r["race_id"]: r for r in store.races(shinba_only) if r["date"] >= since}
    by_race = {}
    names = {}
    for h in store.horses():
        hid = h.get("horse_id")
        if h["race_id"] in races and hid and hid not in known:
            by_race.setdefault(h["race_id"], {})[hid] = h.get("horse_cname") or ""
            names[hid] = h["horse_name"]
    todo_ids = {hid for hs in by_race.values() for hid in hs}
    # 結果ページCNAMEが無い古い行は、開催日ページから取り直す（月単位）
    need = sorted({races[rid]["date"][:6] for rid, hs in by_race.items()
                   if not all(hs.values()) and not races[rid].get("cname")})
    for ym in need:
        y, m = int(ym[:4]), int(ym[4:])
        last = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
        for item in discover(client, date(y, m, 1), last):
            if item["race_id"] in races:
                races[item["race_id"]]["cname"] = item["cname"]
    print(f"血統の未取得 {len(todo_ids)} 頭（{len(by_race)} レース）")
    done = set()
    since = 0
    for rid in sorted(by_race, reverse=True):  # 新しいレースから
        if deadline is not None and time.monotonic() > deadline:
            break
        horses = {k: v for k, v in by_race[rid].items() if k not in done and k not in known}
        if not horses:
            continue
        if not all(horses.values()):
            item = races[rid]
            try:
                html = client.fetch("accessS.html", _sde_cname(item))
                _, hs, _, _ = parse.parse_result(html)
                cn = {h["horse_id"]: h["horse_cname"] for h in hs}
                horses = {k: v or cn.get(k, "") for k, v in horses.items()}
            except Exception as e:
                report.errors.append(f"{rid} 結果ページ: {e}")
                continue
        for hid, cname in horses.items():
            if deadline is not None and time.monotonic() > deadline:
                break
            if not cname:
                continue
            try:
                info = parse.parse_horse(client.fetch("accessU.html", cname))
                info.update({"horse_id": hid, "horse_name": names.get(hid, "")})
                known[hid] = info
                done.add(hid)
                report.saved += 1
                since += 1
            except Exception as e:
                report.errors.append(f"{hid} {names.get(hid, '')}: {e}")
            if since >= commit_every:
                store.save_pedigree(known)
                on_commit(f"data(06_08): 血統 {report.saved}頭")
                since = 0
    report.remaining = len(todo_ids) - len(done)
    store.save_pedigree(known)
    on_commit(f"data(06_08): 血統 計{report.saved}頭")
    return report


def _sde_cname(race: dict) -> str:
    """races CSVの行から結果ページCNAMEを復元する（チェック値はJRAの開催日ページから取り直す）。"""
    cname = race.get("cname")
    if cname:
        return cname
    raise ValueError("結果ページのCNAMEがありません")


def fix_grades(store: ArchiveStore, start: date, end: date,
               client: Optional[JraClient] = None) -> int:
    """
    gradeが空の重賞（初期版の解析不具合で空になった行）を、JRAの月別結果ページの
    重賞欄（G1〜G3・障害重賞）から補う。直したレース数を返す。
    """
    client = client or JraClient()
    return store.set_grades(graded_ids(client, start, end))
