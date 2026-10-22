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

import valuation
VAL_DEBUG = []   # 이번 실행에서 읽은 공모가 산정 구간 샘플(확인용)
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


def _amt(v):
    """'18,678,000,000' · '18678000000.0' · '1,234원' → 정수(원). 소수점을 지우지 않는다(예전엔 .0이 붙으면 10배가 됐음)"""
    t = re.sub(r"[,\s원]", "", str(v or ""))
    m = re.fullmatch(r"-?\d+(?:\.\d+)?", t)
    return int(round(float(t))) if m else int(re.sub(r"[^\d]", "", t) or 0)


def fund_fix(fund, amount=None, old_ratio=None):
    """공모자금 사용 목적 표 점검·보정. 반환 (목록, 점검정보)
    · '합계'·'계' 줄, 다른 항목 합과 같은 줄(합계가 엉뚱한 이름으로 들어온 경우) 제거
    · 단위(천원·백만원) 보정, 일부 항목만 10배로 읽힌 경우 보정
    · 기준 = 신주 모집금액 = 공모금액 × (1 − 구주매출 비중). 사용 목적 표는 신주 몫만 다룬다
    status: ok(1.5% 이내) · price(15% 이내: 신고서 가정 공모가 기준 차이) · total(공모금액 전체와 같음: 구주매출 비중 또는
            DART 수치 확인 필요) · mismatch(원문 확인) · unknown(기준 금액 없음)"""
    from itertools import product
    fund = [dict(f) for f in (fund or []) if f.get("v") and f.get("k") not in ("합계", "계", "총계", "합 계")]
    if not fund:
        return [], None
    old = old_ratio or 0
    exp = round(amount * (1 - old / 100)) if amount and old < 100 else None
    tot = sum(f["v"] for f in fund)
    dup = [i for i, f in enumerate(fund) if abs(f["v"] - (tot - f["v"])) <= max(2, f["v"] * 0.002)] if len(fund) >= 3 else []
    # 후보: (합계 줄 제거 여부) × (단위 배수). 공모 규모에 가장 가까운 조합을 고른다
    variants = [(False, fund)] + ([(True, [f for k, f in enumerate(fund) if k != dup[0]])] if len(dup) == 1 else [])
    refs = [x for x in (exp, amount) if x]
    best = None
    for dropped, fl in variants:
        S = sum(f["v"] for f in fl)
        for mult in (1, 1000, 1000000):
            if refs:
                err = min(abs(S * mult / r - 1) for r in refs)
            else:                                          # 기준 금액이 없으면 10억~1조원 사이가 되는 단위
                err = 0 if 1e9 <= S * mult <= 1e12 else 9
            if best is None or err < best[0] - 1e-9:
                best = (err, dropped, fl, mult)
    _, dropped, fund, mult = best
    if dropped and not refs:                               # 기준이 없으면 합계 줄로 단정하지 않음
        dropped, fund = False, variants[0][1]
    fund = [{"k": f["k"], "v": f["v"] * mult} for f in fund]
    fixes = []
    if dropped:
        fixes.append("합계 줄이 항목으로 들어와 제외")
    if mult > 1 and refs:
        fixes.append(("천원" if mult == 1000 else "백만원") + " 단위 → 원으로 환산")
    S = sum(f["v"] for f in fund)
    # 일부 항목만 10배(소수점·자릿수 오류): 항목 하나가 공모 전체보다 큰, 있을 수 없는 경우에만 고친다
    if exp and S / exp > 1.6 and len(fund) <= 8 and max(f["v"] for f in fund) > (amount or exp) * 1.2:
        cand = None
        for mask in product((0, 1), repeat=len(fund)):
            if not any(mask):
                continue
            s2 = sum(f["v"] / 10 if m else f["v"] for f, m in zip(fund, mask))
            err = abs(s2 / exp - 1)
            if err <= 0.05 and (cand is None or (sum(mask), err) < cand[:2]):   # 가장 적게 고치는 쪽 우선
                cand = (sum(mask), err, mask)
        if cand:
            fund = [{"k": f["k"], "v": round(f["v"] / 10) if m else f["v"]} for f, m in zip(fund, cand[2])]
            fixes.append("일부 금액 10배 오기 → 보정")
    S = sum(f["v"] for f in fund)
    chk = {"expected": exp, "sum": S, "fixed": ", ".join(fixes) or None}
    if not exp:
        chk["status"] = "unknown"
    else:
        r = S / exp
        chk["status"] = "ok" if abs(r - 1) <= 0.015 else ("price" if 0.85 <= r <= 1.15 else "mismatch")
        if any("10배" in x for x in fixes) and abs(r - 1) > 0.005:
            chk["status"] = "mismatch"                     # 10배 오기를 고쳐도 딱 맞지 않음 = 다른 자리에도 오기 → 원문 확인
        elif chk["status"] == "mismatch" and old > 0 and 0.97 <= S / amount <= 1.06:
            chk["status"] = "total"
    return fund, chk


# ---------------------------------------------------------------- 증권신고서 'Ⅴ. 자금의 사용목적' 원문 표
UNIT_RE = re.compile(r"단\s*위\s*[:：]?\s*(백만원|천원|억원|원)")
UNIT_MUL = {"원": 1, "천원": 1000, "백만원": 1000000, "억원": 100000000}
USE_COLS = ("시설", "영업양수", "운영", "채무상환", "타법인", "기타", "연구개발")


def _clean_label(t):
    return re.sub(r"&cr|\s+", "", t or "")


def fund_blocks(xml_text):
    """'자금의 사용목적' 장을 [('text', 문자열) | ('table', 격자)] 순서대로"""
    titles = list(re.finditer(r"<TITLE[^>]*>([^<]*)</TITLE>", xml_text, re.I))
    hit = next((m for m in titles if re.search(r"자금의\s*사용\s*목적", m.group(1))), None)
    if not hit:
        return []
    st = hit.start()
    nxt = next((m for m in titles if m.start() > st + 50 and not re.search(r"자금|조달|사용|발행제비용", m.group(1))
                and re.match(r"\s*(Ⅵ|VI|Ⅶ|제\s*\d\s*부|[가-힣A-Z]{0,2}\s*\d?\s*\.?\s*(그\s*밖|투자자\s*보호|기타))", m.group(1))), None)
    sec = xml_text[st: nxt.start() if nxt else st + 400000]
    blocks, pos = [], 0
    for m in re.finditer(r"<TABLE\b.*?</TABLE>", sec, re.S | re.I):
        blocks.append(("text", strip_tags(sec[pos:m.start()])))
        tb = BeautifulSoup(m.group(0), "html.parser").find("table")
        if tb is not None and len(m.group(0)) < 300000:
            blocks.append(("table", valuation._grid(valuation._rows(tb))))
        pos = m.end()
        if len(blocks) > 80:
            break
    blocks.append(("text", strip_tags(sec[pos:pos + 20000])))
    return blocks


def fund_from_blocks(blocks, gross_hint=None):
    """조달 내역(총공모금액·의무인수·구주매출·발행제비용·순수입금)과 사용 계획(시설·운영…)을 읽는다.
    단위는 표 바로 앞의 '(단위: …)', 없으면 순수입금과 맞는 단위로 정한다."""
    unit, raise_, uses, basis = None, None, None, None
    for kind, b in blocks:
        txt = b if kind == "text" else " ".join(" ".join(r) for r in b)
        if basis is None:
            m = re.search(r"(확정\s*공모\s*가\s*액?[인은]?\s*[\d,]+\s*원|희망\s*공모\s*가\s*액?[^.]{0,30}하단[^.]{0,20}?[\d,]+\s*원|"
                          r"(?:예상|희망)[^.]{0,40}하단[^.]{0,10}[\d,]+\s*원)", re.sub(r"\s+", " ", txt))
            basis = re.sub(r"\s+", " ", m.group(0)) if m else None
        if kind == "text":
            us = UNIT_RE.findall(b)
            if us:
                unit = us[-1]
            continue
        g = b
        us = UNIT_RE.findall(txt)
        if us and len(g) <= 2 and not any(_amt(c) for r in g for c in r if re.fullmatch(r"[\d,\s]+", c or "")):
            unit = us[-1]                      # '(단위: 백만원)'만 있는 작은 표
            continue
        if raise_ is None and any(re.search(r"순\s*(수\s*입|조\s*달)\s*금", r[0] if r else "") for r in g):
            rows = {}
            for r in g:
                if len(r) < 2:
                    continue
                lab = _clean_label(r[0])
                val = next((_amt(c) for c in r[1:] if re.fullmatch(r"-?[\d,\s]+(\.\d+)?", (c or "").strip()) and _amt(c)), None)
                if val is None:
                    continue
                if re.search(r"순(수입|조달)", lab):
                    rows["net"] = val
                elif "의무" in lab and ("인수" in lab or "취득" in lab):
                    rows["sponsor"] = val
                elif "발행제비용" in lab or "발행비용" in lab:
                    rows["cost"] = val
                elif re.search(r"매출(대금|금액)|구주", lab) and not re.search(r"모집|총", lab):
                    rows["sell"] = val
                elif "신주" in lab and "모집" in lab and not re.search(r"구주|매출", lab):
                    rows["newGross"] = val
                elif "gross" not in rows and re.search(r"총|공모|모집", lab):
                    rows["gross"] = val
            if "net" in rows:
                raise_ = {"unit": unit, **rows}
            continue
        if uses is None:
            for i, r in enumerate(g[:-1]):
                heads = [_clean_label(c) for c in r]
                if sum(1 for h in heads if any(h.startswith(k) for k in USE_COLS)) >= 3 and any(h in ("계", "합계") for h in heads):
                    vals = g[i + 1]
                    items, total = [], None
                    for h, v in zip(heads, vals):
                        n = _amt(v) if re.fullmatch(r"-?[\d,\s]+(\.\d+)?", (v or "").strip() or "x") else 0
                        if h in ("계", "합계"):
                            total = n
                        elif n:
                            items.append({"k": re.sub(r"(자금)?$", "자금", h) if h in ("시설", "운영", "기타") else h, "v": n})
                    if items:
                        uses = {"unit": unit, "items": items, "total": total}
                    break
    if not raise_ and not uses:
        return None

    def scale(x, u, ref):
        """단위 배수: 적힌 단위를 우선, 기준 금액과 동떨어지면 맞는 배수로"""
        cands = ([UNIT_MUL[u]] if u else []) + [1, 1000, 1000000]
        if ref:
            ok = [m for m in cands if 0.2 <= x * m / ref <= 5]
            return ok[0] if ok else cands[0]
        return cands[0] if u else (1 if x >= 1e8 else 1000000 if x < 1e6 else 1000)

    out = {"basis": basis}
    if raise_:
        ref = gross_hint
        key = raise_.get("gross") or raise_.get("net")
        mul = scale(key, raise_.get("unit"), ref)
        for k in ("gross", "newGross", "sponsor", "sell", "cost", "net"):
            if raise_.get(k):
                out[k] = raise_[k] * mul
    if uses:
        ref = out.get("net") or gross_hint
        tot = uses["total"] or sum(x["v"] for x in uses["items"])
        mul = scale(tot, uses.get("unit"), ref)
        out["uses"] = [{"k": x["k"], "v": x["v"] * mul} for x in uses["items"]]
        out["usesTotal"] = tot * mul
    # 조달 내역 산식 검산: (신주 모집금액 또는 총공모금액) + 의무인수 − 구주매출 − 발행제비용 = 순수입금
    net = out.get("net")
    if net:
        def calc():
            if out.get("newGross"):                        # 신주 모집금액이 따로 있으면 거기서 시작(구주매출은 이미 빠져 있음)
                return out["newGross"] + (out.get("sponsor") or 0) - (out.get("cost") or 0)
            return ((out.get("gross") or 0) + (out.get("sponsor") or 0)
                    - (out.get("sell") or 0) - (out.get("cost") or 0))
        if (not out.get("gross") and out.get("sell")
                and abs(out["sell"] + (out.get("sponsor") or 0) - (out.get("cost") or 0) - net) / net <= 0.005):
            out["gross"] = out.pop("sell")             # '공모(매출)금액'처럼 총액 줄에 '매출'이 들어간 경우
        out["math"] = abs(calc() - net) / net <= 0.005
        if (out.get("newGross") and out.get("gross") and not out.get("sell") and out["gross"] - out["newGross"] > net * 0.02
                and abs(out["gross"] - out["newGross"] - (out.get("sponsor") or 0)) > net * 0.005):   # 차이가 의무인수면 구주 아님
            out["sell"] = out["gross"] - out["newGross"]     # 총공모금액 − 신주 모집금액 = 구주매출
        if not out["math"] and out.get("gross") and not out.get("newGross"):   # 안 맞으면 구주매출 역산
            sell = out.pop("sell", None)
            out["sell"] = None
            d = calc() - net
            if d > 0:
                out["sell"], out["sellDerived"] = d, True
            elif sell:
                out["sell"] = sell
            out["math"] = abs(calc() - net) / net <= 0.005
            if not out["sell"]:
                out.pop("sell")
    # 검산: 사용 계획 합계 = 순수입금(반올림 차이 0.5% 이내)
    if out.get("net") and out.get("usesTotal"):
        out["tie"] = abs(out["usesTotal"] / out["net"] - 1) <= 0.005
    return out


def doc_offer_amount(fd, old_ratio=None):
    """원문 조달 내역에서 '공모금액'(신주 모집 + 구주 매출, 의무인수 제외). 확정공모가 기준이고 산식이 맞을 때만.
    원문 총액이 신주 몫만인 문서(구주매출 줄 없음 + 구주매출이 있는 공모)는 쓰지 않는다"""
    if not fd or not fd.get("gross") or not fd.get("math") or "확정" not in (fd.get("basis") or ""):
        return None
    if fd.get("newGross"):                               # '총공모금액'이 의무인수까지 더한 값인 문서
        return fd["newGross"] + (fd.get("sell") or 0)
    if not fd.get("sell") and (old_ratio or 0) > 0:
        return None
    return fd["gross"]


def doc_old_ratio(fd, total):
    """원문 조달 내역으로 구주매출 비중(%). 산식이 맞고 원문 총공모금액이 사이트 공모금액(주식수×공모가)과 같을 때만"""
    if (not fd or not fd.get("math") or not fd.get("gross") or not total or not fd.get("sell")
            or fd.get("sellDerived") or not fd.get("tie")):
        return None   # 원문에 '구주매출(매출대금)' 차감 줄이 있을 때만. 없으면 자기주식 매출 등일 수 있어 판단 안 함
    if abs(fd["gross"] / total - 1) > 0.01:              # 원문 총액이 신주 몫만이거나 기준가가 다르면 판단 안 함
        return None
    return round((fd.get("sell") or 0) / fd["gross"] * 100, 1)


def fund_from_doc(xml_text, gross_hint=None):
    try:
        return fund_from_blocks(fund_blocks(xml_text), gross_hint)
    except Exception:
        return None


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

    def offer_filings(self, days=150):
        """최근 발행공시(증권신고서(지분증권)·투자설명서) 전체 — 회사 이름이 겹쳐도 실제 신고한 회사를 찾기 위해"""
        if getattr(self, "_offer", None) is None:
            rows, end = [], date.today()
            while (date.today() - end).days < days:          # 고유번호 없이 조회는 3개월 단위
                bgn = max(end - timedelta(days=85), date.today() - timedelta(days=days))
                page = 1
                while True:
                    try:
                        j = self._get("list.json", bgn_de=bgn.strftime("%Y%m%d"), end_de=end.strftime("%Y%m%d"),
                                      pblntf_ty="C", page_no=page, page_count=100).json()
                    except Exception:
                        break
                    if j.get("status") != "000":
                        break
                    rows += j.get("list", [])
                    if page >= int(j.get("total_page", 1)):
                        break
                    page += 1
                end = bgn - timedelta(days=1)
            self._offer = [r for r in rows if "증권신고서(지분증권)" in r["report_nm"] or
                           ("투자설명서" in r["report_nm"] and "채무" not in r["report_nm"])]
            self.log(f"[DART] 최근 공모 신고서 {len(self._offer)}건")
        return self._offer

    def find_offer(self, name, after=None):
        """이름으로 최근 공모 신고서 찾기 → (고유번호, 신고서). 투자설명서(최종) 우선"""
        def nm(x):
            return re.sub(r"[\s\.\-·]|\(주\)|㈜|주식회사", "", x or "").lower()
        k = nm(name)
        a = after.strftime("%Y%m%d") if after else ""
        rows = [r for r in self.offer_filings() if r.get("rcept_dt", "") >= a]
        c = [r for r in rows if nm(r.get("corp_name")) == k]
        if not c and len(k) >= 3:                      # 이름 일부만 같을 때(예: 영문 병기, '코리아' 생략)는 회사가 하나뿐일 때만
            part = [r for r in rows if k in nm(r.get("corp_name")) or nm(r.get("corp_name")) in k]
            if len({r["corp_code"] for r in part}) == 1:
                c = part
        if not c:
            return None, None
        best = (max([r for r in c if "투자설명서" in r["report_nm"]], key=lambda r: r["rcept_no"], default=None)
                or max(c, key=lambda r: r["rcept_no"]))
        return best["corp_code"], best

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

    def prospectus(self, corp_code, listed, after=None, merger=False):
        """상장일 이전 1년 내 투자설명서(최종) → 없으면 가장 최근 증권신고서(지분증권)
           merger=True(스팩합병): 증권신고서(합병) → 합병 투자설명서"""
        rows = self.filings(corp_code, after or (listed - timedelta(days=365)), listed)
        def pick(word, also=None):
            c = [r for r in rows if word in r["report_nm"] and (not also or also in r["report_nm"])]
            return max(c, key=lambda r: r["rcept_no"]) if c else None
        if merger == "all":                           # 합병 관련 문서 후보 전부(최신순)
            c = [r for r in rows if "합병" in r["report_nm"] and ("증권신고서" in r["report_nm"] or "투자설명서" in r["report_nm"])]
            return sorted(c, key=lambda r: r["rcept_no"], reverse=True)
        if merger:
            return pick("증권신고서", "합병") or pick("투자설명서", "합병") or pick("투자설명서")
        return pick("투자설명서") or pick("증권신고서(지분증권)")

    def document_text(self, rcept_no):
        r = self._get("document.xml", rcept_no=rcept_no)
        if not r.content.startswith(b"PK"):           # zip 대신 오류 메시지가 온 경우(원문 없음·호출 한도 등)
            m = re.search(r"<message>(.*?)</message>|\"message\"\s*:\s*\"(.*?)\"", r.text or "")
            raise RuntimeError("DART 원문 없음: " + ((m.group(1) or m.group(2)) if m else r.text[:80].strip()))
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
        sell_rows = 0
        if kinds:                                   # 모집(신주)·매출(구주)이 따로 줄로 올 수 있어 최근 신고서의 줄을 모두 더한다
            last = max(x.get("rcept_no", "") for x in kinds)
            rows = [x for x in kinds if x.get("rcept_no", "") == last] or kinds[-1:]
            cnts = [num(k.get("stkcnt")) for k in rows]
            if len(rows) > 2 and max(cnts) == sum(cnts) - max(cnts):      # '합계' 줄이 섞여 있으면 그 줄만
                rows = [rows[cnts.index(max(cnts))]]
            out["shares"] = sum(num(k.get("stkcnt")) for k in rows)
            out["amount"] = sum(num(k.get("slta")) for k in rows)
            sell_rows = sum(num(k.get("stkcnt")) for k in rows if "매출" in str(k.get("slmthn") or "") and "모집" not in str(k.get("slmthn") or ""))
        uw = g.get("인수인정보") or []
        if uw:
            last_rcp = max(x.get("rcept_no", "") for x in uw)
            names, detail, fee, amt, how = [], [], 0, 0, None
            for x in uw:
                if x.get("rcept_no", "") != last_rcp:
                    continue
                n = (x.get("actnmn") or "").strip()
                if n and n not in names:
                    names.append(n)
                raw_fee = str(x.get("udtprc") or "")
                f_ = num(raw_fee) if re.fullmatch(r"[\d,\s원\-]+", raw_fee) else 0   # 인수대가(=인수수수료)
                a_ = num(x.get("udtamt")) if re.fullmatch(r"[\d,\s원\-]+", str(x.get("udtamt") or "")) else 0
                mr = re.search(r"(\d(?:\.\d{1,2})?)\s*%", raw_fee)
                if mr and "uwFeeRateTxt" not in out:
                    out["uwFeeRateTxt"] = float(mr.group(1))
                fee += f_
                amt += a_
                how = how or (str(x.get("udtmth") or "").strip() or None)
                if n:
                    detail.append({"n": n, "role": (x.get("actsen") or "").strip(), "amt": a_ or None, "fee": f_ or None})
            out["underwriters"] = names
            out["uwDetail"] = detail
            out["uwFee"] = fee or None
            out["uwMethod"] = how
            base = out.get("amount") or amt
            if fee and base and fee < base * 0.2:
                out["uwFeeRate"] = round(fee / base * 100, 2)
            elif out.get("uwFeeRateTxt"):
                out["uwFeeRate"] = out["uwFeeRateTxt"]
        sellers = g.get("매출인에관한사항") or []
        if sellers and out.get("shares"):
            last_rcp = max(x.get("rcept_no", "") for x in sellers)
            old = sum(num(x.get("slstk")) for x in sellers if x.get("rcept_no", "") == last_rcp)
            out["oldShareRatio"] = round(old / out["shares"] * 100, 1) if old else 0.0
        elif sell_rows and out.get("shares"):
            out["oldShareRatio"] = round(sell_rows / out["shares"] * 100, 1)
        else:
            out["oldShareRatio"] = 0.0 if out.get("shares") else None
        if out.get("oldShareRatio") is not None:
            out["oldShareRatio"] = min(100.0, out["oldShareRatio"])
        uses = g.get("자금의사용목적") or []
        if uses:
            last_rcp = max(x.get("rcept_no", "") for x in uses)
            fund = []
            for x in uses:
                if x.get("rcept_no", "") != last_rcp:
                    continue
                if "se" in x and "amt" in x:        # {구분, 금액} 형태
                    if _amt(x["amt"]) > 0:
                        fund.append({"k": str(x["se"]).strip(), "v": _amt(x["amt"])})
                    continue
                for k, v in x.items():
                    if k in ("rcept_no", "corp_cls", "corp_code", "corp_name"):
                        continue
                    n = _amt(v)
                    if n > 0 and not re.fullmatch(r"\d{8,}", str(v).strip()):
                        fund.append({"k": FUND_NAMES.get(k, k), "v": n})
            out["fundUseRaw"] = fund                    # DART 원래 값(보정은 매번 이 값에서 다시 한다)
            out["fundUse"], out["fundChk"] = fund_fix(fund, out.get("amount"), out.get("oldShareRatio"))
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


def uw_fee(xml_text, plain):
    """증권신고서 '5. 인수 등에 관한 사항 - 나. 인수대가에 관한 사항 - (1) 인수수수료' 표
       → 기본 인수수수료율(%), 금액(원), 성과수수료율(% 이내)"""
    out = {"uwFeeRateDoc": None, "uwFeeAmtDoc": None, "perfFeeRate": None}
    pct = lambda t: next((float(x) for x in re.findall(r"(\d{1,2}(?:\.\d{1,3})?)\s*%", t) if 0.2 <= float(x) <= 10), None)
    for m in re.finditer(r"<TABLE\b.*?</TABLE>", xml_text[:4000000], re.S | re.I):
        chunk = m.group(0)
        if "인수수수료" not in chunk or len(chunk) > 60000:
            continue
        before = strip_tags(xml_text[max(0, m.start() - 600): m.start()])
        unit = 1000 if "천원" in before or "천원" in chunk[:600] else 1_000_000 if "백만원" in before else 1
        grid = expand(table_rows(BeautifulSoup(chunk, "html.parser").find("table")))
        for r in grid:
            head = (r[0] if r else "").replace(" ", "")
            rest = " ".join(dict.fromkeys(r[1:]))
            if "인수수수료" in head and out["uwFeeRateDoc"] is None:
                out["uwFeeRateDoc"] = pct(rest)
                for c in r[1:]:
                    if re.fullmatch(r"[\d,]{4,}", c.replace(" ", "")):
                        out["uwFeeAmtDoc"] = int(c.replace(",", "").replace(" ", "")) * unit
                        break
            elif "성과수수료" in head and out["perfFeeRate"] is None:
                out["perfFeeRate"] = pct(rest)
        if out["uwFeeRateDoc"] is not None:
            break
    if out["uwFeeRateDoc"] is None:                      # 표가 없으면 본문 문장(…인수금액의 4.5%에 해당하는 금액…)
        f = re.sub(r"\s+", " ", plain[:3000000])
        m = re.search(r"인수\s*수수료[^%]{0,250}?(?:인수|공모)\s*(?:금액|총액)의\s*(\d(?:\.\d{1,3})?)\s*%", f)
        out["uwFeeRateDoc"] = float(m.group(1)) if m else fee_rate(plain)
        if out["perfFeeRate"] is None:
            m = re.search(r"성과\s*수수료[^%]{0,200}?(\d(?:\.\d{1,3})?)\s*%", f)
            out["perfFeeRate"] = float(m.group(1)) if m else None
    return out


def fee_info(plain):
    """증권신고서 '5. 인수 등에 관한 사항 - 나. 인수대가에 관한 사항 - (1) 인수수수료' 표
       예) 인수수수료(성과수수료 제외) | KB증권㈜ | 990,036 | 인수금액의 4.5% …  /  성과수수료 | … | 인수금액의 1.5% 이내
       → (기본 수수료율, 성과수수료율)"""
    f = re.sub(r"\s+", " ", plain[:3000000])
    PC = r"(\d{1,2}(?:\.\d{1,3})?)\s*%"
    base = perf = None
    for m in re.finditer(r"인수\s*수수료(?:\s*\(\s*성과\s*수수료\s*제외\s*\))?", f):
        w = f[m.end(): m.end() + 220]
        mm = re.search(r"(?:인수\s*금액|공모\s*금액|총\s*인수\s*금액|모집\s*총액|발행\s*금액)\s*(?:의|x|×|\*)?\s*" + PC, w)
        if mm and 0.3 <= float(mm.group(1)) <= 10:
            base = float(mm.group(1))
            break
    if base is None:
        m = re.search(r"인수\s*수수료[^%]{0,80}?" + PC, f)
        if m and 0.3 <= float(m.group(1)) <= 10:
            base = float(m.group(1))
    for m in re.finditer(r"성과\s*수수료(?!\s*(?:제외|를\s*제외|는\s*제외))", f):
        w = f[m.end(): m.end() + 200]
        mm = re.search(r"(?:인수\s*금액|공모\s*금액|총\s*인수\s*금액)\s*(?:의|x|×)?\s*" + PC, w)
        if mm and 0.1 <= float(mm.group(1)) <= 5:
            perf = float(mm.group(1))
            break
    return base, perf


def fee_rate(plain):
    return fee_info(plain)[0]


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
    out["fundDoc"] = fund_from_doc(xml_text)          # 'Ⅴ. 자금의 사용목적' 원문 표(조달 내역·사용 계획)
    plain = strip_tags(re.sub(r"</(P|TD|TE|TH|TU|TR|TITLE)>", "\n", xml_text[:4000000], flags=re.I))
    out["track"], out["trackEvidence"] = listing_track(plain)
    out["discountRate"] = discount_rate(plain)
    out.update(uw_fee(xml_text, plain))
    try:                                         # 공모가 산정 근거(PER·비교회사·할인율)
        out["valuation"] = valuation.valuation_info(xml_text) or None
        v = out["valuation"] or {}
        ok = bool(v.get("peers")) and v.get("appliedMult") and v.get("discountRange")
        n_ok = sum(1 for x in VAL_DEBUG if x.get("ok"))
        if (ok and n_ok < 3) or (not ok and len(VAL_DEBUG) - n_ok < 8):   # 실패한 문서 위주로 원문 일부 저장
            smp = valuation.debug_sample(xml_text, 15000 if not ok else 3000)
            smp["parsed"], smp["ok"] = out["valuation"], bool(ok)
            VAL_DEBUG.append(smp)
    except Exception as e:
        out["valuation"] = None
        VAL_DEBUG.append({"error": str(e)[:300]}) if len(VAL_DEBUG) < 10 else None
    head = plain[:300000]
    k, y, x = head.count("코스닥시장"), head.count("유가증권시장"), head.count("코넥스시장")
    out["market"] = "코스피" if y > k else "코스닥" if k else ("코넥스" if x else "코스닥")
    out["totalShares"] = total_shares([out.pop("_allRows", None) or []] + out["lockupTables"]) or shares_from_text(plain)
    out["floatAtListing"] = next((p["pct"] for p in out["lockup"] if p["m"] == 0), None)
    return out


def merger_prices(xml_text):
    """합병 증권신고서의 '합병가액' → (스팩 쪽 가액, 상대 회사 가액). 스팩 가액은 보통 2,000원 안팎."""
    plain = strip_tags(re.sub(r"</(P|TD|TE|TH|TU|TR|TITLE)>", "\n", xml_text[:3000000], flags=re.I))
    vals = []
    for m in re.finditer(r"합병\s*가액", plain):
        w = plain[m.start(): m.start() + 260]
        for n in re.findall(r"(\d{1,3}(?:,\d{3})+|\d{3,6})\s*원", w):
            v = int(n.replace(",", ""))
            if 100 <= v <= 5_000_000:
                vals.append(v)
    if not vals:
        return None, None
    from collections import Counter
    spac = [v for v in vals if 1800 <= v <= 2700]
    other = Counter(v for v in vals if not 1800 <= v <= 2700)
    sv = Counter(spac).most_common(1)[0][0] if spac else None
    tv = other.most_common(1)[0][0] if other else None
    return sv, tv


def shares_from_text(plain):
    """본문의 '상장예정주식수 N주', '공모 후 발행주식총수 N주'"""
    vals = []
    for p in (r"상장\s*예정\s*주식\s*(?:총\s*)?수[^\d\n]{0,40}([\d,]{7,})\s*주",
              r"공모\s*후\s*(?:발행\s*)?주식\s*(?:총\s*)?수[^\d\n]{0,40}([\d,]{7,})\s*주",
              r"상장\s*예정\s*주식[^\d\n]{0,40}([\d,]{7,})\s*주"):
        for m in re.finditer(p, plain):
            v = int(m.group(1).replace(",", ""))
            if 1_000_000 <= v <= 5_000_000_000:
                vals.append(v)
    if not vals:
        return None
    from collections import Counter
    return Counter(vals).most_common(1)[0][0]


def total_shares(tables):
    """유통가능물량 표의 '합계' 행에서 상장예정 주식수"""
    best = None
    for rows in tables:
        for r in expand(rows):
            if r and any(k in " ".join(r[:2]).replace(" ", "") for k in ("합계", "총계", "계", "Total")):
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
