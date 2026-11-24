"""
JRA公式サイト（JRADB）へのアクセスを担当する。

JRADBのページはCNAMEパラメータ（末尾2桁はサイト側が発行するチェック値）で
識別される。チェック値は自前で計算せず、必ずサイト上のリンクから取得する
（06_02 4.1）。
"""

import re
import time
from typing import Dict, Optional

import requests

BASE_URL = "https://www.jra.go.jp"
JRADB_URL = BASE_URL + "/JRADB/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
_TIMEOUT_SECONDS = 30
_RETRIES = 3
# JRAサイトへの負荷を避けるため、リクエスト間に最低限の間隔を空ける。
_INTERVAL_SECONDS = 1.0

# 過去レース結果の月別一覧（検索トップ）。月別ページのチェック値はこの
# ページのJavaScript（objParam）に埋め込まれている。
PAST_RESULT_TOP_CNAME = "pw01skl00999999/B3"
# 出馬表トップ（今週の重賞レース一覧を含む）。
HORSE_CARD_TOP_CNAME = "pw01dli00/F3"

_OBJ_PARAM_RE = re.compile(r'objParam\["(\d{4})"\]="([0-9A-F]{2})"')
_ERROR_MARKERS = ("パラメータエラー", "ご指定のページに接続できませんでした")


class JraFetchError(RuntimeError):
    """JRAサイトからの取得失敗を表す。"""


class JraClient:
    def __init__(self, session: Optional[requests.Session] = None,
                 interval: float = _INTERVAL_SECONDS):
        self._interval = interval
        self._session = session or requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._last_request = 0.0
        self._month_checks: Optional[Dict[str, str]] = None

    def fetch(self, page: str, cname: str) -> str:
        """page（例: accessS.html）をCNAME付きPOSTで取得し、HTML文字列を返す。"""
        last_error: Optional[Exception] = None
        for attempt in range(_RETRIES):
            wait = self._last_request + self._interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                response = self._session.post(
                    JRADB_URL + page, data={"CNAME": cname}, timeout=_TIMEOUT_SECONDS
                )
                self._last_request = time.monotonic()
                response.raise_for_status()
                response.encoding = "cp932"
                text = response.text
                if any(marker in text for marker in _ERROR_MARKERS):
                    raise JraFetchError(f"JRAがエラーページを返しました: {page} {cname}")
                return text
            except (requests.RequestException, JraFetchError) as e:
                last_error = e
                time.sleep(3 * (attempt + 1))
        raise JraFetchError(f"取得に失敗しました: {page} {cname}: {last_error}")

    def month_cname(self, year: int, month: int) -> str:
        if self._month_checks is None:
            html = self.fetch("accessS.html", PAST_RESULT_TOP_CNAME)
            self._month_checks = dict(_OBJ_PARAM_RE.findall(html))
        key = f"{year % 100:02d}{month:02d}"
        if key not in self._month_checks:
            raise JraFetchError(f"{year}年{month}月の月別ページが見つかりません")
        return f"pw01skl10{year:04d}{month:02d}/{self._month_checks[key]}"

    def fetch_month(self, year: int, month: int) -> str:
        return self.fetch("accessS.html", self.month_cname(year, month))

    def fetch_latest_month(self) -> str:
        """最新月（当月）の重賞一覧。当月は月別CNAMEでは開けないため検索トップを使う。"""
        return self.fetch("accessS.html", PAST_RESULT_TOP_CNAME)

    def fetch_result(self, cname: str) -> str:
        return self.fetch("accessS.html", cname)

    def fetch_horse_card_top(self) -> str:
        return self.fetch("accessD.html", HORSE_CARD_TOP_CNAME)


def result_url(cname: str) -> str:
    return f"{JRADB_URL}accessS.html?CNAME={cname}"
