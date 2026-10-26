"""일회성: 기술성장(기술특례·성장성) 상장사의 상장 직전 사업연도 실적(DART 재무 API)"""
import json, os, sys, time
from pathlib import Path
import requests
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import dart as dartlib
KEY = os.environ["DART_API_KEY"].strip()
dt = dartlib.Dart(KEY)
items = json.loads((ROOT / "data/ipos.json").read_text("utf-8"))["items"]
sel = [x for x in items if x.get("track") and ("특례" in x["track"] or "성장성" in x["track"]) and "2021" <= x["listed"][:4] <= "2026"]
ACC = {"매출액": "rev", "수익(매출액)": "rev", "영업수익": "rev", "영업이익": "op", "영업이익(손실)": "op", "당기순이익": "net", "당기순이익(손실)": "net"}
def num(s):
    s = str(s or "").replace(",", "").strip()
    try: return int(float(s))
    except ValueError: return None
def fin(corp, year, code="11011"):
    r = requests.get("https://opendart.fss.or.kr/api/fnlttSinglAcnt.json", params={"crtfc_key": KEY, "corp_code": corp, "bsns_year": str(year), "reprt_code": code}, timeout=60).json()
    time.sleep(0.15)
    if r.get("status") != "000": return None
    rows = r.get("list", [])
    out = {}
    for div in ("CFS", "OFS"):
        sub = [x for x in rows if x.get("fs_div") == div]
        if not sub: continue
        for x in sub:
            k = ACC.get(x.get("account_nm", "").replace(" ", ""))
            if k and k not in out:
                out[k] = {"cur": num(x.get("thstrm_amount")), "prev": num(x.get("frmtrm_amount"))}
        out["div"] = div
        break
    return out
res = []
for x in sel:
    y = int(x["listed"][:4])
    row = {"code": x["code"], "name": x["name"], "listed": x["listed"], "track": x["track"], "market": x.get("market")}
    try:
        corp = dt.corp_code(x["code"])
        row["corp"] = corp
        f = fin(corp, y) if corp else None                     # 상장 연도 사업보고서의 전기(=상장 직전 연도)
        if f:
            row["fy"] = y - 1
            row.update({k: f[k]["prev"] for k in ("rev", "op", "net") if k in f}); row["div"] = f.get("div")
            row["curYear"] = {k: f[k]["cur"] for k in ("rev", "op", "net") if k in f}
        else:
            row["note"] = f"{y} 사업보고서 없음"
    except Exception as e:
        row["error"] = str(e)[:200]
    res.append(row)
    print(row["name"], row.get("fy"), row.get("op"), row.get("note") or "", flush=True)
(ROOT / "tmpcheck/result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), "utf-8")
