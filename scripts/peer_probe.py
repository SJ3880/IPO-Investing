"""
일회성 조회(임시 브랜치 전용): 비교회사·유통가능물량을 못 읽은 신규상장사의 투자설명서에서
'공모가격에 대한 의견' 장(표+본문)과 유통가능물량 관련 표를 잘라 data/debug/probe/<code>.json.gz 로 저장
"""
import gzip, json, os, re, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dart as dartlib      # noqa
import valuation            # noqa
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "debug" / "probe"
OUT.mkdir(parents=True, exist_ok=True)
t0 = time.time()
items = json.load(open(ROOT / "data" / "ipos.json"))
items = items if isinstance(items, list) else (items.get("items") or items.get("ipos"))
tg = []
for x in items:
    if x.get("listType") != "신규상장" or not x.get("offer"):
        continue
    if x.get("peers") and x.get("floatAtListing") is not None:
        continue
    f = ROOT / "data" / "detail" / f"{x['code']}.json"
    if not f.exists():
        continue
    d = json.load(open(f))
    if d.get("rcpNo"):
        tg.append((x["listed"], x["code"], x["name"], d["rcpNo"], not x.get("peers"), x.get("floatAtListing") is None))
tg.sort(reverse=True)
print("대상", len(tg), flush=True)
dt = dartlib.Dart(os.environ["DART_API_KEY"], print)
ok = 0
for listed, code, name, rcp, need_peer, need_float in tg:
    if time.time() - t0 > 75 * 60:
        print("시간 초과로 중단", flush=True)
        break
    p = OUT / f"{code}.json.gz"
    if p.exists():
        continue
    try:
        xml = dt.document_text(rcp)
    except Exception as e:
        print(name, "실패", e, flush=True)
        continue
    rec = {"code": code, "name": name, "listed": listed, "rcp": rcp, "needPeer": need_peer, "needFloat": need_float}
    sec = valuation.pricing_section(xml)
    if need_peer and sec:
        tabs = re.findall(r"<TABLE\b.*?</TABLE>", sec, re.S | re.I)
        keep, size = [], 0
        for t in tabs:
            if size + len(t) > 600000:
                break
            keep.append(t); size += len(t)
        rec["secHead"] = sec[:3000]
        rec["secTables"] = keep
        rec["secPlain"] = valuation._plain(sec)[:120000]
    if need_float:
        lt = []
        for m in re.finditer(r"<TABLE\b.*?</TABLE>", xml, re.S | re.I):
            c = m.group(0)
            if ("유통" in c or "매각제한" in c or "보호예수" in c) and len(c) < 500000:
                lt.append(c)
        rec["lockTables"] = lt[:8]
        plain = dartlib.strip_tags(re.sub(r"</(P|TD|TE|TH|TU|TR|TITLE)>", "\n", xml[:4000000], flags=re.I))
        rec["floatText"] = [plain[max(0, m.start() - 300): m.end() + 300] for m in re.finditer(r"유통\s*가능\s*(주식|물량)", plain)][:12]
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False)
    ok += 1
    if ok % 20 == 0:
        print(ok, name, round(time.time() - t0), flush=True)
print("완료", ok, flush=True)
