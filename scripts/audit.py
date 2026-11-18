"""
기존 데이터 원문 대조(매일 새벽, 하루 100개씩 돌아가며)

  · data/ipos.json 의 상장 회사를 오래된 순으로 100개씩 골라
  · DART 증권신고서·주요정보를 '지금 코드'로 처음부터 다시 읽고
  · 저장된 상세(data/detail/<code>.json)와 핵심 숫자를 비교해
  · 다르면 새로 읽은 값으로 바꾸고(낮 업데이트가 이 값을 그대로 씀) data/debug/audit_log.json 에 기록
  · 어디까지 봤는지는 data/debug/audit_state.json (다 돌면 처음부터 다시)
"""
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import update as U          # noqa: E402  (같은 파서·같은 규칙을 그대로 씀)
import dart as dartlib      # noqa: E402

PER_RUN = 100
TIME_LIMIT_MIN = 80
STATE = U.DATA / "debug" / "audit_state.json"
LOGF = U.DATA / "debug" / "audit_log.json"

# 신고서·주요정보에서 오는 값(이것만 새로 읽은 값으로 바꾼다. 시세·공시·요약 등은 그대로 둠)
DOC_KEYS = ("rcpNo", "rcpTitle", "lockup", "lockupTables", "track", "trackEvidence", "techGrades", "floatAtListing",
            "discountRate", "valuation", "fundDoc", "totalShares", "uwFeeRateDoc", "uwFeeAmtDoc", "perfFeeRate",
            "shares", "amount", "uwFee", "uwFeeRate", "oldShareRatio", "underwriters", "uwMethod", "putback", "uwDetail")
CMP = {   # 비교해서 기록할 값: 이름 → 꺼내는 방법
    "신고서 번호": lambda d: d.get("rcpNo"),
    "상장예정주식수": lambda d: d.get("totalShares"),
    "공모주식수": lambda d: d.get("shares"),
    "공모금액": lambda d: d.get("amount"),
    "상장일 유통가능(%)": lambda d: d.get("floatAtListing"),
    "구주매출(%)": lambda d: d.get("oldShareRatio"),
    "인수수수료": lambda d: d.get("uwFee"),
    "인수수수료율(신고서)": lambda d: d.get("uwFeeRateDoc"),
    "성과수수료": lambda d: d.get("perfFeeRate"),
    "평가방법": lambda d: (d.get("valuation") or {}).get("method"),
    "적용 배수": lambda d: (d.get("valuation") or {}).get("appliedMult"),
    "주당 평가가액": lambda d: (d.get("valuation") or {}).get("fairValue"),
    "할인율 범위": lambda d: (d.get("valuation") or {}).get("discountRange"),
    "비교회사": lambda d: [p.get("name") for p in ((d.get("valuation") or {}).get("peers") or [])],
    "기술평가 등급": lambda d: ((d.get("techGrades") or {}).get("grades") if isinstance(d.get("techGrades"), dict) else None),
    "상장트랙": lambda d: d.get("track"),
}


def same(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= max(1e-6, abs(b) * 0.005)
    return a == b


def main():
    if not U.DART_KEY:
        print("DART_API_KEY 가 없어 원문 대조를 건너뜁니다.")
        return
    dt = dartlib.Dart(U.DART_KEY, U.log)
    items = json.loads((U.DATA / "ipos.json").read_text("utf-8"))["items"]
    items = sorted((x for x in items if not x.get("spac") and not U.is_reit(x.get("name"))),
                   key=lambda x: (x["listed"], x["code"]))
    try:
        st = json.loads(STATE.read_text("utf-8"))
    except Exception:
        st = {"cursor": 0, "rounds": 0}
    if st["cursor"] >= len(items):
        st["cursor"], st["rounds"] = 0, st.get("rounds", 0) + 1
    batch = items[st["cursor"]: st["cursor"] + PER_RUN]
    try:
        old_log = json.loads(LOGF.read_text("utf-8"))
    except Exception:
        old_log = []
    runlog = {"at": datetime.now(U.KST).strftime("%Y-%m-%d %H:%M"), "from": st["cursor"], "checked": [], "changed": [], "errors": []}
    t0 = time.time()
    done = 0
    for it in batch:
        if (time.time() - t0) / 60 > TIME_LIMIT_MIN:
            U.log("[원문 대조] 시간 제한 → 나머지는 다음에")
            break
        code, name = it["code"], it["name"]
        p = U.DETAIL / f"{code}.json"
        try:
            det = json.loads(p.read_text("utf-8")) if p.exists() else {}
        except Exception:
            det = {}
        fresh = {}
        try:
            U.dart_detail(dt, code, name, date.fromisoformat(it["listed"]), fresh, 0, heavy=True, light=False,
                          tech=U.has_tech_eval(it.get("track")))
        except Exception as e:
            runlog["errors"].append({"name": name, "err": str(e)[:200]})
            done += 1
            continue
        done += 1
        runlog["checked"].append(name)
        if fresh.get("dartStatus") != "ok":
            if fresh.get("dartStatus"):
                runlog["errors"].append({"name": name, "err": fresh.get("dartStatus")})
            continue
        od = dartlib.doc_old_ratio(fresh.get("fundDoc"), it.get("offerAmount"))   # 낮 업데이트와 같은 기준(원문 조달 내역 우선)
        if od is not None:
            fresh["oldShareRatio"] = od
        diffs = []
        for label, get in CMP.items():
            a, b = get(det), get(fresh)
            if b in (None, [], "") and a not in (None, [], ""):
                continue                 # 이번에 못 읽은 값으로 기존 값을 지우지는 않음
            if not same(a, b):
                diffs.append({"what": label, "old": a, "new": b})
        if diffs:
            for k in DOC_KEYS:
                if fresh.get(k) not in (None, [], ""):
                    det[k] = fresh[k]
            for k in ("parseVer", "valVer", "techVer", "estkVer", "dartDone", "dartStatus"):
                if k in fresh:
                    det[k] = fresh[k]
            runlog["changed"].append({"name": name, "code": code, "listed": it["listed"], "diffs": diffs})
        det["auditAt"] = date.today().isoformat()
        p.write_text(json.dumps(det, ensure_ascii=False, separators=(",", ":")), "utf-8")
        time.sleep(0.2)
    st["cursor"] += done
    st["total"] = len(items)
    st["last"] = runlog["at"]
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")
    LOGF.write_text(json.dumps(([runlog] + old_log)[:30], ensure_ascii=False, indent=1, default=str), "utf-8")
    U.log(f"[원문 대조] {done}개 확인({st['cursor']}/{len(items)}) · 바뀐 회사 {len(runlog['changed'])}개 · 오류 {len(runlog['errors'])}개")


if __name__ == "__main__":
    main()
