"""
ai_router.py - Phase 2: AI Dynamic Routing (상황별 기법 자동 전환)
매뉴얼 Section 4 Phase 2 구현

Gemini AI가 1차 통과 종목의 분봉/일봉/호가 데이터를 분석하여
6가지 트랙(A~F) 중 하나로 라우팅:

  Track A: 데이트레이딩 & 상한가 따라잡기 (D+0 단타)
  Track B: 눌림목 단기 스윙
  Track C: ABC 수급 종가 베팅 (D+1)
  Track D: 세력주 매집 (중장기 스윙)
  Track F: 메가 트렌드 장기 눌림목 스윙 (150/200MA)
"""
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Optional

import requests
from dotenv import load_dotenv

from trader.signals import evaluate_smc_structure
from trader.quant_indicators import wilder_atr
from trader import ai_budget

load_dotenv()
logger = logging.getLogger("ssangbong.ai_router")


# ──────────────────────────────────────────────
# Track 정의
# ──────────────────────────────────────────────
TRACKS = {
    "A": {
        "name": "엔벨로프 발산 스나이핑",
        "emoji": "🚀",
        "sl_pct": 0.03,    # 폴백용 고정 손절 (기본은 꼬리 손절)
        "sl_type": "candle_low",  # Track A 전용: 트리거 캔들 저점 손절
        "tp_pct": 0.07,    # +5~7% 익절
        "order_type": "market",   # God Mode 시장가 즉시
        "hold_days": 0,
    },
    "B": {
        "name": "눌림목 단기 스윙",
        "emoji": "📉",
        "sl_pct": 0.05,    # -5% 손절
        "tp_pct": 0.15,    # +15% 익절
        "order_type": "limit",    # 지정가 분할
        "hold_days": 3,
    },
    "C": {
        "name": "ABC 수급 종가 베팅",
        "emoji": "🌇",
        "sl_pct": 0.05,    # -5% 손절
        "tp_pct": 0.05,    # +5% 이상 익절 (트레일링 연동)
        "order_type": "market",
        "hold_days": 1,
    },
    "D": {
        "name": "세력주 매집",
        "emoji": "🏦",
        "sl_pct": 0.05,    # -5% 손절
        "tp_pct": 0.20,    # +20% 이상 익절
        "order_type": "limit",
        "hold_days": 20,
    },
    "E": {
        "name": "낙폭과대 폭락주 스나이핑",
        "emoji": "💥",
        "sl_pct": 0.15,    # -15% 손절 (폭락주 변동성 허용)
        "tp_pct": 0.05,    # +5% 평단가 대비 기계적 익절
        "order_type": "limit",    # 거미줄 지정가
        "hold_days": 30,
        "spider_levels": [0.48, 0.39, 0.34, 0.30],
        "max_weight_pct": 0.15,
    },
    "F": {
        "name": "메가 트렌드 장기 눌림목",
        "emoji": "🌊",
        "sl_pct": 0.07,    # 200일선 하향 이탈 시 기계적 손절 (보정은 동적 산출)
        "tp_pct": 0.50,    # +50% 구간에서 1차 반익절, 잔량은 추세 추종 장기홀딩
        "order_type": "limit",    # 종가 기준 분할 매집 (God Mode 절대 금지)
        "hold_days": 90,
        "max_weight_pct": 0.20,   # 원금 대비 최대 20% 비중
    },
    "G": {
        "name": "CCI & MACD 더블 모멘텀 스윙",
        "emoji": "📈",
        "sl_pct": 0,       # 고정 % 손절 사용 안 함 (ATR 동적 손절 + 진입일 저가 방어선)
        "tp_pct": 0,       # 고정 % 익절 사용 안 함 (MACD 데드크로스로 청산)
        "order_type": "market",   # 스윙 비중 시장가 진입
        "hold_days": 30,
        "max_weight_pct": 0.10,   # 원금 대비 최대 10% 비중
    },
    "SKIP": {
        "name": "진입 부적합",
        "emoji": "⛔",
        "sl_pct": 0,
        "tp_pct": 0,
        "order_type": "none",
        "hold_days": 0,
    },
}


class MultiAssetCouncil:
    """
    Gemini AI 기반 매매 트랙 라우팅 엔진
    Phase 1 통과 종목을 분석하여 Track A~G 중 하나를 결정
    """

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        # 비용 절감: 라우팅(스크리닝)은 저비용 Flash를 1차로 사용한다.
        # Pro는 Flash가 완전히 실패할 때만, 그리고 일일 예산(ai_budget) 내에서만 폴백.
        self.model_primary  = "gemini-3.5-flash"        # 1차 (저비용/고속)
        self.model_fallback = "gemini-3.1-pro-preview"  # 폴백 (Flash 실패 시, 예산 내)
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"

    # ──────────────────────────────────────────
    # Gemini API 호출 (Pro → Flash 자동 폴백)
    # ──────────────────────────────────────────
    def _call_gemini(self, prompt: str, use_thinking: bool = False) -> Optional[str]:
        """Gemini API 호출. Flash 우선(저비용), 실패 시에만 Pro 폴백.
        Pro는 일일 예산(ai_budget) 내에서만 사용한다."""
        models_to_try = [self.model_primary, self.model_fallback]
        last_idx = len(models_to_try) - 1

        for i, model in enumerate(models_to_try):
            is_pro = "pro" in model.lower()
            # 과금 폭탄 방지: Pro 일일 상한 초과 시 Pro 호출 자체를 건너뛴다.
            if is_pro and not ai_budget.allow_pro():
                logger.warning(
                    f"[AI] Pro 일일 상한({ai_budget.DAILY_PRO_LIMIT}) 도달 → {model} 건너뜀")
                continue

            url = f"{self.base_url}/{model}:generateContent?key={self.api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.1 if use_thinking else 0.3,
                    # 응답은 짧은 JSON이므로 출력 토큰을 크게 줄여 비용 절감
                    "maxOutputTokens": 2048,
                },
            }
            # Pro는 타임아웃 넉넉히, Flash는 빠르게
            timeout = 60 if is_pro else 30

            try:
                resp = requests.post(url, json=payload, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                if is_pro:
                    ai_budget.record_pro()
                if i > 0:
                    logger.info(f"[AI] 폴백 모델({model}) 추론 성공")
                return text.strip()
            except Exception as e:
                logger.warning(f"[AI] {model} 호출 실패 ({e})")
                if i < last_idx:
                    logger.info(f"[AI] 폴백 모델({models_to_try[i+1]})로 재시도...")
                    continue
                else:
                    logger.error(f"[AI] 모든 모델 호출 실패. 규칙 기반으로 폴백합니다.")
                    return None
        return None

    # ──────────────────────────────────────────
    # 규칙 기반 사전 필터 (API 호출 최소화)
    # ──────────────────────────────────────────
    def _rule_based_prefilter(self, stock: dict, candles: list, daily_candles: list,
                               orderbook: dict, now: datetime) -> Optional[str]:
        """
        명확한 케이스는 AI 호출 없이 규칙으로 빠르게 판정
        반환: Track 코드 (A/B/C/D/SKIP) 또는 None (AI 판단 필요)
        """
        change_pct = stock.get("change_pct", 0)
        current_hour = now.hour
        current_minute = now.minute

        # (구) God Mode 즉시 격발 로직 삭제됨
        # Track A는 5분봉 피벗 스나이퍼(run.py Phase 1.5)에서만 진입 가능

        # 종가 베팅 시간대 (15:00~15:30) => Track C 우선 검토
        if current_hour == 15 and 0 <= current_minute <= 30:
            bid_ratio = orderbook.get("bid_ask_ratio", 0)
            if bid_ratio >= 1.5 and change_pct > 0:
                return "C"

        # 하락 종목 (-3% 이하) => 일단 SKIP
        # 단, 폭락주 스나이핑(Track E) 후보는 당일 급락 자체가 전제 조건이므로 면제
        if change_pct <= -3 and "E" not in stock.get("track_hints", []):
            return "SKIP"

        # 분봉 데이터 부족 => SKIP
        if len(candles) < 5:
            return "SKIP"

        return None  # AI 판단 필요

    # ──────────────────────────────────────────
    # 분봉 데이터 요약 (프롬프트용)
    # ──────────────────────────────────────────
    def _summarize_candles(self, candles: list, max_count: int = 20) -> str:
        """분봉 데이터를 텍스트로 요약"""
        recent = candles[:max_count]
        lines = []
        for c in recent:
            body = c["close"] - c["open"]
            direction = "양봉" if body > 0 else "음봉" if body < 0 else "십자"
            lines.append(
                f"  {c['time']} | {direction} O={c['open']:,} H={c['high']:,} "
                f"L={c['low']:,} C={c['close']:,} V={c['volume']:,}"
            )
        return "\n".join(lines)

    def _summarize_daily(self, daily_candles: list, max_count: int = 20) -> str:
        """일봉 데이터를 텍스트로 요약"""
        if not daily_candles:
            return "(일봉 데이터 없음)"
        recent = daily_candles[:max_count]
        lines = []
        for c in recent:
            lines.append(
                f"  {c['date']} | O={c['open']:,} H={c['high']:,} "
                f"L={c['low']:,} C={c['close']:,} V={c['volume']:,}"
            )
        return "\n".join(lines)

    # ──────────────────────────────────────────
    # 메인 라우팅 함수
    # ──────────────────────────────────────────
    def route(self, stock: dict, candles: list, daily_candles: list,
              orderbook: dict) -> dict:
        """
        종목 분석 후 최적 트랙 결정

        반환:
        {
            "track": "A",
            "track_info": {...},
            "reason": "AI 판단 사유",
            "confidence": 0.85,
            "entry_price": 15000,
            "god_mode": False,
        }
        """
        now = datetime.now()

        # 1) 규칙 기반 사전 필터
        rule_result = self._rule_based_prefilter(stock, candles, daily_candles, orderbook, now)
        if rule_result:
            track = TRACKS[rule_result]
            is_god = (rule_result == "A" and stock.get("change_pct", 0) >= 25)
            return {
                "track": rule_result,
                "track_info": track,
                "reason": f"규칙 기반 판정: {track['name']}",
                "confidence": 0.95 if is_god else 0.70,
                "entry_price": stock.get("current", 0),
                "god_mode": is_god,
            }

        # 2) AI 추론 (Gemini)
        return self._ai_route(stock, candles, daily_candles, orderbook, now)

    def _ai_route(self, stock: dict, candles: list, daily_candles: list,
                  orderbook: dict, now: datetime) -> dict:
        """Gemini AI를 사용한 정밀 트랙 판정"""

        candle_summary = self._summarize_candles(candles)
        daily_summary = self._summarize_daily(daily_candles)
        current_time = now.strftime("%H:%M")

        # ML 피처 선제적 추출 (AI 판단 근거로 활용)
        from trader.quant_indicators import get_ml_features
        ml_features = get_ml_features(daily_candles, candles)

        # 엔벨로프 계산 (Period: 20, Percent: .env TRACK_A_ENVELOPE_PCT 기본 12.5)
        env_upper, env_lower, ma20 = 0, 0, 0
        if daily_candles and len(daily_candles) >= 20:
            env_pct = float(os.environ.get("TRACK_A_ENVELOPE_PCT", 12.5)) / 100.0
            closes = [c["close"] for c in daily_candles[:20]]
            ma20 = sum(closes) / 20
            env_upper = int(ma20 * (1 + env_pct))
            env_lower = int(ma20 * (1 - env_pct))
        current_price = stock.get("current", 0)
        in_expansion = current_price > env_upper > 0

        is_reentry = stock.get("is_reentry", False)
        reentry_warning = ""
        if is_reentry:
            reentry_warning = """
🚨 [주의: 최근 손절/매도 이력 종목 - 스마트 재진입 심사] 🚨
이 종목은 최근 60분 이내에 봇에 의해 손절 또는 매도된 이력이 있습니다! 
단순한 '데드캣 바운스'나 휩소일 가능성이 매우 높으므로 극도로 보수적으로 심사하세요.
1. 거래량이 급감한 상태에서 20일선(MA20) 지지를 정확하게 받고 있는지 확인하세요.
2. 하락 구조를 명확히 깨고 거래량이 다시 터지며 상방으로 V자 반등을 시도하는 MSS(Market Structure Shift)가 완벽히 확인되었는지 검증하세요.
3. 이 두 가지 중 하나라도 불확실하다면 무조건 'SKIP'을 선택하세요.
==============================================================
"""

        prompt = f"""{reentry_warning}당신은 대한민국 최고의 주식 트레이딩 전문가이자 섹터 분석가입니다.
현재 검색된 종목은 당일 거래대금과 거래량이 폭발하여 1차 필터를 통과한 '시장 주도주 후보'입니다.

아래 데이터를 바탕으로:
1. 해당 종목의 **섹터 장세 및 최신 호재/모멘텀**을 당신의 지식 베이스에서 찾아내어 설명하세요.
2. 현재 차트(분봉/일봉)가 **추가 상승 여력이 있는 '진입 타점'**인지, 아니면 이미 고점을 찍고 내려오는 '설거지 구간'인지 냉철히 판별하세요.
3. 보수적으로 검증하세요. 아래 [필수 SKIP 조건]에 해당하면 반드시 SKIP하세요. 확실한 근거가 있을 때만 트랙을 부여하세요.

[필수 SKIP 조건 - 하나라도 해당 시 무조건 SKIP]
- 고점 대비 하락 중이고, 직전 3봉 연속 음봉이면서 거래량이 직전 20봉 평균 이하인 경우 (하락 추세 지속)
- 1분봉에서 거래량 2배 이상의 양봉이 한 번도 출현하지 않은 경우 (MSS 미확인)
- 직전 고점 대비 -5% 이상 하락한 상태에서 반등 시도 없는 경우

[분석 및 1분봉 타점 지시]
- 'reason' 항목에 (1)섹터/모멘텀 분석, (2)MSS(Market Structure Shift) 관점의 차트 분석, (3)스마트 타점 및 향후 전망을 정성스럽게 작성하세요.
- [눌림목과 되돌림 판단 - 정량 기준 필수] 반드시 직전 20봉 평균 거래량 대비 2배 이상의 거래량을 동반한 양봉이 출현했는지 확인하세요. 이 조건 없이 단순히 '거래량이 마르며 하락세가 멈추는' 것만으로 MSS라 판단하지 마세요. 거래량 폭발 양봉 없이는 진짜 반전이 아니라 하락 중 일시적 소강일 수 있습니다.
- [스마트 진입가(entry_price)] 돌파 매매가 아니라면 현재가에 무작정 추격 매수하지 말고, 차트상의 이전 BOS(Break of Structure) 라인이나 강력한 1분봉 눌림목 지지선을 'entry_price'에 설정하세요. (만약 되돌림이 끝나고 당장 상승할 자리라면 현재가로 설정)
- [동적 손절가(sl_pct)] 고정 손절 퍼센트 대신, 당신이 설정한 진입가(entry_price) 하단에 위치한 '의미 있는 전저점'이나 '추세 이탈점'까지의 하락 퍼센트(예: 0.04)를 계산하여 'sl_pct'로 반환하세요.
- 만약 투매 물량이 쏟아지거나 반등 기미가 전혀 없는 진짜 '설거지 구간'이라면 과감히 'SKIP' 하되 "언제쯤 다시 진입할 수 있을지(대기 타점)"를 함께 제시하세요.

[종목 정보]
- 종목명: {stock.get('name', '')}
- 종목코드: {stock.get('ticker', '')}
- 현재가: {stock.get('current', 0):,}원
- 등락률: {stock.get('change_pct', 0):+.2f}%
- 누적 거래량: {stock.get('volume', 0):,}주
- 누적 거래대금: {stock.get('trade_amount', 0)//100_000_000}억원
- 시가총액: {stock.get('market_cap', 0)}억원
- 현재 시각: {current_time}
{f"- 사전 필터 통과 트랙: {', '.join(stock.get('track_hints', []))}" if stock.get('track_hints') else "- 사전 필터 통과 트랙: 없음 (정량 조건 미충족, 그래도 차트/모멘텀 기반 판단 가능)"}

[호가 정보]
- 매수총잔량: {orderbook.get('total_bid_qty', 0):,}주
- 매도총잔량: {orderbook.get('total_ask_qty', 0):,}주
- 매수/매도 비율: {orderbook.get('bid_ask_ratio', 0):.2f}

[당일 분봉 (최근)]
{candle_summary}

[최근 일봉 (20일)]
{daily_summary}

[엔벨로프 지표 (Period: 20, Percent: 12.5)]
- 20일 이동평균: {ma20:,.0f}원
- 엔벨로프 상단: {env_upper:,}원
- 엔벨로프 하단: {env_lower:,}원
- 현재가 위치: {"⚡ 발산 영역 (상단 돌파) → Track A 후보" if in_expansion else "정상 범위"}

[정통 퀀트 지표 (ML 기반 피처)]
- 20일 평균 대비 당일 거래량: {ml_features['vol_ratio']:.2f}배
- 20일선 이격도(Env Diff): {ml_features['env_diff']:.2f}%
- 볼린저 밴드 폭(BB Width): {ml_features['bb_width']:.2f}%
- RSI(14): {ml_features['rsi']:.1f}
- MACD 히스토그램: {ml_features['macd']:.2f}
- ADX(14): {ml_features['adx']:.1f}
- ATR(14): {ml_features['atr']:.2f}

[Track A 엔벨로프 발산 스나이핑 전용 지시]
- 현재가가 엔벨로프 상단({env_upper:,}원)을 돌파한 '발산 영역'에 있는지 확인하세요.
- 발산 영역에 있다면, 1분봉에서 거래량이 감소하며 눌림을 주다가 갑자기 거래량이 폭발(직전 20봉 평균 대비 2배 이상)하며 양봉을 뽑아내는 시점을 포착하세요.
- 1분봉 양봉의 저점(꼬리)을 손절선으로 잡으면 너무 타이트하여 휩소에 털릴 수 있습니다. 따라서 주어진 분봉 흐름을 파악하여 최근 '5분봉 기준 눌림목의 최저점'을 유추해 'trigger_candle_low'로 반환하세요.
- 발산 영역이 아닌 종목은 Track A로 판정하지 마세요.

[트랙 선택지]
Track A (엔벨로프 발산 스나이핑): 일봉상 엔벨로프 상단 돌파(발산 영역) + 1분봉 거래량 폭발 양봉 출현 시. God Mode 즉시 시장가 매수, 5~7% 익절. 반드시 trigger_candle_low(5분봉 기준 눌림목 저점)를 반환할 것.
Track B (눌림목 스윙): 기준봉 대비 거래량 급감(10% 이하), 연속 음봉, 20일선 근접 시. 지정가 분할매수.
Track C (종가 베팅): 15:00 이후, 20일 매물대 상향 돌파 중, 매수잔량 압도, 외인/기관 유입 시 종가 진입.
Track D (세력주 매집): 52주 신저가 부근 하락 멈춤, PER 1배 이상, 유보율 200% 이상. 소량 분할 매수.
Track E (낙폭과대 폭락주 스나이핑): 최근 바닥 대비 300%+ 대시세 이력이 있으나 200일 최고가 대비 반토막(-50%+) 급락한 종목. 상장 1년 미만 신규 상장주는 제외. 200일 최고가(peak_200d)를 반드시 반환. 거미줄 4단계 지정가 분할매수(최고가×0.48/0.39/0.34/0.30).
Track F (메가 트렌드 장기 눌림목): 최근 2~3개월 내 평소 대비 3배+ 거래량 폭발과 함께 50%+ 급등(시세 분출)한 이력이 있고, 이후 150일/200일 이동평균선까지 조정이 진행된 종목. 반도체/2차전지/로봇/전력/태양광 등 시대 중심 메가 트렌드 섹터 우량주에만 적용. God Mode 절대 금지 → 종가 기준 분할 매집. 150일선에서 1차 정찰병, 200일선 횡보 확인 시 비중 배팅. 200일선 하향 이탈 시 기계적 손절, +50%에서 1차 반익절 후 잔량 추세 추종 장기 홀딩. ma150과 ma200 값을 반드시 반환할 것.
Track G (CCI & MACD 더블 모멘텀 스윙): 일봉 기준 CCI(50)와 MACD(12,26,9) 모두 동시에 0선을 상향 돌파한 종목. 추세 반전의 초입(무릎)을 잡아 길게 끌고 가는 스윙 전략. 거래대금 500억 이상 필수. 고정 % 손절/익절 없이 ATR 동적 손절과 MACD 데드크로스에 의해서만 청산(Hold-to-TP). 진입일 일봉 저가를 절대 방어선(entry_day_low)으로 반드시 반환할 것. 사전 필터에서 'G'가 태깅된 종목에만 부여 가능.
SKIP: 위 어느 트랙에도 해당하지 않는 경우.

[출력 형식 (반드시 아래 JSON만 출력)]
{{"track": "A", "reason": "...", "confidence": 0.85, "entry_price": 14000, "sl_pct": 0.03, "tp_pct": 0.07, "trigger_candle_low": 13800, "peak_200d": 0, "ma150": 0, "ma200": 0, "entry_day_low": 0}}
"""

        # Thinking 모델로 정밀 판단
        raw = self._call_gemini(prompt, use_thinking=True)

        if not raw:
            logger.warning(f"[AI] Gemini 응답 없음 -> SKIP 처리: {stock.get('name')}")
            return {
                "track": "SKIP",
                "track_info": TRACKS["SKIP"],
                "reason": "AI 응답 실패",
                "confidence": 0,
                "entry_price": stock.get("current", 0),
                "god_mode": False,
            }

        # JSON 파싱
        # JSON 파싱 정규화 (작은따옴표, 줄바꿈 등 대응)
        try:
            import re
            # 마크다운 블록 제거
            cleaned = re.sub(r"```json|```", "", raw).strip()
            # 속성명의 작은따옴표를 큰따옴표로 (간단한 처리)
            cleaned = re.sub(r"'([^']+)':", r'"\1":', cleaned)
            # 값의 작은따옴표 처리 ('A' -> "A")
            cleaned = re.sub(r":\s*'([^']+)'", r': "\1"', cleaned)
            result = json.loads(cleaned)
            # json.loads가 dict가 아닌 타입(str, list 등)을 반환할 수 있음 → 방어
            if not isinstance(result, dict):
                raise ValueError(f"JSON 파싱 결과가 dict가 아님: {type(result).__name__}")
        except (json.JSONDecodeError, IndexError, ValueError, Exception) as e:
            logger.warning(f"[AI] JSON 파싱 실패 ({e}), 원본: {raw[:200]}")
            # 텍스트에서 트랙 추출 시도
            track_code = "SKIP"
            for t in ["A", "B", "C", "D", "E", "F"]:
                if f'"track": "{t}"' in raw or f"Track {t}" in raw:
                    track_code = t
                    break
            # reason 필드 값만 추출 (raw ```json 블록 통째로 매매일지에 저장되던 문제 방지)
            rm = re.search(r"""['"]reason['"]\s*:\s*['"](.+?)['"]\s*[,}\n]""", raw, re.DOTALL)
            clean_reason = rm.group(1).strip().replace("\\n", " ") if rm else "AI 응답 파싱 실패 (형식 오류)"
            result = {
                "track": track_code,
                "reason": clean_reason[:300],
                "confidence": 0.5,
                "entry_price": stock.get("current", 0),
            }

        track_code = result.get("track", "SKIP").upper()
        if track_code not in TRACKS:
            track_code = "SKIP"
            
        track_info = TRACKS[track_code].copy() if track_code in TRACKS else TRACKS["SKIP"].copy()
        
        # AI가 스마트 손절/익절가를 계산했다면 덮어쓰기
        if "sl_pct" in result:
            track_info["sl_pct"] = float(result["sl_pct"])
        if "tp_pct" in result:
            track_info["tp_pct"] = float(result["tp_pct"])

        # entry_price가 현재가와 다르다면 스마트 지정가 매수이므로 limit 오버라이드
        final_entry_price = result.get("entry_price", stock.get("current", 0))
        if track_code != "SKIP" and final_entry_price < stock.get("current", 0):
            track_info["order_type"] = "limit"

        # Track A 전용: 트리거 캔들 저점 (꼬리 손절)
        trigger_candle_low = int(result.get("trigger_candle_low", 0))
        # Track E 전용: 200일 최고가 (거미줄 매수 레벨 계산용)
        peak_200d = int(result.get("peak_200d", 0))
        # Track F 전용: 150일/200일 이동평균
        ma150 = int(result.get("ma150", 0))
        ma200 = int(result.get("ma200", 0))

        # 동적 손절가(Dynamic SL) 산출 (SMC 기반 스윙 로우 최우선)
        current = stock.get("current", 0)
        dynamic_sl = 0
        
        # 일봉 기반 SMC 구조 파악하여 스윙 로우 가져오기
        smc_data = {}
        if daily_candles:
            smc_data = evaluate_smc_structure(daily_candles)
            
        smc_pivot_low = smc_data.get("pivot_low", 0)
        
        if track_code == "A":
            # Track A는 1분봉 단타이므로 일봉 SMC가 아닌 trigger_candle_low를 동적 지지선으로 사용
            dynamic_sl = int(trigger_candle_low * 0.995) if trigger_candle_low > 0 else 0
        elif smc_pivot_low > 0 and smc_pivot_low < current:
            # SMC 지지선(직전 스윙 로우)을 동적 손절가로 채택 (0.5% 버퍼)
            dynamic_sl = int(smc_pivot_low * 0.995)
        else:
            # 기존 로직 (SMC 계산 실패 시 백업)
            if track_code == "B":
                # MA20 (20일선) — B는 정배열 눌림목이라 진입가가 MA20 위에 있음
                closes = [c["close"] for c in daily_candles[:20]] if len(daily_candles) >= 20 else []
                dynamic_sl = int(sum(closes) / 20) if closes else int(current * 0.95)
            elif track_code == "D":
                # D는 검색 조건상 진입가가 MA20 '아래'이므로 MA20을 손절로 쓰면
                # 진입 즉시 손절됨 → 최근 20일 최저가 기반 손절로 교체
                recent_lows = [c["low"] for c in daily_candles[:20]] if daily_candles else []
                low_base = min(recent_lows) if recent_lows else 0
                dynamic_sl = int(low_base * 0.99) if 0 < low_base < current else int(current * 0.95)
            elif track_code == "C":
                # 당일 저가
                dynamic_sl = int(daily_candles[0]["low"]) if daily_candles else int(current * 0.95)
            elif track_code == "F":
                # 200일선 하향 이탈 = 기계적 손절 (200일선 * 0.97)
                if ma200 > 0:
                    dynamic_sl = int(ma200 * 0.97)
                else:
                    if len(daily_candles) >= 200:
                        ma200_calc = sum(c["close"] for c in daily_candles[:200]) / 200
                        dynamic_sl = int(ma200_calc * 0.97)
                    else:
                        dynamic_sl = int(current * 0.93)
            elif track_code == "E":
                # 4차 거미줄 타점(30%) 대비 -10% 하락
                dynamic_sl = int(peak_200d * 0.27) if peak_200d > 0 else int(current * 0.90)

        # 공통 안전장치: 동적 손절가는 반드시 현재가 아래에 있어야 한다
        # (Track D처럼 진입가가 MA20 아래인 경우 폴백 손절가가 현재가 위에 놓여
        #  진입 직후 즉시 손절되는 결함 방지)
        if current > 0 and dynamic_sl >= current:
            dynamic_sl = int(current * 0.95)

        # ATR 기반 동적 TP/SL 계산 (2차 안전장치)
        # '최대 허용 손실 한도'이므로 반드시 일봉 ATR로 산출한다.
        # 1분봉 ATR로 계산하면 -0.2~-0.6%에 박혀 1차 동적 손절(-2~-5%)보다
        # 타이트해지고, 휩쏘 손절(2026-06-01 RISE -0.30% 사례)의 원인이 된다.
        atr_value = wilder_atr(daily_candles, 14) if daily_candles else 0
        atr_sl_price = int(final_entry_price - 2 * atr_value) if atr_value > 0 else 0
        atr_tp_price = int(final_entry_price + 3 * atr_value) if atr_value > 0 else 0
        # 2차 한도는 항상 1차 동적 손절보다 아래(더 넓은 방어선)에 위치해야 한다
        if atr_sl_price > 0 and dynamic_sl > 0 and atr_sl_price >= dynamic_sl:
            atr_sl_price = int(dynamic_sl * 0.99)

        logger.info(f"[AI] {stock.get('name')} -> Track {track_code} "
                    f"({result.get('reason', '')[:50]}...) "
                    f"신뢰도={result.get('confidence', 0):.0%}"
                    f"{f' 꼬리손절={trigger_candle_low:,}' if trigger_candle_low else ''}"
                    f"{f' 동적손절={dynamic_sl:,}' if dynamic_sl else ''}"
                    f"{f' ATR={atr_value:,.0f} SL={atr_sl_price:,} TP={atr_tp_price:,}' if atr_value else ''}"
                    f"{f' 200일고가={peak_200d:,}' if peak_200d else ''}")

        # Track G 전용: 진입일 일봉 저가 (절대 방어선)
        entry_day_low = int(result.get("entry_day_low", 0))

        return {
            "name": stock.get("name", ""),
            "track": track_code,
            "track_info": track_info,
            "reason": result.get("reason", ""),
            "confidence": result.get("confidence", 0),
            "entry_price": final_entry_price,
            "god_mode": False,
            "trigger_candle_low": trigger_candle_low,
            "peak_200d": peak_200d,
            "ma150": ma150,
            "ma200": ma200,
            "entry_day_low": entry_day_low,
            "dynamic_sl_price": dynamic_sl,
            "atr_value": atr_value,
            "atr_sl_price": atr_sl_price,
            "atr_tp_price": atr_tp_price,
            "quant_features": ml_features,
        }

    # ──────────────────────────────────────────
    # 빠른 판단 (Flash 모델, 경량 작업용)
    # ──────────────────────────────────────────
    def quick_assess(self, stock_name: str, change_pct: float,
                     volume_ratio: float) -> str:
        """Flash 모델로 빠른 1줄 판단 (모니터링용)"""
        prompt = (
            f"종목: {stock_name}, 등락률: {change_pct:+.1f}%, "
            f"거래량 비율(20일 평균 대비): {volume_ratio:.1f}배. "
            f"현 상태를 20자 이내로 요약해주세요. (예: '강한 돌파 초기', '눌림 조정 중')"
        )
        result = self._call_gemini(prompt, use_thinking=False)
        return result or "판단 불가"


# ──────────────────────────────────────────────
# 볼륨 확증 필터 (5대 안전장치 #1)
# ──────────────────────────────────────────────
def volume_confirm_filter(candles: list, threshold: float = 1.5) -> bool:
    """
    최근 캔들의 거래량이 직전 20봉 평균 대비 threshold배 이상인지 확인
    True = 통과 (진짜 돌파), False = 가짜 돌파(휩쏘)
    """
    if len(candles) < 21:
        return False
    recent_vol = candles[0].get("volume", 0)
    avg_vol = sum(c.get("volume", 0) for c in candles[1:21]) / 20
    if avg_vol <= 0:
        return False
    ratio = recent_vol / avg_vol
    logger.info(f"[Volume] 최근={recent_vol:,} / 20봉평균={avg_vol:,.0f} = {ratio:.2f}배")
    return ratio >= threshold
