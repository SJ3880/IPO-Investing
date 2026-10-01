"""
IPO 단계별 현황(파이프라인) 만들기 → data/pipeline.json
  심사중 → 심사승인(신고서 제출 전) → 공모 진행(신고서·수요예측·청약·상장대기) → 상장완료
  + 철회·미승인, 월별 추이, 분기별 공모주 성과, 다가오는 일정
"""
import re
from datetime import date, timedelta
from statistics import median


def norm(name):
    s = str(name or "")
    s = re.sub(r"\((유가|코스닥|코넥스|주|구\s*[^)]*)\)", "", s)
    s = s.replace("㈜", "").replace("주식회사", "")
    return re.sub(r"[\s\.\-·]", "", s).lower()


def d(s):
    m = re.search(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", str(s or ""))
    return date(int(m[1]), int(m[2]), int(m[3])) if m else None


def drange(text, ref=None):
    """'2026.09.23~2026.10.01' / '2026-09-23 ~ 10-01' → (시작, 끝)"""
    t = str(text or "")
    full = re.findall(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", t)
    if len(full) >= 2:
        a, b = full[0], full[1]
        return date(*map(int, a)), date(*map(int, b))
    if len(full) == 1:
        a = date(*map(int, full[0]))
        rest = t.split("~", 1)[1] if "~" in t else ""
        m = re.search(r"(\d{1,2})[./-](\d{1,2})", rest)
        if m:
            b = date(a.year, int(m[1]), int(m[2]))
            if b < a:
                b = date(a.year + 1, b.month, b.day)
            return a, b
        return a, a
    return None, None


def iso(x):
    return x.isoformat() if x else None


def num(s):
    v = re.sub(r"[^\d.]", "", str(s or ""))
    try:
        return float(v) if v else None
    except ValueError:
        return None


def no_ipo(it):
    lt = str(it.get("listType") or "")
    return not it.get("offer") and any(k in lt for k in ("이전", "합병"))


def add_months(x, m):
    import calendar
    y, mo = divmod(x.month - 1 + m, 12)
    yy, mm = x.year + y, mo + 1
    return date(yy, mm, min(x.day, calendar.monthrange(yy, mm)[1]))


# ------------------------------------------------------------------ 메인
def build(today, listed_items, pub_df, inv_df, demand38, enrich=None, log=print):
    """
    listed_items : update.py 가 만든 상장완료 종목 목록
    pub_df       : KIND 공모기업 진행현황 (없으면 None)
    inv_df       : KIND 상장예비심사 (없으면 None)
    demand38     : 38 수요예측 결과 행
    enrich(name, filed) → dict : DART에서 공모구조·유통비율·트랙 등 (선택)
    """
    listed_by = {}
    for it in listed_items:
        listed_by.setdefault(norm(it["name"]), []).append(it)

    def is_listed(name, after=None):
        for it in listed_by.get(norm(name), []):
            ld = date.fromisoformat(it["listed"])
            if after is None or ld >= after - timedelta(days=30):
                return it
        return None

    # ---------- 공모 진행
    offering, filed_names = [], {}
    if pub_df is not None and len(pub_df):
        for _, r in pub_df.iterrows():
            name = str(r.get("회사명") or "").strip()
            if not name or "스팩" in name or "기업인수목적" in name:
                continue
            filed = d(r.get("신고서제출일"))
            filed_names.setdefault(norm(name), []).append(filed)
            ds, de = drange(r.get("수요예측일정"))
            ss, se = drange(r.get("청약일정"))
            ld = d(r.get("상장예정일"))
            if is_listed(name, filed):
                continue
            if not filed or (today - filed).days > 200:
                continue
            if ld and ld < today - timedelta(days=3):
                continue                     # 상장예정일이 지났는데 상장 목록에 없음 → 철회·정정 등
            if not ld and se and (today - se).days > 14:
                continue                     # 청약 끝난 지 2주 넘었는데 상장일 미정 → 철회 추정
            if ds and (today - ds).days > 45:
                continue                     # 수요예측 시작 45일이 지났는데 상장 안 됨 → 철회 추정
            if ds and today < ds:
                stage = "신고서 제출"
            elif ds and de and ds <= today <= de:
                stage = "수요예측 중"
            elif ss and today < ss:
                stage = "청약 대기"
            elif ss and se and ss <= today <= se:
                stage = "청약 중"
            elif se and today > se:
                stage = "상장 대기"
            else:
                stage = "신고서 제출"
            offer = num(r.get("확정공모가"))
            amt = num(r.get("공모금액(백만원)"))
            dm = None
            for x in demand38:
                if norm(x["name"]) == norm(name) and filed and -10 <= (x["d"] - filed).days <= 120:
                    dm = x
                    break
            row = {
                "name": name, "filed": iso(filed), "demand": [iso(ds), iso(de)], "sub": [iso(ss), iso(se)],
                "pay": iso(d(r.get("납입일"))), "listDate": iso(ld), "stage": stage,
                "offer": int(offer) if offer else None,
                "amount": int(amt * 1e6) if amt else None,
                "uw": str(r.get("상장주선인") or "").strip() or None,
                "band": (dm or {}).get("band"), "instComp": (dm or {}).get("instComp"),
                "commit": (dm or {}).get("commit"),
            }
            if enrich:
                try:
                    row.update(enrich(name, filed) or {})
                except Exception as e:
                    log(f"  - {name} 공모 상세 실패: {e}")
            if row.get("market") == "코넥스":
                continue
            price = row["offer"] or (row["band"][1] if row.get("band") else None)
            if price and row.get("totalShares"):
                row["marcapAtOffer"] = int(price * row["totalShares"])
            offering.append(row)
    order = {"청약 중": 0, "수요예측 중": 1, "청약 대기": 2, "상장 대기": 3, "신고서 제출": 4}
    offering.sort(key=lambda x: (order.get(x["stage"], 9), x["listDate"] or x["sub"][0] or "9999"))

    # ---------- 예비심사
    review, approved, withdrawn, inv_rows = [], [], [], []
    if inv_df is not None and len(inv_df):
        for _, r in inv_df.iterrows():
            name = str(r.get("name") or "").strip()
            if not name or "스팩" in name or "기업인수목적" in name:
                continue
            ap = d(r.get("applied"))
            rd = d(r.get("resultDate"))
            res = str(r.get("result") or "").strip()
            mk = str(r.get("market") or "")
            market = "코스피" if ("유가" in mk or "코스피" in mk) else "코넥스" if "코넥스" in mk else "코스닥" if mk else None
            row = {"name": name, "applied": iso(ap), "resultDate": iso(rd), "result": res or None,
                   "market": market, "listType": r.get("listType"), "sector": r.get("sector"),
                   "uw": r.get("uw"), "kindCode": r.get("kindCode")}
            inv_rows.append(row)
            if market == "코넥스":
                continue
            if "승인" in res and "미승인" not in res:
                if is_listed(name, ap):
                    continue
                if any(f and ap and f >= ap for f in filed_names.get(norm(name), [])):
                    continue            # 이미 증권신고서 제출 → 공모 진행 탭
                if rd:
                    row["daysSince"] = (today - rd).days
                    exp = add_months(rd, 6)
                    row["expire"] = iso(exp)
                    if exp < today - timedelta(days=30):
                        continue            # 승인 효력 만료 후 한 달 지남 → 목록에서 뺌
                approved.append(row)
            elif "철회" in res or "미승인" in res or "부적격" in res:
                withdrawn.append(row)
            elif not rd or "심사" in res or "진행" in res or not res:
                if is_listed(name, ap):
                    continue
                row["days"] = (today - ap).days if ap else None
                review.append(row)
    # 심사 기간 중앙값(최근 2년 승인건) → 심사중 종목 예상 결과 시점
    durs = [(d(x["resultDate"]) - d(x["applied"])).days for x in inv_rows
            if x["result"] and "승인" in x["result"] and "미승인" not in x["result"]
            and x["resultDate"] and x["applied"] and d(x["applied"]) >= today - timedelta(days=730)]
    med_review = int(median(durs)) if durs else None
    for x in review:
        if med_review and x["applied"]:
            x["expected"] = iso(d(x["applied"]) + timedelta(days=med_review))
    review.sort(key=lambda x: x["applied"] or "", reverse=True)
    approved.sort(key=lambda x: x["resultDate"] or "", reverse=True)
    withdrawn.sort(key=lambda x: x["resultDate"] or "", reverse=True)

    # ---------- 월별 추이
    months = {}
    def bump(dt, key):
        if dt and dt >= date(2020, 1, 1):
            k = f"{dt.year}-{dt.month:02d}"
            months.setdefault(k, {"applied": 0, "approved": 0, "withdrawn": 0, "listed": 0})[key] += 1
    for x in inv_rows:
        if x["market"] == "코넥스":
            continue
        bump(d(x["applied"]), "applied")
        res = x["result"] or ""
        if "승인" in res and "미승인" not in res:
            bump(d(x["resultDate"]), "approved")
        elif "철회" in res or "미승인" in res or "부적격" in res:
            bump(d(x["resultDate"]), "withdrawn")
    for it in listed_items:
        if not no_ipo(it):                   # 공모 없이 들어온 이전상장·합병 등은 제외
            bump(date.fromisoformat(it["listed"]), "listed")
    cur = date(2020, 1, 1)
    monthly = []
    while cur <= today:
        k = f"{cur.year}-{cur.month:02d}"
        monthly.append({"m": k, **months.get(k, {"applied": 0, "approved": 0, "withdrawn": 0, "listed": 0})})
        cur = add_months(cur, 1)

    # ---------- 분기별 공모주 성과
    qs = {}
    for it in listed_items:
        if no_ipo(it):
            continue
        ld = date.fromisoformat(it["listed"])
        qs.setdefault(f"{ld.year} Q{(ld.month - 1) // 3 + 1}", []).append(it)
    quarterly = []
    for q in sorted(qs):
        g = qs[q]
        fr = [x["firstDayRet"] for x in g if x.get("firstDayRet") is not None]
        ic = [x["instComp"] for x in g if x.get("instComp")]
        bp = [x for x in g if x.get("bandPos")]
        top = [x for x in bp if "상단" in x["bandPos"] and "미달" not in x["bandPos"]]
        quarterly.append({
            "q": q, "n": len(g),
            "firstMed": round(median(fr), 1) if fr else None,
            "instCompMed": round(median(ic)) if ic else None,
            "topBand": round(len(top) / len(bp) * 100) if bp else None,
        })

    # ---------- 다가오는 일정 (오늘 ~ 21일)
    cal = []
    end = today + timedelta(days=21)
    for x in offering:
        for kind_, (a, b) in (("수요예측", x["demand"]), ("청약", x["sub"])):
            a, b = d(a), d(b)
            if a and b and b >= today and a <= end:
                cal.append({"date": iso(max(a, today)), "end": iso(b), "type": kind_, "name": x["name"]})
        ld = d(x["listDate"])
        if ld and today <= ld <= end:
            cal.append({"date": iso(ld), "end": iso(ld), "type": "상장", "name": x["name"]})
    cal.sort(key=lambda e: (e["date"], e["type"]))

    return {
        "review": review, "approved": approved, "offering": offering, "withdrawn": withdrawn,
        "monthly": monthly, "quarterly": quarterly, "calendar": cal,
        "medianReviewDays": med_review,
    }
