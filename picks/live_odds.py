"""
締切直前の単勝・複勝オッズを記録する（確定オッズとの差を見るため）。

    python -m live_odds --out ../odds --until 13:30 [--git-push]

その日（JST）の全レースについて、当日9時・12時の時点と、発走の60分前・30分前・
10分前・5分前・3分前（JRAの発売締切は発走2分前）に単勝・複勝オッズページ（pw151）を取り、
odds_live/YYYYMMDD.csv に1頭1行で追記する。発走時刻はオッズページの「発走時刻」から読む。
--eve を付けると翌日の全レースを今の時点で1回だけ取る（point="前日"。前日20時の記録用）。
過去のレースの「締切前オッズ」はJRAに残らないので、記録は今後のレースだけ。
確定オッズは jra-archive の horses/*.csv（tan_odds, fuku_min, fuku_max）にある。
"""

import argparse
import csv
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

from bs4 import BeautifulSoup

from jra_client import JraClient
from weekend import JST, discover, jockey_group, parse_odds_page, parse_post_time

SNAP_MINUTES = (60, 30, 10, 5, 3)  # 発走の何分前に取るか
SNAP_CLOCKS = ("09:00", "12:00")  # 当日のこの時刻にも全レース（発走前のもの）を取る
PRERACE_MIN = 40  # 4騎手（単勝50倍未満）が乗るレースは発走のこの分数前に買い目ページを更新する
REFRESH_CLOCKS = ("09:00", "12:00")  # この時刻にも買い目ページを更新する（定時実行が遅れることがあるため、ここから頼む）
PRERACE_GAP_MIN = 10  # 更新を頼んだあと、この分数は次を頼まない（1回の更新で全レースが新しくなる）
CLOCK_LATE_MIN = 30  # 時刻の記録は30分遅れまで（ジョブの起動遅れ用）
COLUMNS = ["race_id", "date", "venue", "race_no", "post_time", "point", "minutes_before", "fetched_at",
           "horse_no", "horse_name", "jockey", "win_odds", "place_odds_min", "place_odds_max"]



def due_snapshots(post: datetime, now: datetime, taken: set) -> List[str]:
    """今取るべき時点（"60"〜"3" は発走N分前、"09:00" などは当日の時刻）。発走後は取らない。
    発走前の分は遅れて来たら一番近いものだけ取る。"""
    if now >= post:
        return []
    out = []
    for c in SNAP_CLOCKS:
        h, m = map(int, c.split(":"))
        at = post.replace(hour=h, minute=m)
        if c not in taken and at <= now < at + timedelta(minutes=CLOCK_LATE_MIN) and at < post:
            out.append(c)
    due = [str(m) for m in SNAP_MINUTES if str(m) not in taken and now >= post - timedelta(minutes=m)]
    return out + due[-1:]  # 10分前を逃して5分前の時刻なら5分前だけ


def load_taken(out: Path, date: str) -> Dict[str, set]:
    """同じ日に前のジョブが取った分（午前→午後の引き継ぎ）。"""
    path = out / "odds_live" / f"{date}.csv"
    taken: Dict[str, set] = {}
    if path.exists():
        for r in csv.DictReader(open(path, encoding="utf-8")):
            taken.setdefault(r["race_id"], set()).add(r.get("point") or r.get("minutes_before"))
    return taken


def append_rows(out: Path, date: str, rows: List[dict]) -> None:
    path = out / "odds_live" / f"{date}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def snapshot(client, info: dict, post: datetime, point: str, now: datetime, html: Optional[str] = None) -> List[dict]:
    html = html if html is not None else client.fetch("accessO.html", info["cname"])
    return [dict(r, race_id=info["race_id"], date=info["date"], venue=info["venue"], race_no=info["race_no"],
                 post_time=post.strftime("%H:%M"), point=point,
                 minutes_before=int((post - now).total_seconds() // 60),
                 fetched_at=now.strftime("%Y-%m-%d %H:%M:%S"))
            for r in parse_odds_page(html)]


def git_push(root: Path, message: str) -> None:
    def git(*args, check=True):
        return subprocess.run(["git", "-C", str(root), *args], check=check)
    git("add", "-A")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return
    git("commit", "-qm", message)
    for _ in range(5):
        if git("push", "-q", "origin", "HEAD:odds-live", check=False).returncode == 0:
            return
        git("pull", "-q", "--rebase", "origin", "odds-live", check=False)
        time.sleep(5)
    print("[失敗] pushできませんでした", file=sys.stderr)


def has_candidate(client, info: dict) -> bool:
    """買い対象の4騎手が単勝50倍未満の馬に乗っているか（直前に期待値1.1を超えて買いになりうるレース）。"""
    try:
        rows = parse_odds_page(client.fetch("accessO.html", info["cname"]))
    except Exception as e:
        print(f"[失敗] {info.get('race_id')}: {e}", file=sys.stderr)
        return False
    for r in rows:
        try:
            ok = float(r["win_odds"]) < 50
        except (TypeError, ValueError):
            ok = False
        if ok and jockey_group(r["jockey"]) == "bet":
            return True
    return False


def run(out: Path, date: str, until: datetime, client=None,
        clock: Callable[[], datetime] = lambda: datetime.now(JST),
        sleep: Callable[[float], None] = time.sleep, on_commit: Callable[[str], None] = lambda m: None,
        commit_every: int = 1800, poll: float = 20.0,
        on_prerace: Callable[[str, str], bool] = lambda rid, date: False) -> Dict[str, set]:
    client = client or JraClient()
    races = discover(client, [date])
    print(f"{date} 対象 {len(races)} レース")
    posts: Dict[str, datetime] = {}
    prev = load_taken(out, date)
    taken: Dict[str, set] = {rid: set(prev.get(rid, ())) for rid in races}
    for rid, info in sorted(races.items()):
        try:
            html = client.fetch("accessO.html", info["cname"])
        except Exception as e:
            print(f"[失敗] {rid}: {e}", file=sys.stderr)
            continue
        post = parse_post_time(html, date)
        if post:
            posts[rid] = post
    last_commit = clock()
    saved = 0
    prerace_done: set = set()
    last_prerace: Optional[datetime] = None
    while True:
        now = clock()
        pending = [rid for rid, p in posts.items() if p > now]
        if not pending or now >= until:
            break
        for c in REFRESH_CLOCKS:
            h, mi = map(int, c.split(":"))
            at = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            if c not in prerace_done and at <= now < at + timedelta(minutes=CLOCK_LATE_MIN):
                prerace_done.add(c)
                if on_prerace(c, date):
                    last_prerace = clock()
        for rid in sorted(pending, key=lambda r: posts[r]):
            if rid not in prerace_done and clock() >= posts[rid] - timedelta(minutes=PRERACE_MIN):
                prerace_done.add(rid)
                recent = last_prerace and clock() - last_prerace < timedelta(minutes=PRERACE_GAP_MIN)
                if not recent and has_candidate(client, races[rid]) and on_prerace(rid, date):
                    last_prerace = clock()
            for m in due_snapshots(posts[rid], clock(), taken[rid]):
                try:
                    rows = snapshot(client, races[rid], posts[rid], m, clock())
                except Exception as e:
                    print(f"[失敗] {rid} {m}分前: {e}", file=sys.stderr)
                    continue
                append_rows(out, date, rows)
                taken[rid].add(m)
                if m.isdigit():
                    taken[rid].update(str(x) for x in SNAP_MINUTES if x >= int(m))
                saved += 1
        if (clock() - last_commit).total_seconds() >= commit_every:
            on_commit(f"data: 締切前オッズ {date}（{saved}回分）")
            last_commit = clock()
        sleep(poll)
    on_commit(f"data: 締切前オッズ {date}（{saved}回分）")
    print(f"記録 {saved} 回")
    return taken


def eve(out: Path, date: str, client=None, now: Optional[datetime] = None,
        on_commit: Callable[[str], None] = lambda m: None) -> int:
    """前日の夜に、翌日（date）の全レースのオッズを1回取る。"""
    client = client or JraClient()
    races = discover(client, [date])
    done = {rid for rid, v in load_taken(out, date).items() if "前日" in v}
    n = 0
    for rid, info in sorted(races.items()):
        if rid in done:  # レースごとに1回だけ（前日発売が先に始まる重賞など）
            continue
        try:
            html = client.fetch("accessO.html", info["cname"])
        except Exception as e:
            print(f"[失敗] {rid}: {e}", file=sys.stderr)
            continue
        post = parse_post_time(html, date)
        if post is None:
            continue
        append_rows(out, date, snapshot(client, info, post, "前日", now or datetime.now(JST), html=html))
        n += 1
    on_commit(f"data: 前日オッズ {date}（{n}レース）")
    print(f"{date} 前日の記録 {n} レース")
    return n


def make_prerace_dispatch(repo: str, token: str, workflow: str = "weekend_picks.yml"):
    """買い目の更新ワークフロー（weekend_picks.yml）を起動する。"""
    import requests

    api = f"https://api.github.com/repos/{repo}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    def dispatch(rid: str, date: str) -> bool:
        r = requests.post(f"{api}/actions/workflows/{workflow}/dispatches", headers=headers,
                          json={"ref": "main", "inputs": {"dates": date}}, timeout=30)
        print(f"買い目更新を依頼（{rid}）→ {r.status_code}")
        return r.status_code == 204

    return dispatch


def main() -> int:
    ap = argparse.ArgumentParser(description="締切直前の単勝・複勝オッズを記録する")
    ap.add_argument("--out", type=Path, required=True, help="odds-liveブランチの作業ディレクトリ")
    ap.add_argument("--date", help="YYYYMMDD（省略時は今日 JST）")
    ap.add_argument("--until", default="17:30", help="この時刻（JST）で終わる")
    ap.add_argument("--git-push", action="store_true")
    ap.add_argument("--eve", action="store_true", help="翌日の全レースを今の時点で1回取る")
    a = ap.parse_args()
    now = datetime.now(JST)
    on_commit = (lambda msg: git_push(a.out, msg)) if a.git_push else (lambda msg: None)
    if a.eve:
        if not a.date and now.hour < 15:  # 定時実行が深夜・朝まで遅れたら翌日の前日分にはならないので取らない
            print(f"{now:%H:%M} は前日の記録の時間外なので何もしません")
            return 0
        eve(a.out, a.date or (now + timedelta(days=1)).strftime("%Y%m%d"), on_commit=on_commit)
        return 0
    date = a.date or now.strftime("%Y%m%d")
    h, m = map(int, a.until.split(":"))
    until = datetime.strptime(date, "%Y%m%d").replace(hour=h, minute=m, tzinfo=JST)
    import os
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    kw = {"on_prerace": make_prerace_dispatch(repo, token)} if token and repo else {}
    run(a.out, date, until, on_commit=on_commit, **kw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
