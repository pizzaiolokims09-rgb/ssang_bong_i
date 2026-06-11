"""
run.py - 쌍봉봇 메인 심장박동 루프
매뉴얼 Section 7 실행 지침 구현

매매 시간 정책:
  - 프리장: 차단
  - 09:00~09:30: 관망 (스캔만, 매매 안 함)
  - 09:30~15:20: 본장 매매 (Phase 1 -> Phase 2 -> 주문)
  - 15:20~15:30: 종가 베팅
  - 15:30~: 당일 종료
"""
import logging
import os
import sys
import threading
import time
from datetime import datetime

from dotenv import load_dotenv

import trader.pykrx_scanner as pykrx_scanner

# ──────────────────────────────────────────────
# 로깅 설정
# ──────────────────────────────────────────────
load_dotenv()

# data 디렉토리 생성
os.makedirs("data", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("data/ssangbong.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("ssangbong.main")


def main():
    """메인 실행 함수"""
    from trader.kis_client import KISClient
    from trader.signals import BaseScreener, detect_volume_spike, aggregate_5m_candles, find_5m_pivot_low, calculate_envelope, calculate_bb_ema, evaluate_smc_structure, calculate_vwap, calculate_atr, _is_excluded_name
    from trader.quant_indicators import get_ml_features
    from trader.ai_router import MultiAssetCouncil, TRACKS, volume_confirm_filter
    from trader.orders import OrderManager
    from trader.monitor import PositionMonitor
    from trader.telegram_bot import TelegramBot
    from trader.journal import TradeJournal
    from trader.ai_fundamental import FundamentalScanner

    # ──────────────────────────────────────
    # 모듈 초기화
    # ──────────────────────────────────────
    logger.info("=" * 50)
    logger.info("   쌍봉봇 (Ssangbong Bot) 시작")
    logger.info("=" * 50)

    kis = KISClient()
    screener = BaseScreener(kis)
    council = MultiAssetCouncil()
    orders = OrderManager(kis)
    monitor = PositionMonitor(kis, orders)
    telegram = TelegramBot()
    journal = TradeJournal(council)
    fundamental = FundamentalScanner()  # Phase 3: Fail-Close 최종 관문

    # 텔레그램 명령어 및 콜백 등록
    def cmd_status():
        status = monitor.get_portfolio_status()
        status["total_capital"] = orders.total_capital
        status["daily_pnl"] = journal.get_daily_pnl()
        status["cumulative_pnl"] = journal.get_cumulative_pnl()
        telegram.notify_status(status)

    def cmd_profit():
        daily_pnl = journal.get_daily_pnl()
        telegram.send(f"💰 <b>당일 실현 손익</b>: {daily_pnl:+,.0f}원")

    def cmd_journal():
        summary = journal.generate_daily_summary()
        telegram.send(summary)

    # -------- 종목별 비상청산 --------
    def cmd_emergency_menu():
        telegram.send_position_keyboard(orders.positions, "ask_sell", "🚨 <b>어떤 종목을 비상 청산할까요?</b>")

    def ask_sell_execute(ticker: str):
        # 2단계 확인
        name = orders.positions.get(ticker, {}).get("name", ticker)
        telegram.send_confirm_inline(
            message=f"정말 <b>{name}</b> 종목을 지금 시장가로 전량 청산하시겠습니까?",
            confirm_data=f"confirm_sell_{ticker}"
        )

    def confirm_sell_execute(ticker: str):
        if ticker in orders.positions:
            r = orders.sell(ticker, reason="수동 비상청산")
            if r:
                telegram.notify_sell(r)
                journal.record_trade(r, orders.total_capital)
                telegram.send(f"✅ {ticker} 비상 청산 완료.")
            else:
                telegram.send(f"❌ {ticker} 증권사 API 에러(모의투자 미체결 잔고 등)로 청산 주문이 거부되었습니다. 장부에서 강제로 삭제하려면 /강제삭제 {ticker} 를 입력하세요.")
        else:
            telegram.send(f"⚠️ {ticker} 종목을 보유하고 있지 않거나 이미 매도되었습니다.")

    def cmd_force_delete(ticker: str):
        if ticker in orders.positions:
            name = orders.positions[ticker].get("name", ticker)
            del orders.positions[ticker]
            orders._save_positions()
            telegram.send(f"🗑️ {name}({ticker}) 종목이 봇의 장부에서 강제 삭제되었습니다. (실계좌 수동 정리 요망)")
        else:
            telegram.send(f"⚠️ {ticker} 종목이 장부에 없습니다.")

    # -------- 종목별 추가매수 --------
    def cmd_add_buy_menu():
        telegram.send_position_keyboard(orders.positions, "ask_buy", "🛒 <b>어떤 종목을 추가 매수할까요?</b>")

    def ask_buy_execute(ticker: str):
        name = orders.positions.get(ticker, {}).get("name", ticker)
        telegram.send_confirm_inline(
            message=f"정말 <b>{name}</b> 종목을 추가 매수(불타기/물타기) 하시겠습니까?",
            confirm_data=f"confirm_buy_{ticker}"
        )

    def confirm_buy_execute(ticker: str):
        telegram.send(f"🛒 {ticker} 추가 매수는 현재 개발 중인 기능입니다.")

    # -------- 긴급익절 --------
    def cmd_take_profit_menu():
        """수익 중인 종목 목록을 보여주고 선택하면 즉시 익절"""
        if not orders.positions:
            telegram.send("⚠️ 현재 보유 중인 종목이 없습니다.")
            return
        # 전체 보유 종목 표시 (수익/손실 여부와 무관하게 선택 가능)
        telegram.send_position_keyboard(orders.positions, "ask_tp", "💰 <b>어떤 종목을 긴급 익절할까요?</b>")

    def ask_tp_execute(ticker: str):
        pos = orders.positions.get(ticker, {})
        name = pos.get("name", ticker)
        entry = pos.get("entry_price", 0)
        quote = kis.get_quote(ticker)
        current = quote.get("current", entry)
        pnl_pct = (current - entry) / entry * 100 if entry > 0 else 0
        telegram.send_confirm_inline(
            message=f"<b>{name}</b> 종목을 시장가로 전량 매도합니다.\n현재 손익: {pnl_pct:+.2f}%\n\n정말 실행하시겠습니까?",
            confirm_data=f"confirm_tp_{ticker}"
        )

    def confirm_tp_execute(ticker: str):
        if ticker in orders.positions:
            r = orders.sell(ticker, reason="긴급익절(수동)")
            if r:
                telegram.notify_sell(r)
                journal.record_trade(r, orders.total_capital)
                telegram.send(f"✅ {ticker} 긴급익절 완료.")
            else:
                telegram.send(f"❌ {ticker} 매도 주문 실패. 증권사 API를 확인하세요.")
        else:
            telegram.send(f"⚠️ {ticker} 종목을 보유하고 있지 않습니다.")

    # -------- 마스터 비상정지 --------
    def cmd_emergency_stop_ask():
        telegram.send_confirm_inline(
            message="모든 포지션을 <b>전량 시장가 청산</b>하고 신규 진입을 <b>차단(정지)</b> 하시겠습니까?",
            confirm_data="confirm_stop"
        )

    def confirm_emergency_stop():
        results = orders.liquidate_all("텔레그램 마스터 비상정지")
        for r in results:
            telegram.notify_sell(r)
            journal.record_trade(r, orders.total_capital)
        telegram.notify_system("비상정지 완료. 모든 포지션 청산 및 봇 정지됨.")

    def cmd_resume():
        orders.kill_switch = False
        orders._save_daily_state()  # 재시작에도 해제 상태 유지
        telegram.notify_system("봇 재개됨. 킬 스위치 해제.")

    # /start 명령어 (메뉴 재전송)
    def cmd_start():
        telegram.send_menu()

    # 텍스트 명령어 매핑 (탭다운 버튼 텍스트)
    telegram.register_command("start", cmd_start)
    telegram.register_command("상태", cmd_status)
    telegram.register_command("수익", cmd_profit)
    telegram.register_command("매매일지", cmd_journal)
    telegram.register_command("비상청산", cmd_emergency_menu)
    telegram.register_command("긴급익절", cmd_take_profit_menu)
    telegram.register_command("추가매수", cmd_add_buy_menu)
    telegram.register_command("비상정지", cmd_emergency_stop_ask)
    telegram.register_command("재개시", cmd_resume)
    telegram.register_command("강제삭제", cmd_force_delete)
    
    # 2단계 확인용 콜백 핸들러 매핑
    telegram.register_command("ask_sell", ask_sell_execute)          # 종목 선택됨 -> 2단계 물어보기
    telegram.register_command("confirm_sell", confirm_sell_execute)  # 컨펌됨 -> 진짜 팔기
    
    telegram.register_command("ask_buy", ask_buy_execute)            # 종목 선택됨 -> 2단계 물어보기
    telegram.register_command("confirm_buy", confirm_buy_execute)    # 컨펌됨 -> 진짜 사기

    telegram.register_command("ask_tp", ask_tp_execute)              # 긴급익절 종목 선택됨
    telegram.register_command("confirm_tp", confirm_tp_execute)      # 긴급익절 컨펌됨

    telegram.register_command("confirm_stop", confirm_emergency_stop) # 비상정지 컨펌됨

    # 실잔고 기반 자본 동기화 (SYNC_CAPITAL=true 시, 복리 운용)
    orders.sync_capital_from_balance()

    # 시작 알림 및 메인 메뉴 전송
    env_label = "모의투자" if kis.env == "demo" else "실전매매"
    telegram.send(
        f"🤖 <b>쌍봉봇 기동!</b>\n"
        f"환경: {env_label}\n"
        f"계좌: {kis.trade_account_no}\n"
        f"자본: {orders.total_capital:,.0f}원"
    )
    telegram.send_menu()

    # ──────────────────────────────────────
    # 스캔 주기 설정
    # ──────────────────────────────────────
    SCAN_INTERVAL = 60     # 기본 스캔 주기 (초)
    MONITOR_INTERVAL = 10  # 포지션 감시 주기 (초)

    # ──────────────────────────────────────
    # 포지션 감시 전용 스레드
    # 메인 루프가 Gemini 추론(수십 초~수 분)으로 블로킹되어도
    # 손절/익절 감시는 10초 주기를 유지해야 한다.
    # ──────────────────────────────────────
    def monitor_worker():
        while True:
            time.sleep(MONITOR_INTERVAL)
            try:
                t = datetime.now()
                if t.weekday() >= 5:
                    continue
                # 장중(09:00~15:30)에만 감시
                if t.hour < 9 or t.hour >= 16 or (t.hour == 15 and t.minute > 30):
                    continue
                if not orders.positions:
                    continue
                results = monitor.check_all_positions()
                for r in results:
                    telegram.notify_sell(r)
                    journal.record_trade(r, orders.total_capital)
            except Exception as e:
                logger.error(f"[MonitorThread] 감시 루프 에러: {e}", exc_info=True)

    threading.Thread(target=monitor_worker, daemon=True, name="position-monitor").start()
    logger.info("[Main] 포지션 감시 전용 스레드 시작 (10초 주기, 메인 루프와 독립)")

    last_scan_time = 0
    last_monitor_time = 0
    last_idle_log_time = 0
    scan_count = 0
    journal_generated_today = False
    eod_executed_today = False
    
    # 알림 스팸 방지용 캐시 (종목별 SKIP 알림 시간 기록)
    skip_notified_cooldown = {}
    SKIP_NOTIFY_INTERVAL = 3600  # 1시간 (초)

    # AI SKIP 판정 캐시: 최근 SKIP/저신뢰 판정 종목은 일정 시간 재분석 생략
    # (동일 주도주를 60초마다 Gemini Pro로 재분석하던 비용/지연 낭비 차단)
    ai_skip_cache = {}
    AI_SKIP_TTL = 900  # 15분 (초)

    # B/F 전용 수급주 유니버스 캐시 (pykrx 기관/외인 순매수 상위, 5분 갱신)
    # 눌림목(B)/장기 눌림(F)은 '오늘 조용한 종목'이라 거래량 상위 유니버스에
    # 구조적으로 잡히지 않으므로 독립 유니버스가 필요하다.
    swing_universe_cache = {"ts": 0.0, "stocks": []}
    
    BOT_START_TIME = time.time()

    logger.info("메인 루프 진입...")

    # 일일 큐레이션 상태 초기화
    pykrx_fetched_today = False
    daily_swing_candidates = []

    def get_swing_universe() -> list:
        """pykrx 기관/외인 순매수 상위 종목을 B/F 전용 유니버스로 구성 (5분 캐시).
        candidates와 동일한 dict 형태로 반환하여 트랙 스캐너에 바로 투입 가능."""
        if time.time() - swing_universe_cache["ts"] < 300:
            return swing_universe_cache["stocks"]

        stocks = []
        for t in daily_swing_candidates[:40]:
            quote = kis.get_quote(t)
            time.sleep(0.05)
            cur = quote.get("current", 0)
            if cur <= 0:
                continue
            nm = quote.get("name", t)
            if _is_excluded_name(nm):
                continue
            # 최소 유동성: 당일 거래대금 10억 이상
            if quote.get("trade_amount", 0) < 1_000_000_000:
                continue
            stocks.append({
                "ticker":       t,
                "name":         nm,
                "current":      cur,
                "volume":       quote.get("volume", 0),
                "trade_amount": quote.get("trade_amount", 0),
                "market_cap":   quote.get("market_cap", 0),
                "change_pct":   quote.get("change_pct", 0),
                "exec_strength": quote.get("execution_strength", 0),
            })

        swing_universe_cache["ts"] = time.time()
        swing_universe_cache["stocks"] = stocks
        if stocks:
            logger.info(f"[SwingUniverse] pykrx 수급주 유니버스 {len(stocks)}개 갱신")
        return stocks
    
    # 기동 시각이 장중(09:00~15:30)이거나, 16시 이전이면 즉시 1회 스캔 (캐싱)
    now_hour = datetime.now().hour
    if 9 <= now_hour < 16:
        logger.info("봇 기동 중 (장중/마감직후): pykrx 스캔 1회 즉시 실행")
        daily_swing_candidates = pykrx_scanner.fetch_daily_swing_candidates(top_n=50)
        pykrx_fetched_today = True

    # 미국장 연동 주도 테마 리서치 (기동 시 1회 실행)
    daily_themes = fundamental.update_daily_themes()
    theme_map = {}  # {ticker: 테마명} — Phase 3 세그먼트 검증 및 분석 우선순위에 사용
    if daily_themes and "briefing" in daily_themes:
        theme_names = []
        for t in daily_themes.get("themes", []):
            sector_name = t.get("us_sector", "")
            for s in t.get("kr_stocks", []):
                tk = s.get("ticker", "")
                if tk:
                    theme_map[tk] = sector_name
            theme_names.extend([s.get("name", s.get("ticker")) for s in t.get("kr_stocks", [])])
        theme_names_str = ", ".join(theme_names) if theme_names else "없음"
        telegram.send(f"🌍 <b>[글로벌 주도 테마 리서치 완료]</b>\n{daily_themes['briefing']}\n\n추출된 관련주: {theme_names_str}")
    else:
        logger.warning("[Theme] 일일 테마 데이터 로드 실패 또는 없음")

    # ──────────────────────────────────────
    # 메인 무한 루프
    # ──────────────────────────────────────
    try:
        while True:
            # CPU/메모리 누수 방지 및 1초 주기 타이머 역할 (모든 지연 방지)
            time.sleep(1)

            now = datetime.now()
            current_hour = now.hour
            current_minute = now.minute
            current_time = time.time()

            # 텔레그램 명령어 수신 (항시 즉각 반응, 지연 없음)
            telegram.poll_commands()

            # 킬 스위치 활성화 시 매매 차단
            if orders.kill_switch:
                continue

            # ─────────────────────────────
            # 매매 시간 정책 판별
            # ─────────────────────────────
            is_weekday = now.weekday() < 5  # 월~금

            if not is_weekday:
                if current_time - last_idle_log_time > 3600:
                    logger.info("[시간] 주말 대기 중...")
                    last_idle_log_time = current_time
                continue

            # 프리장 + 장 마감 후 (15:30 초과 ~ 익일 09:00 전)
            # 주의: "hour >= 15 and minute > 30" 단독 조건은 16:00~16:30 같은
            # 매시 00~30분 구간을 차단하지 못하므로 hour >= 16 조건을 별도로 둔다.
            is_after_hours = (
                current_hour < 9
                or current_hour >= 16
                or (current_hour == 15 and current_minute > 30)
            )

            if is_after_hours:
                # 자정이 넘어가면 일지 생성 플래그 리셋 (다음날 준비)
                if current_hour == 0:
                    journal_generated_today = False
                    eod_executed_today = False
                    pykrx_fetched_today = False
                    # 메모리 누수 방지: 캐시 초기화
                    skip_notified_cooldown.clear()
                    ai_skip_cache.clear()
                    orders.entry_cooldown.clear()
                    # 일일 카운터 리셋 + 파일 영속화 (재시작에도 유지되는 상태를 날짜 단위로 초기화)
                    orders.reset_daily_state()
                    # Track A 차단 알림 플래그 리셋
                    if hasattr(main, '_track_a_block_logged'):
                        del main._track_a_block_logged
                    import gc; gc.collect()

            # ─────────────────────────────
            # 일일 스윙/메가트렌드 매집주 추출 (08:50)
            # ─────────────────────────────
            if current_hour == 8 and current_minute >= 50 and not pykrx_fetched_today:
                logger.info("[Main] 08:50 일일 스윙/메가트렌드 후보군(pykrx) 추출 시작")
                daily_swing_candidates = pykrx_scanner.fetch_daily_swing_candidates(top_n=50)
                pykrx_fetched_today = True
                # 장 시작 전 실잔고 기준 자본 재배분 (SYNC_CAPITAL=true 시)
                orders.sync_capital_from_balance()

            # 프리장 + 장 마감 후 다시 체크 (8시 50분 작업 이후 스킵 처리)
            if is_after_hours:
                # 장 마감 일지 요약 및 AI 고도화 루틴 (15:35 이후 최초 1회)
                if current_hour == 15 and current_minute > 35 and not journal_generated_today:
                    journal_generated_today = True
                    logger.info("[Main] 장 마감: 일일 매매일지 및 AI 고도화 루틴 시작")
                    
                    # 1) 당일 매매 요약 전송
                    summary = journal.generate_daily_summary()
                    telegram.send(summary)

                    # 1.5) 당일 SKIP 후보 종가 평가 (반사실 ML 데이터 라벨링)
                    journal.evaluate_shadows(kis)

                    # 2) AI 두뇌 고도화 루틴 (30개 도달 시 10개 요약)
                    opt_msg = journal.optimize_brain_if_needed()
                    if opt_msg:
                        telegram.send(opt_msg)
                        
                if current_time - last_idle_log_time > 3600:
                    logger.info("[시간] 장 외 대기 중...")
                    last_idle_log_time = current_time
                continue

            # ─────────────────────────────
            # 포지션 모니터링 (10초 간격)
            # ─────────────────────────────
            if current_time - last_monitor_time >= MONITOR_INTERVAL:
                last_monitor_time = current_time

                # 1) 보유 포지션 손익 감시는 전용 스레드(monitor_worker)에서 수행

                # 2) 미체결 주문 체결 확인
                confirmed = monitor.check_pending_fills()
                for c in confirmed:
                    telegram.send(
                        f"✅ <b>지정가 체결 확인!</b>\n"
                        f"종목: {c['name']} ({c['ticker']})\n"
                        f"진입가: {c['entry_price']:,}원 | Track {c['track']}\n"
                        f"이제 자동 손익 감시가 시작됩니다."
                    )

                # 2.5) 추가 매수 (분할매수) 타점 감시
                pyramid_results = monitor.check_pyramiding(current_hour, current_minute)
                for pr in pyramid_results:
                    telegram.send(
                        f"📈 <b>추가 분할매수 완료! ({pr['step']}차)</b>\n"
                        f"종목: {pr['name']} ({pr['ticker']})\n"
                        f"추가: {pr['added_qty']}주 | 누적: {pr['total_qty']}주\n"
                        f"새 평단가: {pr['new_avg_price']:,.0f}원\n"
                        f"사유: {pr['reason']}"
                    )

                # 3) 미체결 주문 TTL 만료 처리 (5분 경과)
                expired_list = orders.get_expired_pending()
                for exp in expired_list:
                    ticker = exp["ticker"]
                    retry = exp["retry_count"]

                    # 주문 취소
                    orders.cancel_pending(ticker)

                    if retry < orders.MAX_RETRY:
                        # 재시도 가능: 최신 차트로 AI 재분석
                        logger.info(f"[Adaptive] {exp['name']}({ticker}) TTL 만료 -> AI 재분석 ({retry+1}/{orders.MAX_RETRY})")
                        candles = kis.get_minute_chart(ticker)
                        daily_candles = kis.get_daily_chart(ticker)
                        orderbook = kis.get_orderbook(ticker)
                        time.sleep(0.5)

                        if not candles or not daily_candles:
                            logger.info(f"[Adaptive] {exp['name']} API 데이터 미수신 -> 재분석 건너뜀")
                            continue

                        quote = kis.get_quote(ticker)
                        stock_info = {
                            "ticker": ticker, 
                            "name": exp["name"],
                            "current": quote.get("current", 0),
                            "change_pct": quote.get("change_pct", 0),
                            "volume": quote.get("volume", 0)
                        }
                        new_route = council.route(stock_info, candles, daily_candles, orderbook)

                        if new_route["track"] != "SKIP" and new_route["confidence"] >= 0.5:
                            new_route["_retry_count"] = retry + 1
                            buy_result = orders.buy(ticker, new_route)
                            if buy_result:
                                telegram.send(
                                    f"🔄 <b>가격 조정 재주문 ({retry+1}/{orders.MAX_RETRY})</b>\n"
                                    f"종목: {exp['name']} ({ticker})\n"
                                    f"새 진입가: {new_route['entry_price']:,}원"
                                )
                        else:
                            telegram.send(
                                f"⏸️ <b>주문 대기 → 후보 잔류</b>\n"
                                f"종목: {exp['name']} ({ticker})\n"
                                f"사유: 모멘텀 약화 ({retry+1}회차). 다음 스캔에서 재검토합니다."
                            )
                    else:
                        # 5회 재시도 초과: 후보 풀에 잔류시키기 위해 그냥 로그만 남김
                        telegram.send(
                            f"🔕 <b>재시도 한도 도달</b>\n"
                            f"종목: {exp['name']} ({ticker})\n"
                            f"5회 시도 후 미체결. 다음 스캔에서 조건 부합 시 재진입 시도합니다."
                        )

                # 4) 장마감 정리 (15:10~15:20, 1회만 실행)
                if current_hour == 15 and current_minute >= 10 and current_minute < 20:
                    if not eod_executed_today:
                        eod_executed_today = True
                        logger.info("[EOD] 장마감 포지션 정리 시작 (15:10)")
                        eod_results = monitor.check_eod_liquidation()
                        for r in eod_results:
                            telegram.notify_sell(r)
                            journal.record_trade(r, orders.total_capital)
                        if eod_results:
                            telegram.send(f"📋 장마감 정리 완료: {len(eod_results)}개 종목 매도")
                        else:
                            telegram.send("📋 장마감 점검 완료: 정리 대상 없음 (전 종목 홀드)")
                            
                        # 관심종목 일괄 초기화
                        orders.clear_watchlist()

                # 5) 점심정리 로직 제거됨 (사용자 요청)

            # ─────────────────────────────
            # Phase 1 + Phase 2 스캔 (60초 간격)
            # ─────────────────────────────
            if current_time - last_scan_time < SCAN_INTERVAL:
                continue

            last_scan_time = current_time
            scan_count += 1

            # 종가 베팅 시간 판별 (14:45 부터 15:20 마감 전까지)
            is_closing_time = (current_hour == 15 and current_minute >= 0) or (current_hour == 14 and current_minute >= 45)

            # ─────── Watchlist ML Sniper 감시 ───────
            def check_watchlist_sniper():
                """관심종목을 감시하며 ML 승률 70% 도달 시 매수 격발"""
                from trader.quant_indicators import get_ml_features
                if not orders.watchlist:
                    return
                    
                logger.info(f"[Sniper] 관심종목 {len(orders.watchlist)}개 감시 중...")
                for tck in list(orders.watchlist.keys()):
                    item = orders.watchlist[tck]
                    name = item.get("name", tck)
                    trk = item.get("track", "")
                    route_res = item.get("route_result", {})
                    
                    # 현재가 등 데이터 조회
                    cndls = kis.get_minute_chart(tck)
                    d_cndls = kis.get_daily_chart(tck)
                    
                    if not cndls or not d_cndls:
                        continue
                        
                    cur_price = cndls[0].get("close", 0)
                    if cur_price <= 0:
                        continue
                        
                    # 최신 피처값 추출
                    features = get_ml_features(d_cndls, cndls)

                    # ML 승률 게이트 (모델 보유 시 70% 기준)
                    ml_result = fundamental.ml_predict_gate(name, tck, features)
                    prob = ml_result.get("confidence", 0)

                    # 모델 미보유 시 confidence가 항상 50으로 고정되어 70%를
                    # 영원히 못 넘는 데드락이 발생하므로, 룰 기반 폴백으로 격발한다.
                    has_ml_model = os.path.exists("data/ml_brain.pkl")
                    if has_ml_model:
                        fire = prob >= 70
                        wait_reason = f"ML 승률 대기 중: {prob:.1f}%"
                    elif trk == "C":
                        # 종가 베팅: 14:45 이후 도달 시 격발
                        t_now = datetime.now()
                        fire = (t_now.hour == 14 and t_now.minute >= 45) or t_now.hour == 15
                        wait_reason = "종가 베팅 시간(14:45) 대기 중"
                    else:
                        # B/D/F 등: AI가 제시한 진입가 +1% 이내 도달 시 격발
                        target_entry = route_res.get("entry_price", 0)
                        fire = target_entry > 0 and cur_price <= target_entry * 1.01
                        wait_reason = f"진입가 대기 중 (목표 {target_entry:,} / 현재 {cur_price:,})"

                    # 종가베팅(C)은 격발 직전 재검증: 아침에 담긴 종목이
                    # 오후에 망가졌을 수 있으므로 MA5 지지/체결강도/약세 여부 확인
                    if fire and trk == "C":
                        closes5 = [c["close"] for c in d_cndls[:5]]
                        ma5_w = sum(closes5) / len(closes5) if closes5 else 0
                        q_w = kis.get_quote(tck)
                        es_w = q_w.get("execution_strength", 0)
                        prev_close_w = d_cndls[1]["close"] if len(d_cndls) > 1 else 0
                        if (ma5_w > 0 and cur_price < ma5_w) \
                                or (0 < es_w < 100) \
                                or (prev_close_w > 0 and cur_price < prev_close_w):
                            fire = False
                            wait_reason = (
                                f"종배 재검증 실패 (MA5={ma5_w:,.0f} "
                                f"체결강도={es_w:.0f} 전일종가={prev_close_w:,})"
                            )

                    if fire:
                        logger.info(f"[Sniper] 🎯 {name}({tck}) 매수 조건 충족! (ML {prob:.1f}%, 모델={'O' if has_ml_model else 'X'}) -> 매수 격발")
                        route_res["entry_price"] = cur_price
                        route_res["quant_features"] = features

                        b_result = orders.buy(tck, route_res)
                        if b_result:
                            telegram.notify_buy(b_result)
                            logger.info(f"[BUY] {name} Track {trk} Sniper 매수 완료!")

                        orders.remove_from_watchlist(tck)
                    else:
                        logger.info(f"[Sniper] {name}({tck}) {wait_reason}")
                        
            check_watchlist_sniper()

            logger.info(f"[Scan #{scan_count}] Phase 1 스캔 시작 ({now.strftime('%H:%M:%S')})")

            # ─────── Phase 1: Base Screener ───────
            candidates = screener.scan()

            if not candidates:
                logger.info("[Scan] Phase 1 통과 종목 없음")
                continue

            # 재부팅 안전장치: 구동 후 3분(180초) 간은 신규 진입을 보류하고 시장 관망
            if current_time - BOT_START_TIME < 180:
                logger.info(f"[안전장치] 시스템 부팅 안정화 대기 중 (신규 진입 보류). 잔여: {180 - (current_time - BOT_START_TIME):.0f}초")
                continue

            # 시장 폭락 감지 브레이커
            if kis.is_market_crashing():
                logger.warning("📉 시장 지수 급락 감지! 신규 Track A/B/C 진입을 일시 중단합니다.")
                time.sleep(10)
                continue

            # ─────── Phase 1.5: 눌림목 피벗 스나이핑 (Track A 즉시 격발) ───────
            # 09:30 이후 ~ 14:30 이전만 피벗 스나이핑 가동 (장초반 변동성 회피 + 종배 겹침 방지)
            track_a_time_ok = (current_hour == 9 and current_minute >= 30) or (10 <= current_hour < 14) or (current_hour == 14 and current_minute < 30)
            # 일일 Track A 손절 4회 이상이면 비활성화
            track_a_loss_blocked = orders.daily_track_a_losses >= 4
            if track_a_loss_blocked:
                if not hasattr(main, '_track_a_block_logged'):
                    logger.warning("🛑 [안전장치] 일일 Track A 손절 4회 도달! Track A 신규 진입 금일 차단")
                    telegram.send("🛑 Track A 손절 4회 누적 → 금일 Track A 신규 진입 차단됨")
                    main._track_a_block_logged = True
            if track_a_time_ok and not track_a_loss_blocked:
                pullback_stocks = screener.scan_track_a_pullback(candidates[:15])  # 상위 15개 탐색

                for p_stock in pullback_stocks:
                    p_ticker = p_stock["ticker"]

                    # 이미 보유 중이면 건너뛰기
                    if p_ticker in orders.positions or p_ticker in orders.pending_orders:
                        continue

                    # 1분봉 조회
                    minute_candles = kis.get_minute_chart(p_ticker)
                    time.sleep(0.1)
                    
                    if not minute_candles:
                        continue

                    # 저변동성 필터: 1분 ATR이 가격의 0.15% 미만이면 호가 탄력이 없는
                    # 무거운 종목(ETF류 등) → 단타 회전 불가, Track A 부적합
                    atr_1m = calculate_atr(minute_candles, period=20)
                    if atr_1m > 0 and p_stock["current"] > 0 and atr_1m / p_stock["current"] < 0.0015:
                        logger.info(
                            f"  🚫 [저변동성] {p_stock['name']} 1분 ATR={atr_1m:,.0f} "
                            f"({atr_1m / p_stock['current'] * 100:.3f}%) → Track A 부적합"
                        )
                        continue

                    # 5분봉 합성
                    five_min_candles = aggregate_5m_candles(minute_candles)

                    # 1. 1분봉 거래량 폭발 감지
                    spike = detect_volume_spike(minute_candles, vol_multiplier=2.0, lookback=20)
                    
                    # 2. 구조적 MSS 판단 (1분봉 BB하단터치→복귀 + EMA상향 + BOS지지)
                    # 1분봉 사용: KIS API가 30봉 반환 → BB(20) 계산에 충분
                    five_min_trend_shift = False
                    bb_ema = calculate_bb_ema(minute_candles)

                    if bb_ema and len(minute_candles) >= 20:
                        latest = minute_candles[0]
                        bb_lower = bb_ema["bb_lower"]

                        # 조건 1: 직전 10분(10봉) 내 BB 하단 터치 (= 유동성 스윕)
                        recent_candles = minute_candles[1:11]
                        touched_bb_lower = any(
                            c["low"] <= bb_lower for c in recent_candles
                        )

                        # 조건 2: 현재봉이 BB 하단 위로 복귀 (= 스윕 후 반전)
                        recovered_above = latest["close"] > bb_lower

                        # 조건 3: EMA(10) 상향 유지 (= 대세 상승 추세 확인)
                        ema_rising = bb_ema["ema_rising"]

                        # 조건 4: 현재가가 전일 BOS 라인(전일고가) 근처 이상에서 지지
                        bos_intact = latest["close"] >= p_stock["yesterday_high"] * 0.99

                        # SMC 라이브러리 직접 연동 (Phase 3)
                        smc_result = evaluate_smc_structure(minute_candles)
                        smc_confirmed = smc_result["bos"] or smc_result["choch"] or smc_result["liquidity_swept"]

                        if touched_bb_lower and recovered_above and ema_rising and bos_intact:
                            if smc_confirmed:
                                five_min_trend_shift = True
                                logger.info(
                                    f"  ✅ [구조적 MSS + SMC] {p_stock['name']} "
                                    f"완벽한 셋업 확인! (BB하단={bb_lower:,.0f} EMA={bb_ema['ema']:,.0f}) "
                                    f"[SMC: BOS={smc_result['bos']} CHoCH={smc_result['choch']} LiqSwept={smc_result['liquidity_swept']}]"
                                )
                            else:
                                logger.info(
                                    f"  ❌ [SMC 미달] {p_stock['name']} BB조건은 맞으나 SMC 확증 실패 "
                                    f"[BOS={smc_result['bos']} CHoCH={smc_result['choch']} LiqSwept={smc_result['liquidity_swept']}]"
                                )
                        else:
                            logger.info(
                                f"  ❌ [구조적 MSS] {p_stock['name']} 조건 미달 "
                                f"(BB터치={touched_bb_lower} 복귀={recovered_above} "
                                f"EMA상향={ema_rising} BOS={bos_intact})"
                            )
                    elif not bb_ema:
                        logger.info(
                            f"  ⏳ [구조적 MSS] {p_stock['name']} 1분봉 데이터 부족 "
                            f"({len(minute_candles)}봉/최소20봉 필요)"
                        )

                    # 휩쏘 방지를 위해 5분봉 추세 전환(MSS)은 '필수'로 요구함. 
                    # 거래량 폭발은 보조 지표로만 사용.
                    if not five_min_trend_shift:
                        continue
                        
                    trigger_reason = "5분봉 추세 전환(MSS)" + (" + 거래량 폭발" if spike else "")

                    # ── VWAP 필터 (기관 매매 흐름 확인) ──
                    # 당일 VWAP = 누적거래대금 / 누적거래량 (분봉 30개로는 30분치 VWAP밖에 안 나옴)
                    day_volume = p_stock.get("volume", 0)
                    if day_volume > 0 and p_stock.get("trade_amount", 0) > 0:
                        vwap = p_stock["trade_amount"] / day_volume
                    else:
                        vwap = calculate_vwap(minute_candles)  # 폴백: 최근 30분 VWAP
                    if vwap > 0 and p_stock["current"] < vwap:
                        logger.info(
                            f"  🚫 [VWAP] {p_stock['name']} 현재가={p_stock['current']:,} < "
                            f"VWAP={vwap:,.0f} → 기관 매도 구간, 진입 보류"
                        )
                        continue

                    # AI SKIP 캐시: 최근 15분 내 SKIP 판정 종목은 재분석 생략
                    if current_time - ai_skip_cache.get(p_ticker, 0) < AI_SKIP_TTL:
                        logger.info(f"  ⏳ [AI Cache] {p_stock['name']} 최근 SKIP 판정 → 재분석 생략")
                        continue

                    # ── AI 2중 검증 (God Mode 제거) ──
                    logger.info(
                        f"🔍 [Pivot Sniper] {p_stock['name']}({p_ticker}) "
                        f"MSS + VWAP 통과! AI 검증 시작..."
                    )

                    # AI 라우팅에 필요한 데이터 조회
                    daily_candles_p = kis.get_daily_chart(p_ticker)
                    time.sleep(0.1)
                    orderbook_p = kis.get_orderbook(p_ticker)
                    time.sleep(0.1)

                    if not daily_candles_p:
                        logger.info(f"  ⏳ {p_stock['name']} 일봉 데이터 미수신 → 패스")
                        continue

                    p_stock["track_hints"] = ["A"]
                    cd_val_p = orders.sell_cooldown.get(p_ticker)
                    if cd_val_p:
                        cd_ts_p = cd_val_p.get("timestamp", 0) if isinstance(cd_val_p, dict) else cd_val_p
                        p_stock["is_reentry"] = (time.time() - cd_ts_p < 3600)
                    else:
                        p_stock["is_reentry"] = False

                    route_result = council.route(p_stock, minute_candles, daily_candles_p, orderbook_p)

                    # AI가 SKIP 판정하면 진입 보류
                    if route_result["track"] == "SKIP":
                        logger.info(
                            f"  ❌ [AI 검증] {p_stock['name']} AI SKIP → "
                            f"{route_result['reason'][:120]}"
                        )
                        ai_skip_cache[p_ticker] = current_time
                        journal.record_shadow(p_ticker, p_stock["name"],
                                              f"AI SKIP: {route_result['reason'][:100]}",
                                              p_stock["current"],
                                              route_result.get("quant_features", {}))
                        continue

                    # 검색식 통과 종목은 신뢰도 60% 이상 요구
                    if route_result["confidence"] < 0.6:
                        logger.info(
                            f"  ❌ [AI 검증] {p_stock['name']} 신뢰도 부족 "
                            f"({route_result['confidence']:.0%}) → 진입 보류"
                        )
                        ai_skip_cache[p_ticker] = current_time
                        continue

                    # AI가 다른 트랙(B/G 등)으로 판정한 종목을 단타 손절폭으로
                    # 진입하지 않는다 → Phase 2 경로에서 해당 트랙으로 재검토
                    if route_result["track"] != "A":
                        logger.info(
                            f"  ↪️ [AI 검증] {p_stock['name']} AI 판정 Track {route_result['track']} "
                            f"→ 단타(A) 진입 보류 (Phase 2 경로에서 재검토)"
                        )
                        continue

                    # 매뉴얼 원칙: Track A는 엔벨로프(20, 12.5%) 발산 영역 필수
                    env_p = calculate_envelope(daily_candles_p)
                    env_upper_p = env_p.get("env_upper", 0)
                    if not (p_stock["current"] > env_upper_p > 0):
                        logger.info(
                            f"  🚫 [엔벨로프] {p_stock['name']} 발산 영역 아님 "
                            f"(현재가={p_stock['current']:,} ≤ 상단={env_upper_p:,}) → Track A 부적합"
                        )
                        ai_skip_cache[p_ticker] = current_time
                        continue

                    logger.info(
                        f"  ✅ [AI 검증] {p_stock['name']} Track {route_result['track']} "
                        f"승인! 신뢰도={route_result['confidence']:.0%}"
                    )

                    # ── ML 승률 게이트 (로컬 모델, 비용/지연 없음) ──
                    features_p = route_result.get("quant_features") or get_ml_features(daily_candles_p, minute_candles)
                    ml_gate_p = fundamental.ml_predict_gate(p_stock["name"], p_ticker, features_p)
                    if ml_gate_p["verdict"] == "REJECT":
                        logger.info(
                            f"  ❌ [ML Gate] {p_stock['name']} 승률 예측 미달 → 진입 보류 "
                            f"({ml_gate_p.get('reason', '')})"
                        )
                        continue

                    # ── ATR 기반 동적 손절선 (변동성 적응형) ──
                    track_a_info = TRACKS["A"].copy()
                    
                    pivot_low = find_5m_pivot_low(five_min_candles, lookback=6)
                    if pivot_low <= 0 or pivot_low > p_stock["current"]:
                        pivot_low = p_stock["current"] * 0.98
                    
                    atr = calculate_atr(minute_candles, period=20)
                    if atr > 0:
                        # ATR x 1.5 여유를 준 동적 손절
                        dynamic_sl = int(pivot_low - atr * 1.5)
                    else:
                        # ATR 계산 불가 시 피벗 로우 -2% fallback
                        dynamic_sl = int(pivot_low * 0.98)
                    
                    # 최소 -2%, 최대 -5% 범위로 클램핑
                    sl_floor = int(p_stock["current"] * 0.95)  # -5% 최대 손절
                    sl_ceil = int(p_stock["current"] * 0.98)   # -2% 최소 손절
                    dynamic_sl = max(sl_floor, min(dynamic_sl, sl_ceil))
                    
                    verified_route = {
                        "name": p_stock["name"],
                        "track": "A",
                        "track_info": track_a_info,
                        "reason": (
                            f"피벗 스나이핑 + {trigger_reason} + "
                            f"AI 검증({route_result['confidence']:.0%})"
                        ),
                        "confidence": route_result["confidence"],
                        "entry_price": p_stock["current"],
                        "god_mode": False,  # God Mode 제거
                        "trigger_candle_low": pivot_low,
                        "dynamic_sl_price": dynamic_sl,
                        "peak_200d": 0,
                        "atr_value": route_result.get("atr_value", 0),
                        "atr_sl_price": route_result.get("atr_sl_price", 0),
                        "atr_tp_price": route_result.get("atr_tp_price", 0),
                        # ML 학습 데이터 축적: 진입 시점 퀀트 피처 저장
                        "quant_features": features_p,
                    }

                    buy_result = orders.buy(p_ticker, verified_route)
                    if buy_result:
                        telegram.notify_buy(buy_result)
                        telegram.send(
                            f"⚡ <b>[Pivot 스나이핑 (Track A) - AI 검증 완료]</b>\n"
                            f"종목: {p_stock['name']} ({p_ticker})\n"
                            f"진입근거: {trigger_reason} + AI {route_result['confidence']:.0%}\n"
                            f"VWAP: {vwap:,.0f}원 (현재가 > VWAP ✅)\n"
                            f"손절선: {dynamic_sl:,}원 (ATR기반, 피벗={pivot_low:,})"
                        )
                        logger.info(f"[BUY] {p_stock['name']} Track A AI검증 스나이핑 매수 완료!")

                    time.sleep(0.5)

            # ─────── Phase 1.7: 트랙별 데이터 기반 사전 필터 ───────
            # 각 트랙의 영웅문 검색기 정량 조건을 미리 검증하여 AI 참고 정보로 제공
            track_hints = {}  # {ticker: ["B", "C", ...]}

            # B/F 독립 유니버스 (pykrx 수급주) — 거래량 상위에 안 잡히는 눌림목 후보 보강
            swing_stocks = get_swing_universe()
            cand_tickers_top = {c["ticker"] for c in candidates[:10]}
            swing_only = [s for s in swing_stocks if s["ticker"] not in cand_tickers_top]
            swing_by_ticker = {s["ticker"]: s for s in swing_only}

            # Track B: 눌림목 스윙 (항시, 거래량 상위 10개 + 수급주 유니버스)
            try:
                theme_tickers = daily_themes.get("all_tickers", []) if daily_themes else []
                b_pool = (candidates[:10] + swing_only)[:25]
                b_stocks = screener.scan_track_b(b_pool, theme_tickers=theme_tickers)
                for s in b_stocks:
                    track_hints.setdefault(s["ticker"], []).append("B")
            except Exception as e:
                logger.warning(f"[PreFilter] Track B 스캔 에러: {e}")

            # Track C: 종가 베팅 (15:00 이후에만)
            if is_closing_time:
                try:
                    c_stocks = screener.scan_track_c(candidates[:10])
                    for s in c_stocks:
                        track_hints.setdefault(s["ticker"], []).append("C")
                except Exception as e:
                    logger.warning(f"[PreFilter] Track C 스캔 에러: {e}")

            # Track D: 세력주 매집 (오후 14시 이후)
            if current_hour >= 14:
                try:
                    d_stocks = screener.scan_track_d(candidates[:10])
                    for s in d_stocks:
                        track_hints.setdefault(s["ticker"], []).append("D")
                except Exception as e:
                    logger.warning(f"[PreFilter] Track D 스캔 에러: {e}")

            # Track E: 폭락주 스나이핑 (항시, 상위 10개)
            try:
                e_stocks = screener.scan_track_e(candidates[:10])
                for s in e_stocks:
                    track_hints.setdefault(s["ticker"], []).append("E")
            except Exception as e:
                logger.warning(f"[PreFilter] Track E 스캔 에러: {e}")

            # Track F: 메가 트렌드 장기 눌림목 (항시, 상위 10개 + 수급주 유니버스)
            try:
                f_pool = (candidates[:10] + swing_only)[:15]
                f_stocks = screener.scan_track_f(f_pool)
                for s in f_stocks:
                    track_hints.setdefault(s["ticker"], []).append("F")
            except Exception as e:
                logger.warning(f"[PreFilter] Track F 스캔 에러: {e}")

            # Track G: CCI & MACD 더블 모멘텀 스윙 (14:30 이후, 상위 15개)
            # 당일 일봉이 거의 완성된 시점에 판정해 장중 가짜 크로스(리페인트) 방지
            if (current_hour == 14 and current_minute >= 30) or current_hour == 15:
                try:
                    g_stocks = screener.scan_track_g(candidates[:15])
                    for s in g_stocks:
                        track_hints.setdefault(s["ticker"], []).append("G")
                except Exception as e:
                    logger.warning(f"[PreFilter] Track G 스캔 에러: {e}")

            if track_hints:
                logger.info(f"[PreFilter] 트랙 후보 태깅: {track_hints}")

            # ─────── Phase 2: AI Routing ───────
            # 상위 5개 + 트랙 사전필터(B~G)에 태깅된 6~15위 종목 + 수급주 유니버스에서
            # 태깅된 종목까지 분석 (태깅만 해놓고 라우팅을 안 하면 진입 기회가 사라짐)
            hinted_extra = [s for s in candidates[5:15] if s["ticker"] in track_hints]
            swing_hinted = [s for t, s in swing_by_ticker.items() if t in track_hints]
            analysis_targets = candidates[:5] + hinted_extra[:5] + swing_hinted[:3]

            # 글로벌 테마 관련주 우선 분석 (수급이 몰릴 확률이 높은 종목부터)
            analysis_targets.sort(key=lambda s: 0 if s["ticker"] in theme_map else 1)

            for stock in analysis_targets:
                ticker = stock["ticker"]

                # 이미 보유 중이면 건너뛰기
                if ticker in orders.positions:
                    continue

                # AI SKIP 캐시: 최근 15분 내 SKIP 판정 종목은 재분석 생략 (비용 절감)
                if current_time - ai_skip_cache.get(ticker, 0) < AI_SKIP_TTL:
                    continue

                # 포지션 한도 체크 (단기와 중장기 합산 5개 도달 시 중단)
                total_max = orders.max_short_positions + orders.max_swing_positions
                if len(orders.positions) >= total_max:
                    logger.info("[Scan] 최대 포지션 도달 (단기+중장기 꽉 참), 스캔 일시 중단")
                    break

                # 분봉/일봉/호가 데이터 수집
                candles = kis.get_minute_chart(ticker)
                daily_candles = kis.get_daily_chart(ticker)
                orderbook = kis.get_orderbook(ticker)

                # API 응답 실패 방어
                if not candles or not daily_candles:
                    logger.info(f"[Scan] {stock['name']} API 데이터 미수신 -> 패스")
                    continue

                # API 과부하 방지
                time.sleep(0.5)

                # 볼륨 확증 필터는 트랙 판정 후 돌파형(A/C)에만 적용한다.
                # (눌림목 B / 매집 D / 장기 F는 '거래량이 마른' 상태가 전제라
                #  사전 일괄 적용 시 해당 트랙 후보가 구조적으로 전멸함)

                # 사전 필터 결과를 AI에 참고 정보로 전달
                stock["track_hints"] = track_hints.get(ticker, [])

                # 쿨다운 상태 확인 (손절/익절 이력이 있는 종목인지 AI에게 알려주기 위함)
                is_cooldown = False
                if ticker in orders.sell_cooldown:
                    cd_val = orders.sell_cooldown[ticker]
                    cd_ts = cd_val.get("timestamp", 0) if isinstance(cd_val, dict) else cd_val
                    if time.time() - cd_ts < 3600:
                        is_cooldown = True
                stock["is_reentry"] = is_cooldown

                # AI 라우팅
                route_result = council.route(stock, candles, daily_candles, orderbook)

                logger.info(
                    f"[Route] {stock['name']} -> Track {route_result['track']} "
                    f"({route_result['reason']}) "
                    f"신뢰도={route_result['confidence']:.0%}"
                )

                # 하드 필터: Track A는 엔벨로프 발산 영역 필수, B~F는 사전 정량 필터 통과 필수
                ai_track = route_result["track"]
                hints = stock.get("track_hints", [])

                # Track A 하드필터: Phase 2에서 AI가 판정한 경우, 엔벨로프 발산 영역 체크
                if ai_track == "A":
                    env = calculate_envelope(daily_candles) if daily_candles else {}
                    env_upper = env.get("env_upper", 0)
                    if not (stock.get("current", 0) > env_upper > 0):
                        logger.info(
                            f"[HardFilter] {stock['name']} Track A 선택되었으나 "
                            f"엔벨로프 발산 영역 아님 (현재가={stock.get('current',0):,} / "
                            f"상단={env_upper:,}) -> SKIP 강제 전환")
                        route_result["track"] = "SKIP"
                        route_result["reason"] = f"엔벨로프 발산 영역 미달 (현재가 < 상단 {env_upper:,}원)"

                # Track B~F 하드필터: 사전 정량 필터 통과 필수
                elif ai_track in ["B", "C", "D", "E", "F"] and ai_track not in hints:
                    logger.info(
                        f"[HardFilter] {stock['name']} Track {ai_track} 선택되었으나 "
                        f"사전 정량 필터 미통과 -> SKIP 강제 전환 "
                        f"(통과 트랙: {hints or '없음'})")
                    route_result["track"] = "SKIP"
                    route_result["reason"] = f"영웅문 검색기 정량 필터 미통과 (Track {ai_track})"

                # Track B, F 추가 하드필터: 기관/외인 순매수(pykrx) 큐레이션 리스트에 있어야 함
                if ai_track in ["B", "F"] and route_result["track"] != "SKIP":
                    if ticker not in daily_swing_candidates:
                        logger.info(
                            f"[HardFilter] {stock['name']} Track {ai_track} 선택되었으나 "
                            f"일일 pykrx 매집주 큐레이션 리스트에 없어 SKIP"
                        )
                        route_result["track"] = "SKIP"
                        route_result["reason"] = f"pykrx 기관/외인 수급 상위 매집주 리스트에 없음"

                if route_result["track"] == "SKIP":
                    logger.info(f"[SKIP] {stock['name']} 진입 보류: {route_result.get('reason', '')}")
                    ai_skip_cache[ticker] = current_time
                    journal.record_shadow(ticker, stock["name"],
                                          f"SKIP: {route_result.get('reason', '')[:100]}",
                                          stock.get("current", 0),
                                          route_result.get("quant_features", {}))
                    continue

                # 안전장치 #1: 볼륨 확증 필터 (돌파형 트랙 A/C에만 적용)
                if route_result["track"] in ["A", "C"] and not volume_confirm_filter(candles):
                    logger.info(f"[Scan] {stock['name']} 볼륨 확증 미달 (Track {route_result['track']}) -> 패스")
                    continue

                # 종가 베팅 시간이 아닌데 Track C로 판정되면 건너뛰기
                if route_result["track"] == "C" and not is_closing_time:
                    logger.info(f"[Route] Track C지만 종가시간 아님 (15:00~) -> 대기")
                    continue

                # 단타(Track A)는 09:30~14:30만 허용 (장초반 변동성 회피 + 종배 겹침 방지)
                if route_result["track"] == "A":
                    if current_hour == 9 and current_minute < 30:
                        logger.info(f"[Route] Track A는 09:30 이후부터 진입 가능 (장초반 변동성 회피) -> 대기")
                        continue
                    if current_hour > 14 or (current_hour == 14 and current_minute >= 30):
                        logger.info(f"[Route] Track A는 오후 14:30까지만 진입 가능 -> 대기")
                        continue
                    if orders.daily_track_a_losses >= 4:
                        logger.info(f"[Route] Track A 일일 손절 {orders.daily_track_a_losses}회 → 금일 Track A 비활성화")
                        continue

                # 본장 시간에 Track D(중장기)는 오후에만 허용
                if route_result["track"] == "D" and current_hour < 14:
                    logger.info(f"[Route] Track D는 오후 진입 -> 대기")
                    continue

                # Track F는 God Mode 절대 금지 + 종가 기준 분할 매집만 허용
                if route_result["track"] == "F":
                    route_result["god_mode"] = False
                    route_result["track_info"]["order_type"] = "limit"
                    logger.info(f"[Route] Track F 메가트렌드: God Mode 금지, 종가 분할 매집 모드")

                # 신뢰도 50% 미만은 건너뛰기
                if route_result["confidence"] < 0.5:
                    logger.info(f"[Route] 신뢰도 부족 ({route_result['confidence']:.0%}) -> 패스")
                    ai_skip_cache[ticker] = current_time
                    continue

                # ─────── 상한가 원천 차단 ───────
                # 방법 1: change_pct 기반 (candidates에 항상 존재)
                if stock.get("change_pct", 0) >= 25.0:
                    logger.info(
                        f"[Block] {stock['name']}({ticker}) 상한가 차단! "
                        f"등락률={stock['change_pct']:+.1f}% -> 진입 불가"
                    )
                    continue
                # 방법 2: prev_close 기반 (fallback, prev_close가 있는 경우만)
                prev_close = stock.get("prev_close", 0)
                current_price = stock.get("current", 0)
                if prev_close > 0 and current_price > 0:
                    change_from_prev = (current_price - prev_close) / prev_close
                    if change_from_prev >= 0.25:
                        logger.info(
                            f"[Block] {stock['name']}({ticker}) 상한가 근접 차단! "
                            f"전일종가={prev_close:,} 현재={current_price:,} "
                            f"(+{change_from_prev*100:.1f}%) -> 진입 불가"
                        )
                        continue

                # ─────── Phase 3: Fail-Close 최종 관문 ───────
                track = route_result.get("track", "")
                quant_summary = (
                    f"Track: {track}, "
                    f"신뢰도: {route_result.get('confidence', 0):.0%}, "
                    f"ATR: {route_result.get('atr_value', 0):,.0f}, "
                    f"진입가: {route_result.get('entry_price', 0):,}"
                )
                
                # 트랙별 파라미터 분기 (C, D, F는 ML 예측 스킵 후 대기)
                if track in ["C", "D", "F"]:
                    q_features = None
                else:
                    q_features = route_result.get("quant_features", {})
                    
                gate = fundamental.gate_check(
                    stock.get("name", ticker), ticker, track,
                    theme=theme_map.get(ticker, ""),  # 테마 종목이면 세그먼트 팩트체크 가동
                    quant_summary=quant_summary,
                    quant_features=q_features
                )
                if gate["verdict"] == "REJECT":
                    logger.info(
                        f"[Gate REJECT] {stock['name']}({ticker}) "
                        f"Phase 3 검증 실패 -> 매수 차단. "
                        f"사유: {gate.get('summary', '')[:100]}")
                    ai_skip_cache[ticker] = current_time
                    journal.record_shadow(ticker, stock["name"],
                                          f"Gate REJECT: {gate.get('summary', '')[:100]}",
                                          stock.get("current", 0),
                                          route_result.get("quant_features", {}))
                    continue
                elif gate["verdict"] == "REDUCE":
                    logger.info(
                        f"[Gate REDUCE] {stock['name']}({ticker}) "
                        f"Phase 3 비중 삭감. {gate.get('summary', '')[:100]}")
                    route_result["_budget_reduce"] = True  # orders.buy에서 50% 삭감 처리

                # ─────── 주문 실행 또는 관심종목 보관 ───────
                if track in ["C", "D", "F"]:
                    orders.add_to_watchlist(ticker, stock['name'], route_result)
                    telegram.send(f"👀 <b>[{track} 트랙 관심종목 추가]</b>\n종목: {stock['name']}\n펀더멘탈 검증 통과, ML 최적 타점 대기 중")
                    continue
                    
                buy_result = orders.buy(ticker, route_result)
                if buy_result:
                    telegram.notify_buy(buy_result)
                    logger.info(f"[BUY] {stock['name']} Track {route_result['track']} 매수 완료!")

                # API 부하 방지
                time.sleep(1)

            logger.info(f"[Scan #{scan_count}] 완료. 보유={len(orders.positions)}개")

    except KeyboardInterrupt:
        logger.info("사용자 종료 (Ctrl+C)")
        telegram.send("🔴 <b>쌍봉봇 수동 종료</b>")
    except Exception as e:
        logger.critical(f"치명적 에러: {e}", exc_info=True)
        # 인프라성 에러(토큰/네트워크 등)로 보유 포지션을 투매하면 안 되므로
        # 청산하지 않고 그대로 재시작에 맡긴다. (포지션은 positions.json으로 복원됨)
        telegram.send(
            f"🚨 <b>시스템 점검/서버 에러 감지</b>\n"
            f"1분 후 자동 재시작을 시도합니다.\n"
            f"보유 포지션은 청산하지 않고 유지합니다 (재시작 후 감시 재개).\n"
            f"에러 원인: {str(e)[:100]}"
        )
        # 시스템 데몬이 바로 살려내며 스팸을 보내지 못하도록 강제 쿨다운
        time.sleep(60)
    finally:
        logger.info("쌍봉봇 종료")


if __name__ == "__main__":
    main()
