"""
DART(전자공시) 연동 - OpenDART API 키가 있을 때만 동작합니다.
  · 증권신고서/투자설명서에서: 유통가능물량 표, 사업의 내용, 특례상장 여부
  · 증권신고서 주요정보 API에서: 주관사, 공모주식수, 공모금액, 구주매출, 환매청구권
  · 최근 공시 목록
"""
import html
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
# 증권신고서 주요정보 '자금의사용목적' 항목 이름
FUND_NAMES = {"fdpp_fclt": "시설자금", "fdpp_bsninh": "영업양수자금", "fdpp_op": "운영자금",
              "fdpp_dtrp": "채무상환자금", "fdpp_ocsa": "타법인증권 취득자금", "fdpp_etc": "기타자금",
              "fdpp_totam": "합계"}
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

    def recent_disclosures(self, corp_code, days=365, n=8):
        """최근 공시 n건 + 투자에 영향 큰 공시(이벤트) 목록"""
        today = date.today()
        try:
            rows = self.filings(corp_code, today - timedelta(days=days), today)
        except Exception:
            return [], []
        rows.sort(key=lambda x: x.get("rcept_no", ""), reverse=True)
        fmt = lambda r: {"date": f'{r["rcept_dt"][:4]}-{r["rcept_dt"][4:6]}-{r["rcept_dt"][6:]}',
                         "title": re.sub(r"\s+", " ", r["report_nm"]).strip(),
                         "url": VIEWER + r["rcept_no"]}
        events = []
        for r in rows:
            t = r["report_nm"]
            for tag, keys in EVENT_KEYS:
                if any(k in t for k in keys):
                    e = fmt(r)
                    e["tag"] = tag
                    events.append(e)
                    break
        return [fmt(r) for r in rows[:n]], events[:15]

    def financials(self, corp_code):
        """가장 최근 정기보고서의 매출·영업이익·순이익 (이번 기 vs 전년 동기)"""
        y = date.today().year
        tries = [(y, "11014", "3분기"), (y, "11012", "반기"), (y, "11013", "1분기"),
                 (y - 1, "11011", "사업연도"), (y - 1, "11014", "3분기"), (y - 1, "11012", "반기")]
        for year, code, label in tries:
            try:
                j = self._get("fnlttSinglAcnt.json", corp_code=corp_code, bsns_year=str(year), reprt_code=code).json()
            except Exception:
                continue
            if j.get("status") != "000" or not j.get("list"):
                continue
            rows = j["list"]
            fs = "CFS" if any(r.get("fs_div") == "CFS" for r in rows) else "OFS"
            out = {"period": f"{year} {label}", "fs": "연결" if fs == "CFS" else "별도"}
            for key, names in (("rev", ("매출액", "수익(매출액)", "영업수익")),
                               ("op", ("영업이익", "영업이익(손실)")),
                               ("net", ("당기순이익", "당기순이익(손실)", "분기순이익", "반기순이익"))):
                r = next((r for r in rows if r.get("fs_div") == fs and r.get("account_nm", "").strip() in names), None)
                if not r:
                    continue
                cur = r.get("thstrm_add_amount") or r.get("thstrm_amount")
                prv = r.get("frmtrm_add_amount") or r.get("frmtrm_q_amount") or r.get("frmtrm_amount")
                out[key] = to_int(cur)
                out[key + "Prev"] = to_int(prv)
            if "rev" in out or "op" in out:
                return out
        return None

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
        uses = g.get("자금의사용목적") or []
        if uses:
            last_rcp = max(x.get("rcept_no", "") for x in uses)
            fund = []
            for x in uses:
                if x.get("rcept_no", "") != last_rcp:
                    continue
                if "se" in x and "amt" in x:        # {구분, 금액} 형태
                    if num(x["amt"]) > 0:
                        fund.append({"k": str(x["se"]).strip(), "v": num(x["amt"])})
                    continue
                for k, v in x.items():
                    if k in ("rcept_no", "corp_cls", "corp_code", "corp_name"):
                        continue
                    n = num(v)
                    if n > 0 and not re.fullmatch(r"\d{8,}", str(v).strip()):
                        fund.append({"k": FUND_NAMES.get(k, k), "v": n})
            out["fundUse"] = [f for f in fund if f["k"] not in ("합계", "계")]
        put = g.get("일반청약자환매청구권") or []
        out["putback"] = bool([x for x in put if any(str(v).strip() not in ("", "-") for k, v in x.items()
                                                     if k not in ("rcept_no", "corp_cls", "corp_code", "corp_name"))])
        return out


EVENT_KEYS = [
    ("메자닌(CB·BW·EB)", ("전환사채", "신주인수권부사채", "교환사채")),
    ("전환청구·행사", ("전환청구권행사", "신주인수권행사", "교환청구권행사")),
    ("유상증자", ("유상증자",)),
    ("무상증자", ("무상증자",)),
    ("자기주식", ("자기주식",)),
    ("최대주주 변경", ("최대주주변경", "최대주주 변경")),
    ("추가상장", ("추가상장",)),
    ("정정·불성실", ("불성실공시", "조회공시")),
    ("대주주·임원 지분변동", ("임원ㆍ주요주주특정증권등소유상황보고서", "주식등의대량보유상황보고서")),
]


def to_int(v):
    s = re.sub(r"[^\d\-]", "", str(v or ""))
    try:
        return int(s) if s not in ("", "-") else None
    except ValueError:
        return None


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


# 상장트랙: 서로 배타적인 하나만 고른다.
#   증권신고서에는 규정 설명 때문에 모든 트랙 이름이 다 나오므로, '당사는 …' 처럼 회사 자신을 가리키는
#   문장에서만 트랙을 찾는다. 겹치면 더 구체적인 트랙이 우선(사업모델 > 성장성 추천 > 소부장 > 기술평가 > 이익미실현).
TRACKS = [
    ("기술특례(사업모델)", re.compile(r"사업\s*모델\s*(기반\s*)?(특례|기술성장|평가|상장)")),
    ("성장성 추천", re.compile(r"성장성\s*추천|상장주선인\s*(의\s*)?(성장성\s*)?추천")),
    ("기술특례(소부장)", re.compile(r"소재\s*[·ㆍ,\s]?\s*부품\s*[·ㆍ,\s]?\s*장비.{0,40}(특례|전문기업|기술평가)")),
    ("기술특례(기술평가)", re.compile(r"기술\s*평가\s*(특례|결과|등급)|기술성장기업|기술특례")),
    ("이익미실현(테슬라)", re.compile(r"이익\s*미실현\s*(기업|요건|특례)?|테슬라\s*요건")),
]
SELF = re.compile(r"(당사|동사|발행회사|회사)(는|가|의|와|로서|로)")
NEG = re.compile(r"(해당하지\s*않|아닙니다|아니며|아닌|없습니다|제외)")


def listing_track(plain):
    """plain: 태그를 벗긴 증권신고서 본문 → (트랙, 근거 문장)"""
    sents = re.split(r"(?<=[다요])\.\s*|\n", plain)
    hits = {}
    for snt in sents:
        if len(snt) > 600:
            continue
        for m in SELF.finditer(snt):
            clause = snt[m.start(): m.start() + 180]      # '당사는 …' 뒤쪽만 본다
            if NEG.search(clause) or not any(k in clause for k in ("상장", "특례", "요건", "평가", "추천")):
                continue
            for name, rx in TRACKS:
                if rx.search(clause):
                    hits.setdefault(name, clause.strip())
                    break
    for name, _ in TRACKS:
        if name in hits:
            return name, hits[name][:300]
    return "일반", ""


def analyze_document(xml_text):
    """문서 전체를 파싱하지 않고, 필요한 표와 '사업의 내용' 부분만 잘라서 읽는다(빠름)"""
    out = {"lockupTables": [], "lockup": []}
    cands = []
    for m in re.finditer(r"<TABLE\b.*?</TABLE>", xml_text, re.S | re.I):
        chunk = m.group(0)
        if "유통" in chunk and len(chunk) < 300000:
            cands.append(chunk)
    if cands:
        soup = BeautifulSoup("".join(cands), "html.parser")
        for tbl in find_lockup_tables(soup):
            rows = table_rows(tbl)[:60]
            out["lockupTables"].append(rows)
            if not out["lockup"]:
                out["lockup"] = lockup_timeline(rows)
    out["bizText"] = business_text_raw(xml_text)
    plain = strip_tags(re.sub(r"</(P|TD|TE|TH|TU|TR|TITLE)>", "\n", xml_text[:4000000], flags=re.I))
    out["track"], out["trackEvidence"] = listing_track(plain)
    return out


def strip_tags(s):
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return "\n".join(re.sub(r"[ \t\r\f\v]+", " ", x).strip() for x in s.split("\n") if x.strip())


def business_text_raw(xml_text, limit=18000):
    """'II. 사업의 내용' 제목부터 다음 장(III.) 전까지"""
    m = re.search(r"<TITLE[^>]*>[^<]*사업의\s*내용[^<]*</TITLE>", xml_text, re.I)
    if not m:
        m = re.search(r"<TITLE[^>]*>[^<]*사업의\s*개요[^<]*</TITLE>", xml_text, re.I)
    if not m:
        return ""
    rest = xml_text[m.end(): m.end() + 400000]
    nxt = re.search(r"<TITLE[^>]*>\s*(III|Ⅲ|3)\s*\.", rest, re.I)
    if nxt:
        rest = rest[: nxt.start()]
    txt = clean(strip_tags(rest))
    return txt[:limit] if len(txt) > 300 else ""


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
