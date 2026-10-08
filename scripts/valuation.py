"""
증권신고서 '공모가격 결정방법'에서 공모가 산정 근거를 뽑는다.
  · 적용 배수(PER 등)와 평가방법
  · 비교회사(Peer group) 이름과 각 회사의 배수
  · 주당 평가가액, 희망공모가액 밴드, 공모가 할인율(평가가액 대비)
문서마다 표 모양이 달라서, 여러 방법으로 찾고 서로 맞는지(평균 = 적용 배수) 확인한다.
"""
import re

from bs4 import BeautifulSoup

MULT = r"(PER|P/E|PSR|P/S|PBR|P/B|EV\s*/\s*EBITDA|EV\s*/\s*EBIT|EV\s*/\s*Sales|주가수익비율)"
MULT_RE = re.compile(MULT, re.I)
NUM_RE = re.compile(r"-?\d{1,4}(?:,\d{3})*(?:\.\d+)?")
NOT_NAME = re.compile(r"^(구분|회사명|기업명|유사회사|비교회사|유사기업|비교기업|종목명|평균|산술평균|단순평균|합계|"
                      r"적용|최대|최소|최고|최저|중간값|중앙값|비고|단위|항목|기준|내용|순번|번호|시장|업종|주\)|\d+)$")


def _clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def mult_name(s):
    m = MULT_RE.search(s or "")
    if not m:
        return None
    t = re.sub(r"\s+", "", m.group(1)).upper()
    return {"P/E": "PER", "주가수익비율": "PER", "P/S": "PSR", "P/B": "PBR"}.get(t, t)


def _num(s):
    s = _clean(s).replace(" ", "")
    m = NUM_RE.search(s)
    if not m or len(s) > 24:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _multiple(s):
    """'25.31배', '25.31', '25.31x' → 25.31 (배수로 보기 어려운 값은 None)"""
    s = _clean(s)
    if not s or re.search(r"(원|%|주|천|백만|억)", s):
        return None
    if not re.fullmatch(r"-?[\d,]+(\.\d+)?\s*(배|x|X|times)?", s.replace(" ", "")):
        return None
    v = _num(s)
    return v if v is not None and 0 < v < 1000 else None


def _is_name(s):
    s = _clean(s)
    if not s or len(s) > 30 or NOT_NAME.match(s.replace(" ", "")):
        return False
    if re.fullmatch(r"(유사|비교|대상)?(회사|기업|법인)\s*[A-Z\d]+", s.replace(" ", "")):
        return False
    if _num(s) is not None and re.fullmatch(r"[-\d,.\s배%원()]+", s):
        return False
    if MULT_RE.search(s) or re.search(r"(순이익|시가총액|주식수|EBITDA|매출|자본|평균|합계|적용|단위|기준일)", s):
        return False
    if NOT_PEER.search(_name(s)) or not _name(s) or UNIT_ONLY.match(s.replace(" ", "")):
        return False
    return bool(re.search(r"[가-힣A-Za-z]", s))


# 회사명이 아닌 칸: 기간(2022년, 23.5월말, 2025년(E), 반기), 금액·단위(88.99USD, 3,317백만원), 각주만 있는 칸((주3))
NOT_PEER = re.compile(r"^(\d{2,4}\s*[.년/]\s*(\d{1,2}\s*월\s*말?)?.*|.*\d\s*(USD|KRW|EUR|JPY|CNY|원|백만원|억원|천원|달러|%)\s*$|"
                      r"(\d{4}\s*년)?\s*(반기|분기|상반기|하반기|연간|\d\s*Q)\s*(\(E\))?)$", re.I)
UNIT_ONLY = re.compile(r"^(원|천원|백만원|억원|USD|KRW|EUR|JPY|CNY|달러|배|%|주|인원|추정|결산월|결산|연결|별도|"
                       r"K-?IFRS(연결|별도)?|IFRS|GAAP|해당없음|N/?A|-)$", re.I)
FOOTNOTE = re.compile(r"\s*(\(\s*(주|\*)\s*\d+\s*\)|(주|\*)\s*\d+\s*\)|\*+\d*|주\d+)\s*$")


def _name(s):
    s = re.sub(r"\(\s*주\s*\)|㈜|주식회사", "", _clean(s))
    for _ in range(2):
        s = FOOTNOTE.sub("", s)
    s = re.sub(r"\s*\((?:\d{6}|코스닥|코스피|KOSDAQ|KOSPI|유가증권|[A-Z]{2,5}:?[A-Z0-9.]*)\)\s*$", "", s)
    return _clean(s)


# ---------------- 표 → 격자 ----------------
CELL = ["td", "th", "te", "tu"]


def _rows(tbl):
    rows = []
    for tr in tbl.find_all("tr"):
        cells = tr.find_all(CELL, recursive=False) or tr.find_all(CELL)
        rows.append([{"t": _clean(c.get_text(" ")),
                      "cs": int(c.get("colspan")) if str(c.get("colspan") or "").isdigit() else 1,
                      "rs": int(c.get("rowspan")) if str(c.get("rowspan") or "").isdigit() else 1} for c in cells])
    return [r for r in rows if r]


def _grid(rows):
    grid, carry = [], {}
    for r in rows:
        line, col, it = [], 0, iter(r)
        cell = next(it, None)
        while cell is not None or col in carry:
            if col in carry:
                t, left = carry[col]
                line.append(t)
                if left <= 1:
                    del carry[col]
                else:
                    carry[col] = (t, left - 1)
                col += 1
                continue
            for _ in range(max(1, cell["cs"])):
                line.append(cell["t"])
                if cell["rs"] > 1:
                    carry[col] = (cell["t"], cell["rs"] - 1)
                col += 1
            cell = next(it, None)
        grid.append(line)
    w = max((len(r) for r in grid), default=0)
    return [r + [""] * (w - len(r)) for r in grid]


# ---------------- 비교회사 표 ----------------
def peers_from_grid(g):
    """두 가지 모양을 모두 시도
       가로형: 첫 행(들)에 회사명, 'PER' 행에 배수
       세로형: 첫 열에 회사명, 'PER' 열에 배수"""
    best = None
    if not g or len(g) < 2:
        return None
    # 가로형: 배수 행을 찾고, 그 위쪽 행 중 회사명이 가장 많은 행을 이름 행으로
    for i, row in enumerate(g):
        label = " ".join(dict.fromkeys(row[:2]))
        mn = mult_name(label)
        if not mn or re.search(r"(적용|평균)", label) and not re.search(r"(유사|비교)", label):
            continue
        vals = {j: _multiple(c) for j, c in enumerate(row)}
        vals = {j: v for j, v in vals.items() if v is not None and j >= 1}
        if len(vals) < 2:
            continue
        for h in range(i):
            names = {j: _name(g[h][j]) for j in vals if j < len(g[h]) and _is_name(g[h][j])}
            if len(names) >= max(2, len(vals) - 1):
                avg = next((vals[j] for j in vals if re.search(r"평균", g[h][j] if j < len(g[h]) else "")), None)
                peers = [{"name": names[j], "v": vals[j]} for j in sorted(names)]
                cand = {"mult": mn, "peers": peers, "avg": avg}
                if not best or len(peers) > len(best["peers"]):
                    best = cand
                break
    # 세로형: 머리행에서 배수 열을 찾고, 같은 행들의 이름 열을 찾는다
    for h in range(min(4, len(g))):
        for j, head in enumerate(g[h]):
            mn = mult_name(head)
            if not mn or re.search(r"(적용)", head):
                continue
            peers, avg = [], None
            ncol = next((k for k, c in enumerate(g[h]) if k != j and re.search(r"(회사명|기업명|종목명|회사|기업)$", c.replace(" ", ""))), None)
            for r in g[h + 1:]:
                v = _multiple(r[j]) if j < len(r) else None
                if ncol is not None and ncol < len(r) and _is_name(r[ncol]):
                    namecol = r[ncol]
                else:
                    namecol = next((c for c in r[:j] if _is_name(c)), None)
                if v is not None and namecol:
                    peers.append({"name": _name(namecol), "v": v})
                elif v is not None and any(re.search(r"평균", c) for c in r[:j]):
                    avg = v
            seen, uniq = set(), []
            for p in peers:
                if p["name"] not in seen:
                    seen.add(p["name"])
                    uniq.append(p)
            if len(uniq) >= 2 and (not best or len(uniq) > len(best["peers"])):
                best = {"mult": mn, "peers": uniq, "avg": avg}
    return best


# ---------------- 평균 = 적용 배수 검증(엄격) ----------------
def match_tol(applied):
    """적용 배수가 적힌 자릿수 기준 허용 오차. 19.36 → ±0.011, 21.8 → ±0.056, 30 → ±0.506
    (예전엔 ±3%라서, 제외된 회사 1곳이 섞여 있어도(예: 에스투더블유의 안랩) 통과됐다)"""
    s = f"{applied:.4f}".rstrip("0")
    dec = len(s.split(".")[1]) if "." in s and not s.endswith(".") else 0
    return 0.5 * 10 ** (-dec) + 0.006


def mean_ok(peers, applied):
    if not peers or not applied:
        return False
    m = sum(p["v"] for p in peers) / len(peers)
    return abs(m - applied) <= match_tol(applied)


def reconcile_peers(peers, applied, max_n=16, marked=None):
    """비교회사 목록의 평균이 적용 배수와 정확히 맞는지 보고, 안 맞으면
    '최종 단계에서 빠진 회사'를 덜어내 평균이 맞는 가장 큰 조합을 찾는다.
    반환: (회사 목록, 확인됨 여부). 맞는 조합이 없거나 같은 크기 조합이 여러 개면 원래 목록 + 미확인."""
    from itertools import combinations
    if not peers or not applied:
        return peers, False
    if mean_ok(peers, applied):
        return peers, True
    n = len(peers)
    # 각주(주1)·(*1)가 붙은 회사 = 보통 '최종 제외' 표시. 그 회사들만 빼서 맞으면 그대로 채택
    if marked:
        rest = [p for i, p in enumerate(peers) if i not in marked]
        if len(rest) >= 2 and mean_ok(rest, applied):
            return rest, True
    if n > max_n:
        return peers, False
    for k in range(n - 1, 1, -1):
        if n - k > max(1, n // 3):             # 3분의 1 넘게 빼야 맞는 조합은 우연일 가능성이 커서 믿지 않음
            break
        hits = [c for c in combinations(range(n), k) if mean_ok([peers[i] for i in c], applied)]
        if len(hits) == 1:
            return [peers[i] for i in hits[0]], True
        if len(hits) > 1:
            break                               # 같은 크기 조합이 여럿 = 어느 회사가 빠졌는지 모름
    return peers, False


# ---------------- 최종 비교회사: 적용 배수(평균)가 적힌 칸에서 거꾸로 찾기 ----------------
def peers_by_applied(g, applied):
    """표 안에서 '적용 배수(=최종 비교회사 평균)' 값이 적힌 칸을 찾고, 같은 열(세로형) 또는 같은 행(가로형)의
    다른 배수들 중 평균이 그 값과 맞는 회사들 = 최종 비교회사. 1·2차 후보 표는 평균이 안 맞아서 걸러진다."""
    if not g or not applied:
        return None
    best = None
    H, W = len(g), max(len(r) for r in g)
    for i in range(H):
        for j in range(len(g[i])):
            v = _multiple(g[i][j])
            if v is None or abs(v - applied) > max(0.006, applied * 0.002):
                continue
            cands = []
            # 세로형: 같은 열의 위쪽 행들, 이름은 그 행의 앞쪽 칸
            col = []
            for k in range(H):
                if k == i or j >= len(g[k]):
                    continue
                x = _multiple(g[k][j])
                nm = next((c for c in g[k][:j] if _is_name(c)), None)
                if x is not None and nm:
                    col.append({"name": _name(nm), "v": x})
            cands.append(col)
            # 가로형: 같은 행의 다른 칸, 이름은 그 열의 위쪽 행
            row = []
            for l in range(len(g[i])):
                if l == j:
                    continue
                x = _multiple(g[i][l])
                nm = next((g[k][l] for k in range(i - 1, -1, -1) if l < len(g[k]) and _is_name(g[k][l])), None)
                if x is not None and nm:
                    row.append({"name": _name(nm), "v": x})
            cands.append(row)
            for c in cands:
                seen, u = set(), []
                for p in c:
                    if p["name"] not in seen and p["name"] != _name(g[i][0]):
                        seen.add(p["name"])
                        u.append(p)
                if len(u) < 2:
                    continue
                u, ok = reconcile_peers(u, applied)
                if ok and (not best or len(u) > len(best)):
                    best = u
    return best


def peers_from_text(plain, pool, applied):
    """본문 '최종 … 선정' 문장에 나온 회사만 남겨서 평균이 맞으면 그 회사들"""
    if not pool or not applied:
        return None
    f = re.sub(r"\s+", " ", plain)
    for m in re.finditer(r"최종[^.。]{0,400}", f):
        w = m.group(0)
        hit = [p for p in pool if p["name"] and p["name"] in w]
        if len(hit) >= 2 and mean_ok(hit, applied):
            return hit
    return None


# ---------------- 공모가 산정 근거가 있는 구간 ----------------
def _section_from(xml_text, st, limit=1500000):
    rest = xml_text[st: st + limit]
    # 다음 큰 장(Ⅴ. 자금의 사용목적 등)이 나오면 거기서 끊는다
    end = re.search(r"<TITLE[^>]*>\s*(Ⅴ|V|5)\s*\.[^<]*</TITLE>|<TITLE[^>]*>[^<]*자금의\s*사용\s*목적[^<]*</TITLE>",
                    rest[200:], re.I)
    return rest[: end.start() + 200] if end else rest[:900000]


def pricing_section(xml_text):
    """희망공모가 산정 근거(적용 PER·비교회사·할인율)는 증권신고서
       'Ⅳ. 인수인의 의견(분석기관의 평가의견) - 1. 공모가격에 대한 의견'에 있다.
       (‘3. 공모가격 결정방법’은 절차 설명뿐이고 이 장을 참조하라고만 적혀 있음)
       스팩 합병 신고서는 '합병가액 산출근거' 쪽."""
    titles = list(re.finditer(r"<TITLE[^>]*>([^<]*)</TITLE>", xml_text, re.I))
    for pat in (r"인수인의\s*의견|분석기관의\s*평가\s*의견",
                r"공모\s*가격에\s*대한\s*의견",
                r"합병\s*(가액|비율)[^<]{0,20}(산출|산정|근거)|외부평가",
                r"(공모|발행|모집|매출)\s*가(격|액)?\s*(의\s*)?(결정|산정)"):
        hit = next((m for m in titles if re.search(pat, m.group(1))), None)
        if hit:
            return _section_from(xml_text, hit.start())
    # 제목 태그가 없으면 본문에서 '공모가격에 대한 의견' 문구(참조 문장이 아닌 것)를 찾는다
    for m in re.finditer(r"공모\s*가격에\s*대한\s*의견", xml_text):
        if "참고" not in xml_text[m.end(): m.end() + 60] and "참조" not in xml_text[m.end(): m.end() + 60]:
            return _section_from(xml_text, m.start())
    return ""


def _plain(s):
    import html as _h
    s = re.sub(r"</(P|TD|TE|TH|TU|TR|TITLE)>", "\n", s, flags=re.I)
    s = _h.unescape(re.sub(r"<[^>]+>", " ", s))
    return "\n".join(_clean(x) for x in s.split("\n") if x.strip())


WON = r"([\d]{1,3}(?:,\d{3})+|\d{3,7})\s*원"
PCTV = r"(-?\d{1,2}(?:\.\d+)?)\s*%"


def _summary_from_grids(grids):
    out = {}
    for g in grids:
        for row in g:
            label = _clean(" ".join(dict.fromkeys(row[:2])))
            rest = " ".join(row[1:]) if len(row) > 1 else ""
            vals = " ".join(dict.fromkeys(row[1:]))
            if "fair" not in out and re.search(r"주당\s*평가\s*가(액|치|격)", label):
                m = re.search(WON, vals) or re.search(r"([\d,]{4,})", vals)
                if m:
                    out["fair"] = int(m.group(1).replace(",", ""))
            if "disc" not in out and re.search(r"할인율", label) and not re.search(r"현가", label):
                ps = re.findall(PCTV, vals)
                if len(ps) >= 2 and "~" in vals:          # 범위(예: 36.55% ~ 25.30%)만. 단일 값은 현가할인율일 수 있음
                    out["disc"] = [float(p) for p in ps[:2]]
            if "band" not in out and re.search(r"(희망\s*공모\s*가|공모\s*희망\s*가|확정\s*공모\s*가|밴드)", label):
                ws = re.findall(WON, vals) or re.findall(r"([\d]{1,3}(?:,\d{3})+)", vals)
                if len(ws) >= 2:
                    out["band"] = [int(w.replace(",", "")) for w in ws[:2]]
            if "applied" not in out and re.search(r"(적용|평균)", label) and mult_name(label):
                v = next((x for x in (_multiple(c) for c in row[1:]) if x is not None), None)
                if v is None:
                    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*배", rest)
                    v = float(m.group(1)) if m else None
                if v is not None:
                    out["applied"], out["appliedMult"] = v, mult_name(label)
    return out


def _flat(plain):
    return re.sub(r"\s+", " ", plain.replace("|", " "))


def summary_table(plain):
    """【공모가 산정 요약표】와 '가. 평가결과' 표에서 핵심 숫자만 (단위 칸이 따로 있는 표도 허용)"""
    f = _flat(plain[:60000])
    out = {}
    NUM = r"(\d{1,4}(?:,\d{3})*(?:\.\d+)?)"
    m = re.search(r"평가\s*모형\s*" + MULT, f, re.I)
    if m:
        out["appliedMult"] = mult_name(m.group(1))
    # ② 비교대상회사/유사기업 PER(PSR·PBR…) [배] 11.15 [배]
    m = re.search(r"②\s*(?:비교\s*대상\s*회사|비교\s*대상\s*기업|비교\s*회사|비교\s*기업|유사\s*기업|유사\s*회사|적용)?\s*(?:의\s*)?(?:평균\s*)?"
                  + MULT + r"\s*(?:\(\s*배\s*\)|배|x)?\s*" + NUM, f, re.I)
    if m:
        v = float(m.group(2).replace(",", ""))
        if 0 < v < 1000:
            out["applied"] = v
            out["appliedMult"] = out.get("appliedMult") or mult_name(m.group(1))
    m = re.search(r"주당\s*평가\s*가(?:액|치)(?:\s*\([^가-힣]{0,24})?\s*(?:원\s*([\d,]{4,})|([\d,]{4,})\s*원)", f)
    if m:
        out["fair"] = int((m.group(1) or m.group(2)).replace(",", ""))
    PC = r"(\d{1,2}(?:\.\d+)?)\s*%"
    m = re.search(r"평가\s*가(?:액|치)\s*(?:에\s*)?대한\s*할인율\s*(?:%\s*)?" + PC + r"\s*~\s*" + PC, f) \
        or re.search(r"공모\s*가?\s*할인율\s*(?:%\s*)?" + PC + r"\s*~\s*" + PC, f)
    if m:
        out["disc"] = [float(m.group(1)), float(m.group(2))]
    m = re.search(r"(?:희망\s*공모\s*가(?:액|격)?|공모\s*희망\s*가(?:액|격)?)\s*(?:의\s*범위|밴드)?\s*(?:원\s*)?([\d,]{4,})\s*원?\s*~\s*([\d,]{4,})\s*원?", f)
    if m:
        out["band"] = [int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))]
    return out


def valuation_info(xml_text):
    sec = pricing_section(xml_text)
    if not sec:
        return {}
    plain = _plain(sec)
    tables = re.findall(r"<TABLE\b.*?</TABLE>", sec, re.S | re.I)
    grids, pos = [], []
    for t in tables[:80]:
        if len(t) > 400000:
            continue
        soup = BeautifulSoup(t, "html.parser")
        tb = soup.find("table")
        if tb is not None:
            grids.append(_grid(_rows(tb)))
            pos.append(sec.find(t[:200]))
    s = _summary_from_grids(grids)
    # 1순위: 증권신고서 표준 【공모가 산정 요약표】(평가모형 / ② 비교회사 배수 / 주당 평가가액 / ④ 할인율 / 희망공모가액)
    #   기술특례 회사의 '할인율 15%'(현가할인율) 같은 다른 할인율과 섞이지 않도록 이 표를 먼저 읽는다
    for k, v in summary_table(plain).items():
        s[k] = v

    # 본문 문장에서 보완
    if "applied" not in s:
        m = re.search(r"(?:적용|평균)\s*" + MULT + r"[^\d\n]{0,25}(\d{1,3}(?:\.\d+)?)\s*배", plain, re.I)
        if m:
            s["applied"], s["appliedMult"] = float(m.group(2)), mult_name(m.group(1))
    if "fair" not in s:
        m = re.search(r"주당\s*평가\s*가(?:액|치|격)[^\d\n]{0,25}" + WON, plain)
        if m:
            s["fair"] = int(m.group(1).replace(",", ""))
    if "disc" not in s:
        for m in re.finditer(r"할인율[^\d\n%]{0,20}" + PCTV + r"\s*~\s*" + PCTV, plain):
            if "현가" not in plain[max(0, m.start() - 6): m.start()]:
                s["disc"] = [float(m.group(1)), float(m.group(2))]
                break
    if "band" not in s:
        m = re.search(r"희망\s*공모\s*가(?:액|격)?[^\d\n]{0,30}" + WON + r"\s*~\s*" + WON, plain)
        if m:
            s["band"] = [int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))]

    # 비교회사 표: 배수 값이 있는 표 중, 평균이 적용 배수와 맞는 표를 우선
    found = []
    for k, g in enumerate(grids):
        p = peers_from_grid(g)
        if p:
            before = sec[max(0, pos[k] - 3000): pos[k]] if pos[k] >= 0 else ""
            p["final"] = bool(re.search(r"최종", before[-1500:]))
            vs = [x["v"] for x in p["peers"]]
            p["mean"] = sum(vs) / len(vs)
            found.append(p)
    applied = s.get("applied")

    def score(p):
        sc = 0
        ref = applied or p.get("avg")
        if ref and mean_ok(p["peers"], ref):
            sc += 4
        if p.get("avg") and mean_ok(p["peers"], p["avg"]):
            sc += 2
        if p["final"]:
            sc += 2
        if applied and s.get("appliedMult") == p["mult"]:
            sc += 1
        return sc
    # 표 평균이 적용 배수와 정확히 안 맞으면, 최종 단계에서 빠진 회사를 덜어내 맞춰 본다(예: 5곳 중 1곳 제외)
    if applied:
        for p in found:
            ps, ok = reconcile_peers(p["peers"], applied)
            if ok and ps is not p["peers"]:
                p["peers"], p["mean"], p["final"] = ps, sum(x["v"] for x in ps) / len(ps), True
    peer = max(found, key=score) if found else None
    # 최종 비교회사만: 적용 배수와 평균이 맞는 표를 우선. 안 맞으면 '적용 배수 칸'에서 거꾸로, 그다음 '최종 선정' 문장으로
    if applied and not (peer and score(peer) >= 4):
        fin = None
        for g in grids:
            fin = peers_by_applied(g, applied)
            if fin:
                break
        if not fin:
            pool = {}
            for p in found:
                for q in p["peers"]:
                    pool.setdefault(q["name"], q)
            fin = peers_from_text(plain, list(pool.values()), applied)
        if fin:
            peer = {"mult": s.get("appliedMult") or (peer or {}).get("mult"), "peers": fin, "avg": applied,
                    "final": True, "mean": sum(x["v"] for x in fin) / len(fin)}
        elif peer:
            peer = None                     # 평균이 적용 배수와 안 맞는 표 = 1·2차 후보일 가능성 → 보여주지 않음

    out = {"method": s.get("appliedMult") or (peer["mult"] if peer else None),
           "appliedMult": applied if applied is not None else (round(peer["mean"], 2) if peer and score(peer) >= 4 else
                                                              (peer.get("avg") if peer else None)),
           "fairValue": s.get("fair"),
           "bandDoc": s.get("band") if s.get("band") and len(s["band"]) == 2 else None,
           "peers": [{"name": x["name"], "v": round(x["v"], 2)} for x in peer["peers"][:20]] if peer else [],
           "peerChecked": bool(peer and score(peer) >= 4)}
    disc = s.get("disc")
    fair, band = out["fairValue"], out["bandDoc"]
    if fair and band and fair < band[0] * 0.9:          # '주당 평가가액'을 엉뚱한 숫자(단위 칸 등)로 읽은 경우
        ok = [d for d in (disc or []) if 0 < d < 80]
        fair = out["fairValue"] = round(band[1] / (1 - min(ok) / 100)) if ok else None
    if disc:
        disc = sorted(d for d in disc if 0 <= d < 80)
    if not disc and fair and band:                      # 표에 없으면 직접 계산
        disc = sorted(round((1 - b / fair) * 100, 1) for b in band)
    out["discountRange"] = disc or None
    # 근거 문장(사람이 확인하라고)
    m = re.search(r"[^\n]{0,80}(유사회사|비교회사|비교기업|유사기업)[^\n]{0,40}(선정|최종)[^\n]{0,120}", plain)
    out["peerNote"] = _clean(m.group(0))[:220] if m else None
    if not any([out["appliedMult"], out["fairValue"], out["peers"], out["discountRange"]]):
        return {}
    return sanitize(out)


def debug_sample(xml_text, limit=15000):
    """실제 문서에서 잘 읽히는지 확인용(웹 저장소 data/debug 에 남김)"""
    sec = pricing_section(xml_text)
    return {"found": bool(sec), "len": len(sec), "head": _plain(sec)[:limit] if sec else ""}


def sanitize(v):
    """이미 저장된 평가 정보의 비교회사 목록을 다시 점검(문서를 다시 읽지 않고)
    · 회사명이 아닌 칸(기간·금액·각주) 제거, 각주 표시 정리
    · 평균이 적용 배수와 정확히 맞는지 확인하고, 안 맞으면 빠진 회사를 덜어내 맞춰 본다"""
    if not v:
        return v
    ps, marked = [], set()
    for p in v.get("peers") or []:
        raw = p.get("name") or ""
        n = _name(raw)
        if _is_name(n) and n not in {q["name"] for q in ps}:
            if FOOTNOTE.search(re.sub(r"\(\s*주\s*\)\s*$", "", _clean(raw))):
                marked.add(len(ps))
            ps.append({"name": n, "v": p["v"]})
    if len(ps) < 2:
        ps = []
    am = v.get("appliedMult")
    if ps and am:
        ps, ok = reconcile_peers(ps, am, marked=marked)
        # 평균이 적용 배수와 3% 넘게 다르면 최종 비교회사 표가 아님(1·2차 후보·다른 표) → 보여주지 않음
        if not ok and abs(sum(p["v"] for p in ps) / len(ps) - am) / am > 0.03:
            ps = []
    else:
        ok = False
    v["peers"], v["peerChecked"] = ps, bool(ok)
    return v
