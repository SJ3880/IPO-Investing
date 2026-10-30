"""일회성: 코스닥 공모 상장사의 증권신고서 주요정보 '매출인에 관한 사항'(DART estkRs)"""
import json, os, sys, time
from datetime import date, timedelta
from pathlib import Path
import requests
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import dart as dartlib
KEY = os.environ["DART_API_KEY"].strip()
dt = dartlib.Dart(KEY)
items = json.loads((ROOT / "data/ipos.json").read_text("utf-8"))["items"]
sel = [x for x in items if x.get("market") == "코스닥" and not x.get("spac") and x.get("offer")
       and not any(k in str(x.get("listType")) for k in ("이전", "합병"))]
res = []
for x in sel:
    row = {"code": x["code"], "name": x["name"], "listed": x["listed"]}
    try:
        corp = dt.corp_code(x["code"])
        listed = date.fromisoformat(x["listed"])
        j = requests.get("https://opendart.fss.or.kr/api/estkRs.json", params={"crtfc_key": KEY, "corp_code": corp,
                         "bgn_de": (listed - timedelta(days=365)).strftime("%Y%m%d"), "end_de": listed.strftime("%Y%m%d")}, timeout=60).json()
        time.sleep(0.15)
        g = {y.get("title"): y.get("list") for y in j.get("group", [])} if j.get("status") == "000" else {}
        row["status"] = j.get("status")
        for t in ("매출인에관한사항", "증권의종류"):
            lst = g.get(t) or []
            if lst:
                last = max(y.get("rcept_no", "") for y in lst)
                row[t] = [{k: v for k, v in y.items() if k not in ("corp_cls", "corp_code", "corp_name")} for y in lst if y.get("rcept_no") == last]
    except Exception as e:
        row["error"] = str(e)[:200]
    res.append(row)
    print(row["name"], len(row.get("매출인에관한사항") or []), flush=True)
(ROOT / "tmpcheck/sellers.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), "utf-8")
