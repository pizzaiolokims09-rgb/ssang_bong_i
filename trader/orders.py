"""
orders.py - 자본금 분배, 매수/매도 주문 및 5대 안전장치 적용
매뉴얼 Section 5 구현

안전장치:
  1. Volume Confirm (거래량 1.5배 확증) -> ai_router.py에 구현
  2. Double Entry Lock (쿨타임 180초)
  3. Absolute Hard SL (절대 손절선)
  4. Kill Switch & Circuit Breaker
  5. God Mode (AI Bypass 즉각 격발)
"""
import math
import os
import threading
import time
import logging
from datetime import datetime
from typing import Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("ssangbong.orders")


class OrderManager:
    """
    주문 관리 및 5대 안전장치 적용
    """

    def __init__(self, kis_client):
        self.kis = kis_client

        # 환경변수에서 매매 파라미터 로드
        self.total_capital     = float(os.environ.get("TOTAL_CAPITAL", 30_000_000))
        self.cash_buffer       = float(os.environ.get("CASH_BUFFER", 0.20))
        # 포트폴리오 슬롯 분리 (올라운더 전략)
        self.max_short_positions = int(os.environ.get("MAX_SHORT_POSITIONS", 3)) # Track A (+ 미분리 시 C 공유)
        self.max_swing_positions = int(os.environ.get("MAX_SWING_POSITIONS", 2)) # Track B, D
        # Track C 전용 슬롯 (0 = 단기 풀을 A와 공유 = 기존 동작).
        # MAX_C_POSITIONS>0 이고 BUDGET_C_PCT>0 이면 C가 A와 완전히 분리된다 (Phase B).
        self.max_c_positions = int(os.environ.get("MAX_C_POSITIONS", 2))

        self._allocate_budgets()

        self.max_daily_loss = float(os.environ.get("MAX_DAILY_LOSS_PCT", 0.03))

        # 수수료/세금 모델 (실현손익 정확도: 왕복 약 0.18% 비용 반영)
        self.commission_rate = float(os.environ.get("COMMISSION_RATE", 0.00015))  # 위탁수수료 편도 0.015%
        self.sell_tax_rate   = float(os.environ.get("SELL_TAX_RATE", 0.0015))     # 증권거래세(매도) 0.15%

        # 상태 관리
        # 주문/포지션 변경 직렬화 락 (감시 스레드와 메인 스레드의 동시 매도 방지)
        self.lock = threading.RLock()
        self.positions: Dict[str, dict] = {}      # {ticker: {entry_price, qty, track, step, ...}}
        self.pending_orders: Dict[str, dict] = {} # {ticker: {order_time, entry_price, step, ...}}
        self.watchlist: Dict[str, dict] = {}      # {ticker: {name, track, reason, ...}}
        self.daily_pnl: float = 0.0                # 당일 실현 손익
        self.entry_cooldown: Dict[str, float] = {} # {ticker: last_entry_timestamp}
        self.sell_cooldown: Dict[str, float] = {}  # {ticker: last_sell_timestamp}
        self.kill_switch: bool = False              # 비상정지 플래그
        self.daily_track_a_losses: int = 0           # 일일 Track A 손절 카운터
        self.env = os.environ.get("KIS_TRADING_ENV", "demo").lower() # 실투/모의 판별
        
        self.positions_file = "data/positions.json"
        self.pending_file = "data/pending_orders.json"
        self.cooldown_file = "data/cooldowns.json"
        self.watchlist_file = "data/watchlist.json"
        self.daily_state_file = "data/daily_state.json"
        self.PENDING_TTL = 300  # 5분 (초)
        self.MAX_RETRY = 5
        self._load_positions()
        self._load_pending()
        self._load_cooldowns()
        self._load_watchlist()
        self._load_daily_state()

    def _allocate_budgets(self):
        """트랙별 자본금 배분 (.env BUDGET_* 비율로 조정 가능).
        기본값: 단기(A/C) 35%, 스윙(B/D) 20%, 메가트렌드(F) 15%, 폭락주(E) 10%.
        현금 버퍼(CASH_BUFFER)는 위기 대응용 예비금으로 남겨둠.
        track_c_capital: BUDGET_C_PCT=0 이면 C가 단기 풀(A)을 공유(기존 동작),
                         >0 이면 C 전용 예산으로 분리 (Phase B)."""
        # [Phase B 결정] 기본값에 'A 축소(20%) + C 분리(15%)' 반영 (.env 없는 서버에서도 적용).
        short_pct = float(os.environ.get("BUDGET_SHORT_PCT", 0.20))   # 단기 A (손실 중 → 35%→20%)
        swing_pct = float(os.environ.get("BUDGET_SWING_PCT", 0.20))
        f_pct     = float(os.environ.get("BUDGET_F_PCT", 0.15))
        e_pct     = float(os.environ.get("BUDGET_E_PCT", 0.10))        # Track E 제거됨(미사용 헤드룸)
        c_pct     = float(os.environ.get("BUDGET_C_PCT", 0.15))       # 종가베팅 C 전용(검증 흑자)
        self.short_capital   = self.total_capital * short_pct
        self.swing_capital   = self.total_capital * swing_pct
        self.track_f_capital = self.total_capital * f_pct
        self.track_e_capital = self.total_capital * e_pct
        self.track_c_capital = self.total_capital * c_pct  # 0이면 A와 공유

    def _c_separated(self) -> bool:
        """Track C가 단기(A) 풀에서 완전히 분리되었는지 여부.
        BUDGET_C_PCT>0 (전용 예산) 그리고 MAX_C_POSITIONS>0 (전용 슬롯) 일 때만 분리."""
        return getattr(self, "track_c_capital", 0) > 0 and self.max_c_positions > 0

    def sync_capital_from_balance(self):
        """실잔고 기반 복리 운용 (SYNC_CAPITAL=true 설정 시에만)
        모의투자 가상체결 환경에서는 계좌 잔고가 실제 매매를 반영하지 못하므로
        기본은 비활성. 실전 전환 시 true 권장."""
        if os.environ.get("SYNC_CAPITAL", "false").lower() != "true":
            return
        try:
            bal = self.kis.get_balance()
            out2 = bal.get("output2") or [{}]
            tot = float(out2[0].get("tot_evlu_amt", 0) or 0)
        except (ValueError, TypeError, IndexError, AttributeError) as e:
            logger.warning(f"[Orders] 잔고 동기화 실패: {e}")
            return
        if tot >= 1_000_000:
            logger.info(f"[Orders] 실잔고 자본 동기화: {self.total_capital:,.0f} -> {tot:,.0f}원")
            self.total_capital = tot
            self._allocate_budgets()

    # ──────────────────────────────────────────
    # 일일 상태 영속화 (재시작 시 손실 한도/손절 카운터 유지)
    # ──────────────────────────────────────────
    def _load_daily_state(self):
        """당일 누적 손익/손절 카운터/킬스위치 복원 (날짜가 같을 때만)"""
        if not os.path.exists(self.daily_state_file):
            return
        try:
            import json
            with open(self.daily_state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == datetime.now().strftime("%Y-%m-%d"):
                self.daily_pnl = float(data.get("daily_pnl", 0.0))
                self.daily_track_a_losses = int(data.get("daily_track_a_losses", 0))
                self.kill_switch = bool(data.get("kill_switch", False))
                logger.info(
                    f"[Orders] 일일 상태 복원: 손익={self.daily_pnl:+,.0f}원 "
                    f"TrackA손절={self.daily_track_a_losses}회 "
                    f"킬스위치={'ON' if self.kill_switch else 'OFF'}")
        except Exception as e:
            logger.error(f"[Orders] 일일 상태 로드 실패: {e}")

    def _save_daily_state(self):
        try:
            import json
            os.makedirs("data", exist_ok=True)
            with open(self.daily_state_file, "w", encoding="utf-8") as f:
                json.dump({
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "daily_pnl": self.daily_pnl,
                    "daily_track_a_losses": self.daily_track_a_losses,
                    "kill_switch": self.kill_switch,
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[Orders] 일일 상태 저장 실패: {e}")

    def reset_daily_state(self):
        """자정 리셋: 다음 날 Track A 재활성화 및 킬스위치 해제"""
        self.daily_pnl = 0.0
        self.daily_track_a_losses = 0
        self.kill_switch = False
        self._save_daily_state()

    def _load_positions(self):
        """저장된 포지션 로드"""
        if os.path.exists(self.positions_file):
            try:
                import json
                with open(self.positions_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for k, v in data.items():
                        if "entry_time" in v and isinstance(v["entry_time"], str):
                            v["entry_time"] = datetime.fromisoformat(v["entry_time"])
                        self.positions[k] = v
                logger.info(f"[Orders] 이전 포지션 {len(self.positions)}개 로드 완료")
            except Exception as e:
                logger.error(f"[Orders] 포지션 로드 실패: {e}")

    def _load_pending(self):
        """저장된 미체결 주문 로드"""
        if os.path.exists(self.pending_file):
            try:
                import json
                with open(self.pending_file, "r", encoding="utf-8") as f:
                    self.pending_orders = json.load(f)
                if self.pending_orders:
                    logger.info(f"[Orders] 미체결 대기 주문 {len(self.pending_orders)}개 로드 완료")
            except Exception as e:
                logger.error(f"[Orders] 미체결 주문 로드 실패: {e}")

    def _save_positions(self):
        """현재 포지션 저장"""
        try:
            import json
            os.makedirs("data", exist_ok=True)
            safe_pos = {}
            for k, v in self.positions.items():
                v_copy = v.copy()
                if "entry_time" in v_copy and isinstance(v_copy["entry_time"], datetime):
                    v_copy["entry_time"] = v_copy["entry_time"].isoformat()
                safe_pos[k] = v_copy
            with open(self.positions_file, "w", encoding="utf-8") as f:
                json.dump(safe_pos, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[Orders] 포지션 저장 실패: {e}")

    def _save_pending(self):
        """미체결 주문 저장"""
        try:
            import json
            os.makedirs("data", exist_ok=True)
            with open(self.pending_file, "w", encoding="utf-8") as f:
                json.dump(self.pending_orders, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[Orders] 미체결 주문 저장 실패: {e}")

    def _load_cooldowns(self):
        """쿨다운(매수/매도 이력) 파일 로드"""
        if os.path.exists(self.cooldown_file):
            try:
                import json
                with open(self.cooldown_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.entry_cooldown = data.get("entry_cooldown", {})
                    self.sell_cooldown = data.get("sell_cooldown", {})
            except Exception as e:
                logger.error(f"[Orders] 쿨다운 로드 실패: {e}")

    def _save_cooldowns(self):
        """쿨다운(매수/매도 이력) 파일 저장"""
        try:
            import json
            os.makedirs("data", exist_ok=True)
            with open(self.cooldown_file, "w", encoding="utf-8") as f:
                json.dump({
                    "entry_cooldown": self.entry_cooldown,
                    "sell_cooldown": self.sell_cooldown
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[Orders] 쿨다운 저장 실패: {e}")

    def _load_watchlist(self):
        """저장된 관심종목 로드"""
        if os.path.exists(self.watchlist_file):
            try:
                import json
                with open(self.watchlist_file, "r", encoding="utf-8") as f:
                    self.watchlist = json.load(f)
                if self.watchlist:
                    logger.info(f"[Orders] 관심종목 {len(self.watchlist)}개 로드 완료")
            except Exception as e:
                logger.error(f"[Orders] 관심종목 로드 실패: {e}")

    def _save_watchlist(self):
        """관심종목 저장"""
        try:
            import json
            os.makedirs("data", exist_ok=True)
            with open(self.watchlist_file, "w", encoding="utf-8") as f:
                json.dump(self.watchlist, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[Orders] 관심종목 저장 실패: {e}")

    def add_to_watchlist(self, ticker: str, name: str, route_result: dict):
        """관심종목에 추가 (펀더멘탈 통과 후 매수 대기)"""
        self.watchlist[ticker] = {
            "name": name,
            "track": route_result.get("track"),
            "reason": route_result.get("reason", ""),
            "confidence": route_result.get("confidence", 0.0),
            "route_result": route_result,
            "added_time": datetime.now().isoformat()
        }
        self._save_watchlist()
        logger.info(f"[Watchlist] {name}({ticker}) 관심종목 추가 완료 (Track {route_result.get('track')})")

    def remove_from_watchlist(self, ticker: str):
        """관심종목에서 제거"""
        if ticker in self.watchlist:
            del self.watchlist[ticker]
            self._save_watchlist()
            logger.info(f"[Watchlist] {ticker} 관심종목에서 제거됨")

    def clear_watchlist(self):
        """관심종목 전체 초기화 (당일 매수 실패 시 장 마감 후 호출)"""
        if self.watchlist:
            count = len(self.watchlist)
            self.watchlist.clear()
            self._save_watchlist()
            logger.info(f"[Watchlist] 관심종목 {count}개 일괄 초기화 완료")

    # ──────────────────────────────────────────
    # 안전장치 #2: Double Entry Lock (쿨타임)
    # ──────────────────────────────────────────
    def _check_cooldown(self, ticker: str, cooldown_sec: int = 180) -> bool:
        """쿨타임 확인. True=진입 가능, False=쿨다운 중"""
        last_entry = self.entry_cooldown.get(ticker, 0)
        elapsed = time.time() - last_entry
        if elapsed < cooldown_sec:
            remaining = cooldown_sec - elapsed
            logger.warning(f"[Lock] {ticker} 쿨타임 중 (잔여 {remaining:.0f}초)")
            return False
        return True

    # ──────────────────────────────────────────
    # 안전장치 #4: Kill Switch 확인
    # ──────────────────────────────────────────
    def _check_kill_switch(self) -> bool:
        """킬 스위치 확인. True=정상, False=정지"""
        if self.kill_switch:
            logger.critical("[Kill] 비상정지 활성화! 모든 주문 차단")
            return False

        # 일일 최대 손실 한도 체크
        if self.daily_pnl <= -(self.total_capital * self.max_daily_loss):
            logger.critical(f"[Kill] 일일 손실 한도 도달: {self.daily_pnl:,.0f}원")
            self.kill_switch = True
            self._save_daily_state()  # 재시작해도 당일은 차단 유지
            return False

        return True

    # ──────────────────────────────────────────
    # 포지션 사이징 (분할 매수 반영)
    # ──────────────────────────────────────────
    def calculate_position_size(self, price: int, track: str, step: int = 1) -> tuple[int, float, list]:
        """
        트랙 및 분할매수 차수(step)에 따른 수량 계산
        return: (주문 수량, 종목당 할당 예산, 분할 비율 배열)
        """
        if track in ["A", "C"]:
            if track == "C":
                # 종배: 1차 30%, 2차 70%. 분리 시 C 전용 예산/슬롯 사용.
                if self._c_separated():
                    budget_per_slot = self.track_c_capital / self.max_c_positions
                else:
                    budget_per_slot = self.short_capital / self.max_short_positions
                target_ratio = [0.3, 0.7]
            else:
                # 단타(A): 단기 풀 슬롯. 분리 시에도 A는 short_capital 사용.
                budget_per_slot = self.short_capital / self.max_short_positions
                target_ratio = [1.0]      # 단타: 몰빵
        elif track == "E":
            # 폭락주 스나이핑: 전체 할당량(10%) 내 4단계 균등 분할
            budget_per_slot = self.track_e_capital
            target_ratio = [0.25, 0.25, 0.25, 0.25]
        elif track == "F":
            # 메가 트렌드 스윙: 전체 할당량(15%) 내 종가 분할매집 (5분할 기준)
            budget_per_slot = self.track_f_capital
            target_ratio = [0.2, 0.2, 0.2, 0.2, 0.2]
        elif track == "B":
            # 눌림목 스윙: 물타기 없이 단일 진입 (슬롯 예산 전액)
            budget_per_slot = self.swing_capital / self.max_swing_positions
            target_ratio = [1.0]
        else:
            # 세력주 매집 (D): 1차 20%, 2차 30%, 3차 50% 분할
            budget_per_slot = self.swing_capital / self.max_swing_positions
            target_ratio = [0.2, 0.3, 0.5]

        if step > len(target_ratio):
            return 0, budget_per_slot, target_ratio

        ratio = target_ratio[step - 1]
        step_budget = budget_per_slot * ratio
        cost_multiplier = 1.0015  # 수수료 및 슬리피지 여유

        if price <= 0:
            logger.error(f"[Orders] 주문 단가 오류 (price={price}) -> 수량 0 반환")
            return 0, budget_per_slot, target_ratio

        quantity = math.floor(step_budget / (price * cost_multiplier))
        return max(quantity, 0), budget_per_slot, target_ratio

    # ──────────────────────────────────────────
    # 매수 주문 (5대 안전장치 적용)
    # ──────────────────────────────────────────
    def buy(self, ticker: str, route_result: dict) -> Optional[dict]:
        """
        매수 주문 실행 (안전장치 전체 통과 후)

        route_result: ai_router.route()의 반환값
        """
        with self.lock:
            return self._buy_locked(ticker, route_result)

    def _buy_locked(self, ticker: str, route_result: dict) -> Optional[dict]:
        track = route_result["track"]
        track_info = route_result["track_info"]
        entry_price = route_result["entry_price"]
        god_mode = route_result.get("god_mode", False)

        # SKIP 트랙은 매수 안 함
        if track == "SKIP":
            logger.info(f"[Order] {ticker} SKIP 트랙 -> 매수 생략")
            return None

        # 트랙 비활성화 스위치 (단일 관문 차단). A=Phase B, D/E=Phase D-4 제거.
        _track_switch = {
            "A": ("ENABLE_TRACK_A", "true"),   # 기본 가동
            "D": ("ENABLE_TRACK_D", "false"),  # 기본 제거
            "E": ("ENABLE_TRACK_E", "false"),  # 기본 제거
        }
        if track in _track_switch:
            _key, _default = _track_switch[track]
            if os.environ.get(_key, _default).lower() != "true":
                logger.info(f"[Order] {ticker} Track {track} 비활성화({_key}=false) -> 매수 생략")
                return None

        # 안전장치 #4: 킬 스위치
        if not self._check_kill_switch():
            return None

        # 안전장치 #2: 쿨타임 (손절 쿨다운은 AI 판단 무시하고 하드 차단)
        if ticker in self.sell_cooldown:
            cooldown_info = self.sell_cooldown[ticker]
            # 하위 호환: 기존 float 값이면 dict로 변환
            if isinstance(cooldown_info, (int, float)):
                cooldown_info = {"timestamp": cooldown_info, "was_stoploss": False}
            elapsed_since_sell = time.time() - cooldown_info.get("timestamp", 0)
            was_stoploss = cooldown_info.get("was_stoploss", False)
            if elapsed_since_sell < 3600:  # 60분 쿨다운
                if track == "A":
                    # Track A는 무조건 차단 (AI 판단 무시)
                    logger.warning(f"[HardBlock] {ticker} 최근 매도/손절 이력. Track A 재진입 강제 차단 (남은: {int(3600-elapsed_since_sell)}초)")
                    return None
                elif was_stoploss:
                    # 손절 이력 종목은 트랙 전환이어도 60분간 차단 (Revenge Trading 방지)
                    logger.warning(f"[HardBlock] {ticker} 손절 이력 종목. Track {track} 재진입도 60분간 차단 (남은: {int(3600-elapsed_since_sell)}초)")
                    return None
                else:
                    logger.info(f"[Order] {ticker} 익절 이력 있으나 트랙 전환(Track {track})으로 쿨다운 해제 승인")

        if not god_mode and not self._check_cooldown(ticker):
            return None

        # 트랙별 포지션 한도 (슬롯) 확인
        def _cnt(tracks):
            return sum(1 for p in self.positions.values() if p["track"] in tracks) + \
                   sum(1 for p in self.pending_orders.values() if p["track"] in tracks)

        # C 분리 시 단기 풀은 A 단독, 미분리 시 A+C 공유 (기존 동작)
        short_tracks = ["A"] if self._c_separated() else ["A", "C"]
        short_count = _cnt(short_tracks)
        c_count = _cnt(["C"])
        swing_count = _cnt(["B", "D"])
        track_e_count = _cnt(["E"])
        track_f_count = _cnt(["F"])
        track_g_count = _cnt(["G"])

        if track == "A" and short_count >= self.max_short_positions:
            logger.warning(f"[Order] 단기(A) 포지션 한도({self.max_short_positions}) 도달 -> 진입 차단")
            return None
        if track == "C":
            if self._c_separated():
                if c_count >= self.max_c_positions:
                    logger.warning(f"[Order] 종가베팅(C) 전용 슬롯 한도({self.max_c_positions}) 도달 -> 진입 차단")
                    return None
            elif short_count >= self.max_short_positions:
                logger.warning(f"[Order] 단기(A/C 공유) 포지션 한도({self.max_short_positions}) 도달 -> 진입 차단")
                return None
        if track in ["B", "D"] and swing_count >= self.max_swing_positions:
            logger.warning(f"[Order] 중장기 포지션 한도({self.max_swing_positions}) 도달 -> 진입 차단")
            return None
        if track == "E" and track_e_count >= 1:
            logger.warning(f"[Order] 폭락주(Track E) 포지션 한도(1) 도달 -> 진입 차단")
            return None
        if track == "F" and track_f_count >= 1:
            logger.warning(f"[Order] 메가트렌드(Track F) 포지션 한도(1) 도달 -> 진입 차단")
            return None
        if track == "G" and track_g_count >= 1:
            logger.warning(f"[Order] CCI&MACD 스윙(Track G) 포지션 한도(1) 도달 -> 진입 차단")
            return None

        # 이미 보유 중이거나 대기 중인 종목
        if ticker in self.positions:
            logger.warning(f"[Order] {ticker} 이미 보유 중 -> 1차 매수 차단 (물타기는 add_buy 호출)")
            return None
        if ticker in self.pending_orders:
            logger.warning(f"[Order] {ticker} 이미 체결 대기 중 -> 중복 주문 차단")
            return None

        # 시장가 여부 판별 (수량 계산 전에 체결가 현실화 필요)
        is_market = god_mode or track_info["order_type"] == "market"

        # 시장가 주문은 매도1호가(ask1)에 체결되는 것이 현실이므로
        # 진입가를 ask1로 보정 (가상 체결 통계의 슬리피지 반영)
        if is_market:
            q_now = self.kis.get_quote(ticker)
            ask1 = q_now.get("ask1", 0)
            if ask1 > 0:
                if ask1 != entry_price:
                    logger.info(f"[Order] {ticker} 시장가 체결가 보정: {entry_price:,} -> ask1={ask1:,}")
                entry_price = ask1

        # 1차 주문 수량 계산 (step=1)
        quantity, allocated_budget, target_ratio = self.calculate_position_size(entry_price, track, step=1)
        if quantity <= 0:
            logger.warning(f"[Order] {ticker} 주문수량 0 -> 매수 불가")
            return None

        # ── Track E, F 전용 하드 락 (누적 매수 금액 초과 차단) ──
        if track in ["E", "F"]:
            max_budget = self.track_e_capital if track == "E" else self.track_f_capital
            
            existing_cost = 0
            for pos in self.positions.values():
                if pos.get("track") == track:
                    existing_cost += pos.get("entry_price", 0) * pos.get("quantity", 0)
            for pend in self.pending_orders.values():
                if pend.get("track") == track:
                    existing_cost += pend.get("entry_price", 0) * pend.get("quantity", 0)

            new_cost = entry_price * quantity
            if existing_cost + new_cost > max_budget:
                logger.warning(
                    f"[HardLock] Track {track} 할당 예산 초과 차단! "
                    f"기존={existing_cost:,.0f} + 신규={new_cost:,.0f} = "
                    f"{existing_cost + new_cost:,.0f} > 한도={max_budget:,.0f}")
                return None

        # 주문가 결정 (시장가=0, 지정가=entry_price)
        price = 0 if is_market else entry_price

        if god_mode:
            logger.info(f"[GOD MODE] {ticker} 시장가 즉시 격발! qty={quantity}")

        # 주문 발송
        result = self.kis.place_order(ticker, "BUY", quantity, price)
        
        # 가상 체결 로직: 모의투자 환경이고 에러가 나면 강제 성공 처리
        is_virtual = False
        if self.env == "demo" and (not result or result.get("error")):
            logger.warning(f"[Virtual Order] 모의투자 500 에러 감지 -> 가상 매수 체결(시장가) 간주 ({ticker})")
            result = {"order_no": "VIRTUAL_BUY"}
            is_market = True # 가상 체결은 무조건 즉시 체결(시장가)로 처리
            is_virtual = True

        if result and not result.get("blocked") and not result.get("error"):
            order_no = result.get("order_no", "")
            self.entry_cooldown[ticker] = time.time()
            self._save_cooldowns()

            # ML 피처 신뢰성: 라우팅 단계에서 비어 오면(rule-based 경로 등)
            # 진입 시점 일봉으로 재계산하여 매매일지에 빈 dict가 기록되는 것을 방지.
            quant_features = route_result.get("quant_features") or {}
            if not quant_features:
                try:
                    from trader.quant_indicators import get_ml_features
                    _daily = self.kis.get_daily_chart(ticker)
                    quant_features = get_ml_features(_daily or [], [])
                except Exception as e:
                    logger.warning(f"[Order] {ticker} ML 피처 재계산 실패: {e}")
                    quant_features = {}

            if is_market:
                # 시장가: 즉시 체결 → positions에 등록
                self.positions[ticker] = {
                    "name": route_result.get("name", ticker),
                    "entry_price": entry_price,
                    "quantity": quantity,
                    "track": track,
                    "track_info": track_info,
                    "entry_time": datetime.now(),
                    "reason": route_result.get("reason", ""),
                    "sl_pct": track_info["sl_pct"],
                    "tp_pct": track_info["tp_pct"],
                    "god_mode": god_mode,
                    "step": 1,
                    "max_step": len(target_ratio),
                    "target_ratio": target_ratio,
                    "allocated_budget": allocated_budget,
                    "trigger_candle_low": route_result.get("trigger_candle_low", 0),
                    "peak_200d": route_result.get("peak_200d", 0),
                    "spider_levels": track_info.get("spider_levels", []),
                    "dynamic_sl_price": route_result.get("dynamic_sl_price", 0),
                    "entry_day_low": route_result.get("entry_day_low", 0),
                    "atr_value": route_result.get("atr_value", 0),
                    "atr_sl_price": route_result.get("atr_sl_price", 0),
                    "atr_tp_price": route_result.get("atr_tp_price", 0),
                    "quant_features": quant_features,
                }
                self._save_positions()
                logger.info(f"[Order] 1차 시장가 체결: {ticker} Track {track} qty={quantity} (비율 {target_ratio[0]*100}%)")
            else:
                # 지정가: pending_orders에 등록하고 체결 대기
                self.pending_orders[ticker] = {
                    "name": route_result.get("name", ticker),
                    "entry_price": entry_price,
                    "quantity": quantity,
                    "track": track,
                    "track_info": track_info,
                    "order_time": time.time(),
                    "order_no": order_no,
                    "retry_count": route_result.get("_retry_count", 0),
                    "reason": route_result.get("reason", ""),
                    "sl_pct": track_info["sl_pct"],
                    "tp_pct": track_info["tp_pct"],
                    "god_mode": god_mode,
                    "step": 1,
                    "max_step": len(target_ratio),
                    "target_ratio": target_ratio,
                    "allocated_budget": allocated_budget,
                    "trigger_candle_low": route_result.get("trigger_candle_low", 0),
                    "peak_200d": route_result.get("peak_200d", 0),
                    "dynamic_sl_price": route_result.get("dynamic_sl_price", 0),
                    "entry_day_low": route_result.get("entry_day_low", 0),
                    "atr_value": route_result.get("atr_value", 0),
                    "atr_sl_price": route_result.get("atr_sl_price", 0),
                    "atr_tp_price": route_result.get("atr_tp_price", 0),
                    "quant_features": quant_features,
                }
                self._save_pending()
                logger.info(f"[Order] 지정가 매수 대기: {ticker} Track {track} qty={quantity} price={entry_price:,} (TTL={self.PENDING_TTL}초)")

            return {
                "action": "BUY",
                "ticker": ticker,
                "name": route_result.get("name", ticker),
                "quantity": quantity,
                "price": entry_price,
                "track": track,
                "track_info": track_info,
                "god_mode": god_mode,
                "reason": route_result.get("reason", ""),
                "is_pending": not is_market,
            }

        return None

    # ──────────────────────────────────────────
    # 추가 매수 (물타기 / 분할매수)
    # ──────────────────────────────────────────
    def add_buy(self, ticker: str, reason: str = "추가 분할 매수") -> Optional[dict]:
        """
        보유 중인 종목의 다음 스텝(step) 추가 매수 실행
        """
        with self.lock:
            return self._add_buy_locked(ticker, reason)

    def _add_buy_locked(self, ticker: str, reason: str) -> Optional[dict]:
        if ticker not in self.positions:
            return None
        
        pos = self.positions[ticker]
        next_step = pos.get("step", 1) + 1
        max_step = pos.get("max_step", 1)

        if next_step > max_step:
            logger.info(f"[AddBuy] {ticker} 최대 분할 횟수({max_step}) 도달 -> 추가 매수 불가")
            return None

        # 안전장치 #4: 킬 스위치
        if not self._check_kill_switch():
            return None

        # 현재가 조회
        quote = self.kis.get_quote(ticker)
        current_price = quote.get("current", 0)
        if current_price <= 0:
            return None

        # 추가 수량 계산
        quantity, _, _ = self.calculate_position_size(current_price, pos["track"], step=next_step)
        if quantity <= 0:
            return None

        # ── Track E 전용: 피라미딩 시에도 15% 하드 락 검증 ──
        if pos.get("track") == "E":
            max_e_budget = self.total_capital * 0.10  # track_e_capital(10%)과 동일하게 통일
            existing_e_cost = 0
            for t, p in self.positions.items():
                if p.get("track") == "E":
                    existing_e_cost += p.get("entry_price", 0) * p.get("quantity", 0)

            new_cost = current_price * quantity
            if existing_e_cost + new_cost > max_e_budget:
                logger.warning(
                    f"[HardLock] Track E 추가매수 15% 한도 초과 차단! "
                    f"기존={existing_e_cost:,.0f} + 추가={new_cost:,.0f} > "
                    f"한도={max_e_budget:,.0f}")
                return None

        # 분할 매수는 기본적으로 무조건 체결을 위해 시장가로 전송 (또는 지정가+대기)
        # 종가베팅/물타기 등 시급하므로 일단 현재가(또는 시장가)로 매수
        # 모의투자 API 특성상 0(시장가) 전송
        result = self.kis.place_order(ticker, "BUY", quantity, 0)
        
        # 가상 체결 로직: 모의투자 환경이고 에러가 나면 강제 성공 처리
        if self.env == "demo" and (not result or result.get("error")):
            logger.warning(f"[Virtual Order] 모의투자 500 에러 감지 -> 가상 추가매수 체결 간주 ({ticker})")
            result = {"order_no": "VIRTUAL_ADD_BUY"}

        if result and not result.get("error"):
            # 평단가 가중 평균 계산
            old_qty = pos["quantity"]
            old_price = pos["entry_price"]
            
            new_total_qty = old_qty + quantity
            new_avg_price = ((old_price * old_qty) + (current_price * quantity)) / new_total_qty

            pos["quantity"] = new_total_qty
            pos["entry_price"] = new_avg_price
            pos["step"] = next_step
            self._save_positions()

            logger.info(f"[AddBuy] {next_step}차 매수 완료: {ticker} (수량 +{quantity}주) -> 새 평단가 {new_avg_price:,.0f}원")
            
            return {
                "action": "ADD_BUY",
                "ticker": ticker,
                "name": pos.get("name", ticker),
                "added_qty": quantity,
                "total_qty": new_total_qty,
                "new_avg_price": new_avg_price,
                "step": next_step,
                "reason": reason
            }
        
        return None

    # ──────────────────────────────────────────
    # 매도 주문
    # ──────────────────────────────────────────
    def sell(self, ticker: str, reason: str = "수동 매도", ratio: float = 1.0) -> Optional[dict]:
        """보유 종목 시장가 매도 (ratio=1.0이면 전량, 0.5이면 절반)"""
        with self.lock:
            return self._sell_locked(ticker, reason, ratio)

    def _sell_locked(self, ticker: str, reason: str, ratio: float) -> Optional[dict]:
        if ticker not in self.positions:
            logger.warning(f"[Order] {ticker} 미보유 -> 매도 불가")
            return None

        pos = self.positions[ticker]
        sell_qty = max(1, int(pos["quantity"] * ratio)) if ratio < 1.0 else pos["quantity"]

        if sell_qty > pos["quantity"]:
            sell_qty = pos["quantity"]

        result = self.kis.place_order(ticker, "SELL", sell_qty, 0)

        # 가상 체결 로직: 모의투자 환경이고 에러가 나면 강제 성공 처리
        if self.env == "demo" and (not result or result.get("error")):
            logger.warning(f"[Virtual Order] 모의투자 500 에러 감지 -> 가상 매도(익절/손절) 체결 간주 ({ticker})")
            result = {"order_no": "VIRTUAL_SELL"}
            reason += " [가상 체결]"

        if result and not result.get("error"):
            # 전량 매도일 때만 쿨다운 등록
            is_stoploss = "손절" in reason
            if sell_qty >= pos["quantity"]:
                self.sell_cooldown[ticker] = {
                    "timestamp": time.time(),
                    "was_stoploss": is_stoploss,
                }
                self._save_cooldowns()
                # Track A 손절 카운터 증가
                if is_stoploss and pos.get("track") == "A":
                    self.daily_track_a_losses += 1
                    logger.info(f"[Counter] Track A 일일 손절 누적: {self.daily_track_a_losses}회")

            # 실현 손익 계산
            # 시장가 매도는 매수1호가(bid1)에 체결되는 것이 현실 → 슬리피지 반영
            quote = self.kis.get_quote(ticker)
            sell_price = quote.get("bid1", 0) or quote.get("current", pos["entry_price"])

            # 수수료/거래세 차감 (매수 수수료 + 매도 수수료 + 증권거래세)
            buy_amount = pos["entry_price"] * sell_qty
            sell_amount = sell_price * sell_qty
            fees = (buy_amount * self.commission_rate
                    + sell_amount * (self.commission_rate + self.sell_tax_rate))
            pnl = (sell_price - pos["entry_price"]) * sell_qty - fees
            self.daily_pnl += pnl
            self._save_daily_state()

            logger.info(
                f"[Order] 매도 완료: {ticker} qty={sell_qty} "
                f"진입={pos['entry_price']:,} 매도={sell_price:,} "
                f"손익={pnl:+,.0f}원 (비용 {fees:,.0f}원 차감) ({reason})"
            )

            # 수량 차감 및 삭제 처리 (리턴값 구성을 위해 del 전에 미리 계산)
            remaining_qty = pos["quantity"] - sell_qty
            pos_name = pos.get("name", ticker)
            pos_reason = pos.get("reason", "")
            pos_track = pos.get("track", "A")
            pos_features = pos.get("quant_features", {})

            # 종목명 견고화: 빈 문자열이나 티커로 떨어지지 않도록 우선순위 해석
            # (매매일지 name 컬럼에 티커가 박히던 문제 방지)
            resolved_name = quote.get("name") or ""
            if not resolved_name or resolved_name == ticker:
                resolved_name = pos_name if pos_name and pos_name != ticker else (pos_name or ticker)

            pos["quantity"] -= sell_qty
            if pos["quantity"] <= 0:
                del self.positions[ticker]
            self._save_positions()

            return {
                "action": "SELL",
                "ticker": ticker,
                "name": resolved_name,
                "quantity": sell_qty,
                "entry_price": pos["entry_price"],
                "sell_price": sell_price,
                "pnl": pnl,
                "fees": fees,
                "reason": pos_reason,
                "sell_reason": reason,
                "track": pos_track,
                "remaining_qty": max(remaining_qty, 0),
                # ML 학습 데이터: 진입 시점의 퀀트 피처를 매매일지로 전달
                "quant_features": pos_features,
            }

        return None

    # ──────────────────────────────────────────
    # 전량 청산 (비상정지)
    # ──────────────────────────────────────────
    def liquidate_all(self, reason: str = "비상정지") -> list:
        """보유 전 종목 시장가 청산 + 미체결 주문 전부 취소"""
        results = []
        # 미체결 주문 취소
        for ticker in list(self.pending_orders.keys()):
            self.cancel_pending(ticker)
        # 보유 종목 청산
        tickers = list(self.positions.keys())
        for ticker in tickers:
            r = self.sell(ticker, reason=reason)
            if r:
                results.append(r)
        self.kill_switch = True
        self._save_daily_state()
        logger.critical(f"[Kill] 전량 청산 완료: {len(results)}개 종목 ({reason})")
        return results

    # ──────────────────────────────────────────
    # 미체결 주문 관리 (Adaptive Order)
    # ──────────────────────────────────────────
    def confirm_pending(self, ticker: str) -> Optional[dict]:
        """미체결 주문이 체결됨 → positions로 이동"""
        with self.lock:
            return self._confirm_pending_locked(ticker)

    def _confirm_pending_locked(self, ticker: str) -> Optional[dict]:
        if ticker not in self.pending_orders:
            return None

        pend = self.pending_orders[ticker]
        self.positions[ticker] = {
            "name": pend.get("name", ticker),
            "entry_price": pend["entry_price"],
            "quantity": pend["quantity"],
            "track": pend["track"],
            "track_info": pend["track_info"],
            "entry_time": datetime.now(),
            "reason": pend.get("reason", ""),
            "sl_pct": pend["sl_pct"],
            "tp_pct": pend["tp_pct"],
            "god_mode": pend.get("god_mode", False),
            "step": pend.get("step", 1),
            "max_step": pend.get("max_step", 1),
            "target_ratio": pend.get("target_ratio", []),
            "allocated_budget": pend.get("allocated_budget", 0),
            "trigger_candle_low": pend.get("trigger_candle_low", 0),
            "peak_200d": pend.get("peak_200d", 0),
            "spider_levels": pend.get("spider_levels", []),
            "dynamic_sl_price": pend.get("dynamic_sl_price", 0),
            "atr_value": pend.get("atr_value", 0),
            "atr_sl_price": pend.get("atr_sl_price", 0),
            "atr_tp_price": pend.get("atr_tp_price", 0),
            "quant_features": pend.get("quant_features", {}),
        }
        del self.pending_orders[ticker]
        self._save_positions()
        self._save_pending()

        logger.info(f"[Order] 지정가 체결 확인: {pend.get('name', ticker)}({ticker}) -> positions 이동 완료")
        return {
            "action": "CONFIRMED",
            "ticker": ticker,
            "name": pend.get("name", ticker),
            "entry_price": pend["entry_price"],
            "quantity": pend["quantity"],
            "track": pend["track"],
        }

    def cancel_pending(self, ticker: str) -> bool:
        """미체결 주문 취소 (KIS API + 장부 정리)"""
        with self.lock:
            return self._cancel_pending_locked(ticker)

    def _cancel_pending_locked(self, ticker: str) -> bool:
        if ticker not in self.pending_orders:
            return False

        pend = self.pending_orders[ticker]
        order_no = pend.get("order_no", "")

        # KIS 취소 API 호출
        if order_no and not order_no.startswith("VIRTUAL_"):
            self.kis.cancel_order(order_no, ticker, pend["quantity"])
        elif order_no and order_no.startswith("VIRTUAL_"):
            logger.info(f"[Virtual Cancel] 가상 체결 대기 주문 취소 간주 ({ticker})")

        name = pend.get("name", ticker)
        del self.pending_orders[ticker]
        self._save_pending()
        logger.info(f"[Order] 미체결 주문 취소: {name}({ticker})")
        return True

    def get_expired_pending(self) -> list:
        """TTL 만료된 미체결 주문 목록 반환 (취소는 안 함)"""
        expired = []
        now = time.time()
        for ticker, pend in self.pending_orders.items():
            elapsed = now - pend.get("order_time", now)
            if elapsed >= self.PENDING_TTL:
                expired.append({
                    "ticker": ticker,
                    "name": pend.get("name", ticker),
                    "entry_price": pend["entry_price"],
                    "quantity": pend["quantity"],
                    "track": pend["track"],
                    "track_info": pend["track_info"],
                    "retry_count": pend.get("retry_count", 0),
                    "elapsed": elapsed,
                })
        return expired
