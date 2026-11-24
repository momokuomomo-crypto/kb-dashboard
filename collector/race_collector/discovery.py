"""
JRAのページから対象重賞（G1・G2・G3、障害重賞を除く）を発見する
（06_02 4.1）。HTML解析のみを行い、通信はjra_clientが担う。
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import List, Optional

from bs4 import BeautifulSoup

# CNAME中のレース識別部: 場(2) 年(4) 回(2) 日(2) R(2) 開催日(8)
_RACE_KEY_RE = re.compile(r"pw01[sd]de(\d{2})(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})(\d{8})/[0-9A-F]{2}")
_GRADE_ICON_RE = re.compile(r"icon_grade_(?:s_)?(g1|g2|g3|jg1|jg2|jg3)\.png")
_ODDS_CNAME_RE = re.compile(r"pw151ou\w+Z/[0-9A-F]{2}")
_DATE_HEADER_RE = re.compile(r"(\d{1,2})月(\d{1,2})日")

TARGET_GRADES = ("G1", "G2", "G3")

VENUES = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
    "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
}


@dataclass(frozen=True)
class RaceKey:
    venue_code: str
    year: str
    kai: str
    day: str
    race_no: str
    race_date: str  # YYYYMMDD

    @property
    def netkeiba_race_id(self) -> str:
        return f"{self.year}{self.venue_code}{self.kai}{self.day}{self.race_no}"

    @property
    def venue(self) -> str:
        return VENUES.get(self.venue_code, self.venue_code)


@dataclass
class GradedRace:
    key: RaceKey
    grade: str
    short_name: str
    cname: str  # 結果ページ（sde）または出馬表ページ（dde）のCNAME


def parse_race_key(cname: str) -> Optional[RaceKey]:
    m = _RACE_KEY_RE.search(cname)
    if not m:
        return None
    # group(1)は種別(10等)、以降が場・年・回・日・R・開催日
    return RaceKey(m.group(2), m.group(3), m.group(4), m.group(5), m.group(6), m.group(7))


def _grade_of(node) -> Optional[str]:
    img = node.find("img", src=_GRADE_ICON_RE)
    if img is None:
        return None
    return _GRADE_ICON_RE.search(img["src"]).group(1).upper()


def parse_month_graded_races(html: str) -> List[GradedRace]:
    """過去レース結果の月別ページから重賞（結果ページCNAME付き）を抽出する。"""
    soup = BeautifulSoup(html, "html.parser")
    races = []
    for cell in soup.select("div.cell.grade_race li"):
        link = cell.select_one("span.race a[href*='CNAME=']")
        if link is None:
            continue
        cname = link["href"].split("CNAME=", 1)[1]
        key = parse_race_key(cname)
        grade = _grade_of(cell)
        if key is None or grade is None:
            continue
        races.append(GradedRace(key, grade, link.get_text(strip=True), cname))
    return races


def parse_week_graded_races(html: str) -> List[GradedRace]:
    """出馬表トップの「今週の重賞レース」から重賞（出馬表CNAME付き）を抽出する。"""
    soup = BeautifulSoup(html, "html.parser")
    section = soup.find(id="grade_race")
    if section is None:
        return []
    races = []
    for link in section.select("a[href*='accessD.html?CNAME=']"):
        cname = link["href"].split("CNAME=", 1)[1]
        key = parse_race_key(cname)
        grade = _grade_of(link)
        if key is None or grade is None:
            continue
        txt = link.select_one("span.txt")
        name = txt.get_text(strip=True) if txt else link.get_text(strip=True)
        num = link.select_one("span.race_num")
        if num is not None:
            name = name.replace(num.get_text(strip=True), "", 1)
        races.append(GradedRace(key, grade, name, cname))
    return races


def is_target(race: GradedRace) -> bool:
    return race.grade in TARGET_GRADES


def in_range(race: GradedRace, start: date, end: date) -> bool:
    d = race.key.race_date
    return start.strftime("%Y%m%d") <= d <= end.strftime("%Y%m%d")


def parse_result_page(html: str):
    """
    結果ページからレース正式名称（回次を除く）と単勝・複勝オッズページの
    CNAMEを取得する。
    """
    soup = BeautifulSoup(html, "html.parser")
    name_node = soup.select_one("h2 span.race_name")
    if name_node is None:
        raise ValueError("結果ページからレース名を取得できません")
    for icon in name_node.select("span.grade_icon"):
        icon.decompose()
    race_name = name_node.get_text(strip=True)
    odds = _ODDS_CNAME_RE.findall(html)
    return race_name, (odds[0] if odds else None)


def find_odds_cname(html: str, key: RaceKey) -> Optional[str]:
    """出馬表トップ等から、指定レースの単勝・複勝オッズCNAMEを取得する。"""
    ident = f"{key.venue_code}{key.year}{key.kai}{key.day}{key.race_no}{key.race_date}Z"
    for cname in _ODDS_CNAME_RE.findall(html):
        if ident in cname:
            return cname
    return None
