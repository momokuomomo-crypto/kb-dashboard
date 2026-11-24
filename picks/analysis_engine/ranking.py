"""
04_01_評価ルール共通仕様.md 6章「同点処理」を実装する。

総合点の降順で順位を決定し、同点の場合は馬番の昇順で順位を決定する。
評価者ごとに異なる同点処理は定義しない（堅実派・回収率派・大穴派で共通）。
"""

from typing import Dict


def rank_by_score(scores: Dict[int, float]) -> Dict[int, int]:
    """
    馬番をキー、総合点を値とする辞書を受け取り、馬番をキー、順位
    （1始まり）を値とする辞書を返す。

    総合点の降順、同点の場合は馬番の昇順で順位を決定する。
    """
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return {horse_no: rank for rank, (horse_no, _) in enumerate(ordered, start=1)}
