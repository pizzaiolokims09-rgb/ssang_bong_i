"""
signals.py - Phase 1: Base Screener (1차 통합 조건 검색)
매뉴얼 Section 4 Phase 1 구현

필터링 로직:
  1) 관리종목, 투자경고, 우선주, ETF, ETN, SPAC 제외
  2) 시가총액 1조 5천억 이하
  3) 주가 1,000원 ~ 50,000원
  4) 당일 거래대금 30억 이상
  5) 거래량 100만 주 이상

엔벨로프 발산 스캔 (Track A 연동):
  - Phase 1 통과 종목의 일봉을 조회하여 엔벨로프(20, 12.5) 상단 돌파 여부 태깅
  - 발산 영역 진입 종목은 1분봉 거래량 폭발 감지 → God Mode 즉시 격발
"""
import logging
import re
import time
import requests
from bs4 import BeautifulSoup
from typing import Optional
from datetime import datetime

logger = logging.getLogger("ssangbong.signals")

# ──────────────────────────────────────────────
# 제외 필터 키워드
# ──────────────────────────────────────────────
EXCLUDE_KEYWORDS = [
    "우B", "우C", "1우", "2우", "3우",
    "스팩", "SPAC",
]
EXCLUDE_SUFFIX = ["우", "우B"]

# ETF/ETN은 종목코드 패턴으로도 필터
ETF_CODE_PREFIXES = ["Q", "K"]  # KODEX, KOSEF 등


def _is_excluded_name(name: str) -> bool:
    """종목명 기반 제외 필터"""
    name_upper = name.upper()
    # ETF / ETN 키워드
    if any(kw in name_upper for kw in ["ETF", "ETN", "KODEX", "KOSEF", "TIGER", "KBSTAR", "ARIRANG", "ACE", "SOL"]):
        return True
    # 우선주
    if any(name.endswith(s) for s in EXCLUDE_SUFFIX):
        return True
    # SPAC
    if any(kw in name for kw in EXCLUDE_KEYWORDS):
        return True
    # 관리종목/투자경고 (이름에 포함될 경우)
    if any(kw in name for kw in ["관리", "경고", "정리매매"]):
        return True
    return False


def _is_valid_ticker(ticker: str) -> bool:
    """유효 종목코드 (6자리 숫자)"""
    return bool(re.match(r"^\d{6}$", ticker))


def _get_naver_volume_tickers() -> list:
    """네이버 금융 거래량 상위 티커 크롤링 (코스피+코스닥)"""
    tickers = []
    headers = {'User-Agent': 'Mozilla/5.0'}
    for market in ['0', '1']:  # 0: KOSPI, 1: KOSDAQ
        try:
            url = f"https://finance.naver.com/sise/sise_quant.naver?sosok={market}"
            resp = requests.get(url, headers=headers, timeout=5)
            resp.encoding = 'euc-kr'
            soup = BeautifulSoup(resp.content, 'lxml', from_encoding='euc-kr')
            table = soup.find('table', {'class': 'type_2'})
            if table:
                for a in table.find_all('a', href=re.compile(r'code=\d{6}')):
                    match = re.search(r'code=(\d{6})', a['href'])
                    if match:
                        ticker = match.group(1)
                        name = a.text.strip()
                        tickers.append({"ticker": ticker, "name": name})
        except Exception as e:
            logger.error(f"[Screener] 네이버 크롤링 에러: {e}")
    
    # 중복 제거 (티커 기준 순서 유지)
    seen = set()
    result = []
    for t in tickers:
        if t["ticker"] not in seen:
            seen.add(t["ticker"])
            result.append(t)
    return result


# ──────────────────────────────────────────────
# VWAP (Volume Weighted Average Price) 계산
# ──────────────────────────────────────────────
def calculate_vwap(minute_candles: list) -> float:
    """
    1분봉 데이터로 당일 VWAP을 계산합니다.
    VWAP = Σ(Typical Price × Volume) / Σ(Volume)
    Typical Price = (High + Low + Close) / 3

    minute_candles: [{time, open, high, low, close, volume}, ...] (최신 데이터가 앞)
    반환: VWAP 가격 (float). 데이터 없으면 0.
    """
    if not minute_candles or len(minute_candles) < 5:
        return 0

    cum_pv = 0
    cum_vol = 0

    for c in minute_candles:
        vol = c.get("volume", 0)
        if vol <= 0:
            continue
        typical_price = (c["high"] + c["low"] + c["close"]) / 3
        cum_pv += typical_price * vol
        cum_vol += vol

    return cum_pv / cum_vol if cum_vol > 0 else 0


# ──────────────────────────────────────────────
# ATR (Average True Range) 계산
# ──────────────────────────────────────────────
def calculate_atr(minute_candles: list, period: int = 20) -> float:
    """
    분봉 데이터로 ATR을 계산합니다.
    True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)

    minute_candles: [{time, open, high, low, close, volume}, ...] (최신 데이터가 앞)
    반환: ATR 값 (float). 데이터 부족 시 0.
    """
    if not minute_candles or len(minute_candles) < period + 1:
        return 0

    true_ranges = []
    for i in range(period):
        curr = minute_candles[i]
        prev = minute_candles[i + 1]
        tr = max(
            curr["high"] - curr["low"],
            abs(curr["high"] - prev["close"]),
            abs(curr["low"] - prev["close"]),
        )
        true_ranges.append(tr)

    return sum(true_ranges) / len(true_ranges) if true_ranges else 0


# ──────────────────────────────────────────────
# 엔벨로프 계산 함수 (Period: 20, Percent: 12.5)
# ──────────────────────────────────────────────
def calculate_envelope(daily_candles: list, period: int = 20, percent: float = 12.5) -> dict:
    """
    KIS API 일봉 데이터로 엔벨로프 상/하단선 계산.
    daily_candles: [{date, open, high, low, close, volume}, ...] (최신 데이터가 앞)

    반환: {"ma20": int, "env_upper": int, "env_lower": int, "in_expansion": bool}
    """
    if len(daily_candles) < period:
        return {"ma20": 0, "env_upper": 0, "env_lower": 0, "in_expansion": False}

    closes = [c["close"] for c in daily_candles[:period]]
    ma20 = sum(closes) / period
    env_upper = int(ma20 * (1 + percent / 100.0))
    env_lower = int(ma20 * (1 - percent / 100.0))

    return {
        "ma20": int(ma20),
        "env_upper": env_upper,
        "env_lower": env_lower,
        "in_expansion": False,  # 현재가 비교는 호출측에서 수행
    }


# ──────────────────────────────────────────────
# 5분봉 볼린저밴드(20,2) + EMA(10) 계산 (구조적 MSS 판단용)
# ──────────────────────────────────────────────
def calculate_bb_ema(candles: list, bb_period: int = 20, bb_std: float = 2.0,
                     ema_period: int = 10) -> dict:
    """
    5분봉 캔들 데이터로 볼린저밴드와 EMA를 계산.
    candles: [{time, open, high, low, close, volume}, ...] (최신 데이터가 앞)

    반환: {
        "bb_upper": float, "bb_lower": float, "bb_mid": float,
        "ema": float, "ema_rising": bool
    } 또는 데이터 부족 시 None
    """
    if len(candles) < bb_period:
        return None

    # 시간순 정렬 (오래된 순서대로)
    closes = [c["close"] for c in reversed(candles[:bb_period])]

    # 볼린저밴드 계산
    sma = sum(closes) / len(closes)
    variance = sum((x - sma) ** 2 for x in closes) / len(closes)
    std = variance ** 0.5
    bb_upper = sma + bb_std * std
    bb_lower = sma - bb_std * std

    # EMA(10) 계산 - 지수이동평균
    ema_len = max(bb_period, ema_period)
    if len(candles) < ema_len:
        return None

    ema_closes = [c["close"] for c in reversed(candles[:ema_len])]
    multiplier = 2 / (ema_period + 1)
    ema = ema_closes[0]
    for price in ema_closes[1:]:
        ema = price * multiplier + ema * (1 - multiplier)

    # EMA 방향 판단 (현재 vs 3봉 전 EMA 비교)
    ema_rising = False
    if len(candles) >= ema_period + 3:
        prev_closes = [c["close"] for c in reversed(candles[3:ema_len + 3])]
        if len(prev_closes) >= ema_period:
            prev_ema = prev_closes[0]
            for price in prev_closes[1:]:
                prev_ema = price * multiplier + prev_ema * (1 - multiplier)
            ema_rising = ema > prev_ema

    return {
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "bb_mid": sma,
        "ema": ema,
        "ema_rising": ema_rising,
    }

# ──────────────────────────────────────────────
# SMC 구조 판단 (1분봉 기반 BOS, CHoCH, 유동성)
# ──────────────────────────────────────────────
def evaluate_smc_structure(candles: list) -> dict:
    """
    1분봉 캔들 리스트(최신이 앞)를 받아 SMC 구조를 판단합니다.
    """
    if len(candles) < 20:
        return {"bos": False, "choch": False, "liquidity_swept": False, "raw_data_len": len(candles)}

    import pandas as pd
    from smartmoneyconcepts import smc

    # 1. KIS API 데이터를 시간 역순(과거->현재) DataFrame으로 변환
    reversed_candles = list(reversed(candles))
    df = pd.DataFrame(reversed_candles)

    try:
        # 2. SMC 분석 수행 (데이터가 최대 30봉이므로 swing_length=3 적용)
        swings = smc.swing_highs_lows(df, swing_length=3)
        bos_choch = smc.bos_choch(df, swings)
        liquidity = smc.liquidity(df, swings)

        # 3. 최근 5봉 이내에 Bullish BOS/CHoCH나 유동성 스윕(Liquidity Swept) 확인
        recent_bos = bos_choch.tail(5)
        recent_liq = liquidity.tail(5)

        has_bullish_bos = (recent_bos['BOS'] == 1).any()
        has_bullish_choch = (recent_bos['CHOCH'] == 1).any()

        # 유동성 스윕: 1(Bullish)이거나 Swept 인덱스가 존재하는지
        has_swept_liquidity = recent_liq['Swept'].notna().any()

        # 4. 직전 스윙 로우(pivot_low) 추출 (동적 손절가 산출용)
        pivot_low = 0
        try:
            # swings에서 HighLow 컬럼이 -1인 행(스윙 로우)을 필터링
            swing_lows = swings[swings['HighLow'] == -1]
            if not swing_lows.empty:
                # 가장 최근 스윙 로우의 인덱스를 찾아 해당 봉의 low 가격 추출
                last_swing_low_idx = swing_lows.index[-1]
                pivot_low = int(df.loc[last_swing_low_idx, 'low'])
        except Exception:
            pass  # 스윙 로우 계산 실패 시 0 유지 (fallback 로직 사용)

        return {
            "bos": bool(has_bullish_bos),
            "choch": bool(has_bullish_choch),
            "liquidity_swept": bool(has_swept_liquidity),
            "pivot_low": pivot_low,
            "raw_data_len": len(df)
        }
    except Exception as e:
        logger.error(f"[SMC] 계산 에러: {e}")
        return {"bos": False, "choch": False, "liquidity_swept": False, "pivot_low": 0, "raw_data_len": len(df)}


def aggregate_5m_candles(minute_candles: list) -> list:
    if not minute_candles:
        return []
    
    five_min_candles = []
    reversed_candles = list(reversed(minute_candles))
    current_5m = None
    
    for c in reversed_candles:
        if "time" in c and len(c["time"]) >= 4:
            m = int(c["time"][2:4])
            group_m = (m // 5) * 5
            hour = c["time"][:2]
            group_time = f"{hour}{group_m:02d}00"
        else:
            group_time = "000000"
            
        if current_5m is None or current_5m["time"] != group_time:
            if current_5m is not None:
                five_min_candles.append(current_5m)
            current_5m = {
                "time": group_time,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "volume": c["volume"]
            }
        else:
            current_5m["high"] = max(current_5m["high"], c["high"])
            current_5m["low"] = min(current_5m["low"], c["low"])
            current_5m["close"] = c["close"]
            current_5m["volume"] += c["volume"]
            
    if current_5m is not None:
        five_min_candles.append(current_5m)
        
    return list(reversed(five_min_candles))

def find_5m_pivot_low(five_min_candles: list, lookback: int = 5) -> int:
    if not five_min_candles:
        return 0
    recent_candles = five_min_candles[:lookback]
    return min(c["low"] for c in recent_candles)


# ──────────────────────────────────────────────
# 1분봉 거래량 폭발 감지 (Track A 5분봉 스나이퍼 보조지표)
# ──────────────────────────────────────────────
def detect_volume_spike(candles: list, vol_multiplier: float = 2.0,
                        lookback: int = 20) -> Optional[dict]:
    """
    1분봉 데이터에서 거래량 폭발 + 양봉 출현을 감지.
    (현재는 단독 진입 트리거가 아닌 5분봉 MSS의 보조 지표로만 사용됨)
    candles: [{time, open, high, low, close, volume}, ...] (최신 데이터가 앞)
    조건:
    1) 최신 캔들의 거래량이 직전 lookback봉 평균 대비 vol_multiplier배 이상
    2) 최신 캔들이 양봉 (close > open)
    반환: 감지 시 {"trigger_candle_low": int, "volume_ratio": float, ...}
           미감지 시 None
    """
    if len(candles) < lookback + 1:
        return None

    latest = candles[0]
    body = latest["close"] - latest["open"]

    # 양봉이 아니면 패스
    if body <= 0:
        return None

    latest_vol = latest.get("volume", 0)
    if latest_vol <= 0:
        return None

    # 최소 거래대금 필터 (잡주/빈집 휩소 방지, 1분당 최소 2억)
    min_trade_amount = 200_000_000
    if latest_vol * latest["close"] < min_trade_amount:
        return None

    # 직전 lookback봉 평균 거래량 계산
    prev_vols = [c.get("volume", 0) for c in candles[1:lookback + 1]]
    avg_vol = sum(prev_vols) / len(prev_vols) if prev_vols else 0

    if avg_vol <= 0:
        return None

    ratio = latest_vol / avg_vol

    if ratio >= vol_multiplier:
        logger.info(
            f"[VolSpike] 1분봉 거래량 폭발 감지! "
            f"최신={latest_vol:,} / 평균={avg_vol:,.0f} = {ratio:.1f}배 "
            f"(양봉 O={latest['open']:,} C={latest['close']:,} L={latest['low']:,})"
        )
        return {
            "trigger_candle_low": latest["low"],
            "trigger_candle_close": latest["close"],
            "volume_ratio": round(ratio, 2),
            "latest_volume": latest_vol,
            "avg_volume": int(avg_vol),
        }

    return None



class BaseScreener:
    """
    Phase 1 Base Screener
    KIS 거래량 순위 API로 종목을 가져온 뒤 필터 적용
    + 엔벨로프 발산 종목 태깅
    """

    # 매뉴얼 기준 상수
    MAX_MARKET_CAP = 15000   # 억원 (1조 5천억)
    MIN_PRICE = 1000
    MAX_PRICE = 50000
    MIN_TRADE_AMOUNT = 3_000_000_000   # 30억 (원)
    MIN_VOLUME = 1_000_000             # 100만 주

    def __init__(self, kis_client):
        self.kis = kis_client

    def scan(self) -> list:
        """
        1차 통합 조건 검색 실행
        반환: [{ticker, name, current, volume, trade_amount, market_cap, change_pct}, ...]
        """
        logger.info("[Screener] Phase 1 스캔 시작 (네이버 크롤링 + 실전 KIS API 연동)...")

        tickers = _get_naver_volume_tickers()
        if not tickers:
            logger.warning("[Screener] 크롤링 티커 데이터 없음")
            return []

        logger.info(f"[Screener] 실시간 거래량 상위 티커 획득: {len(tickers)}개")

        candidates = []
        for item in tickers[:150]:  # 상위 150개 스캔 (실전 API라 빠름)
            ticker = item["ticker"]
            name = item["name"]

            if not _is_valid_ticker(ticker):
                continue

            time.sleep(0.05)  # KIS 실전 API 초당 20건 제한 준수
            quote = self.kis.get_quote(ticker)
            
            if not quote or quote.get("current", 0) == 0:
                continue

            current = quote.get("current", 0)
            volume  = quote.get("volume", 0)
            trade_amount = quote.get("trade_amount", 0)
            market_cap   = quote.get("market_cap", 0)

            # 필터 2: 종목명 제외 필터
            if _is_excluded_name(name):
                continue
                
            # 당일 상장주 필터 (차트 데이터 부족 방지)
            listing_date = quote.get("listing_date", "")
            today_str = datetime.now().strftime("%Y%m%d")
            if listing_date == today_str:
                logger.info(f"[Screener] 당일 상장주 스캔 제외: {name}({ticker})")
                continue

            # 필터 3: 가격대
            if not (self.MIN_PRICE <= current <= self.MAX_PRICE):
                continue

            # 필터 4: 시가총액 (억원 단위)
            if market_cap > self.MAX_MARKET_CAP and market_cap > 0:
                continue

            # 필터 5: 거래대금 30억 이상
            if trade_amount < self.MIN_TRADE_AMOUNT:
                continue

            # 필터 6: 거래량 100만 주 이상
            if volume < self.MIN_VOLUME:
                continue

            candidates.append({
                "ticker":       ticker,
                "name":         name,
                "current":      current,
                "volume":       volume,
                "trade_amount": trade_amount,
                "market_cap":   market_cap,
                "change_pct":   quote.get("change_pct", 0),
            })

        # 거래대금 순 정렬 (주도주 우선)
        candidates.sort(key=lambda x: -x["trade_amount"])

        logger.info(f"[Screener] Phase 1 통과: {len(candidates)}개 종목")
        for c in candidates[:5]:
            logger.info(f"  -> {c['name']}({c['ticker']}) "
                       f"현재가={c['current']:,} 거래대금={c['trade_amount']//100_000_000}억 "
                       f"등락={c['change_pct']:+.2f}%")

        return candidates


    def scan_track_a_pullback(self, candidates: list) -> list:
        pullback_stocks = []

        for stock in candidates:
            ticker = stock["ticker"]
            current = stock["current"]

            import time
            time.sleep(0.05)
            daily_candles = self.kis.get_daily_chart(ticker)

            # API 응답 실패 방어 (None 또는 빈 리스트)
            if not daily_candles or len(daily_candles) < 20:
                continue

            # 당일 상장주 및 거래 정지 이력 종목 필터
            if sum(1 for c in daily_candles[:20] if c["volume"] > 0) < 20:
                continue

            if len(daily_candles) < 2:
                continue

            today = daily_candles[0]
            yesterday = daily_candles[1]
            yesterday_high = yesterday["high"]

            if yesterday_high <= 0:
                continue

            # 조건 1: 오늘 고가가 전일 고가를 상향 돌파했었는가?
            if today["high"] < yesterday_high * 1.01:
                continue
                
            # 조건 1.5: 당일 고점 대비 너무 많이 하락한 '투매' 종목은 피함 (최대 -8%까지만 허용)
            if current < today["high"] * 0.92:
                continue

            # 조건 1.6: 상한가(+25% 이상) 종목 원천 차단 (매수 즉시 매도되는 낭비 방지)
            if stock.get("change_pct", 0) >= 25.0:
                logger.info(
                    f"  🚫 [Track A] {stock['name']}({ticker}) 상한가 차단! "
                    f"등락률={stock['change_pct']:+.1f}% -> 진입 불가"
                )
                continue

            # 조건 1.7: 당일 시가 대비 이미 +15% 이상 급등한 종목은 추격 매수 금지
            if today["open"] > 0 and current > today["open"] * 1.15:
                logger.info(
                    f"  🚫 [Track A] {stock['name']}({ticker}) 추격 매수 방지! "
                    f"시가={today['open']:,} 현재가={current:,} "
                    f"(시가 대비 +{((current/today['open'])-1)*100:.1f}%)"
                )
                continue

            # 조건 2: 현재가가 전일 고가 부근으로 눌렸는가? (-2% ~ +3% 범위)
            lower_bound = yesterday_high * 0.98
            upper_bound = yesterday_high * 1.03
            
            if lower_bound <= current <= upper_bound:
                stock_with_bos = stock.copy()
                stock_with_bos["yesterday_high"] = yesterday_high
                pullback_stocks.append(stock_with_bos)

                logger.info(
                    f"  🎯 [BOS Pullback] {stock['name']}({ticker}) 전일 고가 부근 눌림! "
                    f"현재가={current:,} / 전일고가={yesterday_high:,}"
                )

        logger.info(f"[Track A] BOS 눌림목 후보: {len(pullback_stocks)}개")
        return pullback_stocks


    # ──────────────────────────────────────────────
    # Track B 전용: 눌림목 단기 스윙 스캔
    # ──────────────────────────────────────────────
    def scan_track_b(self, candidates: list) -> list:
        """
        영웅문 '눌림목 단기 스윙' 검색기 기준:
        1) 거래량 급감: 당일 거래량 <= 전일 거래량의 50%
        2) 연속 음봉: 최근 1봉 이상 close < open
        3) MA20 근접: 현재가가 MA20의 120% 이내 && 현재가 > MA20
        4) 정배열: MA5 >= MA20 >= MA60

        반환: 조건 충족 종목 리스트
        """
        results = []
        for stock in candidates:
            ticker = stock["ticker"]
            time.sleep(0.05)
            daily = self.kis.get_daily_chart(ticker)
            if not daily:
                continue

            if len(daily) < 60:
                continue

            closes = [c["close"] for c in daily]
            volumes = [c["volume"] for c in daily]

            # 이동평균 계산
            ma5  = sum(closes[:5]) / 5
            ma20 = sum(closes[:20]) / 20
            ma60 = sum(closes[:60]) / 60
            current = stock["current"]

            # 조건 1: 거래량 급감 (당일 <= 전일의 50%)
            if volumes[1] <= 0:
                continue
            vol_ratio = volumes[0] / volumes[1]
            if vol_ratio > 0.50:
                continue

            # 조건 2: 최근 1봉 이상 음봉
            if daily[0]["close"] >= daily[0]["open"]:
                continue

            # 조건 3: 현재가 > MA20 && 현재가 <= MA20 * 1.20 (20% 이내 근접)
            if current <= ma20 or current > ma20 * 1.20:
                continue

            # 조건 4: 정배열 (MA5 >= MA20 >= MA60)
            if not (ma5 >= ma20 >= ma60):
                continue

            stock_b = stock.copy()
            stock_b["track_hint"] = "B"
            stock_b["ma5"] = int(ma5)
            stock_b["ma20"] = int(ma20)
            stock_b["ma60"] = int(ma60)
            stock_b["vol_ratio"] = round(vol_ratio, 3)
            results.append(stock_b)

            logger.info(
                f"  📉 [Track B] {stock['name']}({ticker}) 눌림목 감지 "
                f"거래량={vol_ratio:.1%} MA정배열({int(ma5)}≥{int(ma20)}≥{int(ma60)})"
            )

        logger.info(f"[Track B] 눌림목 스윙 후보: {len(results)}개")
        return results

    # ──────────────────────────────────────────────
    # Track C 전용: ABC 수급 종가 베팅 스캔
    # ──────────────────────────────────────────────
    def scan_track_c(self, candidates: list) -> list:
        """
        영웅문 'ABC 수급 종가 배팅' 검색기 기준:
        1) 거래량 70만주+ (Phase 1 100만 기준이므로 자동 충족)
        2) 체결강도 40% 이상
        3) 최근 5봉 내 전일 대비 거래량 50%+ 증가 이력 1회 이상

        반환: 조건 충족 종목 리스트
        """
        results = []
        for stock in candidates:
            ticker = stock["ticker"]

            # 체결강도 조회 (실전 API)
            time.sleep(0.05)
            quote = self.kis.get_quote(ticker)
            exec_strength = quote.get("execution_strength", 0)

            # 조건 2: 체결강도 40% 이상
            if exec_strength < 40.0:
                continue

            # 일봉 데이터로 거래량 급증 이력 확인
            time.sleep(0.05)
            daily = self.kis.get_daily_chart(ticker)
            if not daily or len(daily) < 6:
                continue

            # 조건 3: 최근 5봉 내 전일 대비 거래량 50%+ 증가 이력
            vol_surge_found = False
            for i in range(min(5, len(daily) - 1)):
                prev_vol = daily[i + 1]["volume"]
                if prev_vol > 0 and daily[i]["volume"] / prev_vol >= 1.5:
                    vol_surge_found = True
                    break

            if not vol_surge_found:
                continue

            stock_c = stock.copy()
            stock_c["track_hint"] = "C"
            stock_c["execution_strength"] = exec_strength
            results.append(stock_c)

            logger.info(
                f"  🌇 [Track C] {stock['name']}({ticker}) 종가 베팅 후보 "
                f"체결강도={exec_strength:.1f}%"
            )

        logger.info(f"[Track C] 종가 베팅 후보: {len(results)}개")
        return results

    # ──────────────────────────────────────────────
    # Track D 전용: 세력주 매집 스캔
    # ──────────────────────────────────────────────
    def scan_track_d(self, candidates: list) -> list:
        """
        세력주 매집 검색기 (완화 기준):
        1) PER >= 1 (적자 기업 제외)
        2) 현재가가 MA20(20일선) 이하 (바닥권/조정구간 진입 종목)
        3) 유보율 >= 100% (재무 안정성)

        반환: 조건 충족 종목 리스트
        """
        results = []
        for stock in candidates:
            ticker = stock["ticker"]

            # PER 체크 (실전 API get_quote에서 조회)
            time.sleep(0.05)
            quote = self.kis.get_quote(ticker)
            per = quote.get("per", 0)

            # 조건 1: PER >= 1 (적자/무실적 제외)
            if per < 1.0:
                continue

            # 일봉 데이터로 MA20 체크
            time.sleep(0.05)
            daily = self.kis.get_daily_chart(ticker)
            if not daily or len(daily) < 20:
                continue

            closes = [c["close"] for c in daily]
            ma20 = sum(closes[:20]) / 20
            current = stock["current"]

            # 조건 2: 현재가가 MA20 이하 (바닥권/조정 구간)
            if current > ma20:
                continue

            # 조건 3: 유보율 100% 이상 (재무 안정성 최소 기준)
            time.sleep(0.1)
            fin = self.kis.get_financial_summary(ticker)
            retention = fin.get("retention_ratio", 0)

            if retention < 100:
                continue

            stock_d = stock.copy()
            stock_d["track_hint"] = "D"
            stock_d["per"] = per
            stock_d["retention_ratio"] = retention
            stock_d["ma20"] = int(ma20)
            results.append(stock_d)

            logger.info(
                f"  🏦 [Track D] {stock['name']}({ticker}) 세력주 매집 후보 "
                f"PER={per:.1f} 유보율={retention:.0f}% "
                f"현재가={current:,} MA20={int(ma20):,}"
            )

        logger.info(f"[Track D] 세력주 매집 후보: {len(results)}개")
        return results

    # ──────────────────────────────────────────────
    # Track E 전용: 폭락주 스나이핑 스캔 (Pandas 데이터프레임 연산)
    # ──────────────────────────────────────────────
    def scan_track_e(self, candidates: list) -> list:
        """
        직장인 치트키 '폭락주 매매기법' 100% 수식 기반:
        1) 데이터 250거래일 미만 신규 상장주 원천 제외
        2) 대시세 조건: 최근 바닥 대비 300%+ 상승 이력 필수
        3) 200일 최고가 산출 (rolling max)
        4) 현재가 <= peak_200d * 0.48 (1차 매수선 이하)
        5) 4단계 거미줄 매수 타점 계산

        반환: [{..., "peak_200d": int, "spider_targets": [int,int,int,int]}, ...]
        """
        results = []
        for stock in candidates:
            ticker = stock["ticker"]

            # 300일 일봉 조회 (실전 API)
            time.sleep(0.05)
            daily = self.kis.get_daily_chart(ticker, days=400)
            if not daily:
                continue

            # ── 조건 1: 데이터 250거래일 미만 → 신규 상장주 제외 ──
            if len(daily) < 250:
                continue

            closes = [c["close"] for c in daily]
            highs  = [c["high"] for c in daily]
            lows   = [c["low"] for c in daily]

            # ── 조건 2: 대시세 조건 (바닥 대비 300%+ 상승 이력) ──
            # 전체 기간에서 최저점과 최고점을 찾아 최저→최고 상승률 계산
            period_low  = min(lows)
            period_high = max(highs)

            if period_low <= 0:
                continue

            # 최저점 대비 최고점 상승률 (300% = 4배)
            rise_ratio = (period_high - period_low) / period_low
            if rise_ratio < 3.0:  # 300% 미만이면 대시세 아님
                continue

            # 추가 검증: 최저점이 최고점보다 먼저 나와야 함 (바닥→고점 순서)
            low_idx  = lows.index(period_low)    # daily[0]이 최신이므로 큰 idx = 과거
            high_idx = highs.index(period_high)
            if low_idx <= high_idx:
                # 최저점이 최고점보다 최근 = 이미 하락만 한 종목 (대시세 아님)
                continue

            # ── 조건 3: 200일 최고가 산출 (rolling max) ──
            peak_200d = max(highs[:200]) if len(highs) >= 200 else max(highs)
            if peak_200d <= 0:
                continue

            current = stock["current"]

            # ── 조건 4: 현재가 <= 1차 매수선 (peak * 0.48) ──
            first_target = int(peak_200d * 0.48)
            if current > first_target:
                continue

            # ── 4단계 거미줄 매수 타점 계산 ──
            spider_targets = [
                int(peak_200d * 0.48),
                int(peak_200d * 0.39),
                int(peak_200d * 0.34),
                int(peak_200d * 0.30),
            ]

            stock_e = stock.copy()
            stock_e["track_hint"] = "E"
            stock_e["peak_200d"] = peak_200d
            stock_e["spider_targets"] = spider_targets
            stock_e["rise_ratio"] = round(rise_ratio, 2)
            results.append(stock_e)

            logger.info(
                f"  💥 [Track E] {stock['name']}({ticker}) 폭락주 후보 "
                f"대시세={rise_ratio:.0%} peak={peak_200d:,} 현재={current:,} "
                f"타점={[f'{t:,}' for t in spider_targets]}"
            )

        logger.info(f"[Track E] 폭락주 스나이핑 후보: {len(results)}개")
        return results

    # ──────────────────────────────────────────────
    # Track F 전용: 메가 트렌드 장기 눌림목 스윙 (150/200MA Swing)
    # ──────────────────────────────────────────────
    def scan_track_f(self, candidates: list) -> list:
        """
        메가 트렌드 150/200일선 장기 눌림목 스캔:
        1) 데이터 200거래일 이상 (상장 1년+ 종목만)
        2) 시세 분출 조건: 최근 60거래일 내 50%+ 급등 구간 존재
        3) 역사적 거래량: 시세 분출 기간 평균 거래량이 평소(200일 평균) 대비 2배+
        4) 현재가가 150일선 부근(-5% ~ +3%) 또는 200일선 부근(-3% ~ +5%)에 위치

        반환: [{..., "ma150": int, "ma200": int, "surge_pct": float}, ...]
        """
        results = []
        for stock in candidates:
            ticker = stock["ticker"]

            time.sleep(0.05)
            daily = self.kis.get_daily_chart(ticker, days=300)
            if not daily:
                continue

            # 조건 1: 200거래일 이상 데이터 필수
            if len(daily) < 200:
                continue

            closes = [c["close"] for c in daily]
            volumes = [c["volume"] for c in daily]

            # 150일/200일 이동평균 계산
            ma150 = int(sum(closes[:150]) / 150)
            ma200 = int(sum(closes[:200]) / 200)

            if ma150 <= 0 or ma200 <= 0:
                continue

            current = stock["current"]

            # 조건 2: 시세 분출 검증 - 최근 60거래일 내 50%+ 급등 구간이 있었는가
            surge_pct = 0.0
            for i in range(min(60, len(closes) - 20)):
                window_low = min(closes[i:i+20])
                window_high = max(closes[i:i+20])
                if window_low > 0:
                    local_surge = (window_high - window_low) / window_low
                    surge_pct = max(surge_pct, local_surge)

            if surge_pct < 0.50:  # 50% 급등 이력 없으면 제외
                continue

            # 조건 3: 역사적 거래량 - 시세 분출 기간 거래량이 평소의 2배+
            avg_vol_200 = sum(volumes[:200]) / 200 if volumes else 0
            avg_vol_recent = sum(volumes[:60]) / 60 if len(volumes) >= 60 else 0

            if avg_vol_200 <= 0:
                continue

            vol_surge_ratio = avg_vol_recent / avg_vol_200
            if vol_surge_ratio < 2.0:
                continue

            # 조건 4: 현재가가 150일선 or 200일선 부근인가?
            near_ma150 = (ma150 * 0.95 <= current <= ma150 * 1.03)
            near_ma200 = (ma200 * 0.97 <= current <= ma200 * 1.05)

            if not (near_ma150 or near_ma200):
                continue

            ma_target = "150일선" if near_ma150 else "200일선"

            stock_f = stock.copy()
            stock_f["track_hint"] = "F"
            stock_f["ma150"] = ma150
            stock_f["ma200"] = ma200
            stock_f["surge_pct"] = round(surge_pct, 2)
            stock_f["vol_surge_ratio"] = round(vol_surge_ratio, 2)
            stock_f["ma_target"] = ma_target
            results.append(stock_f)

            logger.info(
                f"  🌊 [Track F] {stock['name']}({ticker}) 메가 트렌드 눌림목 후보 "
                f"시세분출={surge_pct:.0%} 거래량배율={vol_surge_ratio:.1f}x "
                f"{ma_target} 근접 (MA150={ma150:,} MA200={ma200:,} 현재={current:,})"
            )

        logger.info(f"[Track F] 메가 트렌드 눌림목 후보: {len(results)}개")
        return results
