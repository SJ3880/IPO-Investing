"""
일회성 조회: 기술특례(기술성장기업) 상장사의 상장 직전 사업연도 실적(매출·영업이익·순이익).
· 대상: KIND 기타상장통계 > 코스닥 기술성장기업(2021~현재 상장)
· 실적: DART 단일회사 주요계정(fnlttSinglAcnt) — 상장연도 사업보고서의 '전기' 값(= 상장 직전 연도),
        없으면 직전연도 사업보고서 '당기', 그래도 없으면 증권신고서 요약재무 표에서 직전연도 값
결과 → data/debug/tech_profit.json (임시 브랜치에서만 실행)
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
import kind as kindlib          # noqa: E402
import valuation                # noqa: E402
from bs4 import BeautifulSoup   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "debug" / "tech_profit.json"
KEYS = {"rev": ("매출액", "수익(매출액)", "영업수익", "매출"), "op": ("영업이익",), "net": ("당기순이익", "당기순손익", "분기순이익")}


def kind_rows(years):
    rows = []
    for y in years:
        form = {"method": "searchMiscListTypeStatDetailSub", "forward": "miscListTypeStatDetail_sub",
                "currentPageSize": "500", "pageIndex": "1", "marketType": "2", "listMonth": str(y), "listClssCd": "12"}
        df = kindlib.list_table(kindlib.post("listinvstg/miscListTypeStatDetail.do", form, as_data=True))
        print(y, len(df), list(df.columns), flush=True)
        for _, r in df.iterrows():
            rows.append({c: str(r[c]) for c in df.columns} | {"_year": y})
    return rows


def pick(lst, key, col):
    """주요계정 목록에서 계정 하나(연결 우선)"""
    for fs in ("CFS", "OFS"):
        for x in lst:
            if x.get("fs_div") != fs:
                continue
            nm = re.sub(r"\s", "", x.get("account_nm", ""))
            if any(nm.startswith(k) for k in KEYS[key]) and x.get("sj_div") in ("IS", "CIS", None, ""):
                v = re.sub(r"[^\d\-]", "", x.get(col) or "")
                if v not in ("", "-"):
                    return int(v), fs
    return None, None


def fin_api(dt, corp, year, col):
    try:
        j = dt._get("fnlttSinglAcnt.json", corp_code=corp, bsns_year=str(year), reprt_code="11011").json()
    except Exception as e:
        return None
    if j.get("status") != "000":
        return None
    lst = j.get("list", [])
    out = {}
    for k in KEYS:
        v, fs = pick(lst, k, col)
        out[k] = v
        out["fs"] = out.get("fs") or fs
    return out if out.get("op") is not None else None


def fin_doc(dt, corp, listed, year):
    """증권신고서 요약재무 표: '영업이익' 줄이 있는 표에서 year 열"""
    f = dt.prospectus(corp, listed)
    if not f:
        return None
    xml = dt.document_text(f["rcept_no"])
    for m in re.finditer(r"<TABLE\b.*?</TABLE>", xml[:3000000], re.S | re.I):
        t = m.group(0)
        if "영업이익" not in t or len(t) > 200000:
            continue
        g = valuation._grid(valuation._rows(BeautifulSoup(t, "html.parser").find("table")))
        head = next((r for r in g[:4] if any(str(year) in c for c in r)), None)
        if not head:
            continue
        col = next((i for i, c in enumerate(head) if str(year) in c and not re.search(r"(반기|분기|3Q|1Q|2Q|\.0?[369]\.|\.0?[369]월)", c)), None)
        if col is None:
            continue
        unit = re.search(r"단\s*위\s*[:：]?\s*(백만원|천원|원)", xml[max(0, m.start() - 1500): m.start()])
        mul = {"원": 1, "천원": 1000, "백만원": 1000000}[unit.group(1)] if unit else None
        out = {"src": "증권신고서 요약재무", "rcpNo": f["rcept_no"], "head": head[col]}
        for r in g:
            lab = re.sub(r"\s|\(.*?\)", "", r[0] if r else "")
            for k, keys in KEYS.items():
                if k not in out and any(lab.startswith(x) for x in keys) and col < len(r):
                    s = r[col].replace(",", "").strip()
                    neg = s.startswith("(") or s.startswith("-") or s.startswith("△")
                    n = re.sub(r"[^\d.]", "", s)
                    if n:
                        out[k] = (-1 if neg else 1) * float(n) * (mul or 1)
        if out.get("op") is not None:
            out["unit"] = unit.group(1) if unit else None
            return out
    return None


def main():
    key = os.environ.get("DART_API_KEY", "").strip()
    dt = dartlib.Dart(key)
    rows = kind_rows(range(2021, date.today().year + 1))
    out = []
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("시간 초과")))
    for r in rows:
        name = next((r[c] for c in r if "회사명" in c or "기업명" in c or "종목명" in c), "")
        code = re.sub(r"\D", "", next((r[c] for c in r if "코드" in c), ""))
        ld = next((r[c] for c in r if "상장일" in c), "")
        rec = {"name": name, "kindCode": code, "listed": ld, "kind": r}
        signal.alarm(150)
        try:
            corp = None
            if len(code) >= 5:
                corp = dt.corp_code(code[:5] + "0") or dt.corp_code(code[:6])
            corp = corp or dt.corp_by_name(name)
            rec["corp"] = corp
            listed = date.fromisoformat(re.sub(r"[./]", "-", ld)[:10]) if re.match(r"\d{4}", ld) else date(r["_year"], 6, 30)
            y = listed.year
            fin = fin_api(dt, corp, y, "frmtrm_amount") if corp else None
            if fin:
                fin["src"] = f"{y}년 사업보고서 전기"
            elif corp:
                fin = fin_api(dt, corp, y - 1, "thstrm_amount")
                if fin:
                    fin["src"] = f"{y - 1}년 사업보고서 당기"
            if not fin and corp:
                fin = fin_doc(dt, corp, listed, y - 1)
            rec["fy"] = y - 1
            rec["fin"] = fin
        except Exception as e:
            rec["error"] = str(e)[:200]
        finally:
            signal.alarm(0)
        out.append(rec)
        print(name, ld, rec.get("fin"), rec.get("error"), flush=True)
        OUT.parent.mkdir(exist_ok=True)
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")


if __name__ == "__main__":
    main()
