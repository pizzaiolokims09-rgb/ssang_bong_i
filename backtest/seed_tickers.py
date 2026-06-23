"""
seed_tickers.py - 백테스트 시드 종목 리스트

pykrx의 '전 종목 일괄조회/티커목록'은 KRX 로그인이 필요해 로컬에서 막히므로,
백테스트는 시드 티커 리스트를 종목별로 순회한다(단일종목 일봉은 로그인 불필요).

시드 = (1) 봇이 실제 접한 종목(journal.db + daily_theme.json) 자동 추출
      + (2) 아래 번들된 주요 유동주(대형/중형/코스닥 액티브)
      + (3) 사용자 지정 파일(data/backtest_tickers.txt, 한 줄에 한 티커) 있으면 병합
"""
import json
import logging
import os
import re
import sqlite3

logger = logging.getLogger("backtest.seed")

# 주요 유동주 번들 (KOSPI 대형/중형 + KOSDAQ 액티브). 대표성 확보용 — 완전한 유니버스는 아님.
BUNDLED = [
    # KOSPI 대형
    "005930", "000660", "373220", "207940", "005380", "000270", "005490", "035420",
    "035720", "051910", "006400", "068270", "105560", "055550", "012330", "028260",
    "066570", "003550", "096770", "017670", "015760", "034730", "032830", "086790",
    "033780", "003670", "010130", "009150", "011200", "024110", "316140", "138040",
    "010950", "018260", "011070", "030200", "009830", "010140", "047050", "267260",
    # KOSPI 중형/테마
    "042700", "000810", "161390", "002790", "078930", "271560", "139480", "021240",
    "111770", "004020", "001040", "011780", "020150", "007070", "097950", "057050",
    # KOSDAQ 대표/액티브
    "247540", "086520", "091990", "066970", "028300", "196170", "263750", "293490",
    "041510", "067310", "035900", "058470", "240810", "095340", "098460", "357780",
    "112040", "078600", "039030", "222800", "036930", "140860", "214150", "348370",
    "277810", "145020", "214450", "141080", "084850", "237690", "328130", "088800",
]


def _from_journal(db_path: str) -> set:
    out = set()
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        for tbl in ("trades", "shadow_candidates"):
            try:
                for (t,) in cur.execute(f"SELECT DISTINCT ticker FROM {tbl}"):
                    if t and re.match(r"^\d{6}$", str(t)):
                        out.add(t)
            except sqlite3.OperationalError:
                pass
        con.close()
    except Exception as e:
        logger.warning(f"journal 티커 추출 실패: {e}")
    return out


def _from_theme(path: str) -> set:
    try:
        with open(path, encoding="utf-8") as f:
            return set(re.findall(r"\b\d{6}\b", f.read()))
    except Exception:
        return set()


def _from_file(path: str) -> set:
    if not os.path.exists(path):
        return set()
    out = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            t = line.strip()
            if re.match(r"^\d{6}$", t):
                out.add(t)
    return out


def load_seed_tickers(data_dir: str = "data") -> list:
    tickers = set(BUNDLED)
    tickers |= _from_journal(os.path.join(data_dir, "journal.db"))
    tickers |= _from_theme(os.path.join(data_dir, "daily_theme.json"))
    tickers |= _from_file(os.path.join(data_dir, "backtest_tickers.txt"))
    result = sorted(tickers)
    logger.info(f"시드 티커 {len(result)}개 (번들 {len(BUNDLED)} + 데이터 추출 + 사용자파일)")
    return result
