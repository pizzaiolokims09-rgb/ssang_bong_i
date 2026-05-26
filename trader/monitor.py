"""
monitor.py - 포지션 모니터링, 익절/손절 트레일링 스톱
매뉴얼 Section 5 (안전장치 #3: Absolute Hard SL) 구현
"""
import logging
from datetime import datetime

from trader.quant_indicators import get_sell_signals

logger = logging.getLogger("ssangbong.monitor")


class PositionMonitor:
    """
    보유 종목 실시간 감시
    - 손절선 도달 시 강제 청산
    - 익절선 도달 시 차익 실현
    - 트레일링 스톱 적용
    """

    def __init__(self, kis_client, order_manager):
        self.kis = kis_client
        self.orders = order_manager

    def check_all_positions(self) -> list:
        """
        전체 보유 종목 손익 점검
        반환: 체결된 매도 내역 리스트
        """
        results = []
        daily_candles_cache = {}  # 4대 매도 트리거용 일봉 캐시 (종목별 1회만 조회)
        tickers = list(self.orders.positions.keys())

        for ticker in tickers:
            pos = self.orders.positions[ticker]
            quote = self.kis.get_quote(ticker)
            current = quote.get("current", 0)

            if current <= 0:
                continue

            # ─────────────────────────────────
            # 상한가 즉시 매도 (전 트랙 공통)
            # ─────────────────────────────────
            prev_close = quote.get("prev_close", 0)
            if prev_close > 0:
                upper_limit_pct = (current - prev_close) / prev_close
                if upper_limit_pct >= 0.295:  # 29.5% 이상 = 상한가 도달
                    name = pos.get("name", ticker)
                    logger.info(
                        f"[UPPER LIMIT] {name}({ticker}) 상한가 도달! "
                        f"전일종가={prev_close:,} 현재={current:,} (+{upper_limit_pct*100:.1f}%)"
                    )
                    r = self.orders.sell(ticker, reason=f"상한가 즉시 매도 (+{upper_limit_pct*100:.1f}%)")
                    if r:
                        r["trigger"] = "UPPER_LIMIT"
                        results.append(r)
                    continue

            entry = pos["entry_price"]
            change = (current - entry) / entry if entry > 0 else 0
            track = pos["track"]
            sl_pct = pos["sl_pct"]
            tp_pct = pos["tp_pct"]

            # 최고 수익률(High Water Mark) 추적
            max_change = pos.get("max_change", 0)
            if change > max_change:
                pos["max_change"] = change
                max_change = change

            # ─────────────────────────────────
            # 안전장치 #3: 절대 손절선 (Hard SL)
            # ─────────────────────────────────
            # 동적 손절(Dynamic SL) 검사
            dynamic_sl = pos.get("dynamic_sl_price", 0)
            
            if dynamic_sl > 0:
                if current <= dynamic_sl:
                    logger.critical(
                        f"[SL] {ticker} 동적 손절 발동! "
                        f"현재={current:,} <= 지지선={dynamic_sl:,.0f}"
                    )
                    r = self.orders.sell(ticker, reason=f"동적 손절 (지지선 {dynamic_sl:,} 이탈)")
                    if r:
                        r["trigger"] = "STOP_LOSS"
                        results.append(r)
                    continue
            else:
                # 하위 호환: dynamic_sl_price가 없는 기존 보유 종목은 퍼센트 손절 적용
                if change <= -sl_pct:
                    logger.critical(
                        f"[SL] {ticker} 퍼센트 손절 발동! "
                        f"진입={entry:,} 현재={current:,} 손실={change*100:+.2f}%"
                    )
                    r = self.orders.sell(ticker, reason=f"Hard SL {change*100:+.1f}%")
                    if r:
                        r["trigger"] = "STOP_LOSS"
                        results.append(r)
                    continue

            # ─────────────────────────────────
            # 50% 분할 익절 및 트레일링 스탑 (수익 극대화)
            # ─────────────────────────────────
            max_price = entry * (1 + max_change)
            partial_tp_done = pos.get("partial_tp_done", False)

            if not partial_tp_done and change >= 0.05: # 5% 수익 도달 시
                logger.info(
                    f"[PARTIAL TP] {ticker} 5% 수익 구간 도달! 50% 분할 익절 실행. "
                    f"진입={entry:,} 현재={current:,} 수익={change*100:+.2f}%"
                )
                r = self.orders.sell(ticker, ratio=0.5, reason=f"5% 도달 50% 분할 익절")
                if r:
                    r["trigger"] = "PARTIAL_TP"
                    results.append(r)
                
                # 분할 익절 후 상태 업데이트
                pos["partial_tp_done"] = True
                # 동적 손절가를 본절선(+0.5%)으로 상향 조정 (Break-even)
                pos["dynamic_sl_price"] = entry * 1.005 
                self.orders._save_positions()
                continue

            if partial_tp_done:
                # 50% 익절 완료 후: 4대 매도 트리거로 추세 전환 감지 시 청산
                # 분봉 데이터 조회 (1분봉)
                minute_candles = self.kis.get_minute_chart(ticker)
                daily_cndls = self.kis.get_daily_chart(ticker) if daily_candles_cache.get(ticker) is None else daily_candles_cache.get(ticker)
                if not daily_cndls:
                    daily_cndls = []
                daily_candles_cache[ticker] = daily_cndls

                signals = get_sell_signals(minute_candles or [], daily_cndls)
                adx = signals["adx"]

                # 1) 패닉셀 → ADX 무관, 무조건 즉시 청산
                if signals["panic_sell"]:
                    logger.critical(f"[4T] {ticker} 패닉셀 감지! 즉시 청산")
                    r = self.orders.sell(ticker, reason="패닉셀 감지 (거래량 3배+ 폭발)")
                    if r:
                        r["trigger"] = "PANIC_SELL"
                        results.append(r)
                    continue

                # 2) MACD 음전환 → 추세 전환 시그널
                elif signals["macd_bearish"]:
                    logger.info(f"[4T] {ticker} MACD 매도 시그널 (Hist={signals['macd_histogram']:.2f})")
                    r = self.orders.sell(ticker, reason=f"MACD 매도 시그널 (Hist={signals['macd_histogram']:.2f})")
                    if r:
                        r["trigger"] = "MACD_SELL"
                        results.append(r)
                    continue

                # 3) 데드크로스 → ADX 약세(< 20)일 때만 실행
                elif signals["dead_cross"] and adx < 20:
                    logger.info(f"[4T] {ticker} 데드크로스 + ADX 약세({adx:.1f}) → 청산")
                    r = self.orders.sell(ticker, reason=f"데드크로스 (ADX={adx:.1f})")
                    if r:
                        r["trigger"] = "DEAD_CROSS"
                        results.append(r)
                    continue

                # 4) RSI 과매수 → ADX 약세(< 20)일 때만 실행 (강한 추세에선 무시)
                elif signals["rsi_overbought"] and adx < 20:
                    logger.info(f"[4T] {ticker} RSI 과매수({signals['rsi']:.1f}) + ADX 약세({adx:.1f}) → 청산")
                    r = self.orders.sell(ticker, reason=f"RSI 과매수 ({signals['rsi']:.1f}, ADX={adx:.1f})")
                    if r:
                        r["trigger"] = "RSI_OVERBOUGHT"
                        results.append(r)
                    continue

            # ─────────────────────────────────
            # 기존 익절선 도달 (전량 익절)
            # ─────────────────────────────────
            if change >= tp_pct:
                logger.info(
                    f"[TP] {ticker} 익절 도달! "
                    f"진입={entry:,} 현재={current:,} 수익={change*100:+.2f}%"
                )
                r = self.orders.sell(ticker, reason=f"Take Profit {change*100:+.1f}%")
                if r:
                    r["trigger"] = "TAKE_PROFIT"
                    results.append(r)
                continue

            # ─────────────────────────────────
            # ATR 2차 안전장치 (최대 허용 손실 한도)
            # ─────────────────────────────────
            atr_sl = pos.get("atr_sl_price", 0)
            if atr_sl > 0 and current <= atr_sl:
                logger.critical(
                    f"[ATR SL] {ticker} ATR 최대 손실 한도 이탈! "
                    f"현재={current:,} <= ATR SL={atr_sl:,}"
                )
                r = self.orders.sell(ticker, reason=f"ATR 최대손실 한도 ({atr_sl:,} 이탈)")
                if r:
                    r["trigger"] = "ATR_STOP_LOSS"
                    results.append(r)
                continue

            # ─────────────────────────────────
            # 기존 방어 로직 (Trailing Stop / 본절 보존)
            # ─────────────────────────────────
            if not partial_tp_done:
                # 1. 수익 반납 방지 (Trailing Stop): 최고 수익률이 목표 익절가의 70% 이상 도달 후, 
                #    최고점 대비 3% 이상 하락하면 시장가 익절 (수익 보존)
                if max_change >= (tp_pct * 0.7) and change <= (max_change - 0.03):
                    logger.info(
                        f"[TS] {ticker} 기존 트레일링 스탑 발동! "
                        f"최고수익={max_change*100:.1f}% 현재수익={change*100:.1f}% -> 수익 보존 탈출"
                    )
                    r = self.orders.sell(ticker, reason=f"Trailing Stop {change*100:+.1f}%")
                    if r:
                        r["trigger"] = "TRAILING_STOP"
                        results.append(r)
                    continue

                # 2. 본절 보존 (Break-even): 수익이 +3% 이상 났다가 다시 본절(+0.5%) 근처로 위협받으면 탈출
                if max_change >= 0.03 and change <= 0.005:
                    logger.info(
                        f"[BE] {ticker} 본절 보존 발동! "
                        f"최고수익={max_change*100:.1f}% -> 본절선(+0.5%) 위협으로 탈출"
                    )
                    r = self.orders.sell(ticker, reason="본절 보존 (Break-even)")
                    if r:
                        r["trigger"] = "BREAK_EVEN"
                        results.append(r)
                    continue

            # ─────────────────────────────────
            # 트랙별 특수 체크
            # ─────────────────────────────────
            # Track A (단타): 30분 이상 보유 중 본전/약수익(-0.5% ~ +1%) 횡보 시 매도 정리
            if track == "A":
                entry_time = pos.get("entry_time", datetime.now())
                if isinstance(entry_time, str):
                    try:
                        entry_time = datetime.fromisoformat(entry_time)
                    except (ValueError, TypeError):
                        entry_time = datetime.now()
                hold_minutes = (datetime.now() - entry_time).total_seconds() / 60
                if hold_minutes > 30 and -0.005 <= change < 0.01:
                    logger.info(f"[Monitor] Track A {ticker} 30분 경과, 본전/약수익 횡보 -> 정리")
                    r = self.orders.sell(ticker, reason="Track A 시간초과")
                    if r:
                        r["trigger"] = "TIMEOUT"
                        results.append(r)
                    continue

            logger.debug(
                f"[Monitor] {ticker} Track {track} | "
                f"진입={entry:,} 현재={current:,} 손익={change*100:+.2f}%"
            )

        return results

    def get_portfolio_status(self) -> dict:
        """현재 포트폴리오 요약"""
        total_value = 0
        total_pnl = 0
        details = []

        for ticker, pos in self.orders.positions.items():
            quote = self.kis.get_quote(ticker)
            current = quote.get("current", 0)
            entry = pos["entry_price"]
            qty = pos["quantity"]

            value = current * qty
            pnl = (current - entry) * qty
            change = (current - entry) / entry * 100 if entry > 0 else 0

            total_value += value
            total_pnl += pnl

            et = pos.get("entry_time", datetime.now())
            if isinstance(et, str):
                try:
                    et = datetime.fromisoformat(et)
                except (ValueError, TypeError):
                    et = datetime.now()

            details.append({
                "ticker": ticker,
                "name": pos.get("name", quote.get("name", ticker)),
                "track": pos["track"],
                "entry_price": entry,
                "current": current,
                "quantity": qty,
                "value": value,
                "pnl": pnl,
                "change_pct": change,
                "entry_time": et.strftime("%H:%M"),
            })

        return {
            "total_positions": len(self.orders.positions),
            "pending_count": len(self.orders.pending_orders),
            "total_value": total_value,
            "total_pnl": total_pnl,
            "daily_pnl": self.orders.daily_pnl,
            "kill_switch": self.orders.kill_switch,
            "details": details,
        }

    # ──────────────────────────────────────────
    # 미체결 주문 체결 확인 (Adaptive Order)
    # ──────────────────────────────────────────
    def check_pending_fills(self) -> list:
        """
        KIS 체결 조회 API로 pending 주문의 실제 체결 여부 확인.
        반환: 체결 확인된 종목 정보 리스트
        """
        confirmed = []
        if not self.orders.pending_orders:
            return confirmed

        # KIS 당일 체결 내역 조회
        conclusions = self.kis.get_conclusions()

        # 체결된 종목번호 집합
        filled_tickers = set()
        for ccld in conclusions:
            pdno = ccld.get("pdno", "")
            sll_buy = ccld.get("sll_buy_dvsn_cd", "")
            # 매수 체결만 확인 (sll_buy_dvsn_cd: 02=매수)
            if sll_buy == "02" and pdno:
                filled_tickers.add(pdno)

        for ticker in list(self.orders.pending_orders.keys()):
            if ticker in filled_tickers:
                result = self.orders.confirm_pending(ticker)
                if result:
                    confirmed.append(result)
                    logger.info(f"[Pending] {result['name']}({ticker}) 지정가 체결 확인!")

        return confirmed

    # ──────────────────────────────────────────
    # 추가 매수(물타기) 타점 감시
    # ──────────────────────────────────────────
    def check_pyramiding(self, current_hour: int, current_minute: int) -> list:
        """
        보유 종목의 2차, 3차 추가 매수 타점 도달 여부 확인
        """
        results = []
        tickers = list(self.orders.positions.keys())

        for ticker in tickers:
            pos = self.orders.positions[ticker]
            track = pos["track"]
            step = pos.get("step", 1)
            max_step = pos.get("max_step", 1)
            name = pos.get("name", ticker)

            if step >= max_step:
                continue  # 최대 분할 도달

            # 현재가 조회
            quote = self.kis.get_quote(ticker)
            current = quote.get("current", 0)
            if current <= 0:
                continue

            entry = pos["entry_price"]
            change = (current - entry) / entry if entry > 0 else 0

            # Track C (종가베팅): 15:20분 도달 시 2차 진입
            if track == "C" and step == 1:
                if current_hour == 15 and current_minute >= 20:
                    # 급락 방지: -3% 이상 하락 시 2차 진입 포기 및 손절 유도
                    if change <= -0.03:
                        logger.warning(f"[Pyramid] {name}({ticker}) Track C 15:20 도달했으나 급락(-3% 이하)으로 2차 매수 취소")
                        continue
                    
                    logger.info(f"[Pyramid] {name}({ticker}) Track C 15:20 종가 동시호가 진입 (2차 매수 격발)")
                    r = self.orders.add_buy(ticker, reason="종가 2차 진입")
                    if r:
                        results.append(r)
                continue

            # Track B/D (스윙/매집): 가격 기반 피라미딩
            if track in ["B", "D"]:
                trigger = False
                if step == 1 and change <= -0.05:
                    logger.info(f"[Pyramid] {name}({ticker}) Track {track} -5% 도달 (2차 매수 격발)")
                    trigger = True
                elif step == 2 and change <= -0.10:
                    logger.info(f"[Pyramid] {name}({ticker}) Track {track} -10% 도달 (3차 매수 격발)")
                    trigger = True
                
                if trigger:
                    r = self.orders.add_buy(ticker, reason=f"{step+1}차 물타기 진입")
                    if r:
                        results.append(r)
                continue

            # Track E (폭락주 스나이핑): 거미줄 4단계 매수 (peak_200d 기반)
            if track == "E":
                peak = pos.get("peak_200d", 0)
                levels = pos.get("spider_levels", [0.48, 0.39, 0.34, 0.30])
                if peak <= 0 or step > len(levels):
                    continue
                # 다음 단계 레벨 가격
                next_level_price = int(peak * levels[step])  # step은 0-indexed로 다음 단계
                if current <= next_level_price:
                    logger.info(f"[Spider] {name}({ticker}) Track E 거미줄 {step+1}차 레벨 도달 "
                                f"(현재={current:,} <= 레벨={next_level_price:,}, peak={peak:,}×{levels[step]})")
                    r = self.orders.add_buy(ticker, reason=f"거미줄 {step+1}차 진입 (peak×{levels[step]})")
                    if r:
                        results.append(r)
                continue

        return results

    # ──────────────────────────────────────────
    # 장마감 포지션 정리 (15:10 기준)
    # ──────────────────────────────────────────
    def check_eod_liquidation(self) -> list:
        """
        장 마감 전 트랙별 포지션 정리 판단 (15:10에 호출)
        - Track A: 무조건 정리 (당일 마감 필수)
        - Track B: 수익 중이면 홀드, -2% 초과 손실이면 정리, 소폭손실(-2% 이내)은 홀드
        - Track C: 수익 중이면 홀드, -3% 초과 손실이면 정리
        - Track D: 무조건 홀드 (중장기)
        """
        results = []
        tickers = list(self.orders.positions.keys())

        for ticker in tickers:
            pos = self.orders.positions[ticker]
            quote = self.kis.get_quote(ticker)
            current = quote.get("current", 0)

            if current <= 0:
                continue

            entry = pos["entry_price"]
            change = (current - entry) / entry if entry > 0 else 0
            track = pos["track"]
            name = pos.get("name", ticker)

            # Track A: 당일 마감 필수 → 무조건 정리
            if track == "A":
                logger.info(f"[EOD] {name}({ticker}) Track A 장마감 정리 (수익률={change*100:+.2f}%)")
                r = self.orders.sell(ticker, reason=f"장마감 정리 (Track A) {change*100:+.1f}%")
                if r:
                    r["trigger"] = "EOD_LIQUIDATION"
                    results.append(r)
                continue

            # Track B: 소폭손실(-2% 이내) 홀드, -2% 초과 손실이면 정리
            if track == "B":
                if change < -0.02:
                    logger.info(f"[EOD] {name}({ticker}) Track B 손실 정리 ({change*100:+.2f}%)")
                    r = self.orders.sell(ticker, reason=f"장마감 정리 (Track B 손실) {change*100:+.1f}%")
                    if r:
                        r["trigger"] = "EOD_LIQUIDATION"
                        results.append(r)
                else:
                    logger.info(f"[EOD] {name}({ticker}) Track B 홀드 (수익/소폭손실 {change*100:+.2f}%)")
                continue

            # Track C: -3% 초과 손실이면 정리
            if track == "C":
                if change < -0.03:
                    logger.info(f"[EOD] {name}({ticker}) Track C 손실 정리 ({change*100:+.2f}%)")
                    r = self.orders.sell(ticker, reason=f"장마감 정리 (Track C 손실) {change*100:+.1f}%")
                    if r:
                        r["trigger"] = "EOD_LIQUIDATION"
                        results.append(r)
                else:
                    logger.info(f"[EOD] {name}({ticker}) Track C 홀드 ({change*100:+.2f}%)")
                continue

            # Track D/E: 무조건 홀드 (중장기)
            if track in ["D", "E"]:
                logger.info(f"[EOD] {name}({ticker}) Track {track} 홀드 (중장기)")
                continue

        # 미체결 주문도 전부 취소
        for ticker in list(self.orders.pending_orders.keys()):
            name = self.orders.pending_orders[ticker].get("name", ticker)
            self.orders.cancel_pending(ticker)
            logger.info(f"[EOD] {name}({ticker}) 미체결 주문 장마감 취소")

        return results

    # ──────────────────────────────────────────
    # 정오(12:00) 이후 단타 포지션 정리
    # ──────────────────────────────────────────
    def check_midday_liquidation(self, current_hour: int, current_minute: int) -> list:
        """
        12시 이후 Track A(단타) 포지션 정리 및 스윙 전환 전략
        - Track A 종목 중 현재가가 진입가 또는 손절가 위에서 지지받고 있다면 Track C(종배)로 전환.
        - 그렇지 못하고 흐르는 종목은 약손절/본절 탈출.
        """
        results = []
        if current_hour < 12:
            return results

        tickers = list(self.orders.positions.keys())
        for ticker in tickers:
            pos = self.orders.positions[ticker]
            if pos["track"] == "A":
                quote = self.kis.get_quote(ticker)
                current = quote.get("current", 0)
                if current <= 0:
                    continue

                entry = pos["entry_price"]
                change = (current - entry) / entry if entry > 0 else 0
                name = pos.get("name", ticker)
                dynamic_sl = pos.get("dynamic_sl_price", 0)

                # 손절가 이탈 여부
                if dynamic_sl > 0 and current <= dynamic_sl:
                    logger.info(f"[Midday] {name}({ticker}) 지지 이탈. 약손절 ({change*100:+.2f}%)")
                    r = self.orders.sell(ticker, reason=f"오후 단타 지지 이탈 {change*100:+.1f}%")
                    if r:
                        r["trigger"] = "MIDDAY_LIQUIDATION"
                        results.append(r)
                    continue

                # 12시 이후 수익권이거나 진입가 부근(-1.5% 이상)에서 지지 중이면 종가베팅(Track C)으로 전환
                if change >= -0.015:
                    logger.info(f"[Midday] {name}({ticker}) 12시 이후 지지 확인. Track C(종배)로 전환 (수익률: {change*100:+.2f}%)")
                    pos["track"] = "C"
                    pos["reason"] += " [오후 스윙 전환]"
                    self.orders._save_positions()
                else:
                    # 그 외 흐르는 종목은 약손절
                    if current_hour >= 13 or (current_hour == 12 and current_minute >= 30):
                        logger.info(f"[Midday] {name}({ticker}) Track A 오후 모멘텀 상실 약손절 ({change*100:+.2f}%)")
                        r = self.orders.sell(ticker, reason=f"오후 모멘텀 상실 약손절 {change*100:+.1f}%")
                        if r:
                            r["trigger"] = "MIDDAY_LIQUIDATION"
                            results.append(r)

        return results
