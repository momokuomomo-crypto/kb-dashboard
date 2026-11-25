"""
締切直前の単勝・複勝オッズを記録する（確定オッズとの差を見るため）。

    python -m live_odds --out ../odds --until 13:30 [--git-push]

その日（JST）の全レースについて、発走の10分前・5分前・3分前（JRAの発売締切は
発走2分前）に単勝・複勝オッズページ（pw151）を取り、
odds_live/YYYYMMDD.csv に1頭1行で追記する。発走時刻はオッズページの「発走時刻」から読む。
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
from weekend import JST, discover, parse_odds_page

SNAP_MINUTES = (10, 5, 3)  # 発走の何分前に取るか
COLUMNS = ["race_id", "date", "venue", "race_no", "post_time", "minutes_before", "fetched_at",
           "horse_no", "horse_name", "jockey", "win_odds", "place_odds_min", "place_odds_max"]
TIME_RE = re.compile(r"(\d{1,2})時(\d{2})分")


def parse_post_time(html: str, date: str) -> Optional[datetime]:
    """オッズページの「発走時刻：12時15分」を日時にする。"""
    node = BeautifulSoup(html, "html.parser").select_one("div.cell.time")
    m = TIME_RE.search(node.get_text() if node else "")
    if not m:
        return None
    d = datetime.strptime(date, "%Y%m%d")
    return d.replace(hour=int(m.group(1)), minute=int(m.group(2)), tzinfo=JST)


def due_snapshots(post: datetime, now: datetime, taken: set) -> List[int]:
    """今取るべき「発走N分前」。発走後は取らない。遅れて来たら一番近いものだけ取る。"""
    if now >= post:
        return []
    due = [m for m in SNAP_MINUTES if m not in taken and now >= post - timedelta(minutes=m)]
    return due[-1:]  # 10分前を逃して5分前の時刻なら5分前だけ


def append_rows(out: Path, date: str, rows: List[dict]) -> None:
    path = out / "odds_live" / f"{date}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def snapshot(client, info: dict, post: datetime, minutes: int, now: datetime, html: Optional[str] = None) -> List[dict]:
    html = html if html is not None else client.fetch("accessO.html", info["cname"])
    return [dict(r, race_id=info["race_id"], date=info["date"], venue=info["venue"], race_no=info["race_no"],
                 post_time=post.strftime("%H:%M"), minutes_before=minutes,
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


def run(out: Path, date: str, until: datetime, client=None,
        clock: Callable[[], datetime] = lambda: datetime.now(JST),
        sleep: Callable[[float], None] = time.sleep, on_commit: Callable[[str], None] = lambda m: None,
        commit_every: int = 1800, poll: float = 20.0) -> Dict[str, set]:
    client = client or JraClient()
    races = discover(client, [date])
    print(f"{date} 対象 {len(races)} レース")
    posts: Dict[str, datetime] = {}
    taken: Dict[str, set] = {rid: set() for rid in races}
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
    while True:
        now = clock()
        pending = [rid for rid, p in posts.items() if p > now]
        if not pending or now >= until:
            break
        for rid in sorted(pending, key=lambda r: posts[r]):
            for m in due_snapshots(posts[rid], clock(), taken[rid]):
                try:
                    rows = snapshot(client, races[rid], posts[rid], m, clock())
                except Exception as e:
                    print(f"[失敗] {rid} {m}分前: {e}", file=sys.stderr)
                    continue
                append_rows(out, date, rows)
                taken[rid].update(x for x in SNAP_MINUTES if x >= m)
                saved += 1
        if (clock() - last_commit).total_seconds() >= commit_every:
            on_commit(f"data: 締切前オッズ {date}（{saved}回分）")
            last_commit = clock()
        sleep(poll)
    on_commit(f"data: 締切前オッズ {date}（{saved}回分）")
    print(f"記録 {saved} 回")
    return taken


def main() -> int:
    ap = argparse.ArgumentParser(description="締切直前の単勝・複勝オッズを記録する")
    ap.add_argument("--out", type=Path, required=True, help="odds-liveブランチの作業ディレクトリ")
    ap.add_argument("--date", help="YYYYMMDD（省略時は今日 JST）")
    ap.add_argument("--until", default="17:30", help="この時刻（JST）で終わる")
    ap.add_argument("--git-push", action="store_true")
    a = ap.parse_args()
    now = datetime.now(JST)
    date = a.date or now.strftime("%Y%m%d")
    h, m = map(int, a.until.split(":"))
    until = datetime.strptime(date, "%Y%m%d").replace(hour=h, minute=m, tzinfo=JST)
    on_commit = (lambda msg: git_push(a.out, msg)) if a.git_push else (lambda msg: None)
    run(a.out, date, until, on_commit=on_commit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
