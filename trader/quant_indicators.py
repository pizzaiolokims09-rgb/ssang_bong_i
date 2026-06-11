"""
quant_indicators.py - Wilder's Smoothing 기반 정통 퀀트 지표 엔진
TradingView / Investing.com과 동일한 수치를 산출하는 정통 기술적 분석 모듈

구현 지표:
  - RSI (Wilder's Smoothing, period=14)
  - ATR (Wilder's Smoothing, period=14)
  - ADX + DI+/DI- (Wilder's Smoothing, period=14)
  - MACD (EMA 12/26/9)
  - 4대 매도 트리거 통합 판정 (get_sell_signals)

핵심 원칙:
  1) pandas EMA 사용 금지 → Wilder's Smoothing 직접 구현
  2) 주말/공휴일 결측치(거래량 0 봉) 자동 제거
  3) 데이터 부족 시 안전하게 기본값 반환 (Fail-Safe)
"""
import logging
from typing import Optional

logger = logging.getLogger("ssangbong.quant")


# ──────────────────────────────────────────────
# 유틸: 결측치(거래량 0) 필터링
# ──────────────────────────────────────────────
def _filter_zero_volume(candles: list) -> list:
    """
    거래량이 0인 봉(주말/공휴일 ffill 노이즈)을 제거합니다.
    candles: [{open, high, low, close, volume, ...}, ...] (최신이 앞)
    """
    return [c for c in candles if c.get("volume", 0) > 0]


def _extract_closes(candles: list) -> list:
    """캔들 리스트에서 종가만 추출 (시간순: 오래된 → 최신)"""
    return [c["close"] for c in reversed(candles)]


def _extract_hlc(candles: list) -> tuple:
    """캔들 리스트에서 고/저/종가 추출 (시간순: 오래된 → 최신)"""
    highs = [c["high"] for c in reversed(candles)]
    lows = [c["low"] for c in reversed(candles)]
    closes = [c["close"] for c in reversed(candles)]
    return highs, lows, closes


# ──────────────────────────────────────────────
# RSI (Wilder's Smoothing)
# ──────────────────────────────────────────────
def wilder_rsi(candles: list, period: int = 14) -> float:
    """
    Wilder's Smoothing 기반 RSI 계산.
    TradingView RSI(14)와 동일한 수치를 산출합니다.

    Wilder's Smoothing:
      avg_gain = prev_avg_gain * (period-1)/period + current_gain/period
      avg_loss = prev_avg_loss * (period-1)/period + current_loss/period
      RS = avg_gain / avg_loss
      RSI = 100 - 100/(1+RS)

    candles: 최신이 앞. 최소 period+1개 필요.
    반환: RSI 값 (0~100). 데이터 부족 시 50 (중립).
    """
    filtered = _filter_zero_volume(candles)
    if len(filtered) < period + 1:
        return 50.0

    closes = _extract_closes(filtered)

    # 가격 변동 계산
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    if len(deltas) < period:
        return 50.0

    # 첫 period개의 평균 이득/손실 (SMA 시드)
    gains = [max(d, 0) for d in deltas[:period]]
    losses = [abs(min(d, 0)) for d in deltas[:period]]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    # Wilder's Smoothing 적용 (나머지 데이터)
    for d in deltas[period:]:
        current_gain = max(d, 0)
        current_loss = abs(min(d, 0))
        avg_gain = (avg_gain * (period - 1) + current_gain) / period
        avg_loss = (avg_loss * (period - 1) + current_loss) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))

    return round(rsi, 2)


# ──────────────────────────────────────────────
# ATR (Wilder's Smoothing)
# ──────────────────────────────────────────────
def wilder_atr(candles: list, period: int = 14) -> float:
    """
    Wilder's Smoothing 기반 ATR 계산.
    TradingView ATR(14)과 동일한 수치를 산출합니다.

    True Range = max(H-L, |H-prevC|, |L-prevC|)
    ATR = Wilder's Smoothing of TR

    candles: 최신이 앞. 최소 period+1개 필요.
    반환: ATR 값. 데이터 부족 시 0.
    """
    filtered = _filter_zero_volume(candles)
    if len(filtered) < period + 1:
        return 0.0

    highs, lows, closes = _extract_hlc(filtered)

    # True Range 계산 (인덱스 1부터, 이전 종가 필요)
    true_ranges = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return 0.0

    # 첫 period개의 단순 평균 (SMA 시드)
    atr = sum(true_ranges[:period]) / period

    # Wilder's Smoothing 적용
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period

    return round(atr, 2)


# ──────────────────────────────────────────────
# ADX + DI+/DI- (Wilder's Smoothing)
# ──────────────────────────────────────────────
def wilder_adx(candles: list, period: int = 14) -> dict:
    """
    Wilder's Smoothing 기반 ADX, +DI, -DI 계산.

    +DM = max(H[i]-H[i-1], 0) if > max(L[i-1]-L[i], 0) else 0
    -DM = max(L[i-1]-L[i], 0) if > max(H[i]-H[i-1], 0) else 0
    +DI = 100 * smoothed(+DM) / smoothed(TR)
    -DI = 100 * smoothed(-DM) / smoothed(TR)
    DX  = 100 * |+DI - -DI| / (+DI + -DI)
    ADX = Wilder's Smoothing of DX

    candles: 최신이 앞. 최소 2*period+1개 필요 (ADX에 DX 시드 필요).
    반환: {"adx": float, "plus_di": float, "minus_di": float}
           데이터 부족 시 {"adx": 0, "plus_di": 0, "minus_di": 0}
    """
    default = {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    filtered = _filter_zero_volume(candles)

    # ADX 계산에는 최소 2*period+1개 데이터 필요
    if len(filtered) < 2 * period + 1:
        return default

    highs, lows, closes = _extract_hlc(filtered)

    # +DM, -DM, TR 계산
    plus_dm_list = []
    minus_dm_list = []
    tr_list = []

    for i in range(1, len(closes)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]

        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0

        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
        tr_list.append(tr)

    if len(tr_list) < 2 * period:
        return default

    # Wilder's Smoothing 시드 (첫 period개 SMA)
    smooth_plus_dm = sum(plus_dm_list[:period]) / period
    smooth_minus_dm = sum(minus_dm_list[:period]) / period
    smooth_tr = sum(tr_list[:period]) / period

    # DX 리스트 수집 (ADX 시드용)
    dx_list = []

    # 첫 번째 DI/DX 계산
    if smooth_tr > 0:
        plus_di = 100 * smooth_plus_dm / smooth_tr
        minus_di = 100 * smooth_minus_dm / smooth_tr
    else:
        plus_di = 0
        minus_di = 0

    di_sum = plus_di + minus_di
    dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0
    dx_list.append(dx)

    # Wilder's Smoothing 진행 (period 이후 데이터)
    for i in range(period, len(tr_list)):
        smooth_plus_dm = (smooth_plus_dm * (period - 1) + plus_dm_list[i]) / period
        smooth_minus_dm = (smooth_minus_dm * (period - 1) + minus_dm_list[i]) / period
        smooth_tr = (smooth_tr * (period - 1) + tr_list[i]) / period

        if smooth_tr > 0:
            plus_di = 100 * smooth_plus_dm / smooth_tr
            minus_di = 100 * smooth_minus_dm / smooth_tr
        else:
            plus_di = 0
            minus_di = 0

        di_sum = plus_di + minus_di
        dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0
        dx_list.append(dx)

    if len(dx_list) < period:
        return default

    # ADX = DX의 Wilder's Smoothing
    adx = sum(dx_list[:period]) / period
    for d in dx_list[period:]:
        adx = (adx * (period - 1) + d) / period

    return {
        "adx": round(adx, 2),
        "plus_di": round(plus_di, 2),
        "minus_di": round(minus_di, 2),
    }


# ──────────────────────────────────────────────
# MACD (표준 EMA 12/26/9)
# ──────────────────────────────────────────────
def calc_macd(candles: list, fast: int = 12, slow: int = 26,
              signal: int = 9) -> dict:
    """
    표준 MACD 계산.
    MACD Line = EMA(fast) - EMA(slow)
    Signal Line = EMA(signal) of MACD Line
    Histogram = MACD - Signal

    candles: 최신이 앞. 최소 slow+signal개 필요.
    반환: {"macd": float, "signal": float, "histogram": float}
           데이터 부족 시 {"macd": 0, "signal": 0, "histogram": 0}
    """
    default = {"macd": 0.0, "signal": 0.0, "histogram": 0.0}
    filtered = _filter_zero_volume(candles)

    if len(filtered) < slow + signal:
        return default

    closes = _extract_closes(filtered)

    # EMA 계산 헬퍼
    def ema(data: list, period: int) -> list:
        multiplier = 2.0 / (period + 1)
        result = [data[0]]
        for price in data[1:]:
            result.append(price * multiplier + result[-1] * (1 - multiplier))
        return result

    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)

    # MACD Line
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]

    # Signal Line (MACD의 EMA)
    signal_line = ema(macd_line, signal)

    # Histogram
    histogram = [m - s for m, s in zip(macd_line, signal_line)]

    return {
        "macd": round(macd_line[-1], 2),
        "signal": round(signal_line[-1], 2),
        "histogram": round(histogram[-1], 2),
    }


# ──────────────────────────────────────────────
# 이동평균 (데드크로스 판정용)
# ──────────────────────────────────────────────
def calc_ma(candles: list, period: int) -> float:
    """단순이동평균(SMA) 계산. 데이터 부족 시 0."""
    filtered = _filter_zero_volume(candles)
    if len(filtered) < period:
        return 0.0
    closes = [c["close"] for c in filtered[:period]]
    return sum(closes) / period


# ──────────────────────────────────────────────
# 4대 매도 트리거 통합 판정
# ──────────────────────────────────────────────
def get_sell_signals(minute_candles: list, daily_candles: list) -> dict:
    """
    4대 매도 트리거를 종합 판정하여 반환합니다.

    1) 패닉셀: 직전 5봉 평균 대비 3배+ 매도 폭발 + 음봉 (분봉 기준)
    2) MACD 매도: MACD 히스토그램이 양 → 음 전환 (일봉 기준, 직전 2봉 비교)
       ※ KIS 분봉 API는 30봉만 반환해 MACD(12,26,9)에 필요한 35봉을
         확보할 수 없으므로 반드시 일봉으로 계산한다.
    3) 데드크로스: MA5 < MA20 (일봉 기준)
    4) RSI 과매수: RSI > 70

    minute_candles: 분봉 데이터 (최신이 앞, 패닉셀용)
    daily_candles: 일봉 데이터 (최신이 앞, MACD + 데드크로스 + RSI + ADX용)

    반환: {
        "panic_sell": bool,
        "macd_bearish": bool,
        "dead_cross": bool,
        "rsi_overbought": bool,
        "rsi": float,
        "adx": float,
        "plus_di": float,
        "minus_di": float,
        "macd_histogram": float,
    }
    """
    result = {
        "panic_sell": False,
        "macd_bearish": False,
        "dead_cross": False,
        "rsi_overbought": False,
        "rsi": 50.0,
        "adx": 0.0,
        "plus_di": 0.0,
        "minus_di": 0.0,
        "macd_histogram": 0.0,
    }

    # ── 1) 패닉셀 감지 (분봉 기준) ──
    if minute_candles and len(minute_candles) >= 6:
        latest = minute_candles[0]
        prev_vols = [c.get("volume", 0) for c in minute_candles[1:6]]
        avg_vol = sum(prev_vols) / len(prev_vols) if prev_vols else 0

        if avg_vol > 0:
            vol_ratio = latest.get("volume", 0) / avg_vol
            is_bearish = latest["close"] < latest["open"]
            if vol_ratio >= 3.0 and is_bearish:
                result["panic_sell"] = True
                logger.warning(
                    f"[Quant] 패닉셀 감지! 거래량={vol_ratio:.1f}배 + 음봉"
                )

    # ── 2) MACD 매도 시그널 (일봉 기준) ──
    # 분봉은 KIS API 한계로 30봉뿐이라 MACD(12,26,9) 계산이 항상 불가능했음
    # → 일봉 히스토그램의 양 → 음 전환으로 추세 이탈을 감지한다.
    if daily_candles and len(daily_candles) >= 36:
        macd_data = calc_macd(daily_candles)
        result["macd_histogram"] = macd_data["histogram"]

        # 직전 봉 대비 히스토그램 전환 감지 (전일 양수 → 당일 음수)
        prev_macd = calc_macd(daily_candles[1:])
        if prev_macd["histogram"] > 0 and macd_data["histogram"] < 0:
            result["macd_bearish"] = True
            logger.info(
                f"[Quant] MACD 매도 시그널 (일봉): "
                f"전일 Hist={prev_macd['histogram']:.2f} → "
                f"당일 Hist={macd_data['histogram']:.2f}"
            )

    # ── 3) 데드크로스 (일봉 기준) ──
    if daily_candles and len(daily_candles) >= 20:
        ma5 = calc_ma(daily_candles, 5)
        ma20 = calc_ma(daily_candles, 20)

        if ma5 > 0 and ma20 > 0 and ma5 < ma20:
            result["dead_cross"] = True

    # ── 4) RSI 과매수 (일봉 기준) ──
    if daily_candles and len(daily_candles) >= 15:
        rsi = wilder_rsi(daily_candles, 14)
        result["rsi"] = rsi
        if rsi > 70:
            result["rsi_overbought"] = True

    # ── ADX (일봉 기준, 휩소 필터용) ──
    if daily_candles and len(daily_candles) >= 30:
        adx_data = wilder_adx(daily_candles, 14)
        result["adx"] = adx_data["adx"]
        result["plus_di"] = adx_data["plus_di"]
        result["minus_di"] = adx_data["minus_di"]

    return result

# ──────────────────────────────────────────────
# 머신러닝 피처 추출 (ML Feature Extraction)
# ──────────────────────────────────────────────
def get_ml_features(daily_candles: list, minute_candles: list) -> dict:
    """
    ML 모델 학습 및 예측을 위한 정량적 피처 추출
    
    피처 목록:
      - vol_ratio: 최근 20일 평균 거래량 대비 당일 거래량 배수
      - env_diff: 당일 종가와 20일선(엔벨롭 중심선) 간의 이격도 (%)
      - bb_width: 볼린저 밴드 폭 (Upper - Lower) / Middle * 100
      - rsi: RSI(14)
      - macd: MACD 히스토그램
      - adx: ADX(14)
      - atr: ATR(14)
    """
    default = {
        "vol_ratio": 0.0,
        "env_diff": 0.0,
        "bb_width": 0.0,
        "rsi": 50.0,
        "macd": 0.0,
        "adx": 0.0,
        "atr": 0.0
    }
    
    filtered_daily = _filter_zero_volume(daily_candles)
    if not filtered_daily or len(filtered_daily) < 20:
        return default
        
    latest = filtered_daily[0]
    current_close = latest["close"]
    current_vol = latest.get("volume", 0)
    
    # 1. vol_ratio (거래량 배수)
    vols_20 = [c.get("volume", 0) for c in filtered_daily[:20]]
    avg_vol_20 = sum(vols_20) / len(vols_20) if vols_20 else 0
    vol_ratio = (current_vol / avg_vol_20) if avg_vol_20 > 0 else 0.0
    
    # 2. env_diff (20일 이평선 이격도)
    ma20 = calc_ma(filtered_daily, 20)
    env_diff = ((current_close - ma20) / ma20 * 100.0) if ma20 > 0 else 0.0
    
    # 3. bb_width (볼린저 밴드 폭)
    # 표준편차 계산
    if ma20 > 0 and len(filtered_daily) >= 20:
        import math
        closes_20 = [c["close"] for c in filtered_daily[:20]]
        variance = sum((c - ma20) ** 2 for c in closes_20) / 20
        std_dev = math.sqrt(variance)
        upper_bb = ma20 + (2 * std_dev)
        lower_bb = ma20 - (2 * std_dev)
        bb_width = ((upper_bb - lower_bb) / ma20 * 100.0)
    else:
        bb_width = 0.0
        
    # 4. rsi
    rsi = wilder_rsi(filtered_daily, 14)
    
    # 5. macd histogram
    macd_data = calc_macd(filtered_daily)
    
    # 6. adx
    adx_data = wilder_adx(filtered_daily, 14)
    
    # 7. atr
    atr = wilder_atr(filtered_daily, 14)
    
    return {
        "vol_ratio": round(vol_ratio, 2),
        "env_diff": round(env_diff, 2),
        "bb_width": round(bb_width, 2),
        "rsi": rsi,
        "macd": macd_data["histogram"],
        "adx": adx_data["adx"],
        "atr": atr
    }
