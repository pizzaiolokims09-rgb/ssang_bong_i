"""
backtest.py - 쌍봉봇 일봉 백테스트 엔진 (라이브 매매와 완전 독립, 읽기 전용)

무엇을 하나:
  과거 일봉을 pykrx로 가져와, 매 거래일 시점에서 trader/track_rules.py의 검색식을
  그대로 적용하고, 트리거되면 '익일 시가 진입 → 손절/익절/최대보유일 청산'을
  결정론적으로 시뮬레이션한다. 트랙별 거래수·승률·기대값을 산출한다.

무엇을 못 하나 (정직한 한계):
  - Track A의 1분봉 MSS/VWAP, Gemini AI 라우팅/게이트는 일봉으로 재현 불가 → 제외.
    (Track A는 minute_collector.py로 분봉을 모은 뒤 별도 검증)
  - '거래대금 상위 N위' 교차 랭킹은 전종목 일괄조회(KRX 로그인)가 필요 → 절대필터만 적용.
  - 하루 안의 손절/익절 선후관계는 알 수 없어 '같은 날 둘 다 닿으면 손절 우선'(보수적) 가정.
  - Track C 체결강도(장중)·Track D 유보율(pykrx 부재)은 제외/근사.

사용:
  .venv/Scripts/python.exe -m backtest.backtest --start 2024-01-01 --end 2026-06-20
  .venv/Scripts/python.exe -m backtest.backtest --track G --sweep      # 파라미터 스윕
"""
import argparse
import logging
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trader import track_rules
from backtest.seed_tickers import load_seed_tickers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("backtest")

CACHE_DIR = "data/backtest_cache"
ROUND_TRIP_COST = 0.003   # 왕복 수수료+세금+슬리피지 근사 (~0.3%)

# 트랙별 기본 청산 파라미터 (sl_pct, tp_pct, max_hold_days). ai_router.TRACKS 기반.
DEFAULT_EXITS = {
    "B": {"sl_pct": 0.05, "tp_pct": 0.15, "max_hold": 5},
    "C": {"sl_pct": 0.05, "tp_pct": 0.05, "max_hold": 2},
    "E": {"sl_pct": 0.15, "tp_pct": 0.05, "max_hold": 30},
    "F": {"sl_pct": 0.07, "tp_pct": 0.50, "max_hold": 90},
    "G": {"sl_pct": 0.08, "tp_pct": 0.20, "max_hold": 20},  # G는 Hold-to-TP 근사
}


# ──────────────────────────────────────────────
# 데이터 로딩 (pykrx 단일종목 일봉, 디스크 캐시)
# ──────────────────────────────────────────────
def fetch_history(ticker: str, start: str, end: str) -> list:
    """pykrx로 일봉을 가져와 KIS 포맷(최신이 앞)의 list로 반환. 디스크 캐시."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, f"{ticker}_{start}_{end}.csv")
    if os.path.exists(cache):
        df = pd.read_csv(cache, dtype={"date": str})
    else:
        from pykrx import stock
        s = start.replace("-", "")
        e = end.replace("-", "")
        try:
            raw = stock.get_market_ohlcv(s, e, ticker)  # 단일종목 = 로그인 불필요
        except Exception as ex:
            logger.warning(f"{ticker} 조회 실패: {ex}")
            return []
        if raw is None or raw.empty:
            return []
        raw = raw.reset_index()
        # 컬럼: 날짜, 시가, 고가, 저가, 종가, 거래량, (거래대금), (등락률)
        df = pd.DataFrame({
            "date": pd.to_datetime(raw.iloc[:, 0]).dt.strftime("%Y%m%d"),
            "open": raw["시가"].astype(int),
            "high": raw["고가"].astype(int),
            "low": raw["저가"].astype(int),
            "close": raw["종가"].astype(int),
            "volume": raw["거래량"].astype("int64"),
            "value": (raw["거래대금"] if "거래대금" in raw.columns else raw["종가"] * raw["거래량"]).astype("int64"),
        })
        df.to_csv(cache, index=False)
        time.sleep(0.3)  # pykrx 과호출 방지

    bars = df.to_dict("records")          # 오래된→최신 순
    return bars


# ──────────────────────────────────────────────
# 단일 트레이드 시뮬레이션 (오래된→최신 bars, entry_idx에서 익일 진입)
# ──────────────────────────────────────────────
def simulate_trade(bars: list, signal_idx: int, sl_pct: float, tp_pct: float,
                   max_hold: int) -> dict:
    """signal_idx 봉에서 신호 → signal_idx+1 시가 진입 → SL/TP/최대보유 청산.
    반환: {entry, exit, ret_pct, days, outcome} 또는 None(진입 불가)."""
    entry_idx = signal_idx + 1
    if entry_idx >= len(bars):
        return None
    entry = bars[entry_idx]["open"]
    if entry <= 0:
        return None
    sl_price = entry * (1 - sl_pct)
    tp_price = entry * (1 + tp_pct)

    for h in range(entry_idx, min(entry_idx + max_hold, len(bars))):
        bar = bars[h]
        # 같은 날 손절·익절 동시 도달 시 손절 우선(보수적)
        if bar["low"] <= sl_price:
            ret = -sl_pct - ROUND_TRIP_COST
            return {"entry": entry, "exit": int(sl_price), "ret_pct": ret * 100,
                    "days": h - entry_idx, "outcome": "SL"}
        if bar["high"] >= tp_price:
            ret = tp_pct - ROUND_TRIP_COST
            return {"entry": entry, "exit": int(tp_price), "ret_pct": ret * 100,
                    "days": h - entry_idx, "outcome": "TP"}
    # 최대 보유일 도달 → 종가 청산
    last = bars[min(entry_idx + max_hold - 1, len(bars) - 1)]
    ret = (last["close"] - entry) / entry - ROUND_TRIP_COST
    return {"entry": entry, "exit": last["close"], "ret_pct": ret * 100,
            "days": last_idx_days(entry_idx, max_hold, len(bars)), "outcome": "HOLD_EXIT"}


def last_idx_days(entry_idx, max_hold, n):
    return min(entry_idx + max_hold - 1, n - 1) - entry_idx


# ──────────────────────────────────────────────
# 트랙 신호 평가 (특정 시점 t의 '최신이 앞' 윈도우 구성)
# ──────────────────────────────────────────────
def eval_track_at(track: str, bars: list, t: int, env: dict, g_dates: dict = None,
                  track_params: dict = None) -> dict:
    """bars=오래된→최신. t 시점까지의 데이터로 트랙 신호 평가.
    G는 성능상 종목당 1회 사전계산(g_dates: {date: signal_dict})으로 처리.
    track_params: {트랙: {진입 강화 kwargs}} (예: B 진입조건 강화 스윕용)."""
    cur = bars[t]["close"]
    tp = (track_params or {}).get(track, {})
    if track == "G":
        # 거래대금 필터 + 사전계산된 크로스 날짜 매칭
        if bars[t].get("value", 0) < env["min_g_value"]:
            return None
        return (g_dates or {}).get(bars[t]["date"])
    lo = max(0, t - 300)
    window = list(reversed(bars[lo:t + 1]))   # 최신(t)이 앞
    if track == "B":
        return track_rules.evaluate_track_b(window, cur, **tp)
    if track == "C":
        return track_rules.evaluate_track_c(window, cur)
    if track == "E":
        return track_rules.evaluate_track_e(window, cur)
    if track == "F":
        return track_rules.evaluate_track_f(window, cur)
    return None


def precompute_g_dates(bars: list, cci_margin: float = 10.0) -> dict:
    """Track G(CCI50+MACD 0선 동시 상향돌파)를 종목당 1회 벡터화 계산.
    반환: {date: {track,cci_value,macd_value,entry_day_low}}.
    track_rules.evaluate_track_g와 동일한 수식(검증 테스트로 일치 확인)."""
    import numpy as np
    df = pd.DataFrame(bars)
    df = df[df["volume"] > 0].reset_index(drop=True)
    if len(df) < 60:
        return {}
    tp = (df["high"] + df["low"] + df["close"]) / 3
    tp_ma = tp.rolling(window=50).mean()
    tp_md = tp.rolling(window=50).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    cci = (tp - tp_ma) / (0.015 * tp_md)
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    out = {}
    for i in range(1, len(df)):
        if pd.isna(cci.iloc[i]) or pd.isna(cci.iloc[i - 1]):
            continue
        if (cci.iloc[i - 1] < 0 and cci.iloc[i] > cci_margin
                and macd.iloc[i - 1] < 0 and macd.iloc[i] > 0):
            out[df["date"].iloc[i]] = {
                "track": "G", "cci_value": round(float(cci.iloc[i]), 2),
                "macd_value": round(float(macd.iloc[i]), 2),
                "entry_day_low": int(df["low"].iloc[i]),
            }
    return out


# ──────────────────────────────────────────────
# 메인 백테스트
# ──────────────────────────────────────────────
def run_backtest(tickers: list, start: str, end: str, tracks: list,
                 exits: dict, env: dict, track_params: dict = None) -> dict:
    from collections import defaultdict
    results = defaultdict(list)        # track -> [trade dict, ...]
    signal_counts = defaultdict(int)   # track -> 신호 발생 횟수(진입 전)
    universe_days = 0

    for i, tk in enumerate(tickers):
        bars = fetch_history(tk, start, end)
        if len(bars) < 60:
            continue
        if (i + 1) % 20 == 0:
            logger.info(f"  ... {i+1}/{len(tickers)} 종목 처리 (캐시) ")

        # G는 종목당 1회 벡터화 사전계산 (일별 rolling.apply 반복 방지)
        g_dates = precompute_g_dates(bars) if "G" in tracks else {}

        # 한 종목당 트랙별 마지막 진입 인덱스 (중복 진입 방지: 보유 중 재신호 무시)
        last_exit_idx = {t: -1 for t in tracks}

        for t in range(60, len(bars) - 1):
            bar = bars[t]
            # 절대 유니버스 필터 (가격/거래대금/거래량)
            if not track_rules.passes_universe(
                    bar["close"], bar.get("value", 0), bar["volume"],
                    min_price=env["min_price"], max_price=env["max_price"],
                    min_trade_amount=env["min_value"], min_volume=env["min_volume"]):
                continue
            universe_days += 1

            for track in tracks:
                if t <= last_exit_idx[track]:
                    continue   # 아직 보유 중(직전 트레이드 종료 전)이면 신호 무시
                sig = eval_track_at(track, bars, t, env, g_dates, track_params)
                if not sig:
                    continue
                signal_counts[track] += 1
                ex = exits[track]
                trade = simulate_trade(bars, t, ex["sl_pct"], ex["tp_pct"], ex["max_hold"])
                if trade:
                    trade["ticker"] = tk
                    trade["date"] = bar["date"]
                    results[track].append(trade)
                    last_exit_idx[track] = t + 1 + trade["days"]

    return {"results": dict(results), "signals": dict(signal_counts),
            "universe_days": universe_days}


def summarize(out: dict, tracks: list, exits: dict):
    import statistics
    print("\n" + "=" * 74)
    print(" 백테스트 결과 요약 (트랙별)")
    print("=" * 74)
    print(f"{'트랙':<5}{'신호':>7}{'거래':>7}{'승률':>8}{'평균수익':>10}{'기대값':>10}{'TP/SL/H':>14}")
    print("-" * 74)
    grand = []
    for track in tracks:
        trades = out["results"].get(track, [])
        n_sig = out["signals"].get(track, 0)
        ex = exits[track]
        cfg = f"{ex['tp_pct']*100:.0f}/{ex['sl_pct']*100:.0f}/{ex['max_hold']}"
        if not trades:
            print(f"{track:<5}{n_sig:>7}{0:>7}{'-':>8}{'-':>10}{'-':>10}{cfg:>14}")
            continue
        rets = [x["ret_pct"] for x in trades]
        wins = [r for r in rets if r > 0]
        wr = len(wins) / len(rets) * 100
        avg = statistics.mean(rets)
        exp = avg  # per-trade expectancy = 평균수익
        grand.extend(rets)
        print(f"{track:<5}{n_sig:>7}{len(trades):>7}{wr:>7.0f}%{avg:>9.2f}%{exp:>9.2f}%{cfg:>14}")
    print("-" * 74)
    if grand:
        import statistics as st
        wr = sum(1 for r in grand if r > 0) / len(grand) * 100
        print(f"{'합계':<5}{'':<7}{len(grand):>7}{wr:>7.0f}%{st.mean(grand):>9.2f}%")
    print("=" * 74)
    print("※ 신호=검색식 트리거 횟수, 거래=실제 진입수. 비용 0.3% 반영. 손절 우선 가정.")
    print("※ Track A·AI층은 백테스트 불가(분봉 수집 후 별도). C/D는 근사(주석 참조).\n")


def sweep_track(tickers, start, end, track, env):
    """단일 트랙의 SL/TP/보유일 파라미터 스윕."""
    import statistics
    print(f"\n[파라미터 스윕] Track {track}")
    print(f"{'SL%':>5}{'TP%':>5}{'Hold':>6}{'거래':>7}{'승률':>8}{'기대값':>10}")
    print("-" * 42)
    grid = []
    sl_opts = [0.03, 0.05, 0.07] if track != "E" else [0.10, 0.15, 0.20]
    tp_opts = [0.05, 0.10, 0.15, 0.20] if track not in ("F",) else [0.30, 0.50]
    hold_opts = [3, 5, 10] if track in ("B", "C") else [10, 20, 30]
    for sl in sl_opts:
        for tp in tp_opts:
            for hold in hold_opts:
                exits = {track: {"sl_pct": sl, "tp_pct": tp, "max_hold": hold}}
                out = run_backtest(tickers, start, end, [track], exits, env)
                trades = out["results"].get(track, [])
                if not trades:
                    continue
                rets = [x["ret_pct"] for x in trades]
                wr = sum(1 for r in rets if r > 0) / len(rets) * 100
                exp = statistics.mean(rets)
                grid.append((exp, sl, tp, hold, len(trades), wr))
                print(f"{sl*100:>4.0f}%{tp*100:>4.0f}%{hold:>6}{len(trades):>7}{wr:>7.0f}%{exp:>9.2f}%")
    if grid:
        grid.sort(reverse=True)
        e, sl, tp, hold, n, wr = grid[0]
        print("-" * 42)
        print(f"최적: SL -{sl*100:.0f}% / TP +{tp*100:.0f}% / {hold}일 보유 "
              f"→ 기대값 {e:+.2f}% (거래 {n}, 승률 {wr:.0f}%)")


def main():
    ap = argparse.ArgumentParser(description="쌍봉봇 일봉 백테스트")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2026-06-20")
    ap.add_argument("--tracks", default="B,C,E,F,G", help="콤마 구분 (A 제외)")
    ap.add_argument("--track", default="", help="단일 트랙 파라미터 스윕")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="시드 티커 수 제한(테스트용)")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(override=True)
    env = {
        "min_price": int(os.environ.get("SCREEN_MIN_PRICE", 1000)),
        "max_price": int(os.environ.get("SCREEN_MAX_PRICE", 0)),
        "min_value": float(os.environ.get("SCREEN_MIN_TRADE_AMOUNT", 3_000_000_000)),
        "min_volume": int(os.environ.get("SCREEN_MIN_VOLUME", 1_000_000)),
        "min_g_value": 50_000_000_000,
    }

    tickers = load_seed_tickers()
    if args.limit > 0:
        tickers = tickers[:args.limit]
    logger.info(f"백테스트 기간 {args.start}~{args.end}, 종목 {len(tickers)}개, "
                f"유니버스: 가격 {env['min_price']:,}~{env['max_price'] or '무제한'} "
                f"거래대금>={env['min_value']/1e8:.0f}억")

    if args.sweep and args.track:
        sweep_track(tickers, args.start, args.end, args.track.upper(), env)
        return

    tracks = [t.strip().upper() for t in args.tracks.split(",") if t.strip()]
    exits = {t: DEFAULT_EXITS[t] for t in tracks if t in DEFAULT_EXITS}
    out = run_backtest(tickers, args.start, args.end, tracks, exits, env)
    summarize(out, tracks, exits)


if __name__ == "__main__":
    main()
