"""
공모자금 사용 목적 원문 대조용(일회성 점검).
⚠ 표시가 붙은 종목의 증권신고서(최종 투자설명서) 원문에서 'Ⅴ. 자금의 사용목적' 표와
DART 주요정보(estkRs)의 원래 값을 뽑아 data/debug/fund_check.json 에 저장한다.
GitHub Actions(.github/workflows/fund-check.yml)에서 DART_API_KEY 로 실행.
"""
import json
import os
import re
import signal
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dart as dartlib          # noqa: E402
import valuation                # noqa: E402
from bs4 import BeautifulSoup   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# 상장완료 종목(코드)과 공모 진행 중 종목(이름)
CODES = ("159010 111380 318020 464280 079900 378340 352480 459100 270660 262840 330350 088340 387570 136150 "
         "102370 017860 332370 031210 244920 484590 448900 064850 411080 418250 274090 290520 453860 058970 "
         "424870").split()
NAMES = ["래블업", "다비오", "엠에스바이오", "영광"]


def fund_section(xml):
    """'자금의 사용목적' 장(章)의 표·본문"""
    titles = list(re.finditer(r"<TITLE[^>]*>([^<]*)</TITLE>", xml, re.I))
    hit = next((m for m in titles if re.search(r"자금의\s*사용\s*목적", m.group(1))), None)
    if not hit:
        m = re.search(r"자금의\s*사용\s*목적", xml)
        if not m:
            return None
        st = m.start()
    else:
        st = hit.start()
    nxt = next((m for m in titles if m.start() > st + 50 and re.match(r"\s*(Ⅵ|VI|6|제\s*2\s*부|Ⅰ|I)\s*[\.\s]", m.group(1))), None)
    sec = xml[st: nxt.start() if nxt else st + 300000]
    tables = []
    for t in re.findall(r"<TABLE\b.*?</TABLE>", sec, re.S | re.I)[:12]:
        tb = BeautifulSoup(t, "html.parser").find("table")
        if tb is not None:
            g = valuation._grid(valuation._rows(tb))
            if g and len(g) <= 40:
                tables.append(g)
    return {"text": valuation._plain(sec)[:5000], "tables": tables}


def raw_estk(dt, corp, until):
    from datetime import timedelta
    try:
        j = dt._get("estkRs.json", corp_code=corp, bgn_de=(until - timedelta(days=400)).strftime("%Y%m%d"),
                    end_de=until.strftime("%Y%m%d")).json()
    except Exception as e:
        return {"error": str(e)}
    keep = ("자금의사용목적", "증권의종류", "매출인에관한사항")
    return {x.get("title"): x.get("list") for x in j.get("group", []) if x.get("title") in keep}


def main():
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key:
        print("DART_API_KEY 없음")
        return
    dt = dartlib.Dart(key)
    items = {it["code"]: it for it in json.loads((DATA / "ipos.json").read_text("utf-8"))["items"]}
    out = []
    targets = [("name", n) for n in NAMES] + [("code", c) for c in CODES]
    dest = DATA / "debug" / "fund_check.json"
    dest.parent.mkdir(exist_ok=True)

    def alarm(*_):
        raise TimeoutError("120초 초과")
    signal.signal(signal.SIGALRM, alarm)
    for kind, v in targets:
        row = {"key": v}
        t0 = time.time()
        signal.alarm(120)                                   # 한 종목이 오래 걸리면 건너뜀
        try:
            if kind == "code":
                it = items.get(v, {})
                row["name"] = it.get("name")
                listed = date.fromisoformat(it["listed"]) if it.get("listed") else date.today()
                corp = dt.corp_code(v)
                f = dt.prospectus(corp, listed) if corp else None
            else:
                row["name"] = v
                listed = date.today()
                corp, f = dt.find_offer(v)
            if not corp or not f:
                row["error"] = "신고서 못 찾음"
                out.append(row)
                continue
            row["rcpNo"], row["title"] = f["rcept_no"], f["report_nm"]
            row["estk"] = raw_estk(dt, corp, listed)
            sec = fund_section(dt.document_text(f["rcept_no"]))
            row["section"] = sec or {"error": "자금의 사용목적 장을 못 찾음"}
        except Exception as e:
            row["error"] = str(e)[:300]
        finally:
            signal.alarm(0)
        out.append(row)
        print(row.get("name"), row.get("rcpNo"), "ok" if row.get("section") else row.get("error"),
              f"{time.time() - t0:.0f}s", flush=True)
        dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")   # 중간에 끊겨도 남도록 매번 저장


if __name__ == "__main__":
    main()
