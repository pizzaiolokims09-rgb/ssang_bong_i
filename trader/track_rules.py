"""
track_rules.py - 트랙별 정량 검색식의 '순수 함수' 버전 (백테스트/재사용 공용)

목적:
  signals.py의 BaseScreener.scan_track_* 메서드는 self.kis.get_daily_chart()로
  데이터를 직접 가져오며 라이브 전용이다. 백테스트는 동일한 규칙을 과거 일봉에
  적용해야 하므로, 여기에 KIS/네이버/pykrx 어디서 온 데이터든 받을 수 있는
  순수 함수로 규칙의 '판정부'를 분리했다.

⚠️ 라이브(signals.py)와의 관계:
  - 본 모듈은 signals.py의 로직을 충실히 미러링한다. 단, 한 가지만 다르다:
    라이브는 장중 누적거래량을 하루치로 환산(_projected_full_day_volume)하지만,
    백테스트는 '완성된 일봉'을 다루므로 환산이 불필요하다(당일 봉 거래량이 곧 전일치).
  - signals.py의 검색식을 수정하면 본 모듈도 함께 갱신해야 한다(드리프트 주의).
  - 라이브 코드(signals.py)는 안정성을 위해 본 모듈로 리팩터하지 않았다.

데이터 포맷(전 함수 공통):
  daily: [{open, high, low, close, volume, (date)}, ...]  ※ 최신이 index 0 (KIS와 동일)
"""
from typing import Optional


# ──────────────────────────────────────────────
# 유니버스 1차 필터 (signals.BaseScreener.scan 의 절대 필터부)
# ──────────────────────────────────────────────
def passes_universe(price: int, trade_amount: float, volume: int,
                    min_price: int = 1000, max_price: int = 0,
                    min_trade_amount: float = 3_000_000_000, min_volume: int = 1_000_000,
                    market_cap: int = 0, max_market_cap: int = 0) -> bool:
    """절대 유니버스 필터. max_price/max_market_cap <= 0 이면 상한 없음.
    (거래대금 상위 N위 '교차 랭킹'은 일괄조회가 필요하므로 백테스트에선 절대필터만 적용)"""
    if price < min_price:
        return False
    if max_price > 0 and price > max_price:
        return False
    if max_market_cap > 0 and 0 < market_cap and market_cap > max_market_cap:
        return False
    if trade_amount < min_trade_amount:
        return False
    if volume < min_volume:
        return False
    return True


def _ma(closes: list, n: int) -> float:
    return sum(closes[:n]) / n if len(closes) >= n else 0.0


# ──────────────────────────────────────────────
# Track B: 눌림목 단기 스윙 (signals.scan_track_b 미러)
# ──────────────────────────────────────────────
def evaluate_track_b(daily: list, current: int, vol_drop_max: float = 0.70,
                     min_trend_gap: float = 0.0, band_lower: float = 0.02,
                     band_upper: float = 0.03, rsi_min: float = 0.0,
                     rsi_max: float = 100.0) -> Optional[dict]:
    """눌림목 스윙. 강화 파라미터(Phase D-4):
      min_trend_gap: ma20 >= ma60*(1+gap) — 정배열 강도(0=기존)
      band_lower/upper: 눌림 밴드 ma10*(1-lower) ~ ma5*(1+upper)
      rsi_min/max: 건강한 눌림(과매도 붕괴/과열 배제). 기본 0~100=미적용."""
    if not daily or len(daily) < 60:
        return None
    closes = [c["close"] for c in daily]
    volumes = [c["volume"] for c in daily]
    ma5, ma10, ma20, ma60 = _ma(closes, 5), _ma(closes, 10), _ma(closes, 20), _ma(closes, 60)

    if volumes[1] <= 0:
        return None
    vol_ratio = volumes[0] / volumes[1]   # 완성봉 기준 (라이브는 장중 환산)
    if vol_ratio > vol_drop_max:
        return None
    if current < ma10 * (1 - band_lower) or current > ma5 * (1 + band_upper):
        return None
    if current <= ma20 or ma20 < ma60 * (1 + min_trend_gap):
        return None
    if rsi_min > 0.0 or rsi_max < 100.0:
        from trader.quant_indicators import wilder_rsi
        rsi = wilder_rsi(daily, 14)
        if not (rsi_min <= rsi <= rsi_max):
            return None
    return {"track": "B", "ma5": int(ma5), "ma10": int(ma10),
            "ma20": int(ma20), "ma60": int(ma60), "vol_ratio": round(vol_ratio, 3)}


# ──────────────────────────────────────────────
# Track C: ABC 수급 종가 베팅 (signals.scan_track_c 미러, 체결강도 제외)
# ──────────────────────────────────────────────
def evaluate_track_c(daily: list, current: int,
                     surge_mult: float = 1.5, today_vol_max: float = 1.2) -> Optional[dict]:
    # 주: 체결강도(exec_strength)는 장중 지표라 백테스트에서 제외(낙관 편향 주의)
    if not daily or len(daily) < 6:
        return None
    closes = [c["close"] for c in daily]
    ma5 = _ma(closes, 5)
    if current < ma5:
        return None
    vol_surge_found = False
    for i in range(min(5, len(daily) - 1)):
        prev_vol = daily[i + 1]["volume"]
        if prev_vol > 0 and daily[i]["volume"] / prev_vol >= surge_mult:
            vol_surge_found = True
            break
    if not vol_surge_found:
        return None
    today_vol = daily[0]["volume"]
    yest_vol = daily[1]["volume"]
    if yest_vol > 0 and (today_vol / yest_vol) > today_vol_max:   # 완성봉 기준
        return None
    return {"track": "C", "ma5": int(ma5)}


# ──────────────────────────────────────────────
# Track E: 낙폭과대 폭락주 (signals.scan_track_e 미러)
# ──────────────────────────────────────────────
def evaluate_track_e(daily: list, current: int, min_rise: float = 3.0,
                     first_level: float = 0.48) -> Optional[dict]:
    if not daily or len(daily) < 250:
        return None
    highs = [c["high"] for c in daily]
    lows = [c["low"] for c in daily]
    period_low = min(lows)
    period_high = max(highs)
    if period_low <= 0:
        return None
    rise_ratio = (period_high - period_low) / period_low
    if rise_ratio < min_rise:
        return None
    low_idx = lows.index(period_low)     # index 0=최신이므로 큰 idx=과거
    high_idx = highs.index(period_high)
    if low_idx <= high_idx:              # 최저점이 최고점보다 과거여야(바닥→고점)
        return None
    peak_200d = max(highs[:200]) if len(highs) >= 200 else max(highs)
    if peak_200d <= 0:
        return None
    first_target = int(peak_200d * first_level)
    if current > first_target:
        return None
    return {"track": "E", "peak_200d": peak_200d, "rise_ratio": round(rise_ratio, 2),
            "spider_targets": [int(peak_200d * r) for r in (0.48, 0.39, 0.34, 0.30)]}


# ──────────────────────────────────────────────
# Track F: 메가 트렌드 장기 눌림목 (signals.scan_track_f 미러)
# ──────────────────────────────────────────────
def evaluate_track_f(daily: list, current: int, min_surge: float = 0.50,
                     min_vol_ratio: float = 2.0) -> Optional[dict]:
    if not daily or len(daily) < 200:
        return None
    closes = [c["close"] for c in daily]
    volumes = [c["volume"] for c in daily]
    ma150 = int(_ma(closes, 150))
    ma200 = int(_ma(closes, 200))
    if ma150 <= 0 or ma200 <= 0:
        return None
    surge_pct = 0.0
    for i in range(min(60, len(closes) - 20)):
        w_low = min(closes[i:i + 20])
        w_high = max(closes[i:i + 20])
        if w_low > 0:
            surge_pct = max(surge_pct, (w_high - w_low) / w_low)
    if surge_pct < min_surge:
        return None
    avg_vol_200 = sum(volumes[:200]) / 200 if volumes else 0
    avg_vol_recent = sum(volumes[:60]) / 60 if len(volumes) >= 60 else 0
    if avg_vol_200 <= 0:
        return None
    vol_surge_ratio = avg_vol_recent / avg_vol_200
    if vol_surge_ratio < min_vol_ratio:
        return None
    near_ma150 = (ma150 * 0.95 <= current <= ma150 * 1.03)
    near_ma200 = (ma200 * 0.97 <= current <= ma200 * 1.05)
    if not (near_ma150 or near_ma200):
        return None
    return {"track": "F", "ma150": ma150, "ma200": ma200,
            "surge_pct": round(surge_pct, 2), "vol_surge_ratio": round(vol_surge_ratio, 2),
            "ma_target": "150일선" if near_ma150 else "200일선"}


# ──────────────────────────────────────────────
# Track G: CCI & MACD 더블 모멘텀 (signals.scan_track_g 미러)
# ──────────────────────────────────────────────
def evaluate_track_g(daily: list, trade_amount: float,
                     min_trade_amount: float = 50_000_000_000,
                     cci_margin: float = 10.0) -> Optional[dict]:
    if trade_amount < min_trade_amount:
        return None
    if not daily or len(daily) < 60:
        return None
    import pandas as pd
    import numpy as np
    daily_sorted = list(reversed(daily))   # 시간순(오래된→최신)
    df = pd.DataFrame(daily_sorted)
    df = df[df["volume"] > 0].reset_index(drop=True)
    if len(df) < 60:
        return None

    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    tp_ma = typical_price.rolling(window=50).mean()
    # raw=True는 numpy ndarray를 넘기므로 ndarray.abs()가 없어 터진다 → np.abs 사용.
    # (라이브 signals.py에도 같은 버그가 있어 Track G가 매번 예외로 0건이었음)
    tp_md = typical_price.rolling(window=50).apply(
        lambda x: np.abs(x - x.mean()).mean(), raw=True)
    cci = (typical_price - tp_ma) / (0.015 * tp_md)

    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26

    if len(cci) < 2 or pd.isna(cci.iloc[-1]) or pd.isna(cci.iloc[-2]):
        return None
    if pd.isna(macd_line.iloc[-1]) or pd.isna(macd_line.iloc[-2]):
        return None

    cci_t, cci_y = float(cci.iloc[-1]), float(cci.iloc[-2])
    macd_t, macd_y = float(macd_line.iloc[-1]), float(macd_line.iloc[-2])

    if not (cci_y < 0 and cci_t > cci_margin):
        return None
    if not (macd_y < 0 and macd_t > 0):
        return None
    return {"track": "G", "cci_value": round(cci_t, 2), "macd_value": round(macd_t, 2),
            "entry_day_low": int(df["low"].iloc[-1])}


# 트랙 코드 → 평가 함수 매핑 (백테스트 루프에서 사용)
TRACK_EVALUATORS = {
    "B": evaluate_track_b,
    "C": evaluate_track_c,
    "E": evaluate_track_e,
    "F": evaluate_track_f,
    # G는 trade_amount 인자가 달라 백테스트 엔진에서 별도 호출
}
