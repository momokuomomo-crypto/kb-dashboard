"""
04_07_評価ルール詳細（期待値派）.md を実装する。

期待値派は「オッズが実力の割に高い（＝思ったより配当が付く）馬」だけを
複勝で買う。各馬について

    期待値(EV) = 推定複勝率 × 推定複勝配当（倍）

を計算し、EV が閾値（既定 1.0）以上の馬のみを購入対象とする。
EV 1.0 は「100円賭けると平均100円戻る」ことを意味する。

推定複勝率は次の2段階で求める。

1. 市場の見立て：単勝オッズから各馬の勝率を逆算し（控除分を正規化で除去）、
   Harvilleモデルで「3着以内（7頭以下は2着以内）に入る確率」へ変換する。
2. 補正：過去レースの実績で、市場の見立てと堅実派の能力スコアを説明変数に
   した ロジスティック回帰 を学習し、市場の癖（人気馬の過大評価など）と
   能力評価の上乗せ分を反映する。

推定複勝配当は、発売時の複勝オッズ（下限〜上限）のうち、過去の的中時に
実際の払戻がどの位置に来たかの平均で内分して求める。

学習には **対象レースより前の日付の確定済みレースのみ** を使う。そのため
過去レースを再処理しても未来の結果が混ざらず（ウォークフォワード）、
ダッシュボードの期待値派の収支はそのままサンプル外の成績になる。
学習レース数が min_train_races 未満の間は購入しない。
"""

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import csv_io
from .grading import BET_AMOUNT
from .parsing import parse_optional_float, parse_optional_int
from .ranking import rank_by_score

_RACE_CSV_SUFFIX = "_分析.csv"


def _num(value) -> Optional[float]:
    """CSV由来の文字列でも、評価直後の数値でも受け付ける。"""
    return parse_optional_float(None if value is None else str(value))


def _int(value) -> Optional[int]:
    return parse_optional_int(None if value is None else str(value))
_EPS = 1e-4


@dataclass(frozen=True)
class EvParams:
    """期待値派の定数。デフォルト値は04_07の記載と一致する。"""

    # この値以上のEVの馬を購入する
    ev_threshold: float = 0.9
    # 単勝オッズがこの値以上の馬は買わない（超人気薄は市場平均でも回収率が
    # 特に低く、推定も不安定なため）。0なら制限しない
    max_win_odds: float = 50.0
    # 1レースあたりの購入上限（EVの高い順）
    max_bets_per_race: int = 3
    # 学習に必要な確定済みレース数。満たない間は購入しない
    min_train_races: int = 60
    # ロジスティック回帰のL2正則化の強さ（切片には掛けない）
    l2: float = 1.0
    # 複勝オッズから逆算した確率（複勝票の見立て）を説明変数に使うか
    use_place_pool: bool = False
    # 能力スコアを説明変数に使うか
    use_ability: bool = False
    # 騎手の「市場の見立てより何回多く馬券に絡んだか」を説明変数に使うか
    use_jockey: bool = False
    # 騎手成績の縮約（騎乗数がこの値くらいになるまで0に寄せる）
    jockey_prior_rides: int = 30
    bet_amount: int = BET_AMOUNT


@dataclass(frozen=True)
class EvModel:
    coef: Tuple[float, ...]  # (切片, 単勝由来logit[, 複勝由来logit][, 能力z])
    payout_position: float  # 複勝オッズ下限〜上限のどこに払戻が来るか（0〜1）
    train_races: int
    # 騎手名 -> (騎乗数, 複勝回数 - 市場の見立ての合計)
    jockey_stats: Optional[Dict[str, Tuple[int, float]]] = None


# ---------------------------------------------------------------- 市場の見立て


def place_slots(field_size: int) -> int:
    """複勝の対象着順数（JRA：8頭以上は3着まで、5〜7頭は2着まで）。"""
    return 3 if field_size >= 8 else 2


def harville_place_probs(win_probs: Sequence[float], slots: int) -> List[float]:
    """勝率から「slots着以内に入る確率」をHarvilleモデルで計算する。"""
    n = len(win_probs)
    out = [0.0] * n

    def rec(remaining: List[int], prob: float, depth: int) -> None:
        if depth == slots or not remaining:
            return
        total = sum(win_probs[j] for j in remaining)
        if total <= 0:
            return
        for j in remaining:
            pj = prob * win_probs[j] / total
            out[j] += pj
            rec([x for x in remaining if x != j], pj, depth + 1)

    rec(list(range(n)), 1.0, 0)
    return out


def _logit(p: float) -> float:
    p = min(max(p, _EPS), 1 - _EPS)
    return math.log(p / (1 - p))


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    e = math.exp(z)
    return e / (1 + e)


@dataclass
class _Horse:
    horse_no: int
    market_place_prob: float
    pool_place_prob: float
    ability_z: float
    jockey: str
    win_odds: float
    place_odds_min: Optional[float]
    place_odds_max: Optional[float]


def race_features(rows: Iterable[Dict[str, str]]) -> List[_Horse]:
    """
    1レース分の行（win_odds・place_odds_min/max・solid_scoreを含む）から
    馬ごとの特徴量を作る。単勝オッズのない馬（取消・除外）は除く。
    """
    entries = []
    for row in rows:
        horse_no = _int(row.get("horse_no"))
        win_odds = _num(row.get("win_odds"))
        if horse_no is None or not win_odds or win_odds <= 0:
            continue
        entries.append((horse_no, win_odds, row))
    if len(entries) < 2:
        return []

    inv = [1 / o for _, o, _ in entries]
    total = sum(inv)
    win_probs = [x / total for x in inv]
    place_probs = harville_place_probs(win_probs, place_slots(len(entries)))


    # 複勝票の見立て：複勝オッズ（下限・上限の幾何平均）の逆数を、合計が
    # 対象着順数になるよう正規化する
    slots = place_slots(len(entries))
    pool_raw = []
    for _, win_odds, row in entries:
        lo = _num(row.get("place_odds_min"))
        hi = _num(row.get("place_odds_max"))
        mid = math.sqrt(lo * hi) if lo and hi else (lo or hi or win_odds / 3)
        pool_raw.append(1 / max(mid, 1.0))
    pool_total = sum(pool_raw)
    pool_probs = [min(x / pool_total * slots, 1 - _EPS) for x in pool_raw]

    scores = [_num(r.get("solid_score")) or 0.0 for _, _, r in entries]
    mean = sum(scores) / len(scores)
    sd = math.sqrt(sum((s - mean) ** 2 for s in scores) / len(scores))

    horses = []
    for (horse_no, win_odds, row), pp, qp, s in zip(entries, place_probs, pool_probs, scores):
        horses.append(
            _Horse(
                horse_no=horse_no,
                market_place_prob=pp,
                pool_place_prob=qp,
                ability_z=(s - mean) / sd if sd > 0 else 0.0,
                jockey=(row.get("jockey") or "").strip(),
                win_odds=win_odds,
                place_odds_min=_num(row.get("place_odds_min")),
                place_odds_max=_num(row.get("place_odds_max")),
            )
        )
    return horses


def _jockey_feature(h: _Horse, stats: Optional[Dict[str, Tuple[int, float]]], params: EvParams) -> float:
    """騎手の超過複勝率（市場の見立てに対する上振れ）。騎乗数が少ないほど0に縮める。"""
    n, resid = (stats or {}).get(h.jockey, (0, 0.0))
    return resid / (n + params.jockey_prior_rides) * 10


def _x(h: _Horse, params: EvParams, jockey_stats=None) -> List[float]:
    x = [1.0, _logit(h.market_place_prob)]
    if params.use_place_pool:
        x.append(_logit(h.pool_place_prob))
    if params.use_ability:
        x.append(h.ability_z)
    if params.use_jockey:
        x.append(_jockey_feature(h, jockey_stats, params))
    return x


# ---------------------------------------------------------------- 学習


def _solve(a: List[List[float]], b: List[float]) -> List[float]:
    """小さな連立一次方程式をガウスの消去法で解く。"""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        if abs(m[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


def fit_logistic(xs: List[List[float]], ys: List[int], l2: float, iters: int = 25) -> List[float]:
    """L2正則化つきロジスティック回帰をニュートン法（IRLS）で学習する。"""
    d = len(xs[0])
    w = [0.0] * d
    for _ in range(iters):
        grad = [0.0] * d
        hess = [[0.0] * d for _ in range(d)]
        for x, y in zip(xs, ys):
            p = _sigmoid(sum(wi * xi for wi, xi in zip(w, x)))
            g = p - y
            s = p * (1 - p)
            for i in range(d):
                grad[i] += g * x[i]
                for j in range(i, d):
                    hess[i][j] += s * x[i] * x[j]
        for i in range(d):
            for j in range(i):
                hess[i][j] = hess[j][i]
        for i in range(1, d):
            grad[i] += l2 * w[i]
            hess[i][i] += l2
        step = _solve(hess, grad)
        w = [wi - si for wi, si in zip(w, step)]
        if max(abs(s) for s in step) < 1e-8:
            break
    return w


def _payout_position(rows: Dict[str, str]) -> Optional[float]:
    pay = _num(rows.get("place_pay"))
    lo = _num(rows.get("place_odds_min"))
    hi = _num(rows.get("place_odds_max"))
    if not pay or lo is None or hi is None or hi <= lo:
        return None
    return min(max((pay / 100 - lo) / (hi - lo), 0.0), 1.0)


def train(races: Dict[str, List[Dict[str, str]]], params: EvParams = EvParams()) -> Optional[EvModel]:
    """
    確定済みレース（race_id→行）から期待値モデルを学習する。

    騎手成績は日付順に積み上げ、各レースの特徴量にはそのレースより前の日付の
    成績だけを使う（学習データの中でも未来の結果を混ぜない）。
    """
    xs, ys, positions = [], [], []
    n_races = 0
    jockey_stats: Dict[str, Tuple[int, float]] = {}
    by_date: Dict[str, List[List[Dict[str, str]]]] = {}
    for race_id in sorted(races):
        rows = races[race_id]
        if any(r.get("finish_position") for r in rows):
            by_date.setdefault(race_id[:8], []).append(rows)

    for date in sorted(by_date):
        updates: List[Tuple[str, int, float]] = []
        for rows in by_date[date]:
            place_by_no = {_int(r.get("horse_no")): r for r in rows}
            horses = race_features(rows)
            if not horses:
                continue
            n_races += 1
            for h in horses:
                row = place_by_no[h.horse_no]
                y = 1 if row.get("is_place") == "1" else 0
                xs.append(_x(h, params, jockey_stats))
                ys.append(y)
                if h.jockey:
                    updates.append((h.jockey, y, h.pool_place_prob))
                if y:
                    pos = _payout_position(row)
                    if pos is not None:
                        positions.append(pos)
        for jockey, y, p in updates:
            n, resid = jockey_stats.get(jockey, (0, 0.0))
            jockey_stats[jockey] = (n + 1, resid + y - p)

    if n_races < params.min_train_races or not xs:
        return None
    coef = fit_logistic(xs, ys, params.l2)
    position = sum(positions) / len(positions) if positions else 0.0
    return EvModel(
        coef=tuple(coef), payout_position=position, train_races=n_races, jockey_stats=jockey_stats
    )


def load_history(
    dirs: Iterable[Path], before_date: Optional[str], exclude_race_id: str = "", exclude_date: str = ""
) -> Dict[str, List[Dict[str, str]]]:
    """
    分析CSV（{race_id}_分析.csv）から、before_dateより前の確定済みレースを読み込む
    （before_dateがNoneなら日付で絞らず、exclude_dateの日のレースだけ除く）。
    同じrace_idが複数ディレクトリにある場合は先に見つかった方を使う。
    """
    races: Dict[str, List[Dict[str, str]]] = {}
    for d in dirs:
        if d is None or not Path(d).is_dir():
            continue
        for path in sorted(Path(d).glob(f"*{_RACE_CSV_SUFFIX}")):
            race_id = path.name[: -len(_RACE_CSV_SUFFIX)]
            if race_id in races or race_id == exclude_race_id or race_id[:8] == exclude_date:
                continue
            if before_date is not None and race_id[:8] >= before_date:
                continue
            rows = csv_io.read_csv_rows(path)
            if rows and any(r.get("finish_position") for r in rows):
                races[race_id] = rows
    return races


def model_for_race(
    dirs: Iterable[Path], race_id: str, params: "EvParams" = None
) -> Tuple[Optional[EvModel], str]:
    """
    対象レースの評価に使うモデルと学習方法を返す（04_07 4.3）。

    原則は対象レースより前の日付の確定済みレースだけで学習する（"前のレースで学習"）。
    それが min_train_races に満たない初期のレース（過去分の再処理時のみ起こる）は、
    対象レースと同じ日のレースを除いた全確定済みレースで学習する（"交差検証"）。
    どちらでも学習データに対象レース自身の結果は含まれない。
    """
    params = params or EvParams()
    dirs = list(dirs)
    model = train(load_history(dirs, before_date=race_id[:8], exclude_race_id=race_id), params)
    if model is not None:
        return model, "前のレースで学習"
    model = train(
        load_history(dirs, before_date=None, exclude_race_id=race_id, exclude_date=race_id[:8]), params
    )
    return model, ("交差検証" if model is not None else "")


# ---------------------------------------------------------------- 評価


def evaluate(
    rows: List[Dict[str, str]],
    model: Optional[EvModel],
    params: EvParams = EvParams(),
) -> Dict[int, Dict[str, object]]:
    """
    1レース分の行（win_odds・place_odds_min/max・solid_scoreを含む）を評価し、
    馬番ごとに推定複勝率・期待値・期待値順位・購入フラグ・購入金額を返す。
    modelがNone（学習データ不足）の場合は全馬を購入対象外にする。
    """
    horse_nos = [n for n in (_int(r.get("horse_no")) for r in rows) if n is not None]
    results: Dict[int, Dict[str, object]] = {
        n: {"place_prob": "", "expected_value": "", "ev_rank": "", "bet_flag": 0, "bet_amount": 0}
        for n in horse_nos
    }
    if model is None:
        return results

    evs: Dict[int, float] = {}
    buyable = set()
    for h in race_features(rows):
        z = sum(w * x for w, x in zip(model.coef, _x(h, params, model.jockey_stats)))
        p = _sigmoid(z)
        results[h.horse_no]["place_prob"] = round(p, 4)
        if h.place_odds_min is None:
            continue
        hi = h.place_odds_max if h.place_odds_max is not None else h.place_odds_min
        expected_odds = h.place_odds_min + model.payout_position * (hi - h.place_odds_min)
        ev = p * expected_odds
        evs[h.horse_no] = ev
        if not params.max_win_odds or h.win_odds < params.max_win_odds:
            buyable.add(h.horse_no)
        results[h.horse_no]["expected_value"] = round(ev, 3)

    # rank_by_scoreは整数スコア前提ではないが、同点は馬番順で決まる
    ranks = rank_by_score(evs) if evs else {}
    for horse_no, rank in ranks.items():
        results[horse_no]["ev_rank"] = rank
    picks = sorted(
        (n for n in buyable if evs[n] >= params.ev_threshold), key=lambda n: ranks[n]
    )[: params.max_bets_per_race]
    for horse_no in picks:
        results[horse_no]["bet_flag"] = 1
        results[horse_no]["bet_amount"] = params.bet_amount
    return results
