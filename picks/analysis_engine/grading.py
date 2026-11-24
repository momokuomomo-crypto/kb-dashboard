"""
04_05_評価運用ルール.md を実装する。

評価結果（順位）を評価ランク（S/A/B/C）へ変換し、購入対象・購入金額・
購入券種を決定する。評価アルゴリズム自体はここでは扱わない
（04_05 1章「評価アルゴリズムは定義しない」）。

購入金額は本来運用時に設定する値だが、設定方法が設計未定義のため、
暫定固定値 1,000円で実装する（ISSUE-002、将来config化予定）。
"""

from dataclasses import dataclass
from typing import Dict

# 3章 評価ランク：S=3頭, A=3頭, B=6頭, C=残り（累計人数の閾値）
_S_CUTOFF = 3
_A_CUTOFF = 6
_B_CUTOFF = 12

# ISSUE-002: 暫定固定値。将来config化する。
BET_AMOUNT = 1000

# 6章 購入券種
BET_TYPE = "複勝"


@dataclass(frozen=True)
class GradingParams:
    """評価ランク・購入金額の判定定数。バックテストでの変更検証用に外部化する。

    デフォルト値は04_05_評価運用ルール.mdに記載の値と一致する。
    """

    s_cutoff: int = _S_CUTOFF
    a_cutoff: int = _A_CUTOFF
    b_cutoff: int = _B_CUTOFF
    bet_amount: int = BET_AMOUNT


def grade_for_rank(rank: int, params: GradingParams = GradingParams()) -> str:
    """順位からS/A/B/Cランクを決定する。"""
    if rank <= params.s_cutoff:
        return "S"
    if rank <= params.a_cutoff:
        return "A"
    if rank <= params.b_cutoff:
        return "B"
    return "C"


def build_purchase_info(
    ranks: Dict[int, int], params: GradingParams = GradingParams()
) -> Dict[int, Dict[str, object]]:
    """
    馬番をキー、順位を値とする辞書から、馬番ごとの評価ランク・
    購入対象フラグ・購入金額を返す。

    購入対象はS評価のみとする（4章）。
    """
    results: Dict[int, Dict[str, object]] = {}
    for horse_no, rank in ranks.items():
        grade = grade_for_rank(rank, params)
        is_target = grade == "S"
        results[horse_no] = {
            "grade": grade,
            "bet_flag": 1 if is_target else 0,
            "bet_amount": params.bet_amount if is_target else 0,
        }
    return results
