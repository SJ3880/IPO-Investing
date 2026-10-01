"""
KIND(한국거래소 상장공시시스템) 수집
  · 공모기업 진행현황(pubofrprogcom)  : 신고서 제출 → 수요예측 → 청약 → 상장예정
  · 신규상장기업(listingcompany)       : 상장유형·상장주선인
  · 상장예비심사 현황(listinvstgcom)   : 청구 → 승인/미승인/철회
엔드포인트 형식은 오픈소스 krx-kind-data-api(beaten-by-the-market) 참고.
예비심사 화면은 형식이 공개돼 있지 않아 화면에서 자동으로 찾아 쓰고,
실패하면 data/debug/ 에 원본 HTML을 남겨 고칠 수 있게 한다.
"""
import io
import re
from datetime import date
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://kind.krx.co.kr"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
CODE_RE = re.compile(r"(?:fnDetailView|companysummary_open|openCompanyInfoNew|fnViewDetail)\('([A-Za-z0-9]+)'")
S = requests.Session()


def post(path, form, as_data=True, timeout=60):
    h = {"User-Agent": UA, "Referer": f"{BASE}/"}
    url = f"{BASE}/{path}"
    r = S.post(url, data=form, headers=h, timeout=timeout) if as_data else S.post(url, params=form, headers=h, timeout=timeout)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def list_table(html, columns=None, code_col="회사코드"):
    """KIND 목록 표 → DataFrame (+ onclick 안의 회사코드)"""
    soup = BeautifulSoup(html, "html.parser")
    best = None
    for t in soup.find_all("table"):
        rows = (t.find("tbody") or t).find_all("tr")
        if sum(1 for tr in rows if len(tr.find_all("td")) >= 3) >= 1:
            if best is None or len(rows) > len((best.find("tbody") or best).find_all("tr")):
                best = t
    if best is None:
        return pd.DataFrame()
    head = [re.sub(r"\s+", "", th.get_text()) for th in best.find_all("th")]
    body_rows = [tr for tr in (best.find("tbody") or best).find_all("tr") if len(tr.find_all("td")) >= 3]
    data = []
    for tr in body_rows:
        cells = [re.sub(r"\s+", " ", td.get_text(" ")).strip() for td in tr.find_all("td")]
        m = CODE_RE.search(str(tr))
        data.append(cells + [m.group(1) if m else None])
    n = max(len(r) for r in data)
    cols = list(columns) if columns else (head[: n - 1] if len(head) >= n - 1 else [f"c{i}" for i in range(n - 1)])
    cols = (cols + [f"c{i}" for i in range(len(cols), n - 1)])[: n - 1] + [code_col]
    return pd.DataFrame([r + [None] * (n - len(r)) for r in data], columns=cols)


# ------------------------------------------------------------------ 공모기업 진행현황
def pubofr(from_date="2020-01-01", to_date=None):
    to_date = to_date or date.today().isoformat()
    form = {"method": "searchPubofrProgComSub", "forward": "pubofrprogcom_sub", "searchMode": "1",
            "searchCodeType": "", "currentPageSize": "3000", "pageIndex": "1", "orderMode": "1",
            "orderStat": "D", "searchCorpName": "", "searchCorpNameTmp": "", "isurCd": "",
            "repIsuSrtCd": "", "bzProcsNo": "", "detailMarket": "", "marketType": "",
            "repMajAgntDesignAdvserComp": "", "repMajAgntComp": "", "designAdvserComp": "",
            "fromDate": from_date, "toDate": to_date}
    html = post("listinvstg/pubofrprogcom.do", form, as_data=True)
    cols = ["회사명", "신고서제출일", "수요예측일정", "청약일정", "납입일", "확정공모가",
            "공모금액(백만원)", "상장예정일", "상장주선인"]
    return list_table(html, cols, "업무처리번호")


# ------------------------------------------------------------------ 신규상장기업
def listing_companies(market, from_date="2020-01-01", to_date=None):
    """market: '1'=코스피, '2'=코스닥"""
    to_date = to_date or date.today().isoformat()
    form = {"method": "searchListingTypeSub", "forward": "listingtype_sub", "currentPageSize": "3000",
            "pageIndex": "1", "orderMode": "1", "orderStat": "D", "marketType": market,
            "listTypeArrStr": "01|02|03|04|05|", "secuGrpArrStr": "0|ST|FS|MF|SC|RT|DR|",
            "fromDate": from_date, "toDate": to_date}
    html = post("listinvstg/listingcompany.do", form, as_data=False)
    cols = ["회사명", "상장일", "상장유형", "증권구분", "업종", "국적", "상장주선인"]
    return list_table(html, cols, "회사코드")


# ------------------------------------------------------------------ 상장예비심사
INVSTG_MAIN = "listinvstg/listinvstgcom.do"


def invstg(from_date="2020-01-01", to_date=None, debug_dir=None, log=print):
    """상장예비심사 기업 목록. 화면의 숨은 폼값과 조회 메서드를 자동으로 찾아 호출한다."""
    to_date = to_date or date.today().isoformat()
    h = {"User-Agent": UA, "Referer": f"{BASE}/"}
    main = S.get(f"{BASE}/{INVSTG_MAIN}", params={"method": "searchListInvstgCorpMain"}, headers=h, timeout=60)
    main.encoding = "utf-8"
    soup = BeautifulSoup(main.text, "html.parser")
    base_form = {}
    for inp in soup.find_all(["input", "select"]):
        nm = inp.get("name")
        if not nm:
            continue
        if inp.name == "select":
            opt = inp.find("option", selected=True) or inp.find("option")
            base_form[nm] = opt.get("value", "") if opt else ""
        elif inp.get("type") not in ("checkbox", "radio") or inp.has_attr("checked"):
            base_form[nm] = inp.get("value", "")
    methods = re.findall(r"['\"](search\w*Sub)['\"]", main.text)
    forwards = re.findall(r"['\"](\w*_sub)['\"]", main.text)
    cand = [("searchListInvstgCorpSub", "listinvstgcom_sub")]
    for m in methods:
        for f in (forwards or [""]):
            if (m, f) not in cand:
                cand.append((m, f))
    last_html = ""
    for m, f in cand[:8]:
        form = dict(base_form)
        form.update({"method": m, "forward": f, "currentPageSize": "3000", "pageIndex": "1",
                     "fromDate": from_date, "toDate": to_date})
        for k in list(form):
            if k.lower().endswith("fromdate") or k.lower() in ("startdate", "fromdt"):
                form[k] = from_date
            if k.lower().endswith("todate") or k.lower() in ("enddate", "todt"):
                form[k] = to_date
        try:
            html = post(INVSTG_MAIN, form, as_data=True)
        except Exception as e:
            log(f"[KIND 예비심사] {m}/{f} 실패: {e}")
            continue
        last_html = html
        df = list_table(html)
        if len(df) and any("회사명" in c for c in df.columns):
            log(f"[KIND 예비심사] {m}/{f} 로 {len(df)}건 조회")
            return normalize_invstg(df)
    if debug_dir:
        Path(debug_dir).mkdir(parents=True, exist_ok=True)
        (Path(debug_dir) / "kind_invstg_main.html").write_text(main.text, "utf-8")
        (Path(debug_dir) / "kind_invstg_last.html").write_text(last_html or "", "utf-8")
    raise RuntimeError("예비심사 목록을 찾지 못함(data/debug 에 원본 저장)")


def normalize_invstg(df):
    """화면 컬럼 이름이 조금 달라도 표준 이름으로 맞춘다"""
    def pick(*keys, no=()):
        for k in keys:                      # 앞의 키워드가 우선
            for c in df.columns:
                if k in c and not any(x in c for x in no):
                    return c
        return None
    m = {
        "name": pick("회사명", "기업명"),
        "applied": pick("신청일", "청구일", "접수일"),
        "result": pick("심사결과", "진행상황", "결과", "진행", "상태", no=("일",)),
        "resultDate": pick("결과확정일", "결정일", "확정일", "승인일", "처리일"),
        "listType": pick("상장유형", "유형"),
        "market": pick("시장", "법인구분", "증권구분"),
        "sector": pick("업종"),
        "uw": pick("주선인", "주관"),
    }
    out = pd.DataFrame({k: (df[v] if v else None) for k, v in m.items()})
    out["kindCode"] = df.get("회사코드")
    out["raw"] = df.drop(columns=[c for c in ("회사코드",) if c in df.columns]).to_dict("records")
    return out


# ------------------------------------------------------------------ 특례상장 목록 (상장트랙 판별의 기준)
def special_listings(years, debug_dir=None, log=print):
    """KIND 기타상장통계 > 코스닥 '기술성장기업' 상세 목록(연도별)
    = 코스닥 상장규정상 기술평가특례(사업모델 포함) + 성장성특례로 상장한 회사. (유가증권시장엔 해당 구분 없음)
    → {회사명 또는 '#회사코드': 유형 문자열}"""
    out, sample, counts = {}, None, {}
    for y, mkt in [(y, "2") for y in years]:
        form = {"method": "searchMiscListTypeStatDetailSub", "forward": "miscListTypeStatDetail_sub",
                "currentPageSize": "500", "pageIndex": "1", "marketType": mkt,
                "listMonth": str(y), "listClssCd": "12"}
        html = post("listinvstg/miscListTypeStatDetail.do", form, as_data=True)
        df = list_table(html)
        if sample is None and len(df):
            sample = {"columns": list(df.columns), "rows": df.head(5).astype(str).values.tolist()}
        name_c = next((c for c in df.columns if "회사명" in c or "기업명" in c or "종목명" in c), None)
        if not name_c:
            continue
        counts[y] = int(df[name_c].astype(str).str.strip().replace("nan", "").astype(bool).sum())
        type_cs = [c for c in df.columns if any(k in c for k in ("유형", "구분", "특례", "트랙", "요건"))]
        for _, r in df.iterrows():
            nm = str(r[name_c]).strip()
            if nm and nm != "nan":
                v = " ".join(str(r[c]) for c in type_cs if str(r[c]) != "nan")
                out[nm] = v
                code = str(r.get("회사코드") or "").strip()
                if len(code) >= 5:
                    out["#" + code[:5]] = v                 # 회사코드(종목코드 앞 5자리)로도 찾을 수 있게
    if debug_dir and sample:
        Path(debug_dir).mkdir(parents=True, exist_ok=True)
        import json
        sample["countsByYear"] = counts
        (Path(debug_dir) / "kind_special_sample.json").write_text(json.dumps(sample, ensure_ascii=False, indent=1), "utf-8")
    # KIND 화면의 연도별 건수(예: 2022년 28 · 2023년 35 · 2024년 42 · 2025년 35)와 맞는지 로그로 확인
    log(f"[KIND 기술성장기업] 연도별 {counts} · 합계 {len([k for k in out if not k.startswith('#')])}개")
    return out


def growth_listings(log=print):
    """KIND 성장성특례(상장주선인 추천) 상장 목록 → 회사명 집합"""
    form = {"method": "listingForeignCompanyList", "forward": "growthReportList", "currentPageSize": "500",
            "pageIndex": "1", "kosdaqBbsTpCd": "23", "searchTextType": "1",
            "startDate": "2015-01-01", "endDate": date.today().isoformat()}
    h = {"User-Agent": UA, "Referer": f"{BASE}/"}
    r = S.post(f"{BASE}/corpgeneral/growthReport.do", data=form, headers=h, timeout=60)
    names = set()
    for enc in ("utf-8", "euc-kr"):
        try:
            html = r.content.decode(enc)
        except UnicodeDecodeError:
            continue
        df = list_table(html)
        name_c = next((c for c in df.columns if "회사명" in c or "기업명" in c or "종목명" in c), None)
        if name_c:
            names = {str(x).strip() for x in df[name_c] if str(x).strip() not in ("", "nan")}
            names |= {"#" + str(c)[:5] for c in df.get("회사코드", []) if c and len(str(c)) >= 5}
            break
    if not names:
        log("[KIND 성장성특례] 목록이 비어 있음(화면 형식 확인 필요)")
    log(f"[KIND 성장성특례] {len([n for n in names if not n.startswith('#')])}개")
    return names


def track_from_kind(text):
    t = str(text or "")
    if "성장성" in t:
        return "성장성 추천"
    if "사업모델" in t:
        return "기술특례(사업모델)"
    if "소부장" in t or "소재" in t:
        return "기술특례(소부장)"
    if "이익미실현" in t or "테슬라" in t:
        return "이익미실현(테슬라)"
    return "기술특례(기술평가)"
