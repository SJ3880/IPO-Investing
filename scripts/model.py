"""
'과거 유사 사례' 기반 수익 확률

아이디어: 지금 이 종목과 비슷한 처지(상장 후 경과일, 시초가·공모가 대비 위치, 최근 20일 흐름,
고점 대비 낙폭)였던 과거 코스닥 공모주들이 그 뒤 60거래일(약 3개월) 동안 올랐는지를 센다.

  · 표본: 최근 5년 코스닥 신규상장주의 상장 후 10~250거래일 구간, 5거래일 간격 스냅샷
  · 유사도: 5개 지표를 표준화한 뒤 거리(가까운 순 300개 스냅샷)
  · 같은 종목의 스냅샷끼리 결과가 겹치므로 '종목 단위'로 평균낸 뒤 확률을 계산
  · 자기 자신은 제외, 결과가 확정된(60거래일이 지난) 과거 표본만 사용
  · 검증: 최근 상장 종목을 '그 종목 상장 이전 표본만으로' 예측해 실제와 비교(보정표)

한계: 상장폐지 종목이 빠져 있고(생존 편향), 시장 전체 흐름·실적·수급은 반영하지 못함.
"""
import math
from statistics import median

import numpy as np

H = 60          # 몇 거래일 뒤 수익을 볼지
STEP = 5
T_MIN, T_MAX = 10, 250
K = 300
MIN_STOCKS = 15


def features(bars, t, offer):
    c = bars[t][4]
    o0 = bars[0][1]
    c20 = bars[max(0, t - 20)][4]
    peak = max(b[2] for b in bars[: t + 1])
    return [
        math.log(c / o0),
        math.log(c / offer) if offer else float("nan"),
        math.log(c / c20),
        math.log(c / peak),
        math.log(t + 1),
    ]


def build_samples(universe):
    """universe: [{code, listed, offer, bars}] → X, y(수익률), code, listed"""
    X, y, codes, listed = [], [], [], []
    for s in universe:
        bars = s["bars"]
        for t in range(T_MIN, min(T_MAX, len(bars) - 1 - H) + 1, STEP):
            if bars[t][4] <= 0:
                continue
            X.append(features(bars, t, s.get("offer")))
            y.append(bars[t + H][4] / bars[t][4] - 1)
            codes.append(s["code"])
            listed.append(s["listed"])
    return np.array(X, float), np.array(y, float), np.array(codes), np.array(listed)


class SimilarCaseModel:
    def __init__(self, universe):
        self.X, self.y, self.codes, self.listed = build_samples(universe)
        if len(self.X):
            self.mu = np.nanmean(self.X, axis=0)
            self.sd = np.nanstd(self.X, axis=0) + 1e-9
            self.Z = (self.X - self.mu) / self.sd
        self.base = float((self.y > 0).mean()) if len(self.y) else None

    def predict(self, feat, exclude_code=None, before_listed=None):
        if not len(self.X):
            return None
        z = (np.array(feat, float) - self.mu) / self.sd
        mask = np.ones(len(self.Z), bool)
        if exclude_code is not None:
            mask &= self.codes != exclude_code
        if before_listed is not None:
            mask &= self.listed < before_listed
        use = ~np.isnan(z)
        Z = self.Z[mask][:, use]
        if not len(Z):
            return None
        d = np.nansum((Z - z[use]) ** 2, axis=1)
        d[np.isnan(Z).any(axis=1)] = np.inf       # 공모가 모르는 표본은 공모가 지표 비교 불가
        idx = np.argsort(d)[:K]
        idx = idx[np.isfinite(d[idx])]
        yy, cc = self.y[mask][idx], self.codes[mask][idx]
        per = {}
        for code, r in zip(cc, yy):
            per.setdefault(code, []).append(r)
        if len(per) < MIN_STOCKS:
            return None
        pos = [np.mean(np.array(v) > 0) for v in per.values()]
        big = [np.mean(np.array(v) >= 0.10) for v in per.values()]
        loss = [np.mean(np.array(v) <= -0.20) for v in per.values()]
        rets = [float(np.mean(v)) for v in per.values()]
        return {
            "p": round(float(np.mean(pos)) * 100, 1),
            "p10": round(float(np.mean(big)) * 100, 1),
            "pLoss20": round(float(np.mean(loss)) * 100, 1),
            "med": round(median(rets) * 100, 1),
            "n": len(per),
        }

    def backtest(self, universe, recent_from):
        """recent_from 이후 상장 종목을 '그 이전 상장 종목 표본'만으로 예측 → 보정표"""
        buckets = [(0, 40), (40, 55), (55, 70), (70, 101)]
        res = {b: [0, 0, 0.0] for b in buckets}  # [건수, 실제 상승, 예측합]
        for s in universe:
            if s["listed"] < recent_from:
                continue
            bars = s["bars"]
            for t in range(T_MIN, min(T_MAX, len(bars) - 1 - H) + 1, 20):
                pr = self.predict(features(bars, t, s.get("offer")), s["code"], s["listed"])
                if not pr:
                    continue
                up = bars[t + H][4] > bars[t][4]
                for b in buckets:
                    if b[0] <= pr["p"] < b[1]:
                        res[b][0] += 1
                        res[b][1] += up
                        res[b][2] += pr["p"]
        rows = []
        for (lo, hi), (n, up, ps) in res.items():
            if n:
                rows.append({"range": f"{lo}~{min(hi, 100)}%", "n": n,
                             "predicted": round(ps / n, 1), "actual": round(up / n * 100, 1)})
        return rows
