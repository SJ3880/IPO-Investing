"""
공모주 투자 Insight - 데이터 자동 수집 스크립트
GitHub Actions가 평일 장 마감 후 자동으로 실행합니다. (직접 건드릴 필요 없음)

 1) KIND(한국거래소): 최근 5년 코스닥 신규상장 목록 (화면엔 2년, 5년치는 확률 계산용)
 2) 38커뮤니케이션: 공모가 / 수요예측 결과(기관경쟁률·의무보유확약·주관사)
    - 못 찾은 공모가는 data/offer_prices.csv 에 적으면 그 값이 우선
 3) 네이버 금융: 일봉, 시가총액·PER 등 참고지표
 4) DART(OpenDART 키가 있을 때): 유통가능물량, 사업의 내용, 공모 구조, 최근 공시
 5) Claude API(키가 있을 때): 사업 내용 3~4문단 요약
 6) 과거 유사 사례 기반 '3개월 뒤 수익 확률'
"""
import csv
import io
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import requests.adapters
import urllib3

urllib3.disable_warnings()

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dart as dartlib          # noqa: E402
from model import SimilarCaseModel, features  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PRICES = DATA / "prices"
DETAIL = DATA / "detail"
KST = ZoneInfo("Asia/Seoul")
SHOW_YEARS = 2       # 화면에 보여줄 기간
MODEL_YEARS = 5      # 확률 계산에 쓰는 과거 기간
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
DART_KEY = os.environ.get("DART_API_KEY", "").strip()
CLAUDE_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MAX_AI_PER_RUN = 300
PARSE_VER = 2         # 증권신고서 해석 방식 버전. 올리면 이미 읽은 문서도 한 번 다시 읽음(AI 요약은 유지)
DART_BUDGET_MIN = 40   # 증권신고서 읽기·AI 요약에 쓸 최대 시간(분). 남은 종목은 다음 실행 때 이어서


def log(*a):
    print(*a, flush=True)


def norm(name: str) -> str:
    s = str(name)
    s = re.sub(r"\((유가|코스닥|코넥스|주|구\s*[^)]*)\)", "", s)
    s = s.replace("㈜", "").replace("주식회사", "")
    s = re.sub(r"[\s\.\-·]", "", s)
    return s.lower()


def is_spac(name: str) -> bool:
    return "스팩" in name or "기업인수목적" in name


def first_date(s):
    m = re.search(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", str(s))
    return date(int(m[1]), int(m[2]), int(m[3])) if m else None


def num(s):
    v = pd.to_numeric(re.sub(r"[^\d.\-]", "", str(s)) or "x", errors="coerce")
    return None if pd.isna(v) else float(v)


# ---------------------------------------------------------------- 1. KIND
def fetch_kosdaq_listings(since: date) -> pd.DataFrame:
    r = requests.get("https://kind.krx.co.kr/corpgeneral/corpList.do",
                     params={"method": "download", "searchType": "13", "marketType": "kosdaqMkt"},
                     headers=UA, timeout=60)
    r.raise_for_status()
    df = pd.read_html(io.StringIO(r.content.decode("euc-kr", errors="ignore")), header=0)[0]
    df = df.rename(columns={"회사명": "name", "종목코드": "code", "업종": "sector",
                            "주요제품": "product", "상장일": "listed"})
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["listed"] = pd.to_datetime(df["listed"], errors="coerce").dt.date
    df = df[df["listed"] >= since]
    df = df[~df["name"].astype(str).map(is_spac)]
    log(f"[KIND] 최근 {MODEL_YEARS}년 코스닥 신규상장(스팩 제외): {len(df)}개")
    return df[["code", "name", "sector", "product", "listed"]].sort_values("listed", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------- 2. 38커뮤니케이션
class LegacySSL(requests.adapters.HTTPAdapter):
    """38커뮤니케이션은 오래된 보안(SSL) 방식을 써서 기본 설정으로는 접속이 거부됨"""
    def init_poolmanager(self, *a, **kw):
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)


S38 = requests.Session()
S38.mount("https://www.38.co.kr", LegacySSL())


def get_38(path):
    last = None
    for base in ("https://www.38.co.kr", "http://www.38.co.kr"):
        try:
            r = S38.get(base + path, headers=UA, timeout=30)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
    raise last


def read_38(o, since, date_key, max_pages=200):
    """38 표를 페이지 넘기며 읽어 DataFrame 목록으로"""
    frames = []
    for page in range(1, max_pages + 1):
        try:
            r = get_38(f"/html/fund/index.htm?o={o}&page={page}")
            tables = pd.read_html(io.StringIO(r.content.decode("euc-kr", errors="ignore")), match=date_key)
        except Exception as e:
            log(f"[38:{o}] {page}페이지 중단: {e}")
            break
        cand = []
        for t in tables:
            if isinstance(t.columns, pd.MultiIndex):
                t.columns = [" ".join(map(str, c)) for c in t.columns]
            t.columns = [str(c) for c in t.columns]
            if any("기업명" in c for c in t.columns) and any(date_key in c for c in t.columns):
                cand.append(t)
        if not cand:
            break
        t = min(cand, key=len)
        frames.append(t)
        dcol = next(c for c in t.columns if date_key in c)
        dates = [d for d in (first_date(x) for x in t[dcol]) if d]
        if not dates or min(dates) < since:
            break
        time.sleep(0.4)
    return frames


def fetch_38(since):
    offers, demand = [], []
    for t in read_38("nw", since, "신규상장일"):
        c_name = next(c for c in t.columns if "기업명" in c)
        c_date = next(c for c in t.columns if "신규상장일" in c)
        c_off = next((c for c in t.columns if "공모가" in c and "대비" not in c and "/" not in c), None)
        for _, r in t.iterrows():
            d = first_date(r[c_date])
            if d and c_off:
                o = num(r[c_off])
                offers.append({"name": str(r[c_name]), "d": d, "offer": int(o) if o else None})
    for t in read_38("r1", since - timedelta(days=60), "예측일"):
        col = lambda k: next((c for c in t.columns if k in c), None)
        c_name, c_date = col("기업명"), col("예측일")
        c_inst, c_commit, c_uw, c_band = col("경쟁률"), col("확약"), col("주간사"), col("희망")
        for _, r in t.iterrows():
            d = first_date(r[c_date])
            if not d:
                continue
            demand.append({
                "name": str(r[c_name]), "d": d,
                "instComp": num(r[c_inst]) if c_inst else None,
                "commit": num(r[c_commit]) if c_commit else None,
                "uw38": str(r[c_uw]).strip() if c_uw and str(r[c_uw]) != "nan" else None,
                "band": band(r[c_band]) if c_band else None,
            })
    log(f"[38] 공모가 {len(offers)}행, 수요예측 {len(demand)}행")
    return offers, demand


def band(text):
    """'13,000~15,000' → [13000, 15000]"""
    nums = [int(x.replace(",", "")) for x in re.findall(r"\d[\d,]*", str(text)) if x.replace(",", "").isdigit()]
    nums = [n for n in nums if n >= 100]
    return [min(nums), max(nums)] if len(nums) >= 2 else None


def band_pos(offer, b):
    if not offer or not b:
        return None
    lo, hi = b
    if offer > hi:
        return f"밴드 상단 초과(+{(offer / hi - 1) * 100:.0f}%)"
    if offer == hi:
        return "밴드 상단"
    if offer < lo:
        return f"밴드 하단 미달({(offer / lo - 1) * 100:.0f}%)"
    if offer == lo:
        return "밴드 하단"
    return "밴드 안"


def load_manual_tracks():
    """data/listing_track.csv (종목코드,회사명,상장트랙) - 자동 판별이 틀렸을 때 직접 고치는 곳"""
    p = DATA / "listing_track.csv"
    out = {}
    if p.exists():
        with open(p, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = (row.get("종목코드") or "").strip().zfill(6)
                t = (row.get("상장트랙") or "").strip()
                if code.strip("0") and t:
                    out[code] = t
    return out


def parse_won(text):
    """'1조 2,345억' → 원"""
    if not text:
        return None
    t = str(text).replace(",", "")
    jo = re.search(r"([\d.]+)\s*조", t)
    eok = re.search(r"([\d.]+)\s*억", t)
    v = (float(jo.group(1)) * 1e12 if jo else 0) + (float(eok.group(1)) * 1e8 if eok else 0)
    return v or None


def match_row(name, listed, rows, lo, hi):
    """이름이 같고 날짜가 listed+lo ~ listed+hi 일 사이인 행"""
    n = norm(name)
    for exact in (True, False):
        for r in rows:
            rn = norm(r["name"])
            ok = rn == n if exact else (n in rn or rn in n)
            if ok and lo <= (r["d"] - listed).days <= hi:
                return r
    return None


def load_manual_offers():
    p = DATA / "offer_prices.csv"
    out = {}
    if p.exists():
        with open(p, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = (row.get("종목코드") or "").strip().zfill(6)
                val = (row.get("공모가") or "").replace(",", "").strip()
                if code.strip("0") and val.isdigit():
                    out[code] = int(val)
    return out


# ---------------------------------------------------------------- 3. 네이버
def fetch_daily(code, count=1400):
    r = requests.get("https://fchart.stock.naver.com/sise.nhn",
                     params={"symbol": code, "timeframe": "day", "count": count, "requestType": "0"},
                     headers=UA, timeout=30)
    r.raise_for_status()
    out = []
    for item in re.findall(r'data="([^"]+)"', r.text):
        d, o, h, l, c, v = item.split("|")
        if int(c) > 0:
            out.append([f"{d[:4]}-{d[4:6]}-{d[6:]}", int(o) or int(c), int(h) or int(c), int(l) or int(c), int(c), int(v)])
    return out


def load_or_fetch(code, listed):
    """저장해 둔 일봉이 있으면 최근 15거래일만 받아 이어 붙인다(빠름).
    겹치는 날 가격이 다르면(액면분할·무상증자 등으로 과거 가격이 수정된 경우) 전체를 새로 받는다."""
    p = PRICES / f"{code}.json"
    since = listed.isoformat()
    if p.exists():
        try:
            old = json.loads(p.read_text("utf-8"))
        except Exception:
            old = []
        if old:
            recent = fetch_daily(code, count=15)
            if recent:
                have = {b[0]: b for b in old}
                overlap = [b for b in recent if b[0] in have]
                same = all(have[b[0]][4] == b[4] for b in overlap)
                if overlap and same:
                    merged = {b[0]: b for b in old}
                    merged.update({b[0]: b for b in recent})
                    return [merged[d] for d in sorted(merged) if d >= since], "recent"
    time.sleep(0.1)
    return [b for b in fetch_daily(code) if b[0] >= since], "full"


def fetch_market(code):
    """시가총액·PER·PBR·외국인소진율 등 (네이버 모바일, 실패해도 무시)"""
    try:
        j = requests.get(f"https://m.stock.naver.com/api/stock/{code}/integration", headers=UA, timeout=20).json()
        want = ("시총", "PER", "PBR", "EPS", "BPS", "외인소진율", "52주 최고", "52주 최저", "배당수익률")
        return {x["key"]: x["value"] for x in j.get("totalInfos", []) if x.get("key") in want}
    except Exception:
        return {}


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round((a / b - 1) * 100, 2)


# ---------------------------------------------------------------- 4. DART 상세
def dart_detail(dt, code, name, listed, det, ai_budget, heavy=True):
    """det(기존 상세)를 갱신. 무거운 작업(문서 파싱·요약)은 한 번만."""
    corp = dt.corp_code(code)
    if not corp:
        det["dartStatus"] = "고유번호 없음"
        return ai_budget
    tried = det.get("dartTried")
    retry = not det.get("dartDone") and (not tried or (date.today() - date.fromisoformat(tried)).days >= 7)
    reparse = det.get("dartDone") and det.get("rcpNo") and det.get("parseVer", 1) < PARSE_VER
    need_doc = heavy and (retry or reparse)
    if need_doc:
        det["dartTried"] = date.today().isoformat()
        try:
            f = dt.prospectus(corp, listed)
            if f:
                det["rcpNo"] = f["rcept_no"]
                det["rcpTitle"] = re.sub(r"\s+", " ", f["report_nm"]).strip()
                info = dartlib.analyze_document(dt.document_text(f["rcept_no"]))
                det["lockup"] = info["lockup"]
                det["lockupTables"] = info["lockupTables"]
                det["track"] = info["track"]
                det["trackEvidence"] = info["trackEvidence"]
                det["parseVer"] = PARSE_VER
                det.pop("listingType", None)
                det["_bizText"] = info["bizText"]
                det.update(dt.estk(corp, listed))
                det["dartDone"] = True
                det["dartStatus"] = "ok"
            else:
                det["dartStatus"] = "증권신고서 없음(스팩합병·이전상장 등)"
                det["track"] = "공모 없음(스팩합병·이전상장)"
        except Exception as e:
            det["dartStatus"] = f"오류: {e}"[:200]
            log(f"  - {name} DART 실패: {e}")
    # 사업 요약: AI 키가 있으면 요약, 없으면 원문 앞부분 발췌
    biz = det.get("_bizText") or ""
    if heavy and biz and not det.get("summaryAI") and CLAUDE_KEY and ai_budget > 0:
        try:
            det["summary"] = dartlib.summarize(name, biz, CLAUDE_KEY)
            det["summaryAI"] = True
            ai_budget -= 1
        except Exception as e:
            log(f"  - {name} 요약 실패: {e}")
    if biz and not det.get("summary"):
        det["summary"] = excerpt(biz)
        det["summaryAI"] = False
    det["disclosures"], det["events"] = dt.recent_disclosures(corp)
    fin_at = det.get("finAt")
    if not fin_at or (date.today() - date.fromisoformat(fin_at)).days >= 14:
        try:
            det["fin"] = dt.financials(corp)
            det["finAt"] = date.today().isoformat()
        except Exception as e:
            log(f"  - {name} 재무 실패: {e}")
    return ai_budget


def excerpt(text, n=1200):
    t = re.sub(r"^.*?(사업의 개요|업계의 현황|회사의 현황)", r"\1", text, count=1)
    return t[:n] + ("…" if len(t) > n else "")


# ---------------------------------------------------------------- main
def main():
    for p in (DATA, PRICES, DETAIL):
        p.mkdir(exist_ok=True)
    today = datetime.now(KST).date()
    show_since = today - timedelta(days=365 * SHOW_YEARS)
    model_since = today - timedelta(days=365 * MODEL_YEARS)

    prev_path = DATA / "ipos.json"
    prev = {}
    if prev_path.exists():
        try:
            prev = {x["code"]: x for x in json.loads(prev_path.read_text("utf-8"))["items"]}
        except Exception:
            pass

    try:
        listings = fetch_kosdaq_listings(model_since)
    except Exception as e:
        log(f"[오류] KIND 목록을 받지 못했습니다: {e}")
        sys.exit(0 if prev else 1)

    try:
        offers38, demand38 = fetch_38(model_since)
    except Exception as e:
        log(f"[38] 실패: {e}")
        offers38, demand38 = [], []
    manual = load_manual_offers()

    universe, items, missing = [], [], []
    n_fetch = {"full": 0, "recent": 0}
    for i, row in listings.iterrows():
        code, name, listed = row["code"], str(row["name"]), row["listed"]
        show = listed >= show_since
        try:
            bars, how = load_or_fetch(code, listed)
            n_fetch[how] += 1
        except Exception as e:
            log(f"  - {name}({code}) 시세 실패: {e}")
            if code in prev and show:
                items.append(prev[code])
            continue
        if not bars:
            continue
        m38 = match_row(name, listed, offers38, -7, 7)
        offer = manual.get(code) or (m38 or {}).get("offer") or (prev.get(code) or {}).get("offer")
        dm = match_row(name, listed, demand38, -60, 0) or {}
        sector = None if pd.isna(row["sector"]) else str(row["sector"])
        universe.append({"code": code, "listed": listed.isoformat(), "offer": offer, "bars": bars,
                         "uw": dm.get("uw38"), "sector": sector,
                         "firstRet": pct(bars[0][4], offer), "nowRet": pct(bars[-1][4], offer)})
        (PRICES / f"{code}.json").write_text(json.dumps(bars, separators=(",", ":")), "utf-8")
        if not show:
            continue

        if not offer:
            missing.append((code, name))
        closes = [b[4] for b in bars]
        val20 = [b[4] * b[5] for b in bars[-20:]]
        price, peak = closes[-1], max(b[2] for b in bars)
        ma20 = round(sum(closes[-20:]) / min(20, len(closes)))
        items.append({
            "code": code, "name": name,
            "sector": sector,
            "product": None if pd.isna(row["product"]) else str(row["product"]),
            "listed": listed.isoformat(), "days": (today - listed).days, "tradingDays": len(bars),
            "offer": offer, "open": bars[0][1], "firstClose": bars[0][4], "price": price, "asOf": bars[-1][0],
            "retVsOpen": pct(price, bars[0][1]), "retVsOffer": pct(price, offer),
            "openVsOffer": pct(bars[0][1], offer),
            "peak": peak, "fromPeak": pct(price, peak),
            "ret5d": pct(price, closes[-6]) if len(closes) > 5 else None,
            "ret20d": pct(price, closes[-21]) if len(closes) > 20 else None,
            "ma20": ma20, "aboveMa20": price >= ma20,
            "instComp": dm.get("instComp"), "commit": dm.get("commit"), "uw38": dm.get("uw38"),
            "band": dm.get("band"), "bandPos": band_pos(offer, dm.get("band")),
            "firstDayRet": pct(bars[0][4], offer),
            "tradeValue20": round(sum(val20) / len(val20)) if val20 else None,
        })
        if (i + 1) % 25 == 0:
            log(f"  … {i + 1}/{len(listings)}")

    log(f"[시세] 최근분만 갱신 {n_fetch['recent']}개, 전체 새로 받음 {n_fetch['full']}개")

    # ---- 확률 모델
    log(f"[모델] 표본 종목 {len(universe)}개로 학습")
    mdl = SimilarCaseModel(universe)
    by_code = {u["code"]: u for u in universe}
    for it in items:
        u = by_code.get(it["code"])
        if u and len(u["bars"]) > 10:
            t = len(u["bars"]) - 1
            it["prob"] = mdl.predict(features(u["bars"], t, u["offer"]), exclude_code=it["code"])
        else:
            it["prob"] = None
    try:
        calib = mdl.backtest(universe, (today - timedelta(days=365 * 2)).isoformat())
    except Exception as e:
        log(f"[모델] 검증 실패: {e}")
        calib = []
    model_meta = {"samples": int(len(mdl.y)), "stocks": len(universe),
                  "base": round(mdl.base * 100, 1) if mdl.base is not None else None,
                  "horizonDays": 60, "calibration": calib}

    # ---- 비교 통계: 같은 업종 / 같은 주관사의 과거 공모주 성과
    add_peer_stats(items, universe, today)

    # ---- 1차 저장 (아래 DART 단계가 오래 걸려도 목록·차트는 먼저 확보)
    save(items, model_meta, missing, dart_on=bool(DART_KEY))

    # ---- 상세(DART·시장지표)
    dt = dartlib.Dart(DART_KEY, log) if DART_KEY else None
    t0 = time.time()
    left = 0
    if not dt:
        log("[DART] DART_API_KEY 가 없어 유통물량·사업요약·공시는 건너뜁니다.")
    ai_budget = MAX_AI_PER_RUN
    manual_tracks = load_manual_tracks()
    for it in items:
        p = DETAIL / f"{it['code']}.json"
        det = json.loads(p.read_text("utf-8")) if p.exists() else {}
        det["market"] = fetch_market(it["code"]) or det.get("market", {})
        if dt:
            try:
                heavy = (time.time() - t0) < DART_BUDGET_MIN * 60
                if not heavy and (not det.get("dartDone") or det.get("parseVer", 1) < PARSE_VER):
                    left += 1
                ai_budget = dart_detail(dt, it["code"], it["name"], date.fromisoformat(it["listed"]), det, ai_budget, heavy)
            except Exception as e:
                log(f"  - {it['name']} DART 전체 실패: {e}")
        # 목록 화면용 요약 필드
        it["track"] = manual_tracks.get(it["code"]) or det.get("track")
        it["trackManual"] = it["code"] in manual_tracks
        it["marcap"] = det["market"].get("시총")
        mc = parse_won(it["marcap"])
        if mc and it.get("price"):
            shares = mc / it["price"]
            it["marcapNum"] = round(mc)
            it["marcapAtOffer"] = round(shares * it["offer"]) if it.get("offer") else None
        fin = det.get("fin") or {}
        it["opLoss"] = fin.get("op") is not None and fin["op"] < 0
        it["mezz"] = any(e.get("tag") == "메자닌(CB·BW·EB)" for e in det.get("events") or [])
        it["hasLockup"] = bool(det.get("lockup") or det.get("lockupTables"))
        p.write_text(json.dumps(det, ensure_ascii=False, separators=(",", ":")), "utf-8")
        time.sleep(0.1)

    if left:
        log(f"[DART] 시간 제한으로 {left}개 종목은 다음 실행 때 이어서 처리합니다.")
    save(items, model_meta, missing, dart_on=bool(dt))
    log(f"[완료] {len(items)}개 저장, 공모가 미확인 {len(missing)}개")


def split_uw(text):
    return [u.strip() for u in re.split(r"[,/·]", text or "") if u.strip()]


def summarize_group(rows):
    first = [r["firstRet"] for r in rows if r["firstRet"] is not None]
    now = [r["nowRet"] for r in rows if r["nowRet"] is not None]
    if len(now) < 3:
        return None
    med = lambda v: sorted(v)[len(v) // 2]
    return {"n": len(now), "firstMed": round(med(first), 1) if first else None, "nowMed": round(med(now), 1),
            "aboveOffer": round(sum(1 for v in now if v > 0) / len(now) * 100)}


def add_peer_stats(items, universe, today):
    for it in items:
        others = [u for u in universe if u["code"] != it["code"] and u["listed"] < it["listed"]]
        sec = [u for u in others if it.get("sector") and u.get("sector") == it["sector"]]
        it["sectorStats"] = summarize_group(sec)
        stats = []
        for name in split_uw(it.get("uw38")):
            g = [u for u in others if name in split_uw(u.get("uw"))]
            st = summarize_group(g)
            if st:
                st["name"] = name
                stats.append(st)
        it["uwStats"] = stats


def save(items, model_meta, missing, dart_on):
    items.sort(key=lambda x: x["listed"], reverse=True)
    out = {"updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "count": len(items),
           "dart": dart_on, "ai": bool(CLAUDE_KEY), "model": model_meta, "items": items}
    (DATA / "ipos.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
    with open(DATA / "missing_offer.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["종목코드", "회사명"])
        w.writerows(missing)


if __name__ == "__main__":
    main()
