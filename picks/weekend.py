"""
週末の全レース（重賞・非重賞・新馬戦）から「期待値派×指定騎手」の買い目を出す。

    python -m weekend --archive ../archive --out ../site_picks

1. jra-archive（確定済みの全レース）で期待値派のモデルを学習する
   （kb-project 04_07。対象レースより前の確定レースのみで学習）。
2. JRAのオッズページから、今日・明日（JST）の全レースの単勝・複勝オッズと騎手を取る。
3. 期待値1.0以上・単勝50倍未満・指定騎手（M.デムーロ、C.デムーロ、ルメール、川田）の
   馬（1レース3頭まで）を買い目にする。複勝で1レース3,000円。2頭以上なら、どれが
   当たってもそのレースの合計を上回って戻るように複勝オッズ下限の逆数で割り振る。
   複勝オッズの下限が1.2倍未満の馬は買わない。
   武豊と、検証で両期間とも100%を超えた騎手（岩田康誠・団野大成・戸崎圭太・田辺裕信）は
   「参考（買わない）」として表示する。
4. picks.json（週ごとの記録）と picks.html を出力する。

全レースでの検証（5,256レース、前半〜2025-09-05／後半）：指定4騎手×期待値1.0以上は
1レース3,000円のこの賭け方で前半104%・後半116%（135レース、+42,300円）。確定オッズでの検証なので、実際の購入時点の
オッズとはずれがある。
"""

import argparse
import csv
import glob
import json
import re
import sys
from collections import OrderedDict, defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from analysis_engine import ev
from jra_client import JraClient

JST = timezone(timedelta(hours=9))
ODDS_TOP_CNAME = "pw15oli00/6D"
ORL_RE = re.compile(r"pw15orl\d{2}(\d{2})(\d{4})(\d{2})(\d{2})(\d{8})/[0-9A-F]{2}")
TANPUKU_RE = re.compile(r"pw151ou\d{2}(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})(\d{8})Z/[0-9A-F]{2}")
VENUES = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
    "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
}
BET_JOCKEYS = ("M.デムーロ", "C.デムーロ", "ルメール", "川田")
WATCH_JOCKEYS = ("武 豊", "岩田 康誠", "団野", "戸崎", "田辺")
EV_THRESHOLD = 1.0
MIN_PLACE_ODDS = 1.2
RACE_BUDGET = 3000  # 1レースに賭ける金額


# ---------------------------------------------------------------- 学習


def load_archive_races(archive: Path) -> "OrderedDict[str, List[dict]]":
    """jra-archiveのhorses・payoutsを期待値派の行形式（{日付}_{race_id}→行）にする。"""
    def rd(sub):
        for f in sorted(glob.glob(str(archive / sub / "*.csv"))):
            with open(f, encoding="utf-8-sig", newline="") as fh:
                yield from csv.DictReader(fh)

    fuku: Dict[str, Dict[str, str]] = defaultdict(dict)
    for p in rd("payouts"):
        if p["bet_type"] == "複勝" and p["payout"].isdigit():
            fuku[p["race_id"]][p["combo"]] = p["payout"]
    races: Dict[str, List[dict]] = defaultdict(list)
    for h in rd("horses"):
        rid = h["race_id"]
        if not fuku.get(rid):
            continue
        races[f"{h['date']}_{rid}"].append({
            "horse_no": h["umaban"], "win_odds": h["tan_odds"],
            "place_odds_min": h["fuku_min"], "place_odds_max": h["fuku_max"],
            "finish_position": h["finish"] if h["finish"].isdigit() else "",
            "is_place": "1" if h["umaban"] in fuku[rid] else "0",
            "place_pay": fuku[rid].get(h["umaban"], ""), "jockey": h["jockey"],
        })
    return OrderedDict(sorted(races.items()))


# ---------------------------------------------------------------- JRA


def parse_odds_page(html: str) -> List[dict]:
    """単勝・複勝オッズページ（pw151）。取消・除外（単勝なし）の馬も含めて返す。"""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("table.tanpuku tr"):
        num = tr.find("td", class_="num")
        if num is None or not num.get_text(strip=True).isdigit():
            continue

        def txt(cls, sub=None):
            td = tr.find("td", class_=cls)
            if td is None:
                return ""
            node = td.select_one(sub) if sub else td
            return node.get_text(" ", strip=True) if node else ""

        fuku = txt("odds_fuku")
        lo, hi = (fuku.split("-") + [""])[:2] if "-" in fuku else (fuku, fuku)
        out.append({
            "horse_no": num.get_text(strip=True), "horse_name": txt("horse"),
            "win_odds": _num(txt("odds_tan")), "place_odds_min": _num(lo), "place_odds_max": _num(hi),
            "jockey": re.sub(r"\s+", " ", txt("jockey")),
        })
    return out


def _num(s: str) -> str:
    s = (s or "").strip()
    try:
        return str(float(s))
    except ValueError:
        return ""


def odds_cnames(html: str) -> Dict[str, dict]:
    """ページ内の単勝・複勝オッズCNAME。{race_id: {cname, date, venue, race_no}}"""
    out = {}
    for m in TANPUKU_RE.finditer(html):
        venue, year, kai, day, rno, date = m.groups()
        rid = f"{year}{venue}{kai}{day}{rno}"
        out.setdefault(rid, {"cname": m.group(0), "date": date, "venue": VENUES.get(venue, venue),
                             "race_no": int(rno), "race_id": rid})
    return out


def discover(client: JraClient, dates: List[str]) -> Dict[str, dict]:
    """オッズのトップ→開催ごとのレース一覧→各レースの単勝・複勝オッズCNAME。"""
    top = client.fetch("accessO.html", ODDS_TOP_CNAME)
    races = odds_cnames(top)
    for m in {m.group(0): m for m in ORL_RE.finditer(top)}.values():
        if m.group(5) not in dates:
            continue
        try:
            races.update(odds_cnames(client.fetch("accessO.html", m.group(0))))
        except Exception as e:  # 1開催の失敗で全体を止めない
            print(f"レース一覧の取得に失敗: {m.group(0)}: {e}", file=sys.stderr)
    return {k: v for k, v in races.items() if v["date"] in dates}


# ---------------------------------------------------------------- 買い目


def evaluate_race(rows: List[dict], model) -> List[dict]:
    # 上限3頭は指定騎手の馬の中で数える（検証と同じ）ので、ここでは絞らない
    params = replace(ev.EvParams(), ev_threshold=EV_THRESHOLD, max_bets_per_race=99)
    res = ev.evaluate([r for r in rows if r["win_odds"]], model, params)
    out = []
    for r in rows:
        v = res.get(int(r["horse_no"]))
        if not v or v["bet_flag"] != 1:
            continue
        out.append(dict(r, expected_value=v["expected_value"]))
    return out


def allocate(cands: List[dict]) -> List[dict]:
    """
    1レース分の指定騎手の候補に購入額を付ける（1レース3,000円）。
    1頭なら3,000円。2頭以上なら、どれが当たっても3,000円を上回って戻るように
    複勝オッズ下限の逆数で割り振る（Σ1/オッズ下限 < 0.9 になるまで期待値の低い馬を外す）。
    複勝オッズ下限が1.2倍未満の馬は買わない。参考の騎手は買わない。
    """
    for c in cands:
        c["stake"] = 0
    legs = sorted((c for c in cands if c["group"] == "bet" and float(c["place_odds_min"] or 0) >= MIN_PLACE_ODDS),
                  key=lambda c: -c["expected_value"])
    while legs:
        inv = [1 / float(c["place_odds_min"]) for c in legs]
        if len(legs) == 1 or sum(inv) < 0.9:
            stakes = [RACE_BUDGET * x / sum(inv) // 100 * 100 for x in inv]
            stakes[0] += RACE_BUDGET - sum(stakes)
            total = sum(stakes)
            if all(float(c["place_odds_min"]) * y > total for c, y in zip(legs, stakes)):
                for c, y in zip(legs, stakes):
                    c["stake"] = int(y)
                return cands
        legs = legs[:-1]
    return cands


def jockey_group(name: str) -> Optional[str]:
    if any(j in name for j in BET_JOCKEYS):
        return "bet"
    if any(j in name for j in WATCH_JOCKEYS):
        return "watch"
    return None


# ---------------------------------------------------------------- 出力


def weekend_key(date: str) -> str:
    d = datetime.strptime(date, "%Y%m%d")
    return (d - timedelta(days=(d.weekday() - 5) % 7)).strftime("%Y%m%d")  # その週の土曜


def bet_label(c: dict) -> str:
    return f"複勝{c['stake']:,}円" if c.get("stake") else "参考"


def render(store: dict, now: str) -> str:
    wd = "月火水木金土日"
    parts = [
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'><title>週末の買い目</title>",
        "<style>:root{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#ddd;--acc:#0b6e4f}"
        "@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--mut:#aaa;--line:#333;--acc:#4fc79c}}"
        "body{background:var(--bg);color:var(--fg);font-family:system-ui,sans-serif;margin:0 auto;max-width:860px;padding:16px}"
        "table{border-collapse:collapse;width:100%;font-size:14px}th,td{border-bottom:1px solid var(--line);padding:6px 4px;text-align:left}"
        ".n{text-align:right}.mut{color:var(--mut);font-size:13px}.buy{color:var(--acc);font-weight:600}.scroll{overflow-x:auto}</style></head><body>",
        "<h1>週末の買い目（期待値派×騎手）</h1>",
        f"<p class='mut'>更新 {escape(now)}（JST）。全レースが対象。期待値1.0以上・単勝50倍未満で、"
        "M.デムーロ・C.デムーロ・ルメール・川田が乗る馬の複勝を、1レース3,000円。"
        "1頭なら3,000円、2頭以上なら、どれが当たっても3,000円を上回って戻るように複勝オッズに合わせて割り振ります。"
        "複勝オッズの下限が1.2倍未満の馬は買いません。"
        "「参考」は武豊・岩田康誠・団野大成・戸崎圭太・田辺裕信で、成績を追うだけで買いません。"
        "オッズは取得時点のもので発走までに変わります。全5,256レースの検証（確定オッズ）では、この賭け方で前半104%・後半116%（135レース）でしたが、"
        "保証ではありません。</p>",
        "<p class='mut'><a href='./'>ダッシュボードへ戻る</a></p>",
    ]
    for wk in sorted(store, reverse=True):
        data = store[wk]
        parts.append(f"<h2>{wk[4:6]}/{wk[6:]}（土）の週</h2>")
        rows = sorted(data["picks"].values(), key=lambda c: (c["date"], c["venue"], c["race_no"], -c["stake"]))
        if not rows:
            parts.append("<p class='mut'>今のところ該当なし（見送り）。</p>")
            continue
        body = []
        for c in rows:
            d = datetime.strptime(c["date"], "%Y%m%d")
            res = c.get("result", "")
            body.append(
                f"<tr><td>{d.month}/{d.day}({wd[d.weekday()]})</td><td>{escape(c['venue'])}{c['race_no']}R</td>"
                f"<td>{escape(c['horse_no'])} {escape(c['horse_name'])}</td><td>{escape(c['jockey'])}</td>"
                f"<td class='n'>{escape(c['win_odds'])}</td><td class='n'>{escape(c['place_odds_min'])}-{escape(c['place_odds_max'])}</td>"
                f"<td class='n'>{c['expected_value']:.2f}</td>"
                f"<td class='{'buy' if c['stake'] else 'mut'}'>{bet_label(c)}</td>"
                f"<td>{escape(res)}</td></tr>"
            )
        total = sum(c["stake"] for c in rows)
        parts.append(
            f"<p>購入 {sum(1 for c in rows if c['stake'])}点・{total:,}円</p><div class='scroll'><table><thead><tr><th>日付</th><th>レース</th>"
            "<th>馬</th><th>騎手</th><th class='n'>単勝</th><th class='n'>複勝</th><th class='n'>期待値</th><th>買い</th>"
            f"<th>結果</th></tr></thead><tbody>{''.join(body)}</tbody></table></div>"
        )
    parts.append("</body></html>")
    return "".join(parts)


def run(archive: Path, out: Path, dates: List[str], client: Optional[JraClient] = None, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(JST)
    client = client or JraClient()
    races = load_archive_races(archive)
    model = ev.train(races)
    print(f"学習: {model.train_races if model else 0}レース")
    store_path = out / "picks.json"
    store = json.loads(store_path.read_text(encoding="utf-8")) if store_path.exists() else {}
    today = now.strftime("%Y%m%d")
    for wk in store.values():  # 終わった日の買い目は固定する
        for c in wk["picks"].values():
            if c["date"] < today:
                c["locked"] = True
    found = discover(client, dates)
    print(f"対象レース: {len(found)}")
    for rid, info in sorted(found.items(), key=lambda kv: (kv[1]["date"], kv[1]["venue"], kv[1]["race_no"])):
        try:
            rows = parse_odds_page(client.fetch("accessO.html", info["cname"]))
        except Exception as e:
            print(f"オッズの取得に失敗: {rid}: {e}", file=sys.stderr)
            continue
        wk = store.setdefault(weekend_key(info["date"]), {"picks": {}})
        # 同じレースの古い買い目は最新のオッズで置き換える（確定済みの買いは残す）
        for k in [k for k, c in wk["picks"].items() if c["race_id"] == rid and not c.get("locked")]:
            del wk["picks"][k]
        cands = [dict(c, group=jockey_group(c["jockey"])) for c in evaluate_race(rows, model)]
        for g in ("bet", "watch"):
            for c in sorted((c for c in cands if c["group"] == g), key=lambda c: -c["expected_value"])[:3]:
                wk["picks"][f"{rid}-{c['horse_no']}"] = dict(c, **{k: info[k] for k in ("race_id", "date", "venue", "race_no")})
    for wk in store.values():
        by_race = defaultdict(list)
        for c in wk["picks"].values():
            if not c.get("locked"):
                by_race[c["race_id"]].append(c)
        for cands in by_race.values():
            allocate(cands)
    out.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(store, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / "picks.html").write_text(render(store, now.strftime("%Y-%m-%d %H:%M")), encoding="utf-8")
    return store


def main() -> int:
    ap = argparse.ArgumentParser(description="週末の全レースから期待値派×騎手の買い目を出す")
    ap.add_argument("--archive", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--dates", help="対象日（YYYYMMDD,カンマ区切り）。省略時は今日と明日（JST）")
    a = ap.parse_args()
    now = datetime.now(JST)
    dates = a.dates.split(",") if a.dates else [now.strftime("%Y%m%d"), (now + timedelta(days=1)).strftime("%Y%m%d")]
    run(a.archive, a.out, dates)
    return 0


if __name__ == "__main__":
    sys.exit(main())
