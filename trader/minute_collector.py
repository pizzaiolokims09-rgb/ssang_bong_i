"""
minute_collector.py - Track A 백테스트용 분봉 데이터 수집기

배경:
  Track A(단타)는 1분봉 MSS/VWAP/거래량폭발이 핵심인데, 과거 분봉을 대량으로
  구할 방법이 없어(KIS는 최근 ~30봉, pykrx는 분봉 미제공) 백테스트가 불가능하다.
  → 그렇다면 '지금부터' 분봉을 차곡차곡 모아두면 몇 달 뒤 Track A 백테스트가 가능해진다.

동작:
  - run.py가 Track A 후보의 분봉을 이미 조회하므로(피벗 스나이퍼), 그 데이터를 재활용해
    추가 API 호출 없이 수집한다.
  - data/minute_history/{YYYYMMDD}.jsonl 에 (ticker, time, OHLCV) 라인을 누적.
  - (ticker, time) 중복은 메모리 셋으로 제거 → 매 스캔마다 새 분봉만 append.
  - 진입 의사결정 컨텍스트(현재가/VWAP/엔벨로프/MSS 플래그 등)도 함께 저장하면
    향후 백테스트가 '그 시점 봇이 본 화면'을 그대로 재현할 수 있다.

활성화: .env COLLECT_MINUTE_DATA=true (기본 false — 끄면 오버헤드 0).
"""
import json
import logging
import os
from datetime import datetime

logger = logging.getLogger("ssangbong.collector")


class MinuteCollector:
    def __init__(self, out_dir: str = "data/minute_history"):
        self.out_dir = out_dir
        # 기본 true: Track A 테스트와 병행해 분봉을 축적(향후 단타 백테스트용). 끄려면 .env=false.
        self.enabled = os.environ.get("COLLECT_MINUTE_DATA", "true").lower() == "true"
        self._seen = set()          # {(date, ticker, time)} 중복 방지
        self._seen_day = None       # 날짜 바뀌면 _seen 리셋
        if self.enabled:
            os.makedirs(out_dir, exist_ok=True)
            logger.info(f"[Collector] 분봉 수집 활성화 → {out_dir}/ (Track A 백테스트 대비)")

    def _roll_day(self, day: str):
        if self._seen_day != day:
            self._seen_day = day
            self._seen = set()

    def collect(self, ticker: str, name: str, minute_candles: list, context: dict = None):
        """이미 조회된 분봉(minute_candles)에서 신규 봉만 골라 JSONL에 append.
        minute_candles: [{time, open, high, low, close, volume}, ...] (최신이 앞)."""
        if not self.enabled or not minute_candles:
            return
        try:
            day = datetime.now().strftime("%Y%m%d")
            self._roll_day(day)
            path = os.path.join(self.out_dir, f"{day}.jsonl")
            ctx = context or {}
            new_lines = []
            for c in minute_candles:
                tm = c.get("time", "")
                if not tm:
                    continue
                key = (day, ticker, tm)
                if key in self._seen:
                    continue
                self._seen.add(key)
                new_lines.append(json.dumps({
                    "date": day, "ticker": ticker, "name": name,
                    "time": tm, "open": c.get("open", 0), "high": c.get("high", 0),
                    "low": c.get("low", 0), "close": c.get("close", 0),
                    "volume": c.get("volume", 0),
                    # 진입 의사결정 컨텍스트 (백테스트 재현용, 선택적)
                    "ctx": ctx,
                }, ensure_ascii=False))
            if new_lines:
                with open(path, "a", encoding="utf-8") as f:
                    f.write("\n".join(new_lines) + "\n")
        except Exception as e:
            logger.warning(f"[Collector] {ticker} 분봉 수집 실패: {e}")
