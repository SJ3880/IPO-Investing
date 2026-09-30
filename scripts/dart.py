"""
DART(전자공시) 연동 - OpenDART API 키가 있을 때만 동작합니다.
  · 증권신고서/투자설명서에서: 유통가능물량 표, 사업의 내용, 특례상장 여부
  · 증권신고서 주요정보 API에서: 주관사, 공모주식수, 공모금액, 구주매출, 환매청구권
  · 최근 공시 목록
"""
import io
import re
import time
import warnings
import zipfile
from datetime import date, timedelta
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup
try:
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:
    pass

API = "https://opendart.fss.or.kr/api"
VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="


class Dart:
    def __init__(self, key, log=print):
        self.key = key
        self.log = log
        self._corp = None

    def _get(self, path, **params):
        params["crtfc_key"] = self.key
        for attempt in range(3):
            try:
                r = requests.get(f"{API}/{path}", params=params, timeout=60)
                r.raise_for_status()
                time.sleep(0.12)  # 분당 호출 제한 여유
                return r
            except Exception as e:
                if attempt == 2:
                    raise
                time.sleep(2)

    # ---------------------------------------------------------- 종목코드 → DART 고유번호
    def corp_code(self, stock_code):
        if self._corp is None:
            r = self._get("corpCode.xml")
            z = zipfile.ZipFile(io.BytesIO(r.content))
            root = ET.fromstring(z.read(z.namelist()[0]))
            self._corp = {}
            for el in root.iter("list"):
                sc = (el.findtext("stock_code") or "").strip()
                if sc:
                    self._corp[sc] = el.findtext("corp_code")
            self.log(f"[DART] 고유번호 {len(self._corp)}개 로드")
        return self._corp.get(stock_code)

    def filings(self, corp_code, bgn, end, **kw):
        out, page = [], 1
        while True:
            j = self._get("list.json", corp_code=corp_code, bgn_de=bgn.strftime("%Y%m%d"),
                          end_de=end.strftime("%Y%m%d"), page_no=page, page_count=100, **kw).json()
            if j.get("status") != "000":
                break
            out += j.get("list", [])
            if page >= int(j.get("total_page", 1)):
                break
            page += 1
        return out

    def recent_disclosures(self, corp_code, days=120, n=8):
        today = date.today()
        try:
            rows = self.filings(corp_code, today - timedelta(days=days), today)
        except Exception:
            return []
        rows.sort(key=lambda x: x.get("rcept_no", ""), reverse=True)
        return [{"date": f'{r["rcept_dt"][:4]}-{r["rcept_dt"][4:6]}-{r["rcept_dt"][6:]}',
                 "title": re.sub(r"\s+", " ", r["report_nm"]).strip(),
                 "url": VIEWER + r["rcept_no"]} for r in rows[:n]]

    def prospectus(self, corp_code, listed):
        """상장일 이전 1년 내 투자설명서(최종) → 없으면 가장 최근 증권신고서(지분증권)"""
        rows = self.filings(corp_code, listed - timedelta(days=365), listed)
        def pick(word):
            c = [r for r in rows if word in r["report_nm"]]
            return max(c, key=lambda r: r["rcept_no"]) if c else None
        return pick("투자설명서") or pick("증권신고서(지분증권)")

    def document_text(self, rcept_no):
        r = self._get("document.xml", rcept_no=rcept_no)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = sorted(z.namelist(), key=lambda n: (not n.startswith(rcept_no), -z.getinfo(n).file_size))
        raw = z.read(names[0])
        for enc in ("utf-8", "cp949", "euc-kr"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="ignore")

    def estk(self, corp_code, listed):
        """증권신고서 주요정보(지분증권)"""
        try:
            j = self._get("estkRs.json", corp_code=corp_code,
                          bgn_de=(listed - timedelta(days=365)).strftime("%Y%m%d"),
                          end_de=listed.strftime("%Y%m%d")).json()
        except Exception:
            return {}
        if j.get("status") != "000":
            return {}
        g = {x.get("title", ""): x.get("list", []) for x in j.get("group", [])}
        num = lambda s: int(re.sub(r"[^\d]", "", str(s)) or 0)
        out = {}
        kinds = g.get("증권의종류") or []
        if kinds:
            k = kinds[-1]
            out["shares"] = num(k.get("stkcnt"))
            out["amount"] = num(k.get("slta"))
        uw = g.get("인수인정보") or []
        if uw:
            last_rcp = max(x.get("rcept_no", "") for x in uw)
            names = []
            for x in uw:
                if x.get("rcept_no", "") == last_rcp and x.get("actnmn") and x["actnmn"] not in names:
                    names.append(x["actnmn"].strip())
            out["underwriters"] = names
        sellers = g.get("매출인에관한사항") or []
        if sellers and out.get("shares"):
            last_rcp = max(x.get("rcept_no", "") for x in sellers)
            old = sum(num(x.get("slstk")) for x in sellers if x.get("rcept_no", "") == last_rcp)
            out["oldShareRatio"] = round(old / out["shares"] * 100, 1) if old else 0.0
        else:
            out["oldShareRatio"] = 0.0 if out.get("shares") else None
        put = g.get("일반청약자환매청구권") or []
        out["putback"] = bool([x for x in put if any(str(v).strip() not in ("", "-") for k, v in x.items()
                                                     if k not in ("rcept_no", "corp_cls", "corp_code", "corp_name"))])
        return out


# ------------------------------------------------------------------ 문서 파싱
CELL = ["td", "th", "te", "tu"]
PERIOD_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(년|개월|월|일)")


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def period_months(text):
    t = clean(text)
    if not t or t in ("-", "해당사항없음"):
        return None
    if "상장일" in t or "유통가능" in t or t == "없음":
        return 0
    m = PERIOD_RE.search(t)
    if not m:
        return None
    v, u = float(m.group(1)), m.group(2)
    return round(v * 12 if u == "년" else v / 30 if u == "일" else v, 2)


def to_num(s):
    s = clean(s).replace(",", "").replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


def table_rows(tbl):
    rows = []
    for tr in tbl.find_all("tr"):
        cells = tr.find_all(CELL, recursive=False) or tr.find_all(CELL)
        rows.append([{"t": clean(c.get_text(" ")),
                      "cs": int(c.get("colspan") or 1) if str(c.get("colspan") or "1").isdigit() else 1,
                      "rs": int(c.get("rowspan") or 1) if str(c.get("rowspan") or "1").isdigit() else 1}
                     for c in cells])
    return [r for r in rows if r]


def find_lockup_tables(soup):
    """유통가능물량 관련 표 후보를 점수순으로"""
    keys = ["유통가능", "유통제한", "매각제한", "의무보유", "보호예수", "상장일", "1개월", "3개월", "6개월", "1년"]
    scored = []
    for tbl in soup.find_all("table"):
        txt = clean(tbl.get_text(" "))
        if "유통" not in txt or len(txt) > 12000:
            continue
        s = sum(txt.count(k) for k in keys)
        if s >= 4:
            scored.append((s, tbl))
    scored.sort(key=lambda x: -x[0])
    return [t for _, t in scored[:2]]


def lockup_timeline(rows):
    """표에서 '기간(개월) → 지분율(%)'을 뽑아 누적 유통가능 비율로 만든다. 실패하면 []"""
    flat = expand(rows)
    # 방법 A: 헤더에 '기간' 열과 '비율/지분율' 열이 있는 주주별 표 → 기간별 합산
    for hi, head in enumerate(flat[:4]):
        p_col = next((i for i, h in enumerate(head) if "기간" in h), None)
        r_col = next((i for i, h in enumerate(head) if ("비율" in h or "지분율" in h)), None)
        if p_col is None or r_col is None:
            continue
        agg = {}
        for r in flat[hi + 1:]:
            if len(r) != len(head) or any(k in " ".join(r) for k in ("합계", "소계", "총계")):
                continue
            m, v = period_months(r[p_col]), to_num(r[r_col])
            if m is None and "유통가능" in " ".join(r[:p_col]):
                m = 0
            if m is None or v is None or v > 100:
                continue
            agg[m] = agg.get(m, 0) + v
        if len(agg) >= 2 and 80 <= sum(agg.values()) <= 120:
            return cumulative(agg)
    # 방법 B: 행 머리가 '상장일/1개월/3개월…'인 기간별 추이 표
    pts = {}
    for r in flat:
        if not r:
            continue
        m = period_months(r[0]) if ("후" in r[0] or "상장일" in r[0] or PERIOD_RE.fullmatch(r[0].replace(" ", "") or "x")) else None
        if m is None:
            continue
        pcts = [to_num(x) for x in r[1:] if "%" in x or re.fullmatch(r"\d{1,3}\.\d+", x.replace(" ", ""))]
        pcts = [x for x in pcts if x is not None and 0 <= x <= 100]
        if pcts:
            pts[m] = pcts[-1]
    if len(pts) >= 2:
        return [{"m": m, "pct": round(v, 2)} for m, v in sorted(pts.items())]
    return []


def expand(rows):
    """rowspan/colspan 을 풀어 직사각형 격자로"""
    grid, carry = [], {}
    for r in rows:
        line, col, it = [], 0, iter(r)
        cell = next(it, None)
        while cell is not None or col in carry:
            if col in carry:
                t, left = carry[col]
                line.append(t)
                if left <= 1:
                    del carry[col]
                else:
                    carry[col] = (t, left - 1)
                col += 1
                continue
            for _ in range(max(1, cell["cs"])):
                line.append(cell["t"])
                if cell["rs"] > 1:
                    carry[col] = (cell["t"], cell["rs"] - 1)
                col += 1
            cell = next(it, None)
        grid.append(line)
    return grid


def cumulative(agg):
    out, acc = [], 0.0
    for m in sorted(agg):
        acc += agg[m]
        out.append({"m": m, "pct": round(min(acc, 100), 2)})
    return out


def business_text(soup, limit=18000):
    """'사업의 내용' 장 본문"""
    for title in soup.find_all("title"):
        t = clean(title.get_text())
        if "사업의 내용" in t or "사업의 개요" in t:
            sec = title.parent
            txt = clean(sec.get_text(" "))
            if len(txt) > 500:
                return txt[:limit]
    return ""


def listing_type(text):
    tags = []
    if "기술성장기업" in text or "기술특례" in text:
        tags.append("기술특례")
    if "성장성 추천" in text or "성장성추천" in text:
        tags.append("성장성특례")
    if "이익미실현" in text:
        tags.append("이익미실현(테슬라)")
    return tags or ["일반"]


def analyze_document(xml_text):
    soup = BeautifulSoup(xml_text, "html.parser")
    out = {"lockupTables": [], "lockup": []}
    for tbl in find_lockup_tables(soup):
        rows = table_rows(tbl)
        if len(rows) > 60:
            rows = rows[:60]
        out["lockupTables"].append(rows)
        if not out["lockup"]:
            out["lockup"] = lockup_timeline(rows)
    text = clean(soup.get_text(" "))
    out["bizText"] = business_text(soup)
    out["listingType"] = listing_type(text[:400000])
    return out


# ------------------------------------------------------------------ AI 요약 (선택)
def summarize(name, biz_text, api_key, model="claude-haiku-4-5-20251001"):
    prompt = (
        f"아래는 '{name}'의 증권신고서 중 '사업의 내용' 원문이다. 주식 투자자가 빠르게 이해하도록 "
        "한국어로 3~4개 문단(각 3~5문장)으로 요약하라.\n"
        "1문단: 무슨 사업을 하는 회사인지, 핵심 제품·서비스\n"
        "2문단: 매출 구조와 주요 고객·시장\n"
        "3문단: 기술력·경쟁력과 경쟁 구도\n"
        "4문단: 투자 시 유의할 리스크나 관전 포인트\n"
        "원문에 없는 내용·수치는 절대 지어내지 말고, 머리말·제목·목록 없이 문단만 출력하라.\n\n"
        f"<원문>\n{biz_text}\n</원문>"
    )
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": model, "max_tokens": 1500, "messages": [{"role": "user", "content": prompt}]},
        timeout=120,
    )
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()
