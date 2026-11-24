"""
CSV値の共通パース処理を行う。

04_01_評価ルール共通仕様.md 4章「情報不足時」に基づき、値が空欄または
数値として解釈できない場合は None を返す（推測による補完は行わない）。
"""

from typing import Optional


def parse_optional_int(value: Optional[str]) -> Optional[int]:
    value = (value or "").strip()
    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_optional_float(value: Optional[str]) -> Optional[float]:
    value = (value or "").strip()
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None
