"""
JRA公式サイト（JRADB）の過去レースページを解析する（06_08 非重賞を含む全レースアーカイブ）。

通信は行わず、HTML文字列から必要な値を取り出すだけにする。
  月別ページ   → 開催日一覧（srl）
  開催日ページ → レース一覧（sde＝結果ページ、レース名）
  結果ページ   → レース情報・全頭の成績（通過順・騎手等）・払戻金・オッズページCNAME
  オッズページ → 単勝・複勝（pw151）、枠連〜3連単（pw153〜pw158）の確定オッズ
"""

import re
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

SRL_RE = re.compile(r"pw01srl\d{2}(\d{2})(\d{4})(\d{2})(\d{2})(\d{8})/[0-9A-F]{2}")
SDE_RE = re.compile(r"pw01sde\d{2}(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})(\d{8})/[0-9A-F]{2}")
ODDS_RE = re.compile(r"pw15([1345678])ou\d{2}(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})\d{8}Z(?:99)?/[0-9A-F]{2}")

VENUES = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
    "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
}

# オッズページ番号 → 券種
ODDS_PAGES = {"3": "枠連", "4": "馬連", "5": "ワイド", "6": "馬単", "7": "3連複", "8": "3連単"}
_COMBO_TABLE_CLASSES = ("waku", "umaren", "wide", "umatan", "fuku3", "trio", "tan3", "tierce")


def race_id_of(venue: str, year: str, kai: str, day: str, race_no: str) -> str:
    """netkeibaと同じ12桁のレースID（年・場・回・日・R）。既存のurl.txtと揃える。"""
    return f"{year}{venue}{kai}{day}{race_no}"


def _txt(node) -> str:
    return node.get_text(" ", strip=True) if node is not None else ""


def _num(s: str) -> Optional[float]:
    s = s.replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_month_days(html: str) -> List[Tuple[str, str]]:
    """月別ページ等から開催日ページ（srl）を抽出する。[(開催日YYYYMMDD, CNAME)]"""
    out = {}
    for m in SRL_RE.finditer(html):
        out[m.group(0)] = m.group(5)
    return sorted(((d, c) for c, d in out.items()), key=lambda x: x)


def parse_day_races(html: str) -> List[dict]:
    """開催日ページからレース一覧を抽出する。"""
    soup = BeautifulSoup(html, "html.parser")
    races = {}
    for tr in soup.find_all("tr"):
        a = tr.find("a", href=SDE_RE) or tr.find("a", onclick=SDE_RE)
        attr = (a.get("href") or a.get("onclick") or "") if a else str(tr)
        m = SDE_RE.search(attr) or SDE_RE.search(str(tr))
        if not m:
            continue
        tds = tr.find_all("td")
        name = ""
        for td in tds:
            if "race_name" in (td.get("class") or []) or td.find(class_="race_name"):
                name = _txt(td)
                break
        if not name and len(tds) > 1:
            name = _txt(tds[1])
        venue, year, kai, day, rno, date = m.groups()
        rid = race_id_of(venue, year, kai, day, rno)
        races[rid] = {"race_id": rid, "date": date, "venue": VENUES.get(venue, venue),
                      "race_no": int(rno), "name": name, "cname": m.group(0)}
    return sorted(races.values(), key=lambda r: r["race_id"])


def is_shinba(name: str) -> bool:
    return "新馬" in name


def _corner_list(td) -> List[str]:
    return [li.get_text(strip=True) for li in td.select("li")] if td is not None else []


def parse_result(html: str) -> Tuple[dict, List[dict], List[dict], Dict[str, str]]:
    """結果ページを解析する。(レース情報, 全頭成績, 払戻金, オッズページCNAME{券種コード: CNAME})"""
    soup = BeautifulSoup(html, "html.parser")
    header = soup.select_one("div.race_header")
    if header is None:
        raise ValueError("結果ページの形式が想定と異なります（race_headerなし）")
    race = {}
    race["date_text"] = _txt(header.select_one(".cell.date"))
    race["start_time"] = _txt(header.select_one(".cell.time strong"))
    race["weather"] = _txt(header.select_one("li.weather .txt"))
    baba = header.select_one(".cell.baba li:not(.weather)")
    race["track_condition"] = _txt(baba.select_one(".txt")) if baba else ""
    # グレードアイコンはレース名の中にあるため、名前から除く前に読む
    grade_img = header.find("img", src=re.compile(r"icon_grade"))
    g = re.search(r"icon_grade_(?:s_)?(\w+)\.png", grade_img["src"]) if grade_img else None
    race["grade"] = g.group(1).upper() if g else ""
    name = header.select_one("span.race_name")
    if name is not None:
        for icon in name.select("span.grade_icon"):
            icon.decompose()
    race["race_name"] = _txt(name)
    race["category"] = _txt(header.select_one(".type .category"))
    race["class"] = _txt(header.select_one(".type .class"))
    race["rule"] = _txt(header.select_one(".type .rule"))
    race["weight_rule"] = _txt(header.select_one(".type .weight"))
    course = header.select_one(".type .course")
    ctext = _txt(course)
    dm = re.search(r"([\d,]+)\s*メートル", ctext)
    race["distance"] = int(dm.group(1).replace(",", "")) if dm else None
    detail = _txt(course.select_one(".detail")) if course else ""
    race["course_detail"] = detail.strip("（）()")
    race["surface"] = ("障害" if "障害" in detail else "芝" if "芝" in detail
                       else "ダート" if "ダート" in detail else "")

    horses = []
    table = None
    for t in soup.select("table.basic.narrow-xy"):
        if t.find("td", class_="horse"):
            table = t
            break
    if table is None:
        raise ValueError("結果ページに成績表がありません")
    for tr in table.select("tbody tr") or table.find_all("tr")[1:]:
        if tr.find("td", class_="horse") is None:
            continue

        def cell(cls):
            return tr.find("td", class_=cls)

        waku_img = cell("waku").find("img") if cell("waku") else None
        wm = re.search(r"枠(\d)", waku_img.get("alt", "")) if waku_img else None
        horse_a = cell("horse").find("a")
        hcn = re.search(r"pw01dud\d{2}(\d+)/[0-9A-F]{2}", horse_a.get("href", "")) if horse_a else None
        jockey = cell("jockey")
        jid = re.search(r"pw04kmk(\d+)/", str(jockey)) if jockey else None
        trainer = cell("trainer")
        tid = re.search(r"pw05cmk(\d+)/", str(trainer)) if trainer else None
        corners = _corner_list(cell("corner"))
        hw = _txt(cell("h_weight"))
        hwm = re.match(r"(\d+)\s*\(([^)]*)\)", hw)
        horses.append({
            "finish": _txt(cell("place")),
            "waku": int(wm.group(1)) if wm else None,
            "umaban": int(_txt(cell("num"))) if _txt(cell("num")).isdigit() else None,
            "horse_name": _txt(cell("horse")),
            "horse_id": hcn.group(1) if hcn else "",
            "horse_cname": hcn.group(0) if hcn else "",
            "sex_age": _txt(cell("age")),
            "weight_carried": _num(_txt(cell("weight"))),
            "jockey": _txt(jockey).replace("　", " "),
            "jockey_id": jid.group(1) if jid else "",
            "time": _txt(cell("time")),
            "margin": _txt(cell("margin")),
            "corner_positions": "-".join(corners),
            "corner_first": int(corners[0]) if corners and corners[0].isdigit() else None,
            "corner_last": int(corners[-1]) if corners and corners[-1].isdigit() else None,
            "last_3f": _num(_txt(cell("f_time"))),
            "body_weight": int(hwm.group(1)) if hwm else None,
            "body_weight_diff": hwm.group(2) if hwm else "",
            "trainer": _txt(trainer).replace("　", " "),
            "trainer_id": tid.group(1) if tid else "",
            "popularity": int(_txt(cell("pop"))) if _txt(cell("pop")).isdigit() else None,
        })

    payouts = []
    for li in soup.select("div.refund_area li"):
        dt = li.select_one("dl > dt")
        if dt is None:
            continue
        for line in li.select("div.line"):
            yen = _num(_txt(line.select_one(".yen")).replace("円", ""))
            pop = re.search(r"\d+", _txt(line.select_one(".pop")))
            payouts.append({
                "bet_type": dt.get_text(strip=True),
                "combo": _txt(line.select_one(".num")).replace(" ", ""),
                "payout": int(yen) if yen is not None else None,
                "popularity": int(pop.group(0)) if pop else None,
            })

    return race, horses, payouts, find_odds_cnames(html)


def find_odds_cnames(html: str, race_id: str = "") -> Dict[str, str]:
    """ページ内のオッズページCNAME。race_id指定時はそのレースのものだけ。{券種コード: CNAME}"""
    odds = {}
    for m in ODDS_RE.finditer(html):
        rid = race_id_of(*m.groups()[1:6])
        if race_id and rid != race_id:
            continue
        odds.setdefault(m.group(1), m.group(0))
    return odds


def parse_tanpuku(html: str) -> Dict[int, dict]:
    """単勝・複勝オッズページ（pw151）。{馬番: {tan_odds, fuku_min, fuku_max}}"""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for tr in soup.select("table.tanpuku tr"):
        num = tr.find("td", class_="num")
        if num is None or not _txt(num).isdigit():
            continue
        fuku = tr.find("td", class_="odds_fuku")
        out[int(_txt(num))] = {
            "tan_odds": _num(_txt(tr.find("td", class_="odds_tan"))),
            "fuku_min": _num(_txt(fuku.select_one(".min"))) if fuku else None,
            "fuku_max": _num(_txt(fuku.select_one(".max"))) if fuku else None,
        }
    return out


def parse_combo_odds(html: str, bet_type: str) -> List[dict]:
    """
    枠連〜3連単のオッズページ。各表の見出し（caption、または3連系の1着・2着欄）と
    行見出しから組み合わせを作る。ワイドは「下限 - 上限」を持つ。
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for table in soup.find_all("table"):
        classes = table.get("class") or []
        if not any(c in classes for c in _COMBO_TABLE_CLASSES):
            continue
        holder = table.find_parent("li")
        prefix = []
        if holder is not None:
            prefix = [_txt(d) for d in holder.select("div.p_line div.num")]
        if not prefix:
            cap = table.find("caption")
            img = cap.find("img") if cap is not None else None
            if img is not None and re.search(r"枠(\d)", img.get("alt", "")):
                prefix = [re.search(r"枠(\d)", img["alt"]).group(1)]
            else:
                prefix = re.findall(r"\d+", _txt(cap)) if cap else []
        for tr in table.find_all("tr"):
            th = tr.find("th")
            td = tr.find("td")
            if th is None or td is None:
                continue
            vals = re.findall(r"\d+(?:\.\d+)?", _txt(td).replace(",", ""))
            if not vals:
                continue
            combo = "-".join(prefix + [_txt(th)])
            rows.append({
                "bet_type": bet_type,
                "combo": combo,
                "odds": float(vals[0]),
                "odds_max": float(vals[1]) if len(vals) > 1 else None,
            })
    return rows


_HORSE_FIELDS = {
    "父": "sire", "母": "dam", "母の父": "broodmare_sire", "母の母": "dam_dam",
    "生年月日": "birth_date", "毛色": "coat", "性別": "sex", "産地": "origin",
    "生産牧場": "breeder", "馬主名": "owner", "取引市場": "market",
}


def parse_horse(html: str) -> dict:
    """競走馬ページ（accessU）から血統（父・母・母の父等）と基本情報を取り出す。"""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for dl in soup.find_all("dl"):
        dt, dd = dl.find("dt"), dl.find("dd")
        if dt is None or dd is None:
            continue
        key = _HORSE_FIELDS.get(dt.get_text(strip=True))
        if key is None or key in out:
            continue
        for extra in dd.select("span.sanku"):
            extra.decompose()
        out[key] = dd.get_text(" ", strip=True)
    if "sire" not in out:
        raise ValueError("競走馬ページに父の情報がありません")
    return out
