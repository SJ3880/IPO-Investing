"""일회성 조회(임시 브랜치): 기관경쟁률·확약비율이 빈 신규상장사의 투자설명서에서 수요예측 결과 표 저장"""
import gzip, json, os, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dart as dartlib  # noqa
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "debug" / "demand_probe.json.gz"
items = json.load(open(ROOT / "data" / "ipos.json"))
items = items if isinstance(items, list) else (items.get("items") or items.get("ipos"))
dt = dartlib.Dart(os.environ["DART_API_KEY"], print)
res = []
for x in items:
    if x.get("listType") != "신규상장" or not x.get("offer") or x["listed"] < "2020":
        continue
    if x.get("instComp") is not None and x.get("commit") is not None:
        continue
    d = json.load(open(ROOT / "data" / "detail" / f"{x['code']}.json"))
    if not d.get("rcpNo"):
        continue
    try:
        xml = dt.document_text(d["rcpNo"])
    except Exception as e:
        print(x["name"], e); continue
    tabs = []
    for m in re.finditer(r"<TABLE\b.*?</TABLE>", xml, re.S | re.I):
        c = m.group(0)
        if len(c) < 200000 and ("확약" in c or "경쟁률" in c or "참여건수" in c or "수요예측" in c):
            tabs.append(c)
    plain = dartlib.strip_tags(re.sub(r"</(P|TD|TE|TH|TU|TR|TITLE)>", "\n", xml[:4000000], flags=re.I))
    snip = [plain[max(0, m.start() - 400): m.end() + 600] for m in re.finditer(r"수요\s*예측\s*(결과|참여)", plain)][:10]
    res.append({"code": x["code"], "name": x["name"], "listed": x["listed"], "rcp": d["rcpNo"], "tables": tabs[:40], "snip": snip})
    print(x["name"], len(tabs), flush=True)
with gzip.open(OUT, "wt", encoding="utf-8") as fh:
    json.dump(res, fh, ensure_ascii=False)
