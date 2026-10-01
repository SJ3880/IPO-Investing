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


def stage_of(res):
    """KIND 예비심사 '심사결과' → 단계
    청구서 접수 = 심사중 / 심사 승인 = 예비심사 승인(상장 전) / 상장 승인 = 상장 완료
    심사 철회·심사 미승인·공모 철회·상장 철회·승인효력기간 만료 = 철회·미승인"""
    r = (res or "").replace(" ", "")
    if r in ("청구서접수", "") or "접수" in r or "심사중" in r:
        return "review"
    if r == "심사승인":
        return "approved"
    if r == "상장승인":
        return "listed"
    if any(k in r for k in ("철회", "미승인", "만료", "부적격")):
        return "withdrawn"
    return "other"


def no_ipo(it):
    lt = str(it.get("listType") or "")
    return bool(it.get("spac")) or (not it.get("offer") and any(k in lt for k in ("이전", "합병")))


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
    offering, filed_names, skipped = [], {}, []
    if pub_df is not None and len(pub_df):
        for _, r in pub_df.iterrows():
            name = str(r.get("회사명") or "").strip()
            if not name or "스팩" in name or "기업인수목적" in name:
                continue
            filed = d(r.get("신고서제출일"))
            ds, de = drange(r.get("수요예측일정"))
            ss, se = drange(r.get("청약일정"))
            ld = d(r.get("상장예정일"))
            filed_names.setdefault(norm(name), []).append(filed or ds or ss)
            near = [x for x in (ds, ss, ld) if x]
            recent = (filed and (today - filed).days <= 200) or \
                     (near and max(near) >= today - timedelta(days=45))
            why = ("이미 상장" if is_listed(name, filed) else
                   "신고서 200일 경과" if not recent else
                   "상장예정일 지남" if ld and ld < today - timedelta(days=3) else
                   "청약 후 2주·상장일 미정" if not ld and se and (today - se).days > 14 else
                   "수요예측 후 45일 경과" if ds and (today - ds).days > 45 else None)
            if why:
                if recent and why != "이미 상장":
                    skipped.append({"name": name, "filed": iso(filed), "why": why})
                continue
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
            elif de and today > de:                  # 수요예측은 끝났는데 청약 일정 정보가 없음
                stage = "상장 대기" if ld else "청약 대기"
            else:
                stage = "신고서 제출"
            offer = num(r.get("확정공모가"))
            amt = num(r.get("공모금액(백만원)"))
            dm = None
            base = filed or ds or ss
            for x in demand38:
                if norm(x["name"]) == norm(name) and base and -20 <= (x["d"] - base).days <= 120:
                    dm = x
                    break
            band38 = None
            nums = [int(v.replace(",", "")) for v in re.findall(r"\d[\d,]*", str(r.get("희망공모가") or "")) if v.replace(",", "").isdigit()]
            nums = [v for v in nums if v >= 100]
            if len(nums) >= 2:
                band38 = [min(nums), max(nums)]
            sc = str(r.get("청약경쟁률") or "").split(":")[0]
            sub_comp = num(sc) if re.search(r"\d", sc) else None
            row = {
                "name": name, "filed": iso(filed), "demand": [iso(ds), iso(de)], "sub": [iso(ss), iso(se)],
                "pay": iso(d(r.get("납입일"))), "listDate": iso(ld), "stage": stage,
                "offer": int(offer) if offer else None,
                "amount": int(amt * 1e6) if amt else None,
                "uw": str(r.get("상장주선인") or "").strip() or None,
                "band": band38 or (dm or {}).get("band"), "instComp": (dm or {}).get("instComp"),
                "commit": (dm or {}).get("commit"), "subComp": sub_comp,
            }
            if enrich:
                try:
                    base_d = filed or ((ds or ss) - timedelta(days=60) if (ds or ss) else None)
                    row.update(enrich(name, base_d) or {})   # 최근 증권신고서가 있는 같은 이름 회사만 연결됨
                except Exception as e:
                    log(f"  - {name} 공모 상세 실패: {e}")
            if row.get("market") == "코넥스":
                skipped.append({"name": name, "filed": iso(filed), "why": "코넥스로 판별"})
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
            lt = str(r.get("listType") or "")
            if not name or "스팩" in name or "기업인수목적" in name or "재상장" in lt:
                continue                    # 스팩 자체 상장·재상장은 제외(스팩합병 대상회사는 표시)
            ap = d(r.get("applied"))
            rd = d(r.get("resultDate"))
            res = str(r.get("result") or "").strip()
            mk = str(r.get("market") or "")
            market = "코스피" if ("유가" in mk or "코스피" in mk) else "코넥스" if "코넥스" in mk else "코스닥" if mk else None
            row = {"name": name, "applied": iso(ap), "resultDate": iso(rd), "result": res or None,
                   "market": market, "listType": r.get("listType"), "sector": r.get("sector"),
                   "uw": r.get("uw"), "kindCode": r.get("kindCode"), "stage": stage_of(res),
                   "spac": "스팩" in lt}
            inv_rows.append(row)
            if market == "코넥스":
                continue
            st = row["stage"]
            if st == "listed":
                continue                    # 상장 승인 = 상장까지 끝남 → 상장완료 탭
            if st == "approved":
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
            elif st == "withdrawn":
                withdrawn.append(row)
                if res.replace(" ", "") in ("공모철회", "상장철회", "승인효력기간만료"):
                    # 예비심사는 통과했지만 공모·상장 단계에서 철회했거나 승인 효력이 끝난 회사 → 심사승인 탭에도 표시
                    approved.append({**row, "after": True})
            elif st == "review":
                if is_listed(name, ap):
                    continue
                row["days"] = (today - ap).days if ap else None
                review.append(row)
    # 심사 기간 중앙값(최근 2년 승인건) → 심사중 종목 예상 결과 시점
    durs = [(d(x["resultDate"]) - d(x["applied"])).days for x in inv_rows
            if x["result"] == "심사 승인" and x["resultDate"] and x["applied"] and not x["spac"]
            and d(x["applied"]) >= today - timedelta(days=730)]
    med_review = int(median(durs)) if durs else None
    for x in review:
        if med_review and x["applied"]:
            x["expected"] = iso(d(x["applied"]) + timedelta(days=med_review))
    review.sort(key=lambda x: x["applied"] or "", reverse=True)
    approved.sort(key=lambda x: x["resultDate"] or "", reverse=True)
    approved.sort(key=lambda x: bool(x.get("after")))      # 대기 중인 회사가 위, 승인 후 철회는 아래
    withdrawn.sort(key=lambda x: x["resultDate"] or "", reverse=True)

    # ---------- 월별 추이
    months = {}
    def bump(dt, key):
        if dt and dt >= date(2020, 1, 1):
            k = f"{dt.year}-{dt.month:02d}"
            months.setdefault(k, {"applied": 0, "approved": 0, "withdrawn": 0, "listed": 0})[key] += 1
    for x in inv_rows:
        if x["market"] == "코넥스" or x["spac"]:      # 월별 추이는 직상장(공모) 기준
            continue
        bump(d(x["applied"]), "applied")
        res = x["result"] or ""
        if res in ("심사 승인", "상장 승인"):
            bump(d(x["resultDate"]), "approved")
        elif res in ("심사 철회", "심사 미승인"):
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
        "medianReviewDays": med_review, "offeringSkipped": skipped[:60],
    }
