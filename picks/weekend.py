"""
週末の全レース（重賞・非重賞・新馬戦）から「期待値派×指定騎手」の買い目を出す。

    python -m weekend --archive ../archive --out ../site_picks

1. jra-archive（確定済みの全レース）で期待値派のモデルを学習する
   （kb-project 04_07。対象レースより前の確定レースのみで学習）。
2. JRAのオッズページから、今日・明日（JST）の全レースの単勝・複勝オッズと騎手を取る。
3. 期待値1.0以上・単勝50倍未満・指定騎手（M.デムーロ、C.デムーロ、ルメール、川田）の
   馬（1レース3頭まで）を買い目にする。複勝で、金額はそのレースで一番高い期待値に比例（1.1→3,000円、1.2→6,000円、上限12,000円。2026-11-28から。逃げ先行2倍は
   後から全期間を見て決めた条件だったのでやめた）。期待値1.1未満の馬は買わず「参考」に出す。2頭以上なら、どれが
   当たってもそのレースの合計を上回って戻るように複勝オッズ下限の逆数で割り振る。
   複勝オッズの下限が1.2倍未満の馬は買わない。
   武豊と、検証で両期間とも100%を超えた騎手（岩田康誠・団野大成・戸崎圭太・田辺裕信）は
   「参考（買わない）」として表示する。
   前走で最初のコーナーを先頭集団（2番手以内か、頭数の2割以内）で回った馬には「逃げ先行」の印を付ける
   （参考。金額は変えない）
4. picks.json（週ごとの記録）と picks.html を出力する。

全レースでの検証（6,845レース、前半〜2025-09-05／後半）：指定4騎手×期待値1.0以上は
期待値1.1以上を期待値に比例した金額で買うと前半141%・後半146%（79レース、+263,200円、最大の落ち込み−52,750円）。ただし利益の大半は上位3レースで、
1.1の区切りは全期間を見て決めたので、本当の検証は実戦で行う（買った時点のオッズを記録、元手から6万円減ったら停止）。確定オッズでの検証なので、実際の購入時点の
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
NOTIFY_USER = "momokuomomo-crypto"  # 買いが出たらIssueでメンションする（GitHubの通知・メールが届く）
ODDS_TOP_CNAME = "pw15oli00/6D"
ORL_RE = re.compile(r"pw15orl\d{2}(\d{2})(\d{4})(\d{2})(\d{2})(\d{8})/[0-9A-F]{2}")
# 発売中は「ou」の後ろに「S3」などが付く（例 pw151ouS3052026050701…）。確定後は日付の後ろに「Z」が付く。発売中は付かないことがあるので、0〜3文字の英数字を許す
TANPUKU_RE = re.compile(r"pw151ou(?:\d{2}|[A-Z]\d)(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})(\d{8})[0-9A-Z]{0,3}/[0-9A-F]{2}")
VENUES = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
    "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
}
BET_JOCKEYS = ("M.デムーロ", "C.デムーロ", "ルメール", "川田")
WATCH_JOCKEYS = ("武 豊", "岩田 康誠", "団野", "戸崎", "田辺")
EV_THRESHOLD = 1.0
MIN_PLACE_ODDS = 1.2
BET_MIN_EV = 1.1  # 指定騎手でもこれ未満は買わない（参考に出す）
BASE_STAKE, MAX_STAKE = 3000, 12000  # 期待値1.1で3,000円、0.1上がるごとに+3,000円、上限12,000円


STYLE_MULTIPLIER = 1  # 一番期待値の高い買い馬が「逃げ先行」なら金額をこの倍にする


def style_multiplier(c: dict) -> int:
    return STYLE_MULTIPLIER if c.get("style") == "逃げ先行" else 1


def race_budget(top_ev: float) -> int:
    """そのレースで一番高い期待値に比例した金額（1.1→3,000円、1.2→6,000円、1.3→9,000円、上限12,000円、100円単位）。"""
    return int(min(MAX_STAKE, max(BASE_STAKE, round(BASE_STAKE * (top_ev - 1) / 0.1 / 100) * 100)))


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


def last_run_styles(archive: Path) -> Dict[str, str]:
    """馬名→前走の位置取り（逃げ先行／中団／差し追込）。前走の最初のコーナー順位と頭数で決める。"""
    rows = []
    for f in sorted(glob.glob(str(archive / "horses" / "*.csv"))):
        with open(f, encoding="utf-8-sig", newline="") as fh:
            rows.extend(csv.DictReader(fh))
    runners: Dict[str, int] = defaultdict(int)
    for h in rows:
        runners[h["race_id"]] += 1
    last: Dict[str, tuple] = {}
    for h in rows:
        name, cf = (h.get("horse_name") or "").strip(), (h.get("corner_first") or "").strip()
        if not name or not cf.isdigit():
            continue
        key = (h["date"], h["race_id"])
        if name not in last or key > last[name][0]:
            last[name] = (key, int(cf), runners[h["race_id"]])
    return {name: style_of(cf, n) for name, (_, cf, n) in last.items()}


def style_of(corner_first: int, runners: int) -> str:
    if corner_first <= max(2, round(runners * 0.2)):
        return "逃げ先行"
    return "中団" if corner_first <= runners * 0.6 else "差し追込"


# ---------------------------------------------------------------- JRA


TIME_RE = re.compile(r"(\d{1,2})時(\d{2})分")


def parse_post_time(html: str, date: str) -> Optional[datetime]:
    """オッズページの「発走時刻：12時15分」を日時にする。"""
    node = BeautifulSoup(html, "html.parser").select_one("div.cell.time")
    m = TIME_RE.search(node.get_text() if node else "")
    if not m:
        return None
    d = datetime.strptime(date, "%Y%m%d")
    return d.replace(hour=int(m.group(1)), minute=int(m.group(2)), tzinfo=JST)


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
    orl = {m.group(0): m for m in ORL_RE.finditer(top)}
    print(f"オッズのトップ: 開催 {sorted({m.group(5) for m in orl.values()})}・単複 {len(races)}件・{len(top)}文字")
    if not orl:  # 構造確認用
        print("オッズのトップのCNAME:", sorted(set(re.findall(r"pw1[0-9a-z]{4,6}\d*[0-9A-Z]*/[0-9A-F]{2}", top)))[:20])
    for m in orl.values():
        if m.group(5) not in dates:
            continue
        try:
            page = client.fetch("accessO.html", m.group(0))
            found = odds_cnames(page)
            if not found:  # 構造確認用
                title = re.search(r"<title>(.*?)</title>", page, re.S)
                print(f"単複のCNAMEなし: {m.group(0)}・{len(page)}文字・{title.group(1).strip() if title else ''}",
                      sorted(set(re.findall(r"doAction\([^)]*\)", page)))[:30])
            races.update(found)
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
    1レース分の指定騎手の候補に購入額を付ける。金額は買う馬の一番高い期待値で決める（race_budget）。
    1頭なら全額。2頭以上なら、どれが当たってもそのレースの合計を上回って戻るように
    複勝オッズ下限の逆数で割り振る（Σ1/オッズ下限 < 0.9 になるまで期待値の低い馬を外す）。
    複勝オッズ下限が1.2倍未満の馬は買わない。参考の騎手は買わない。
    """
    for c in cands:
        c["stake"] = 0
    legs = sorted((c for c in cands if c["group"] == "bet" and c["expected_value"] >= BET_MIN_EV
                   and float(c["place_odds_min"] or 0) >= MIN_PLACE_ODDS),
                  key=lambda c: -c["expected_value"])
    while legs:
        inv = [1 / float(c["place_odds_min"]) for c in legs]
        if len(legs) == 1 or sum(inv) < 0.9:
            budget = race_budget(legs[0]["expected_value"]) * style_multiplier(legs[0])
            stakes = [budget * x / sum(inv) // 100 * 100 for x in inv]
            stakes[0] += budget - sum(stakes)
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


def odds_line(c: dict) -> str:
    """直前チェック用：複勝オッズ下限がいくつまで下がったら見送りか（他の条件は今のまま、の目安）。"""
    if c.get("group") != "bet" or not c.get("place_odds_min"):
        return ""
    plo, e = float(c["place_odds_min"]), c["expected_value"]
    skip = max(MIN_PLACE_ODDS, plo * BET_MIN_EV / e)
    k = style_multiplier(c)
    if e < BET_MIN_EV:
        return f"{skip:.1f}以上になれば{race_budget(BET_MIN_EV) * k:,}円"
    return f"{skip:.1f}未満は見送り"


def bet_label(c: dict) -> str:
    return f"複勝{c['stake']:,}円" if c.get("stake") else "参考"


def render(store: dict, now: str) -> str:
    wd = "月火水木金土日"
    parts = [
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'><meta name='robots' content='noindex,nofollow'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'><title>週末の買い目</title>",
        "<style>:root{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#ddd;--acc:#0b6e4f}"
        "@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--mut:#aaa;--line:#333;--acc:#4fc79c}}"
        "body{background:var(--bg);color:var(--fg);font-family:system-ui,sans-serif;margin:0 auto;max-width:860px;padding:16px}"
        "table{border-collapse:collapse;width:100%;font-size:14px}th,td{border-bottom:1px solid var(--line);padding:6px 4px;text-align:left}"
        ".tag{display:inline-block;margin-left:4px;padding:0 4px;border:1px solid var(--acc);border-radius:3px;color:var(--acc);font-size:12px}"
        ".n{text-align:right}.mut{color:var(--mut);font-size:13px}.buy{color:var(--acc);font-weight:600}.scroll{overflow-x:auto}</style></head><body>",
        "<h1>週末の買い目（期待値派×騎手）</h1>",
        f"<p class='mut'>更新 {escape(now)}（JST）。全レースが対象。期待値1.0以上・単勝50倍未満で、"
        "M.デムーロ・C.デムーロ・ルメール・川田が乗る馬の複勝を買います。1レースの金額は、そのレースで一番高い期待値が"
        "期待値1.1以上の馬を、期待値に比例した金額で買います（1.1→3,000円、1.2→6,000円、1.3→9,000円、上限12,000円）。期待値1.1未満の馬は買わず、参考として表示します。"
        "2頭以上なら、どれが当たってもそのレースの合計を上回って戻るように複勝オッズに合わせて割り振ります。"
        "複勝オッズの下限が1.2倍未満の馬は買いません。"
        "「参考」は武豊・岩田康誠・団野大成・戸崎圭太・田辺裕信で、成績を追うだけで買いません。"
        "「逃げ先行」は前走で最初のコーナーを先頭集団で回った馬の印です（参考。金額は変えません）。"
        "オッズは取得時点のもので発走までに変わります。全6,845レースの検証（確定オッズ）では、この賭け方で前半141%・後半146%（79レース）でしたが、利益の大半は上位3レースによるもので、"
        "保証ではありません。</p>"
        "<p class='mut'>発走直前の確認：「直前の目安」は、複勝オッズの下限（例 1.5-2.3倍なら1.5）がこの値を下回ったら見送り、"
        "という目安です（期待値は複勝オッズにほぼ比例するとして計算）。4騎手が乗るレースは、発走の約40分前に最新オッズでこのページを自動で更新します（ほかに当日9時・12時）。直前に期待値が上がって買いに加わることもあります。発走後は買った時点の買い目のまま残します。</p>",
        "<p class='mut'><a href='./'>ダッシュボードへ戻る</a> ・ <a href='summary.html'>これまでのまとめ</a> ・ <a href='history.html'>購入履歴</a></p>",
    ]
    for wk in sorted(store, reverse=True):
        data = store[wk]
        parts.append(f"<h2>{wk[4:6]}/{wk[6:]}（土）の週</h2>")
        rows = sorted(data["picks"].values(), key=lambda c: (c["date"], c.get("post_time") or "99", c["venue"], c["race_no"], -c["stake"]))
        if not rows:
            parts.append("<p class='mut'>今のところ該当なし（見送り）。</p>")
            continue
        body = []
        for c in rows:
            d = datetime.strptime(c["date"], "%Y%m%d")
            res = c.get("result", "")
            body.append(
                f"<tr><td>{d.month}/{d.day}({wd[d.weekday()]}){f"<br><b>{escape(c['post_time'])}</b>" if c.get('post_time') else ''}</td><td>{escape(c['venue'])}{c['race_no']}R</td>"
                f"<td>{escape(c['horse_no'])} {escape(c['horse_name'])}{"<span class='tag'>逃げ先行</span>" if c.get('style') == '逃げ先行' else ''}</td><td>{escape(c['jockey'])}</td>"
                f"<td class='n'>{escape(c['win_odds'])}</td><td class='n'>{escape(c['place_odds_min'])}-{escape(c['place_odds_max'])}{f"<br><span class='mut'>{escape(c['odds_at'])}時点</span>" if c.get('odds_at') else ''}</td>"
                f"<td class='n'>{c['expected_value']:.2f}</td>"
                f"<td class='{'buy' if c['stake'] else 'mut'}'>{bet_label(c)}</td><td class='mut'>{odds_line(c)}</td>"
                f"<td>{escape(res)}</td></tr>"
            )
        total = sum(c["stake"] for c in rows)
        parts.append(
            f"<p>購入 {sum(1 for c in rows if c['stake'])}点・{total:,}円</p><div class='scroll'><table><thead><tr><th>日付・発走</th><th>レース</th>"
            "<th>馬</th><th>騎手</th><th class='n'>単勝</th><th class='n'>複勝</th><th class='n'>期待値</th><th>買い</th><th>直前の目安（複勝下限）</th>"
            f"<th>結果</th></tr></thead><tbody>{''.join(body)}</tbody></table></div>"
        )
    parts.append("</body></html>")
    return "".join(parts)


def run(archive: Path, out: Path, dates: List[str], client: Optional[JraClient] = None, now: Optional[datetime] = None,
        notify_file: Optional[Path] = None) -> dict:
    now = now or datetime.now(JST)
    client = client or JraClient()
    races = load_archive_races(archive)
    model = ev.train(races)
    styles = last_run_styles(archive)
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
            html = client.fetch("accessO.html", info["cname"])
            rows = parse_odds_page(html)
        except Exception as e:
            print(f"オッズの取得に失敗: {rid}: {e}", file=sys.stderr)
            continue
        post = parse_post_time(html, info["date"])
        wk = store.setdefault(weekend_key(info["date"]), {"picks": {}})
        if post and post <= now and any(c["race_id"] == rid for c in wk["picks"].values()):
            continue  # 発走後は、買った時点の買い目を残す
        extra = {"post_time": post.strftime("%H:%M") if post else "", "odds_at": now.strftime("%H:%M")}
        # 同じレースの古い買い目は最新のオッズで置き換える（確定済みの買いは残す）
        for k in [k for k, c in wk["picks"].items() if c["race_id"] == rid and not c.get("locked")]:
            del wk["picks"][k]
        cands = [dict(c, group=jockey_group(c["jockey"]), style=styles.get(c["horse_name"].strip(), ""))
                 for c in evaluate_race(rows, model)]
        for g in ("bet", "watch"):
            for c in sorted((c for c in cands if c["group"] == g), key=lambda c: -c["expected_value"])[:3]:
                wk["picks"][f"{rid}-{c['horse_no']}"] = dict(c, **{k: info[k] for k in ("race_id", "date", "venue", "race_no")}, **extra)
    for wk in store.values():
        by_race = defaultdict(list)
        for c in wk["picks"].values():
            if not c.get("locked"):
                by_race[c["race_id"]].append(c)
        for cands in by_race.values():
            allocate(cands)
    notes = collect_notifications(store, now)
    out.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(store, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / "picks.html").write_text(render(store, now.strftime("%Y-%m-%d %H:%M")), encoding="utf-8")
    if notify_file and notes:
        notify_file.write_text(notes, encoding="utf-8")
    return store


def collect_notifications(store: dict, now: datetime) -> str:
    """前回から新しく「買い」になった馬と、買いから外れた馬（発走前のもの）を通知用の文にする。
    通知済みは各週の notified に {キー: レース・馬} で残し、同じ買いを2回知らせない。"""
    new, dropped = [], []
    hm = now.strftime("%H:%M")
    today = now.strftime("%Y%m%d")
    upcoming = lambda c: (c["date"], c.get("post_time") or "99:99") > (today, hm)
    for wk in store.values():
        sent = wk.setdefault("notified", {})
        cur = {k: c for k, c in wk["picks"].items() if c.get("stake") and not c.get("locked")}
        for k, c in cur.items():
            if k not in sent and upcoming(c):
                new.append(c)
                sent[k] = {f: c.get(f, "") for f in ("date", "post_time", "venue", "race_no", "horse_no", "horse_name", "jockey")}
        for k in [k for k in sent if k not in cur]:
            c = sent[k]
            if not isinstance(c, dict):
                del sent[k]
                continue
            locked = wk["picks"].get(k, {}).get("locked")
            if upcoming(c) and not locked:
                dropped.append(c)
                del sent[k]
            elif not upcoming(c):
                del sent[k]
    if not new and not dropped:
        return ""
    wd = "月火水木金土日"
    def line(c):
        d = datetime.strptime(c["date"], "%Y%m%d")
        return (f"- {d.month}/{d.day}({wd[d.weekday()]}) {c.get('post_time', '')}発走 {c['venue']}{c['race_no']}R "
                f"{c['horse_no']} {c['horse_name']}（{c['jockey']}）")
    parts = [f"@{NOTIFY_USER} 買い目が変わりました（{hm} のオッズ）", ""]
    if new:
        parts.append("**買い**")
        parts += [f"{line(c)} 複勝{c['stake']:,}円・期待値{c['expected_value']:.2f}・{odds_line(c)}" for c in new]
        parts.append("")
    if dropped:
        parts.append("**見送りに変更**（オッズが下がったため）")
        parts += [line(c) for c in dropped]
        parts.append("")
    parts.append("最新は https://momokuomomo-crypto.github.io/kb-dashboard/picks.html")
    return "\n".join(parts) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="週末の全レースから期待値派×騎手の買い目を出す")
    ap.add_argument("--archive", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--dates", help="対象日（YYYYMMDD,カンマ区切り）。省略時は今日と明日（JST）")
    ap.add_argument("--notify-file", type=Path, help="新しい買いがあれば通知文をここに書く")
    a = ap.parse_args()
    now = datetime.now(JST)
    dates = a.dates.split(",") if a.dates else [now.strftime("%Y%m%d"), (now + timedelta(days=1)).strftime("%Y%m%d")]
    run(a.archive, a.out, dates, notify_file=a.notify_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
