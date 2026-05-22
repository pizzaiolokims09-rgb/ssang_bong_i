import os
import datetime
from pykrx import stock
import logging
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

def fetch_daily_swing_candidates(top_n=50) -> list:
    """
    KOSPI, KOSDAQ 시장에서 외국인/기관 순매수대금 상위 종목을 추출하여 리스트로 반환.
    이 목록은 스윙/메가트렌드(Track B, F) 진입 시 우량 매집주 필터로 사용됨.
    """
    load_dotenv()
    if not os.getenv("KRX_ID") or not os.getenv("KRX_PW"):
        logger.error("KRX_ID 또는 KRX_PW가 .env에 설정되지 않아 pykrx 스캔을 진행할 수 없습니다.")
        return []

    try:
        today = datetime.datetime.today()
        # 최근 7일 중 가장 최신의 거래일 찾기
        target_date = None
        for i in range(7):
            d = today - datetime.timedelta(days=i)
            # 주말 제외
            if d.weekday() >= 5:
                continue
            date_str = d.strftime("%Y%m%d")
            # 오늘 날짜면 데이터가 아직 안 나왔을 수 있으므로 15:30 이전엔 전날 사용
            if i == 0 and today.hour < 16:
                continue
            
            # 거래일 여부 체크 (티커 리스트가 있으면 거래일로 간주)
            tickers = stock.get_market_ticker_list(date_str, market="KOSPI")
            if len(tickers) > 0:
                target_date = date_str
                break
                
        if not target_date:
            logger.error("최근 거래일을 찾을 수 없습니다.")
            return []

        logger.info(f"[{target_date}] 기준 외국인/기관 순매수 상위 종목 스캔을 시작합니다.")
        candidates = set()

        # 각 시장, 투자자별로 순매수대금(순매수거래대금) 상위 n개 추출
        for market in ["KOSPI", "KOSDAQ"]:
            for investor in ["외국인", "기관합계"]:
                df = stock.get_market_net_purchases_of_equities_by_ticker(
                    target_date, target_date, market, investor
                )
                if df.empty:
                    continue
                
                # '순매수거래대금' 기준으로 내림차순 정렬 후 상위 추출
                if "순매수거래대금" in df.columns:
                    df = df.sort_values(by="순매수거래대금", ascending=False)
                    top_tickers = df.head(top_n).index.tolist()
                    candidates.update(top_tickers)
                    logger.info(f"{market} {investor} 순매수 상위 {len(top_tickers)}개 추가")
                
        result = list(candidates)
        logger.info(f"일일 스윙/메가트렌드 큐레이션 완료: 총 {len(result)} 종목 추출됨.")
        return result

    except Exception as e:
        logger.error(f"pykrx 스캔 중 오류 발생: {e}")
        return []

if __name__ == "__main__":
    # 단독 실행 테스트
    logging.basicConfig(level=logging.INFO)
    cands = fetch_daily_swing_candidates(top_n=10)
    print("Candidates count:", len(cands))
    print(cands[:10])
