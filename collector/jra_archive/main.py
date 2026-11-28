"""
全レースアーカイブ（06_08）のCLI。

    python -m jra_archive.main collect --archive DIR [--from YYYY-MM-DD] [--to YYYY-MM-DD]
        [--shinba-only] [--no-combos] [--budget-min N] [--git-push]

--from/--to省略時は直近8日間（毎週の自動収集用）。--git-pushを付けると
一定件数ごとにDIR（jra-archiveブランチの作業ツリー）でcommit・pushする。
"""

import argparse
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .collector import collect, collect_pedigree, fix_grades
from race_collector.jra_client import JraClient
from .store import ArchiveStore


def _git(root: Path, *args, check=True, capture=False):
    return subprocess.run(["git", "-C", str(root), *args], check=check,
                          capture_output=capture, text=True)


def _git_commit_push(root: Path, message: str) -> None:
    """
    DIR（jra-archiveの作業ツリー）の変更をcommitしてpushする。他の収集ジョブが
    先にpushしていた場合は、最新のjra-archiveに自分の変更を行単位で重ね直して
    （CSVはキーで和集合、自分の行を優先）再pushする。rebaseの衝突で止まらない。
    """
    _git(root, "add", "-A")
    if _git(root, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return
    changed = _git(root, "diff", "--cached", "--name-only", capture=True).stdout.split()
    _git(root, "commit", "-qm", message)
    for attempt in range(5):
        if _git(root, "push", "-q", "origin", "HEAD:jra-archive", check=False).returncode == 0:
            return
        time.sleep(5 * (attempt + 1))
        ours = {f: (root / f).read_bytes() for f in changed if (root / f).exists()}
        _git(root, "fetch", "-q", "--depth", "1", "origin", "jra-archive")
        _git(root, "reset", "-q", "--hard", "FETCH_HEAD")
        for f, data in ours.items():
            path = root / f
            path.parent.mkdir(parents=True, exist_ok=True)
            if f.endswith(".csv") and path.exists():
                path.write_text(merge_csv(path.read_text(encoding="utf-8"),
                                          data.decode("utf-8"), f), encoding="utf-8")
            else:
                path.write_bytes(data)
        _git(root, "add", "-A")
        if _git(root, "diff", "--cached", "--quiet", check=False).returncode == 0:
            return
        _git(root, "commit", "-qm", message)
    raise RuntimeError("jra-archiveブランチへのpushに失敗しました")


_MERGE_KEYS = {
    "pedigree": ("horse_id",),
    "races": ("race_id",),
    "horses": ("race_id", "umaban", "horse_id"),
    "payouts": ("race_id", "bet_type", "combo"),
}


def merge_csv(theirs: str, ours: str, path: str) -> str:
    """同じCSVの2版を、キーで和集合にする（同じキーは自分の行を優先、列は和集合）。"""
    import csv
    import io

    key_cols = _MERGE_KEYS.get(path.split("/", 1)[0])
    t = list(csv.DictReader(io.StringIO(theirs)))
    o_reader = csv.DictReader(io.StringIO(ours))
    o = list(o_reader)
    cols = list(o_reader.fieldnames or [])
    for r in t:
        for k in r:
            if k not in cols:
                cols.append(k)
    if key_cols is None:
        key_cols = tuple(cols)
    rows = {}
    for r in t + o:
        rows[tuple(r.get(k, "") for k in key_cols)] = r
    out = io.StringIO()
    w = csv.DictWriter(out, cols, extrasaction="ignore", restval="", lineterminator="\n")
    w.writeheader()
    for k in sorted(rows, key=lambda k: tuple((0, int(x), "") if str(x).isdigit() else (1, 0, str(x)) for x in k)):
        w.writerow(rows[k])
    return out.getvalue()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--archive", required=True)
    c.add_argument("--from", dest="start")
    c.add_argument("--to", dest="end")
    c.add_argument("--shinba-only", action="store_true")
    c.add_argument("--graded-only", action="store_true", help="G1〜G3のみ")
    c.add_argument("--no-combos", action="store_true")
    c.add_argument("--budget-min", type=float, default=0)
    c.add_argument("--git-push", action="store_true")
    pd = sub.add_parser("pedigree")
    pd.add_argument("--archive", required=True)
    pd.add_argument("--shinba-only", action="store_true")
    pd.add_argument("--since", default="", help="この日付（YYYYMMDD）以降のレースの出走馬だけ")
    pd.add_argument("--budget-min", type=float, default=0)
    pd.add_argument("--interval", type=float, default=1.0)
    pd.add_argument("--git-push", action="store_true")
    fg = sub.add_parser("fix-grades")
    fg.add_argument("--archive", required=True)
    fg.add_argument("--from", dest="start", default="2024-11-01")
    fg.add_argument("--to", dest="end")
    fg.add_argument("--git-push", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "fix-grades":
        root = Path(a.archive)
        end = datetime.strptime(a.end, "%Y-%m-%d").date() if a.end else date.today()
        n = fix_grades(ArchiveStore(root), datetime.strptime(a.start, "%Y-%m-%d").date(), end)
        print(f"グレードを補ったレース: {n}")
        if a.git_push:
            _git_commit_push(root, f"data(06_08): 重賞のグレードを補完（{n}レース）")
        return 0

    if a.cmd == "pedigree":
        root = Path(a.archive)
        deadline = time.monotonic() + a.budget_min * 60 if a.budget_min else None
        on_commit = (lambda msg: _git_commit_push(root, msg)) if a.git_push else (lambda msg: None)
        report = collect_pedigree(ArchiveStore(root), shinba_only=a.shinba_only, since=a.since,
                                  client=JraClient(interval=a.interval), deadline=deadline,
                                  on_commit=on_commit)
        print(f"血統 保存 {report.saved} / 残り {report.remaining} / 失敗 {len(report.errors)}")
        for e in report.errors[:50]:
            print("  " + e)
        return 1 if len(report.errors) > max(10, report.saved // 10) else 0

    jst = datetime.utcnow() + timedelta(hours=9)
    # 省略時の終了日：JSTで18時以降なら今日（その日のレースは終わっている）、それ前なら昨日
    default_end = jst.date() if jst.hour >= 18 else jst.date() - timedelta(days=1)
    end = datetime.strptime(a.end, "%Y-%m-%d").date() if a.end else default_end
    start = datetime.strptime(a.start, "%Y-%m-%d").date() if a.start else end - timedelta(days=7)
    root = Path(a.archive)
    deadline = time.monotonic() + a.budget_min * 60 if a.budget_min else None
    on_commit = (lambda msg: _git_commit_push(root, msg)) if a.git_push else (lambda msg: None)
    report = collect(ArchiveStore(root), start, end, shinba_only=a.shinba_only,
                     graded_only=a.graded_only, with_combos=not a.no_combos, deadline=deadline, on_commit=on_commit)
    print(f"保存 {report.saved} / 取得済み {report.skipped} / 残り {report.remaining} / 失敗 {len(report.errors)}")
    for e in report.errors:
        print("  " + e)
    # 失敗が全体の1割を超えたら異常として扱う
    return 1 if report.errors and len(report.errors) > max(3, report.saved // 10) else 0


if __name__ == "__main__":
    sys.exit(main())
