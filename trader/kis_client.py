"""
kis_client.py - KIS(한국투자증권) API 통신 모듈 (하이브리드 모드)
조회(시세, 차트, 순위)는 항상 실전매매(real) API 사용
주문, 잔고조회는 KIS_TRADING_ENV 설정에 따라 분기
"""
import json
import os
import threading
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("ssangbong.kis")

TR_MAP = {
    "demo": {
        "balance":       "VTTC8434R",
        "buy":           "VTTC0802U",
        "sell":          "VTTC0801U",
        "cancel":        "VTTC0803U",
        "price":         "FHKST01010100",
        "minute_chart":  "FHKST03010200",
        "daily_chart":   "FHKST03010100",
        "volume_rank":   "FHKST01010300",
        "orderbook":     "FHKST01010200",
        "conclusion":    "VTTC8001R",
    },
    "real": {
        "balance":       "TTTC8434R",
        "buy":           "TTTC0802U",
        "sell":          "TTTC0801U",
        "cancel":        "TTTC0803U",
        "price":         "FHKST01010100",
        "minute_chart":  "FHKST03010200",
        "daily_chart":   "FHKST03010100",
        "volume_rank":   "FHPST01710000",
        "orderbook":     "FHKST01010200",
        "conclusion":    "TTTC8001R",
    },
}

class KISClient:
    """한국투자증권 API 래퍼 (하이브리드 모드)"""

    def __init__(self):
        self.env = os.environ.get("KIS_TRADING_ENV", "demo")
        self.account_pd = os.environ.get("KIS_ACCOUNT_PRODUCT_CODE", "01")

        # 1) 정보 수집용 고정 세팅 (항상 실전 API)
        self.real_app_key    = os.environ.get("KIS_APP_KEY", "")
        self.real_app_secret = os.environ.get("KIS_APP_SECRET", "")
        self.real_base_url   = "https://openapi.koreainvestment.com:9443"

        # 2) 매매용 세팅 (모의 or 실전)
        if self.env == "demo":
            self.trade_app_key    = os.environ.get("KIS_PAPER_APP_KEY", self.real_app_key)
            self.trade_app_secret = os.environ.get("KIS_PAPER_APP_SECRET", self.real_app_secret)
            self.trade_account_no = os.environ.get("KIS_PAPER_ACCOUNT_NO", os.environ.get("KIS_ACCOUNT_NO", ""))
            self.trade_base_url   = "https://openapivts.koreainvestment.com:29443"
        else:
            self.trade_app_key    = self.real_app_key
            self.trade_app_secret = self.real_app_secret
            self.trade_account_no = os.environ.get("KIS_ACCOUNT_NO", "")
            self.trade_base_url   = self.real_base_url

        self._token_cache = Path("data/kis_token.json")
        self._token_cache.parent.mkdir(parents=True, exist_ok=True)
        self._api_error_count = 0
        # 토큰 발급 직렬화 락 (감시 스레드와 메인 스레드의 동시 발급 → 403 방지)
        self._token_lock = threading.Lock()
        # 일봉 차트 캐시 (장중 트랙 스캔이 같은 종목 일봉을 매분 재조회하는 부하 절감)
        self._daily_cache: dict = {}
        self._daily_cache_ttl = 180  # 초 (당일봉 갱신을 위해 3분 제한)

        logger.info(f"[KIS] 하이브리드 엔진 시동 (매매환경={self.env}, 매매계좌={self.trade_account_no})")

    def _get_token(self, env_type: str) -> str:
        app_key = self.real_app_key if env_type == "real" else self.trade_app_key
        app_secret = self.real_app_secret if env_type == "real" else self.trade_app_secret
        base_url = self.real_base_url if env_type == "real" else self.trade_base_url
        
        cache_key = f"token_{env_type}"

        with self._token_lock:
            if self._token_cache.exists():
                try:
                    cached = json.loads(self._token_cache.read_text())
                    if cache_key in cached:
                        expires_at = datetime.fromisoformat(cached[cache_key]["expires_at"])
                        if expires_at > datetime.now() + timedelta(minutes=5):
                            return cached[cache_key]["token"]
                except (json.JSONDecodeError, KeyError):
                    pass

            logger.info(f"[KIS] 새 Access Token 발급 중... ({env_type})")
            token = None
            last_err = None
            # KIS 토큰 발급은 분당 1회 제한(403) → 실패 시 대기 후 재시도 (봇 크래시 방지)
            for attempt in range(3):
                try:
                    resp = requests.post(
                        f"{base_url}/oauth2/tokenP",
                        headers={"content-type": "application/json"},
                        json={
                            "grant_type": "client_credentials",
                            "appkey": app_key,
                            "appsecret": app_secret,
                        },
                        timeout=10,
                    )
                    resp.raise_for_status()
                    token = resp.json()["access_token"]
                    break
                except Exception as e:
                    last_err = e
                    if attempt < 2:
                        logger.warning(f"[KIS] 토큰 발급 실패 ({attempt+1}/3): {e} -> 65초 후 재시도")
                        time.sleep(65)

            if not token:
                logger.critical(f"[KIS] 토큰 발급 3회 연속 실패: {last_err} -> 이번 요청 건너뜀 (봇/포지션 유지)")
                return ""

            expires_at = datetime.now() + timedelta(hours=23)

            # 기존 캐시 로드 후 업데이트
            cached = {}
            if self._token_cache.exists():
                try:
                    cached = json.loads(self._token_cache.read_text())
                except json.JSONDecodeError:
                    pass

            cached[cache_key] = {"token": token, "expires_at": expires_at.isoformat()}
            self._token_cache.write_text(json.dumps(cached))

            logger.info(f"[KIS] 토큰 발급 완료 ({env_type})")
            return token

    def _headers(self, tr_id: str, env_type: str = "real") -> dict:
        app_key = self.real_app_key if env_type == "real" else self.trade_app_key
        app_secret = self.real_app_secret if env_type == "real" else self.trade_app_secret
        token = self._get_token(env_type)
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def _hashkey(self, payload: dict, env_type: str = "real") -> str:
        app_key = self.real_app_key if env_type == "real" else self.trade_app_key
        app_secret = self.real_app_secret if env_type == "real" else self.trade_app_secret
        base_url = self.real_base_url if env_type == "real" else self.trade_base_url
        
        try:
            resp = requests.post(
                f"{base_url}/uapi/hashkey",
                headers={
                    "content-type": "application/json",
                    "appkey": app_key,
                    "appsecret": app_secret,
                },
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("HASH", "")
        except Exception as e:
            logger.error(f"[KIS] Hashkey 발급 실패: {e}")
            return ""

    def _safe_request(self, method: str, url: str, **kwargs) -> Optional[dict]:
        try:
            kwargs.setdefault("timeout", 10)
            if method == "GET":
                resp = requests.get(url, **kwargs)
            else:
                resp = requests.post(url, **kwargs)
            resp.raise_for_status()
            self._api_error_count = 0
            return resp.json()
        except requests.exceptions.JSONDecodeError as e:
            logger.error(f"[KIS] JSON 파싱 에러 (응답 본문 이상): {e}")
            return None
        except Exception as e:
            self._api_error_count += 1
            logger.error(f"[KIS] API 에러 ({self._api_error_count}/5): {e}")
            if self._api_error_count >= 5:
                logger.critical("[KIS] 서킷 브레이커 발동! (에러 5회 연속 누적)")
                return None
            return None

    # =========================================================
    # 정보 수집용 API (무조건 REAL)
    # =========================================================
    def get_quote(self, ticker: str) -> dict:
        result = self._safe_request(
            "GET",
            f"{self.real_base_url}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=self._headers(TR_MAP["real"]["price"], env_type="real"),
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        )
        if not result:
            return {"current": 0, "ask1": 0, "bid1": 0, "volume": 0, "market_cap": 0}

        out = result.get("output", {})

        # 체결강도 = 매수체결량 합계 / 매도체결량 합계 * 100 (100 초과 = 매수 우위)
        # 주의: seln_cnqn_smtn 단독은 '매도체결량 합계'일 뿐이므로 비율로 직접 계산
        seln_qty = float(out.get("seln_cnqn_smtn", 0) or 0)   # 매도 체결량 합계
        shnu_qty = float(out.get("shnu_cnqn_smtn", 0) or 0)   # 매수 체결량 합계
        exec_strength = round(shnu_qty / seln_qty * 100, 1) if seln_qty > 0 else 0.0

        return {
            "current":    int(out.get("stck_prpr", 0)),
            "ask1":       int(out.get("askp1", 0)),
            "bid1":       int(out.get("bidp1", 0)),
            "volume":     int(out.get("acml_vol", 0)),
            "trade_amount": int(out.get("acml_tr_pbmn", 0)),
            "market_cap": int(out.get("hts_avls", 0)),
            "per":        float(out.get("per", 0) or 0),
            "high":       int(out.get("stck_hgpr", 0)),
            "low":        int(out.get("stck_lwpr", 0)),
            "open":       int(out.get("stck_oprc", 0)),
            "prev_close": int(out.get("stck_sdpr", 0)),
            "change_pct": float(out.get("prdy_ctrt", 0) or 0),
            "name":       out.get("hts_kor_isnm", ticker),
            "execution_strength": exec_strength,
            # 상장일 (YYYYMMDD)
            "listing_date": out.get("stck_lstn_date", ""),
        }

    def get_minute_chart(self, ticker: str, period: str = "1") -> list:
        result = self._safe_request(
            "GET",
            f"{self.real_base_url}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            headers=self._headers(TR_MAP["real"]["minute_chart"], env_type="real"),
            params={
                "FID_ETC_CLS_CODE": "",
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": ticker,
                "FID_INPUT_HOUR_1": datetime.now().strftime("%H%M%S"),
                "FID_PW_DATA_INCU_YN": "Y",
            },
        )
        if not result:
            return []

        candles = []
        for item in result.get("output2", []):
            try:
                candles.append({
                    "time":   item.get("stck_cntg_hour", ""),
                    "open":   int(item.get("stck_oprc", 0)),
                    "high":   int(item.get("stck_hgpr", 0)),
                    "low":    int(item.get("stck_lwpr", 0)),
                    "close":  int(item.get("stck_prpr", 0)),
                    "volume": int(item.get("cntg_vol", 0)),
                })
            except (ValueError, TypeError):
                continue
        return candles

    def get_daily_chart(self, ticker: str, start_date: str = "", end_date: str = "",
                        days: int = 90) -> list:
        if not end_date:
            end_date = datetime.now().strftime("%Y%m%d")
        if not start_date:
            start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

        # 캐시 확인 (TTL 3분)
        cache_key = (ticker, start_date, end_date)
        hit = self._daily_cache.get(cache_key)
        if hit and time.time() - hit[0] < self._daily_cache_ttl:
            return hit[1]

        result = self._safe_request(
            "GET",
            f"{self.real_base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            headers=self._headers(TR_MAP["real"]["daily_chart"], env_type="real"),
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": ticker,
                "FID_INPUT_DATE_1": start_date,
                "FID_INPUT_DATE_2": end_date,
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0",
            },
        )
        if not result:
            return []

        candles = []
        for item in result.get("output2", []):
            try:
                candles.append({
                    "date":   item.get("stck_bsop_date", ""),
                    "open":   int(item.get("stck_oprc", 0)),
                    "high":   int(item.get("stck_hgpr", 0)),
                    "low":    int(item.get("stck_lwpr", 0)),
                    "close":  int(item.get("stck_clpr", 0)),
                    "volume": int(item.get("acml_vol", 0)),
                })
            except (ValueError, TypeError):
                continue

        if candles:
            # 캐시 무한 증식 방지 (500개 초과 시 통째로 비움)
            if len(self._daily_cache) > 500:
                self._daily_cache.clear()
            self._daily_cache[cache_key] = (time.time(), candles)
        return candles

    def get_market_index(self, market_code: str = "0001") -> dict:
        """
        코스피(0001), 코스닥(1001) 등 시장 지수 현재가 조회
        """
        result = self._safe_request(
            "GET",
            f"{self.real_base_url}/uapi/domestic-stock/v1/quotations/inquire-index-price",
            headers=self._headers("FHPUP02100000", env_type="real"),
            params={
                "FID_COND_MRKT_DIV_CODE": "U",
                "FID_INPUT_ISCD": market_code,
            },
        )
        if not result:
            return {}
        
        out = result.get("output", {})
        return {
            "current": float(out.get("bstp_nmix_prpr", 0) or 0),
            "high": float(out.get("bstp_nmix_hgpr", 0) or 0),
            "low": float(out.get("bstp_nmix_lwpr", 0) or 0),
            "change_pct": float(out.get("bstp_nmix_prdy_ctrt", 0) or 0)
        }

    def is_market_crashing(self) -> bool:
        """
        당일 고점 대비 지수가 일정 수준(-1.5%) 이상 급락 중인지 판단
        """
        kosdaq = self.get_market_index("1001")
        time.sleep(0.05)
        kospi = self.get_market_index("0001")
        
        crashing = False
        
        for name, idx in [("KOSDAQ", kosdaq), ("KOSPI", kospi)]:
            if not idx or idx.get("high", 0) == 0:
                continue
            
            # 고점 대비 하락률 계산
            drop_from_high_pct = ((idx["current"] - idx["high"]) / idx["high"]) * 100
            
            # 당일 고점 대비 -3.0% 이상 하락 시 폭락장으로 규정
            if drop_from_high_pct <= -3.0:
                logger.warning(f"🚨 [시장 경보] {name} 지수 고점 대비 폭락 중! ({drop_from_high_pct:.2f}%)")
                crashing = True
                
        return crashing

    def get_volume_rank(self) -> list:
        result = self._safe_request(
            "GET",
            f"{self.real_base_url}/uapi/domestic-stock/v1/quotations/volume-rank",
            headers=self._headers(TR_MAP["real"]["volume_rank"], env_type="real"),
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "20101",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_BLNG_CLS_CODE": "0",
                "FID_TRGT_CLS_CODE": "111111111",
                "FID_TRGT_EXLS_CLS_CODE": "000000",
                "FID_INPUT_PRICE_1": "",
                "FID_INPUT_PRICE_2": "",
                "FID_VOL_CNT": "",
                "FID_INPUT_DATE_1": "",
            },
        )
        if not result:
            return []

        stocks = []
        for item in result.get("output", []):
            try:
                stocks.append({
                    "ticker":       item.get("mksc_shrn_iscd", ""),
                    "name":         item.get("hts_kor_isnm", ""),
                    "current":      int(item.get("stck_prpr", 0)),
                    "change_pct":   float(item.get("prdy_ctrt", 0) or 0),
                    "volume":       int(item.get("acml_vol", 0)),
                    "trade_amount": int(item.get("acml_tr_pbmn", 0)),
                    "market_cap":   int(item.get("hts_avls", 0) or 0),
                })
            except (ValueError, TypeError):
                continue
        return stocks

    def get_orderbook(self, ticker: str) -> dict:
        result = self._safe_request(
            "GET",
            f"{self.real_base_url}/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            headers=self._headers(TR_MAP["real"]["orderbook"], env_type="real"),
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        )
        if not result:
            return {"total_ask_qty": 0, "total_bid_qty": 0, "bid_ask_ratio": 0}

        out = result.get("output1", {})
        total_ask = int(out.get("total_askp_rsqn", 0) or 0)
        total_bid = int(out.get("total_bidp_rsqn", 0) or 0)
        return {
            "total_ask_qty": total_ask,
            "total_bid_qty": total_bid,
            "bid_ask_ratio": round(total_bid / max(total_ask, 1), 2),
        }

    # =========================================================
    # 매매/계좌용 API (설정에 따라 분기)
    # =========================================================
    def get_balance(self) -> dict:
        tr_id = TR_MAP[self.env]["balance"]
        result = self._safe_request(
            "GET",
            f"{self.trade_base_url}/uapi/domestic-stock/v1/trading/inquire-balance",
            headers=self._headers(tr_id, env_type=self.env),
            params={
                "CANO": self.trade_account_no,
                "ACNT_PRDT_CD": self.account_pd,
                "AFHR_FLPR_YN": "N", "OFL_YN": "",
                "INQR_DVSN": "02", "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
            },
        )
        if not result:
            return {"output1": [], "output2": [{}]}
        return result

    def place_order(self, ticker: str, side: str, quantity: int, price: int = 0) -> dict:
        live_flag = os.environ.get("ENABLE_LIVE_TRADING", "false").lower()
        if live_flag != "true" and self.env == "real":
            logger.warning("[KIS] ENABLE_LIVE_TRADING=false -> 실전 주문 차단됨")
            return {"blocked": True, "reason": "ENABLE_LIVE_TRADING=false"}

        tr_id = TR_MAP[self.env]["buy"] if side == "BUY" else TR_MAP[self.env]["sell"]
        ord_dvsn = "01" if price > 0 else "00"

        body = {
            "CANO": self.trade_account_no,
            "ACNT_PRDT_CD": self.account_pd,
            "PDNO": ticker,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price) if price > 0 else "0",
        }

        headers = self._headers(tr_id, env_type=self.env)
        headers["hashkey"] = self._hashkey(body, env_type=self.env)

        result = self._safe_request(
            "POST",
            f"{self.trade_base_url}/uapi/domestic-stock/v1/trading/order-cash",
            headers=headers,
            json=body,
        )
        if result:
            order_no = result.get("output", {}).get("ODNO", "") if isinstance(result.get("output"), dict) else ""
            logger.info(f"[KIS] 주문 완료: {side} {ticker} x {quantity} @ {price} (주문번호: {order_no})")
            result["order_no"] = order_no
        return result or {"error": "주문 실패"}

    def cancel_order(self, order_no: str, ticker: str, quantity: int) -> dict:
        """미체결 주문 취소 (KIS 주문취소 API)"""
        tr_id = TR_MAP[self.env]["cancel"]

        body = {
            "CANO": self.trade_account_no,
            "ACNT_PRDT_CD": self.account_pd,
            "KRX_FWDG_ORD_ORGNO": "",
            "ORGN_ODNO": order_no,
            "ORD_DVSN": "01",
            "RVSE_CNCL_DVSN_CD": "02",  # 02=취소
            "ORD_QTY": str(quantity),
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
        }

        headers = self._headers(tr_id, env_type=self.env)
        headers["hashkey"] = self._hashkey(body, env_type=self.env)

        result = self._safe_request(
            "POST",
            f"{self.trade_base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl",
            headers=headers,
            json=body,
        )
        if result:
            logger.info(f"[KIS] 주문 취소 완료: {ticker} 주문번호={order_no}")
        else:
            logger.error(f"[KIS] 주문 취소 실패: {ticker} 주문번호={order_no}")
        return result or {"error": "취소 실패"}

    def get_conclusions(self) -> list:
        tr_id = TR_MAP[self.env]["conclusion"]
        result = self._safe_request(
            "GET",
            f"{self.trade_base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            headers=self._headers(tr_id, env_type=self.env),
            params={
                "CANO": self.trade_account_no,
                "ACNT_PRDT_CD": self.account_pd,
                "INQR_STRT_DT": datetime.now().strftime("%Y%m%d"),
                "INQR_END_DT": datetime.now().strftime("%Y%m%d"),
                "SLL_BUY_DVSN_CD": "00",
                "INQR_DVSN": "00",
                "PDNO": "",
                "CCLD_DVSN": "00",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        if not result:
            return []
        return result.get("output1", [])

    def get_financial_summary(self, ticker: str) -> dict:
        """
        네이버 금융 크롤링으로 유보율/총자산증감률 조회
        Track D 세력주 매집 재무 필터용
        """
        result = {"retention_ratio": 0, "asset_growth_pct": 0}
        try:
            url = f"https://finance.naver.com/item/main.naver?code={ticker}"
            headers = {'User-Agent': 'Mozilla/5.0'}
            resp = requests.get(url, headers=headers, timeout=5)
            resp.encoding = 'euc-kr'

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.content, 'lxml', from_encoding='euc-kr')

            # 유보율 추출 (네이버 금융 종목 메인 페이지 테이블)
            tables = soup.find_all('table', {'class': 'tb_type1'})
            for table in tables:
                for row in table.find_all('tr'):
                    th = row.find('th')
                    tds = row.find_all('td')
                    if th and '유보율' in th.text and tds:
                        text = tds[0].text.strip().replace(',', '').replace('%', '')
                        try:
                            result["retention_ratio"] = float(text)
                        except ValueError:
                            pass
                    if th and '총자산' in th.text and len(tds) >= 2:
                        try:
                            cur = float(tds[0].text.strip().replace(',', ''))
                            prev = float(tds[1].text.strip().replace(',', ''))
                            if prev > 0:
                                result["asset_growth_pct"] = round((cur - prev) / prev * 100, 1)
                        except (ValueError, IndexError):
                            pass

            logger.info(f"[KIS] 재무요약 {ticker}: 유보율={result['retention_ratio']}%, "
                        f"총자산증감={result['asset_growth_pct']}%")
        except Exception as e:
            logger.warning(f"[KIS] 재무요약 크롤링 실패 ({ticker}): {e}")
        return result

