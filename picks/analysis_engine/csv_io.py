"""
CSV入出力を担当する。

入力CSV（馬柱/オッズ/結果）は03_interfaceシステムでValidation済みであることを
前提とし、本モジュールでは入力チェックを行わない
（04_00_分析システム設計.md 2章「本システムの責務としない: CSV入力チェック」）。

出力CSV（分析CSV）のヘッダは 01_06_分析CSV設計.md の```csvコードブロックから
実行時に取得する。設計書と実装の二重管理を避けるため、ヘッダをここへ
ハードコーディングしない（03_interfaceシステムのheader_checker.pyと同じ方針）。
"""

import csv
import re
from pathlib import Path
from typing import Dict, List

_CSV_BLOCK_PATTERN = re.compile(r"```csv\s*\n(.+?)\n```", re.DOTALL)


def load_csv_header(design_doc_path: Path) -> List[str]:
    """CSV設計書（Markdown）から```csvコードブロック内のヘッダ行を取得する。"""
    text = design_doc_path.read_text(encoding="utf-8")
    match = _CSV_BLOCK_PATTERN.search(text)
    if not match:
        raise ValueError(f"設計書からCSVヘッダ定義を取得できません: {design_doc_path}")
    header_line = match.group(1).strip().splitlines()[0]
    return [col.strip() for col in header_line.split(",")]


def read_csv_rows(file_path: Path) -> List[Dict[str, str]]:
    """CSVファイルを読み込み、行を辞書のリストとして返す。"""
    with open(file_path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv_rows(file_path: Path, header: List[str], rows: List[Dict[str, str]]) -> None:
    """分析CSVを出力する。行に存在しない列は空欄として出力する。"""
    with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header, restval="")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
