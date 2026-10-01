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
            self._corp, self._by_name, best = {}, {}, {}
            for el in root.iter("list"):
                sc = (el.findtext("stock_code") or "").strip()
                cc = el.findtext("corp_code")
                if sc:
                    self._corp[sc] = cc
                key = re.sub(r"[\s\.\-·]|\(주\)|㈜|주식회사", "", el.findtext("corp_name") or "").lower()
                md = el.findtext("modify_date") or ""
                if key and md >= best.get(key, ""):
                    best[key] = md
                    self._by_name[key] = cc
            self.log(f"[DART] 고유번호 {len(self._corp)}개 로드")
        return self._corp.get(stock_code)

    def corp_by_name(self, name):
        """비상장 회사도 이름으로 고유번호 찾기 (같은 이름이 여럿이면 최근 수정된 것)"""
        if self._corp is None:
            self.corp_code("000000")
        def nm(x):
            return re.sub(r"[\s\.\-·]|\(주\)|㈜|주식회사", "", x or "").lower()
        c = self._by_name.get(nm(name))
        return c

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

    def prospectus(self, corp_code, listed, after=None):
        """상장일 이전 1년 내 투자설명서(최종) → 없으면 가장 최근 증권신고서(지분증권)"""
        rows = self.filings(corp_code, after or (listed - timedelta(days=365)), listed)
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
        def real(v):
            v = str(v).strip()
            return v not in ("", "-") and "없음" not in v and "해당" not in v
        out["putback"] = bool([x for x in put if any(real(v) for k, v in x.items()
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
    """'상장일로부터 6개월' → 6, '-'·'없음'·'상장일' → 0, 모르면 None"""
    t = clean(text)
    if not t:
        return None
    t = re.sub(r"\d{4}\s*[년./-]\s*\d{1,2}\s*[월./-]?\s*\d{0,2}\s*일?(까지)?", "", t)   # '2027년 3월 15일까지' 같은 날짜 제거
    ms = PERIOD_RE.findall(t)
    if ms:
        tot = 0.0
        for v, u in ms[:2]:                                   # '1년 6개월' → 18
            v = float(v)
            tot += v * 12 if u == "년" else v / 30 if u == "일" else v
        return round(tot, 2) if tot <= 120 else None
    if t in ("-", "–", "없음", "해당없음", "해당사항없음", "상장일", "상장 당일") or "유통가능" in t \
            or "제한없음" in t.replace(" ", ""):
        return 0
    return None


PCT_RE = re.compile(r"^-?\d{1,3}(\.\d+)?\s*%?$")


def to_pct(s):
    t = clean(s).replace(",", "")
    if not t or t in ("-", "–"):
        return 0.0
    if not PCT_RE.match(t):
        return None
    v = float(t.rstrip("%").strip())
    return v if 0 <= v <= 100 else None


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


def _is_num_row(r):
    return sum(1 for c in r if re.fullmatch(r"[\d,]+(\.\d+)?%?", clean(c).replace(" ", ""))) >= 2


def holders_timeline(grid):
    """주주별 표(매각제한물량·유통가능물량 지분율, 매각제한기간) → 기간별 누적 유통가능 비율"""
    hn = next((i for i, r in enumerate(grid) if _is_num_row(r)), None)
    if not hn or hn > 5:
        return []
    width = max(len(r) for r in grid)
    labels = []
    for j in range(width):
        parts = []
        for r in grid[:hn]:
            if j < len(r) and r[j] not in parts:
                parts.append(r[j])
        labels.append(" ".join(parts).replace(" ", ""))

    def col(*need, avoid=()):
        for j, l in enumerate(labels):
            if all(any(k in l for k in alt.split("|")) for alt in need) and not any(a in l for a in avoid):
                return j
        return None
    per_c = col("기간")
    rest_c = col("매각제한|보호예수|의무보유|유통제한", "지분율|비율|%", avoid=("기간", "사유"))
    float_c = col("유통가능", "지분율|비율|%")
    post_c = col("공모후", "지분율|비율|%")
    if per_c is None:
        return []
    agg, seen, seen_fp = {}, {}, None
    for r in grid[hn:]:
        if len(r) <= per_c:
            continue
        if re.search(r"(소계|합계|총계)", " ".join(r)) or re.match(r"^\(?주\s*\d*\)", r[0]):
            continue
        m = period_months(r[per_c])
        if rest_c is not None or float_c is not None:
            rp = to_pct(r[rest_c]) if rest_c is not None and rest_c < len(r) else None
            fp = to_pct(r[float_c]) if float_c is not None and float_c < len(r) else None
            fkey = (r[0], r[1] if len(r) > 1 else "", r[float_c] if float_c is not None and float_c < len(r) else "")
            if fp and fkey != seen_fp:           # 같은 주주의 유통가능분이 바로 다음 줄에 반복되면 한 번만
                agg[0] = agg.get(0, 0) + fp
            seen_fp = fkey
            if rp:
                if not m:
                    continue
                key = (r[0], r[1] if len(r) > 1 else "", r[rest_c])
                if key in seen:                 # 같은 주식이 기간 두 개로 두 줄 적힌 경우 → 긴 기간만
                    if m <= seen[key]:
                        continue
                    agg[seen[key]] -= rp
                seen[key] = m
                agg[m] = agg.get(m, 0) + rp
        elif post_c is not None:
            v = to_pct(r[post_c]) if post_c < len(r) else None
            if v is None or m is None:
                continue
            agg[m] = agg.get(m, 0) + v
    agg = {k: v for k, v in agg.items() if v > 0.001}
    tot = sum(agg.values())
    if len(agg) < 2 or not (80 <= tot <= 115):
        return []
    out, acc = [], 0.0
    for k in sorted(agg):
        acc += agg[k]
        out.append({"m": k, "pct": round(min(acc / tot * 100, 100), 2)})
    return out


def series_timeline(grid):
    """'상장일 / 1개월 후 / 3개월 후 …' 기간별 추이 표"""
    pts = {}
    for r in grid:
        if not r:
            continue
        h = r[0]
        if not ("후" in h or "상장일" in h or "상장시" in h.replace(" ", "")):
            continue
        m = period_months(h) if PERIOD_RE.search(h) else (0 if "상장" in h else None)
        if m is None:
            continue
        ps = [to_pct(x) for x in r[1:] if "%" in x or re.fullmatch(r"\d{1,3}\.\d+", x.replace(" ", ""))]
        ps = [x for x in ps if x]
        if ps:
            pts[m] = ps[-1]
    vals = [pts[k] for k in sorted(pts)]
    if len(pts) >= 2 and all(b >= a - 0.01 for a, b in zip(vals, vals[1:])):
        return [{"m": k, "pct": round(pts[k], 2)} for k in sorted(pts)]
    return []


def lockup_timeline(rows):
    grid = expand(rows)
    return holders_timeline(grid) or series_timeline(grid)


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
    ("성장성 추천", re.compile(r"제?\s*39\s*호\s*나\s*목")),
    ("기술특례(사업모델)", re.compile(r"사업\s*모델\s*(기반\s*)?(특례|기술성장|평가|상장)")),
    ("성장성 추천", re.compile(r"성장성\s*추천|상장주선인\s*(의\s*)?(성장성\s*)?추천")),
    ("기술특례(소부장)", re.compile(r"소재\s*[·ㆍ,\s]?\s*부품\s*[·ㆍ,\s]?\s*장비.{0,40}(특례|전문기업|기술평가)")),
    ("기술특례(기술평가)", re.compile(r"기술\s*평가\s*(특례|결과|등급)|기술성장기업|기술특례")),
    ("이익미실현(테슬라)", re.compile(r"이익\s*미실현\s*(기업|요건|특례)?|테슬라\s*요건")),
]
SELF = re.compile(r"(당사|동사|발행회사)(는|가|의|와|로서|로|에)")
NEG = re.compile(r"(해당하지\s*않|아닙니다|아니며|아닌|없습니다|제외)")


def listing_track(plain):
    """plain: 태그를 벗긴 증권신고서 본문 → (트랙, 근거 문장)"""
    sents = re.split(r"(?<=[다요])\.\s*|\n", plain)
    hits = {}
    for snt in sents:
        if len(snt) > 600:
            continue
        for m in SELF.finditer(snt):
            clause = snt[m.start(): m.start() + 220]      # '당사는 …' 뒤쪽만 본다
            clause = re.sub(r"\([^)]*\)", "", clause)       # 괄호 안 규정 설명 제거
            if NEG.search(clause) or not any(k in clause for k in ("상장", "특례", "요건", "평가", "추천")):
                continue
            for name, rx in TRACKS:
                if rx.search(clause):
                    hits.setdefault(name, clause.strip())
                    break
    for name, _ in TRACKS:
        if name in hits:
            return name, hits[name][:300]
    ev = discount_evidence(plain)          # 2순위: 공모가 산정의 '현가할인율' 문단
    if ev:
        return "기술특례(기술평가)", "할인율 근거: " + ev
    return "일반", ""


# 기술특례 상장사는 미래 추정이익을 현재가치로 할인(현가할인율)해 공모가를 정하고,
# 신고서에 '코스닥 기술특례상장기업 적용실적, 연할인율' 같은 비교표를 싣는다.
# (모든 회사에 있는 '공모가 할인율'(평가가액 대비 할인)과는 다르므로 '현가'·'기술특례' 문구를 함께 본다)
TECH_WORDS = ("기술특례상장기업", "기술특례 상장기업", "기술평가기업", "기술성장기업", "기술특례기업", "기술특례상장")


def discount_evidence(plain):
    for m in re.finditer(r"할인율", plain):
        w = plain[max(0, m.start() - 350): m.end() + 350]
        if "현가" not in w and "적용실적" not in w:
            continue
        if not any(k in w for k in TECH_WORDS):
            continue
        if re.search(r"(기술특례|기술성장|기술평가)[^.\n]{0,40}(해당하지 않|아닙니다|아니므로)", w):
            continue
        a = max(0, m.start() - 120)
        return clean(plain[a: m.end() + 120])
    return ""


def discount_rate(plain):
    """미래 추정이익에 적용한 현가할인율(%)"""
    pats = [r"(\d{1,2}(?:\.\d+)?)\s*%\s*의\s*현가\s*할인율",
            r"현가\s*할인율\s*(?:은|을|로|:|\()?\s*(?:연\s*)?(\d{1,2}(?:\.\d+)?)\s*%",
            r"할인율\s*(?:은|을|로)?\s*연\s*(\d{1,2}(?:\.\d+)?)\s*%"]
    for p in pats:
        m = re.search(p, plain)
        if m:
            v = float(m.group(1))
            if 5 <= v <= 60:
                return v
    return None


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
        tables = soup.find_all("table")
        for tbl in tables:                      # 1순위: 실제로 기간별 비율이 계산되는 표
            txt = tbl.get_text(" ")
            if not any(k in txt for k in ("매각제한", "보호예수", "의무보유", "유통가능")):
                continue
            rows = table_rows(tbl)
            tl = lockup_timeline(rows)
            if tl:
                out["lockup"] = tl
                out["lockupTables"] = [rows[:80]]
                out["_allRows"] = rows
                break
        if not out["lockup"]:
            out["lockupTables"] = [table_rows(t)[:80] for t in find_lockup_tables(soup)[:1]]
    out["bizText"] = business_text_raw(xml_text)
    plain = strip_tags(re.sub(r"</(P|TD|TE|TH|TU|TR|TITLE)>", "\n", xml_text[:4000000], flags=re.I))
    out["track"], out["trackEvidence"] = listing_track(plain)
    out["discountRate"] = discount_rate(plain)
    head = plain[:300000]
    k, y, x = head.count("코스닥시장"), head.count("유가증권시장"), head.count("코넥스시장")
    out["market"] = "코스피" if y > k else "코스닥" if k else ("코넥스" if x else "코스닥")
    out["totalShares"] = total_shares([out.pop("_allRows", None) or []] + out["lockupTables"])
    out["floatAtListing"] = next((p["pct"] for p in out["lockup"] if p["m"] == 0), None)
    return out


def total_shares(tables):
    """유통가능물량 표의 '합계' 행에서 상장예정 주식수"""
    best = None
    for rows in tables:
        for r in expand(rows):
            if r and any(k in r[0] for k in ("합계", "총계")) or (len(r) > 1 and "합계" in r[1]):
                nums = [int(re.sub(r"[^\d]", "", c)) for c in r if re.fullmatch(r"[\d,]{7,}", c.replace(" ", ""))]
                if nums:
                    best = max(best or 0, max(nums))
    return best


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
    """사업의 내용 → 한 줄 요약 + 4개 항목 × 짧은 요점 (JSON)"""
    prompt = (
        f"아래는 '{name}'의 증권신고서 '사업의 내용' 원문이다. 공모주 투자자가 30초 안에 읽도록 요약하라.\n\n"
        "반드시 아래 JSON 형식 하나만 출력하라(설명·코드블록 없이).\n"
        '{"oneLine": "회사를 한 문장으로(40자 이내)",\n'
        ' "sections": [\n'
        '  {"title": "무엇을 하나", "points": ["핵심 제품·서비스", "..."]},\n'
        '  {"title": "어떻게 버나", "points": ["매출 구성·비중, 주요 고객·판매처", "..."]},\n'
        '  {"title": "강점", "points": ["기술·경쟁력·시장 지위", "..."]},\n'
        '  {"title": "리스크", "points": ["투자 시 유의점", "..."]}]}\n\n'
        "규칙:\n"
        "- 각 항목 요점은 2~3개, 요점 하나는 45자 이내의 명사형 문장('~함', '~임' 또는 명사로 끝)\n"
        "- 원문에 있는 숫자(매출 비중 %, 고객사명, 점유율, 인증·특허 수 등)는 살려서 쓸 것\n"
        "- 원문에 없는 내용·수치는 절대 지어내지 말 것. 해당 정보가 없으면 그 항목 points를 빈 배열로\n"
        "- 홍보성 수식어(국내 최고, 혁신적 등)는 빼고 사실만\n\n"
        f"<원문>\n{biz_text}\n</원문>"
    )
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": model, "max_tokens": 1200, "messages": [{"role": "user", "content": prompt}]},
        timeout=120,
    )
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json().get("content", [])).strip()
    return parse_summary(text)


def parse_summary(text):
    import json
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            j = json.loads(m.group(0))
            secs = [{"title": str(x.get("title", "")).strip(),
                     "points": [str(p).strip() for p in (x.get("points") or []) if str(p).strip()][:3]}
                    for x in j.get("sections", []) if isinstance(x, dict)]
            if j.get("oneLine") or secs:
                return {"oneLine": str(j.get("oneLine", "")).strip(), "sections": secs}
        except Exception:
            pass
    return text  # 형식이 깨지면 글 그대로
