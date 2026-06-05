"""
ai_fundamental.py - Phase 3: AI Fundamental Deep Scan (심층 재무 & 리스크 검증)
매뉴얼 V2 Section 4 Phase 3 구현

Track B/C/D로 판정되어 '오버나잇(하루 이상 보유)'이 필요할 경우,
매수 전 아래 4가지 심층 검증을 Gemini Pro 모델로 수행:

  1. 세그먼트 팩트체크: 가짜 테마주 필터링 (Track A/B 적용)
  2. CEO 말바뀜 탐지기: 오버나잇 리스크 봉쇄 (Track C/D 적용)
  3. 재무 변곡점 & 해자 분석: 매집주 확신 부여 (Track D 적용)
  4. 악마의 대변인: 홀딩 리스크 최종 관문 (전 트랙 익절 전 적용)
  5. 상폐 리스크 스캔: 폭락주 치명적 악재 필터링 (Track E 전용)
"""
import json
import logging
import os
import re
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("ssangbong.fundamental")


class FundamentalScanner:
    """
    Gemini Pro 기반 심층 펀더멘털 검증 엔진
    Phase 2(ai_router) 통과 후, 매수 직전에 호출하여
    PASS / REDUCE / REJECT 판정을 내린다.
    """

    # 판정 결과 상수
    PASS   = "PASS"    # 통과 → 원래 비중 그대로 매수
    REDUCE = "REDUCE"  # 주의 → 비중 절반 삭감 후 매수
    REJECT = "REJECT"  # 거부 → 매수 차단

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.model_pro   = "gemini-3.1-pro-preview"        # 심층 분석 (정밀)
        self.model_flash = "gemini-3.5-flash"              # 폴백용 (경량)
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"

    # ──────────────────────────────────────────
    # Gemini API 호출 (ai_router.py 패턴 답습)
    # ──────────────────────────────────────────
    def _call_gemini(self, prompt: str, use_pro: bool = True) -> Optional[str]:
        """Gemini API 호출. Pro 실패 시 Flash 폴백."""
        models = [self.model_pro, self.model_flash] if use_pro else [self.model_flash]

        for model in models:
            url = f"{self.base_url}/{model}:generateContent?key={self.api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": 4096,
                },
            }
            try:
                resp = requests.post(url, json=payload, timeout=120)
                resp.raise_for_status()
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return text.strip()
            except Exception as e:
                logger.warning(f"[Fundamental] Gemini 호출 실패 ({model}): {e}")
                if model == models[-1]:
                    return None
                logger.info(f"[Fundamental] 폴백 모델({self.model_flash})로 재시도...")
        return None

    def _call_gemini_with_search(self, prompt: str, use_pro: bool = True) -> Optional[str]:
        """Gemini API 호출 (Google Search Grounding 활성화). 시황 리서치 전용."""
        models = [self.model_pro, self.model_flash] if use_pro else [self.model_flash]

        for model in models:
            url = f"{self.base_url}/{model}:generateContent?key={self.api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "tools": [{"googleSearch": {}}],
                "generationConfig": {
                    "temperature": 0.2,
                    "maxOutputTokens": 4096,
                },
            }
            try:
                resp = requests.post(url, json=payload, timeout=120)
                resp.raise_for_status()
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return text.strip()
            except Exception as e:
                logger.warning(f"[Fundamental] Gemini Search 호출 실패 ({model}): {e}")
                if model == models[-1]:
                    return None
                logger.info(f"[Fundamental] 폴백 모델({self.model_flash})로 재시도...")
        return None

    # ──────────────────────────────────────────
    # JSON 파싱 헬퍼 (ai_router.py 패턴 답습)
    # ──────────────────────────────────────────
    def _parse_json(self, raw: str) -> Optional[dict]:
        """AI 응답에서 JSON 추출 및 정규화"""
        try:
            cleaned = re.sub(r"```json|```", "", raw).strip()
            cleaned = re.sub(r"'([^']+)':", r'"\1":', cleaned)
            cleaned = re.sub(r":\s*'([^']+)'", r': "\1"', cleaned)
            return json.loads(cleaned)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"[Fundamental] JSON 파싱 실패: {e}, 원본: {raw[:200]}")
            return None

    # ══════════════════════════════════════════
    # 검증 #1: 세그먼트 팩트체크 (가짜 테마주 필터)
    # 적용 대상: Track A, B
    # ══════════════════════════════════════════
    def check_segment(self, stock_name: str, ticker: str, theme: str) -> dict:
        """
        주도 테마 뉴스로 엮였으나 실제 세그먼트 매출 비중이 미미한
        '가짜 테마주'를 걸러낸다.

        반환: {"verdict": "PASS|REDUCE|REJECT", "reason": "...", "segment_pct": 0.15}
        """
        prompt = f"""당신은 DART 전자공시 사업보고서 분석 전문가입니다.

[임무]
아래 종목이 현재 시장에서 '{theme}' 테마로 급등하고 있습니다.
이 종목의 최신 사업보고서(또는 반기/분기보고서)를 기반으로,
'{theme}' 관련 사업부(Segment)의 실제 매출 비중을 추정하세요.

[종목 정보]
- 종목명: {stock_name}
- 종목코드: {ticker}
- 시장 주도 테마: {theme}

[분석 기준]
1. 해당 기업의 전체 매출 중 '{theme}' 관련 세그먼트가 차지하는 비중(%)을 추정하세요.
2. 10% 미만이면 '가짜 테마주'로 판정 → "REDUCE" (비중 절반 삭감, 단타로만 대응)
3. 5% 미만이고 관련 기술/특허/제품이 전무하면 → "REJECT" (매수 차단)
4. 10% 이상이면 → "PASS"
5. 판단 근거를 구체적으로 서술하세요 (예: 주력 매출원, 자회사 현황, 관련 특허 유무).

[출력 형식 (반드시 아래 JSON만 출력)]
{{"verdict": "PASS", "reason": "...", "segment_pct": 0.15}}"""

        raw = self._call_gemini(prompt, use_pro=True)
        if not raw:
            logger.warning(f"[Segment] AI 응답 실패 -> 안전하게 REDUCE 처리: {stock_name}")
            return {"verdict": self.REDUCE, "reason": "AI 응답 실패 (안전 모드)", "segment_pct": 0}

        result = self._parse_json(raw)
        if not result:
            return {"verdict": self.REDUCE, "reason": f"파싱 실패: {raw[:300]}", "segment_pct": 0}

        verdict = result.get("verdict", "REDUCE").upper()
        if verdict not in [self.PASS, self.REDUCE, self.REJECT]:
            verdict = self.REDUCE

        logger.info(f"[Segment] {stock_name} 테마({theme}) -> {verdict} "
                    f"(세그먼트 비중={result.get('segment_pct', 0):.0%})")
        return {
            "verdict": verdict,
            "reason": result.get("reason", ""),
            "segment_pct": result.get("segment_pct", 0),
        }

    # ══════════════════════════════════════════
    # 검증 #2: CEO 말바뀜 탐지기 (오버나잇 리스크 봉쇄)
    # 적용 대상: Track C, D
    # ══════════════════════════════════════════
    def check_ceo_trust(self, stock_name: str, ticker: str) -> dict:
        """
        CEO의 과거 주주서한/IR/어닝콜에서 약속 번복 또는 외부 탓 변명 이력을 탐지.
        신뢰도가 낮으면 오버나잇 포지션을 기계적으로 차단한다.

        반환: {"verdict": "PASS|REJECT", "trust_grade": "A|B|C|D", "reason": "..."}
        """
        prompt = f"""당신은 기업 IR(Investor Relations) 분석 전문가이자 CEO 신뢰도 평가사입니다.

[임무]
아래 종목의 CEO(대표이사)가 과거 3년간 주주에게 한 약속을 얼마나 지켰는지 평가하세요.

[종목 정보]
- 종목명: {stock_name}
- 종목코드: {ticker}

[평가 기준]
당신의 지식 베이스에서 이 기업의 최근 3년 IR 활동(주주서한, 어닝콜, 실적 가이던스, 기자간담회 등)을 검토하여:

1. 실적 가이던스 번복 횟수: 매출/영업이익 가이던스를 제시한 후 하향 조정하거나 달성하지 못한 횟수
2. 외부 탓 변명 빈도: 실적 부진을 매크로, 환율, 업황, 계절성 등 외부 요인 탓으로 돌린 빈도
3. 유상증자/CB/BW 발행 이력: 최근 3년 내 주주 희석 이벤트(유상증자, 전환사채, 신주인수권부사채 등) 발행 여부
4. 지배구조 리스크: 횡령, 배임, 사적 유용, 특수관계인 거래 등 경영진 신뢰 훼손 이슈

[신뢰도 등급]
A등급: 가이던스 이행률 높고 주주친화적 → "PASS"
B등급: 일부 번복 있으나 합리적 사유 → "PASS"
C등급: 잦은 번복, 외부 탓 변명 빈번, CB/유증 이력 있음 → "REJECT"
D등급: 심각한 신뢰 훼손 (횡령/배임/반복적 주주 기만) → "REJECT"

[출력 형식 (반드시 아래 JSON만 출력)]
{{"verdict": "PASS", "trust_grade": "B", "reason": "..."}}"""

        raw = self._call_gemini(prompt, use_pro=True)
        if not raw:
            logger.warning(f"[CEO Trust] AI 응답 실패 -> 안전하게 REJECT 처리: {stock_name}")
            return {"verdict": self.REJECT, "trust_grade": "?", "reason": "AI 응답 실패 (안전 모드)"}

        result = self._parse_json(raw)
        if not result:
            return {"verdict": self.REJECT, "trust_grade": "?", "reason": f"파싱 실패: {raw[:300]}"}

        verdict = result.get("verdict", "REJECT").upper()
        grade = result.get("trust_grade", "?").upper()
        if verdict not in [self.PASS, self.REJECT]:
            verdict = self.REJECT

        logger.info(f"[CEO Trust] {stock_name} -> {verdict} (신뢰도 {grade}등급)")
        return {
            "verdict": verdict,
            "trust_grade": grade,
            "reason": result.get("reason", ""),
        }

    # ══════════════════════════════════════════
    # 검증 #3: 재무 변곡점 & 경제적 해자 (매집주 확신)
    # 적용 대상: Track D
    # ══════════════════════════════════════════
    def check_moat_and_turnaround(self, stock_name: str, ticker: str,
                                   financial_summary: str = "") -> dict:
        """
        Track D(세력주 매집) 진입 시, FCF 트렌드 반등(턴어라운드)과
        독점적 경쟁력(Moat)을 분석하여 투자 비중(Sizing) 결정.

        반환: {"verdict": "PASS|REDUCE|REJECT", "moat_score": 0~10,
               "turnaround": true/false, "reason": "..."}
        """
        prompt = f"""당신은 워런 버핏의 투자 철학을 기반으로 기업의 경제적 해자(Moat)와 재무 변곡점을 분석하는 전문가입니다.

[임무]
아래 종목의 장기 투자 매력도를 평가하세요.

[종목 정보]
- 종목명: {stock_name}
- 종목코드: {ticker}
{f"- 참고 재무 데이터: {financial_summary}" if financial_summary else ""}

[분석 항목]

1. 잉여현금흐름(FCF) 트렌드 분석
   - 최근 3~5년의 FCF(영업현금흐름 - CAPEX)가 적자에서 흑자로 전환(턴어라운드)되었는지
   - 영업이익률이 저점을 찍고 반등하는 '변곡점' 구간인지
   - FCF가 지속 적자이고 개선 기미가 없으면 → 투자 부적합

2. 경제적 해자(Moat) 평가 (0~10점)
   - 브랜드 파워: 소비자 인지도, 가격 프리미엄 부과 능력
   - 전환 비용: 고객이 경쟁사로 이탈하기 어려운 구조적 장벽
   - 네트워크 효과: 사용자 증가 → 서비스 가치 증가 선순환
   - 원가 우위: 규모의 경제, 독점 기술, 희소 자원 접근성
   - 특허/인허가 독점: 법적 보호 장벽
   각 항목 0~2점, 합산 0~10점

3. 종합 판정
   - Moat 7점 이상 + 턴어라운드 확인 → "PASS" (비중 확대 가능)
   - Moat 4~6점 또는 턴어라운드 불확실 → "REDUCE" (소량만)
   - Moat 3점 이하 + FCF 지속 적자 → "REJECT"

[출력 형식 (반드시 아래 JSON만 출력)]
{{"verdict": "PASS", "moat_score": 7, "turnaround": true, "reason": "..."}}"""

        raw = self._call_gemini(prompt, use_pro=True)
        if not raw:
            logger.warning(f"[Moat] AI 응답 실패 -> REDUCE 처리: {stock_name}")
            return {"verdict": self.REDUCE, "moat_score": 0, "turnaround": False,
                    "reason": "AI 응답 실패 (안전 모드)"}

        result = self._parse_json(raw)
        if not result:
            return {"verdict": self.REDUCE, "moat_score": 0, "turnaround": False,
                    "reason": f"파싱 실패: {raw[:300]}"}

        verdict = result.get("verdict", "REDUCE").upper()
        if verdict not in [self.PASS, self.REDUCE, self.REJECT]:
            verdict = self.REDUCE

        logger.info(f"[Moat] {stock_name} -> {verdict} "
                    f"(해자={result.get('moat_score', 0)}점, "
                    f"턴어라운드={'Y' if result.get('turnaround') else 'N'})")
        return {
            "verdict": verdict,
            "moat_score": result.get("moat_score", 0),
            "turnaround": result.get("turnaround", False),
            "reason": result.get("reason", ""),
        }

    # ══════════════════════════════════════════
    # 검증 #4: 악마의 대변인 (Devil's Advocate)
    # 적용 대상: 전 트랙 (익절 보류 시 호출)
    # ══════════════════════════════════════════
    def devils_advocate(self, stock_name: str, ticker: str,
                        track: str, current_pnl_pct: float,
                        hold_reason: str = "") -> dict:
        """
        수익 극대화를 위해 익절을 미루고 홀딩하려 할 때,
        AI에게 '홀딩하면 안 되는 치명적 이유 3가지'를 찾게 한다.
        치명적 리스크 발견 시 기계적 익절 집행.

        반환: {"verdict": "HOLD|SELL", "fatal_risks": [...], "reason": "..."}
        """
        prompt = f"""당신은 '악마의 대변인(Devil's Advocate)' 역할을 수행하는 냉혹한 리스크 분석가입니다.
당신의 임무는 오직 하나: 이 주식을 지금 당장 팔아야 하는 이유를 찾는 것입니다.

[현재 상황]
- 종목명: {stock_name}
- 종목코드: {ticker}
- 트랙: Track {track}
- 현재 평가 수익률: {current_pnl_pct:+.2f}%
- 홀딩 유지 근거: {hold_reason or "수익 극대화를 위한 목표가 미도달"}

[당신의 임무]
위 종목을 계속 홀딩하면 안 되는 '치명적인 이유' 3가지를 최대한 찾아내세요.

[검토 항목]
1. 밸류에이션 과열: 현재 주가가 이미 펀더멘털 대비 과도하게 오른 상태는 아닌지
2. 수급 이탈 징후: 외국인/기관이 이탈하기 시작했는지, 거래량이 급감했는지
3. 뉴스/이벤트 리스크: 유상증자, CB 전환, 대주주 매도, 실적 발표, 공매도 재개 등
4. 섹터/매크로 역풍: 금리 인상, 규제 강화, 경쟁 심화 등 외부 악재
5. 차트 구조 붕괴: 주요 지지선 이탈, 데드크로스, 하락 추세 전환 신호

[판정 기준]
- 치명적 리스크가 2개 이상 발견되면 → "SELL" (즉시 익절)
- 경미한 리스크만 있거나 치명적 리스크 1개 이하 → "HOLD"
- 각 리스크의 심각도를 "치명적/경미" 로 표기

[출력 형식 (반드시 아래 JSON만 출력)]
{{"verdict": "HOLD", "fatal_risks": [{{"risk": "...", "severity": "치명적"}}, {{"risk": "...", "severity": "경미"}}], "reason": "..."}}"""

        raw = self._call_gemini(prompt, use_pro=True)
        if not raw:
            logger.warning(f"[Devil] AI 응답 실패 -> 안전하게 SELL 처리: {stock_name}")
            return {"verdict": "SELL", "fatal_risks": [{"risk": "AI 응답 실패", "severity": "치명적"}],
                    "reason": "AI 응답 실패 시 안전을 위해 익절 집행"}

        result = self._parse_json(raw)
        if not result:
            return {"verdict": "SELL", "fatal_risks": [], "reason": f"파싱 실패: {raw[:300]}"}

        verdict = result.get("verdict", "SELL").upper()
        if verdict not in ["HOLD", "SELL"]:
            verdict = "SELL"

        risks = result.get("fatal_risks", [])
        fatal_count = sum(1 for r in risks if r.get("severity", "") == "치명적")

        logger.info(f"[Devil] {stock_name} -> {verdict} "
                    f"(치명적 리스크 {fatal_count}개 / 총 {len(risks)}개)")
        return {
            "verdict": verdict,
            "fatal_risks": risks,
            "fatal_count": fatal_count,
            "reason": result.get("reason", ""),
        }

    # ══════════════════════════════════════════
    # 검증 #5: 상폐 리스크 스캔 (Track E 전용)
    # 적용 대상: Track E (낙폭과대 폭락주 스나이핑)
    # ══════════════════════════════════════════
    def check_delisting_risk(self, stock_name: str, ticker: str) -> dict:
        """
        Track E는 폭락한 종목의 '기술적 반등'을 노리는 기법이므로,
        기업의 성장성/CEO 신뢰도는 무시하되,
        상장폐지/거래정지로 직결되는 치명적 악재만 스캔한다.

        반환: {"verdict": "PASS|REJECT", "risk_type": "...", "reason": "..."}
        """
        prompt = f"""당신은 DART 전자공시 및 증권 뉴스 분석 전문가입니다.

[임무]
아래 종목은 최고가 대비 반토막 이상 급락한 '낙폭과대주'입니다.
기술적 반등을 노리고 매수하려 합니다.
그전에 이 종목이 '돌이킬 수 없는 치명적 악재'로 폭락한 것인지 판별해 주세요.

[종목 정보]
- 종목명: {stock_name}
- 종목코드: {ticker}

[치명적 악재 판별 기준 (아래 중 하나라도 해당되면 REJECT)]
1. 임상 실패 / 핵심 기술 붕괴: 바이오/제약 종목의 임상 3상 실패, 핵심 특허 무효화 등 '재료의 완전 소멸'
2. 횡령/배임/사기: 대표이사 또는 최대주주의 횡령, 배임, 자금 사적 유용으로 검찰 수사/기소 중
3. 감사의견 거절/한정: 외부 감사인이 '의견거절' 또는 '한정의견'을 표명한 이력
4. 자본잠식: 완전자본잠식 상태이거나 부분자본잠식이 2년 이상 지속
5. 상장폐지/거래정지 사유 발생: 이미 거래정지 예고 또는 상장적격성 심사 진행 중
6. 사업 완전 소멸: 주력 사업이 완전히 소멸하여 매출이 0에 수렴하는 상태

[중요 주의사항]
- 단순한 실적 부진, 업황 악화, 주가 하락은 치명적 악재가 아닙니다 (이것들은 반등 가능).
- 위 6가지처럼 '돌이킬 수 없는', '재료 자체가 소멸된' 경우에만 REJECT하세요.
- 확실한 증거가 없으면 PASS하세요 (추측으로 REJECT하지 말 것).

[출력 형식 (반드시 아래 JSON만 출력)]
{{"verdict": "PASS", "risk_type": "none", "reason": "..."}}"""

        raw = self._call_gemini(prompt, use_pro=True)
        if not raw:
            logger.warning(f"[Delisting] AI 응답 실패 -> 안전하게 REJECT 처리: {stock_name}")
            return {"verdict": self.REJECT, "risk_type": "ai_failure",
                    "reason": "AI 응답 실패 (폭락주 특성상 안전 우선)"}

        result = self._parse_json(raw)
        if not result:
            return {"verdict": self.REJECT, "risk_type": "parse_failure",
                    "reason": f"파싱 실패: {raw[:300]}"}

        verdict = result.get("verdict", "REJECT").upper()
        if verdict not in [self.PASS, self.REJECT]:
            verdict = self.REJECT

        logger.info(f"[Delisting] {stock_name} -> {verdict} "
                    f"(리스크={result.get('risk_type', 'none')})")
        return {
            "verdict": verdict,
            "risk_type": result.get("risk_type", "none"),
            "reason": result.get("reason", ""),
        }

    # ══════════════════════════════════════════════
    # 검증 #6: 시대 중심주(메가 트렌드) 팩트체크 (Track F 전용)
    # 적용 대상: Track F (메가 트렌드 장기 눌림목 스윙)
    # ══════════════════════════════════════════════
    def check_mega_trend(self, stock_name: str, ticker: str) -> dict:
        """
        Track F는 150/200일선 눌림목에서 비중 배팅하는 장기 전략이므로,
        해당 종목이 '단순 테마주'가 아닌 '당대의 시대 중심 섹터'에 속하는
        우량주인지 AI가 검증한다.
        시대 중심주가 아니면 150일선 지지가 나와도 매수를 전면 거부(REJECT)한다.

        반환: {"verdict": "PASS|REJECT", "sector": "...", "mega_trend_score": 0~10, "reason": "..."}
        """
        prompt = f"""당신은 대한민국 증권사 리서치 센터 애널리스트입니다.

[임무]
아래 종목이 현재 시대의 확실한 '메가 트렌드(시대 중심) 섹터'에 속하는 우량주인지 검증하세요.
이 검증은 150/200일 이동평균선에서 장기 비중 배팅(최대 원금의 20%)을 하기 전 최종 관문입니다.
단순한 찌라시 테마주나 소형주에 20%를 배팅하면 치명적이므로 매우 깐깐하게 판단해야 합니다.

[종목 정보]
- 종목명: {stock_name}
- 종목코드: {ticker}

[판단 기준]
1. 시대 중심 섹터 해당 여부 (0~10점)
   다음 중 해당하는 메가 트렌드 섹터에 속하는지 확인:
   - AI 반도체 (HBM, 후공정, 패키징, OSAT, 파운드리 등)
   - 2차전지 / 전고체 배터리 밸류체인
   - 로봇 / 자동화 / 인간형 로봇
   - 전력 인프라 (HVDC, 변압기, 전선, 송전)
   - 태양광 / 신재생에너지
   - 방산 / 우주항공
   - 데이터센터 / 클라우드 인프라
   각 섹터 해당성 0~5점, 실제 매출 기여도 0~5점, 합산 0~10점

2. 우량주 검증
   - 시가총액 5,000억 이상의 중형주+ 수준인지
   - 기관/외인 수급 기반이 있는지 (개인 만의 뇈잡이 아닌지)
   - 실제 매출/영업이익이 있는 기업인지 (적자 바이오/스타트업 제외)

3. 종합 판정
   - 메가 트렌드 7점 이상 + 우량주 확인 → "PASS"
   - 5~6점: 섹터는 맞으나 직접 수혜도 불분명 → "REJECT" (장기 비중 배팅에는 부적합)
   - 4점 이하: 테마주 / 찌라시 → "REJECT"

[출력 형식 (반드시 아래 JSON만 출력)]
{{"verdict": "PASS", "sector": "반도체 후공정", "mega_trend_score": 8, "reason": "..."}}"""

        raw = self._call_gemini(prompt, use_pro=True)
        if not raw:
            logger.warning(f"[MegaTrend] AI 응답 실패 -> 안전하게 REJECT 처리: {stock_name}")
            return {"verdict": self.REJECT, "sector": "unknown", "mega_trend_score": 0,
                    "reason": "AI 응답 실패 (장기 비중 배팅은 확실한 검증이 필수)"}

        result = self._parse_json(raw)
        if not result:
            return {"verdict": self.REJECT, "sector": "unknown", "mega_trend_score": 0,
                    "reason": f"파싱 실패: {raw[:300]}"}

        verdict = result.get("verdict", "REJECT").upper()
        if verdict not in [self.PASS, self.REJECT]:
            verdict = self.REJECT

        score = result.get("mega_trend_score", 0)
        logger.info(f"[MegaTrend] {stock_name} -> {verdict} "
                    f"(섹터={result.get('sector', '?')}, 점수={score}점)")
        return {
            "verdict": verdict,
            "sector": result.get("sector", "unknown"),
            "mega_trend_score": score,
            "reason": result.get("reason", ""),
        }

    # ══════════════════════════════════════════════
    # 검증 #7: Fail-Close 최종 관문 (월스트리트 수석 트레이더 페르소나)
    # 적용 대상: 전 트랙 (매수 직전 최종 검토)
    # ══════════════════════════════════════════════
    def fail_close_final_gate(self, stock_name: str, ticker: str, track: str,
                              quant_summary: str = "") -> dict:
        """
        Fail-Close 최종 관문.
        정량적 데이터(퀀트)가 매수 합격을 주더라도,
        AI 에이전트가 마지막으로 검토하여 데이터에 모순이 있거나
        변동성 리스크가 감지되면 무조건 거래를 차단(REJECT)합니다.

        반환: {"verdict": "PASS|REJECT", "confidence": 0~100, "reason": "..."}
        """
        prompt = f"""[System Persona]
당신은 월스트리트에서 20년 경력의 수석 트레이더(리스크 관리 전문가)입니다.
당신의 유일한 임무는 아래 매수 신호를 최종 검토하여,
'진짜 들어가도 되는지' 판단하는 것입니다.

당신의 핵심 원칙:
- 의심이 있으면 사지 않는다 (Fail-Close)
- 데이터 간 모순이 있으면 사지 않는다
- 확신이 70% 미만이면 사지 않는다
- 100번의 기회 중 1번을 놓치더라도, 1번의 대손을 막는 것이 우선이다
- 뉴스 감성(Sentiment)과 거시경제 환경을 반드시 고려하라

[Quant 판단 요약]
{quant_summary or '퀀트 데이터 없음'}

[검토 대상]
- 종목명: {stock_name}
- 종목코드: {ticker}
- 트랙: Track {track}

[검토 항목]
1. 거시경제 환경: 현재 금리/환율/VIX 방향이 이 종목에 역풍은 아닌지
2. 뉴스 감성: 이 종목/섹터 관련 최신 뉴스가 부정적인지
3. 데이터 모순 검증: Quant 판단에 논리적 모순이 있는지
   (예: 거래량 폭발인데 하락중, RSI 과매수인데 매수 신호 등)
4. 변동성 리스크: 이 종목이 굉장히 위험한 시점(실적발표 전, CB 전환 예정 등)인지

[판정 기준]
- 확신 70% 이상 + 모순/리스크 없음 → "PASS"
- 확신 70% 미만 또는 모순/리스크 발견 → "REJECT"

[출력 형식 (반드시 아래 JSON만 출력)]
{{"verdict": "PASS", "confidence": 85, "reason": "..."}}"""

        raw = self._call_gemini(prompt, use_pro=True)
        if not raw:
            logger.warning(f"[Fail-Close] AI 응답 실패 -> REJECT: {stock_name}")
            return {"verdict": self.REJECT, "confidence": 0,
                    "reason": "AI 응답 실패 (Fail-Close 원칙에 따라 REJECT)"}

        result = self._parse_json(raw)
        if not result:
            return {"verdict": self.REJECT, "confidence": 0,
                    "reason": f"파싱 실패: {raw[:300]}"}

        verdict = result.get("verdict", "REJECT").upper()
        confidence = result.get("confidence", 0)

        # Fail-Close: 확신 70% 미만이면 무조건 REJECT
        if confidence < 70:
            verdict = self.REJECT
            logger.warning(
                f"[Fail-Close] {stock_name} 확신도 {confidence}% < 70% -> 강제 REJECT")

        if verdict not in [self.PASS, self.REJECT]:
            verdict = self.REJECT

        logger.info(f"[Fail-Close] {stock_name} Track {track} -> {verdict} "
                    f"(확신도={confidence}%)")
        return {
            "verdict": verdict,
            "confidence": confidence,
            "reason": result.get("reason", ""),
        }

    # ══════════════════════════════════════════
    # 검증 #8: ML 기반 승률 예측 관문
    # ══════════════════════════════════════════
    def ml_predict_gate(self, stock_name: str, ticker: str, quant_features: dict) -> dict:
        """머신러닝 모델을 통한 승률 예측 게이트"""
        try:
            import joblib
            import pandas as pd
            import os
            
            model_path = "data/ml_brain.pkl"
            if not os.path.exists(model_path):
                return {"verdict": self.PASS, "confidence": 50, "reason": "ML 모델 없음 (학습 대기 중)"}
                
            model = joblib.load(model_path)
            
            # 피처 스키마 맞추기
            features = pd.DataFrame([{
                "vol_ratio": quant_features.get("vol_ratio", 0.0),
                "env_diff": quant_features.get("env_diff", 0.0),
                "bb_width": quant_features.get("bb_width", 0.0),
                "rsi": quant_features.get("rsi", 50.0),
                "macd": quant_features.get("macd", 0.0),
                "adx": quant_features.get("adx", 0.0),
                "atr": quant_features.get("atr", 0.0)
            }])
            
            # 클래스 1(승리)의 예측 확률
            prob = model.predict_proba(features)[0][1] * 100
            
            if prob < 40:
                logger.warning(f"[ML Gate] {stock_name} 승률 예측 {prob:.1f}% < 40% -> REJECT")
                return {"verdict": self.REJECT, "confidence": prob, "reason": f"ML 승률 예측이 {prob:.1f}%로 너무 낮습니다."}
            else:
                logger.info(f"[ML Gate] {stock_name} 승률 예측 {prob:.1f}% -> PASS")
                return {"verdict": self.PASS, "confidence": prob, "reason": f"ML 승률 예측: {prob:.1f}%"}
                
        except Exception as e:
            logger.error(f"[ML Gate] 예측 중 에러: {e}")
            return {"verdict": self.PASS, "confidence": 50, "reason": f"ML 에러: {e}"}

    # ══════════════════════════════════════════
    # 통합 게이트: 트랙별 필수 검증 자동 실행
    # ══════════════════════════════════════════
    def gate_check(self, stock_name: str, ticker: str, track: str,
                   theme: str = "", quant_summary: str = "", quant_features: dict = None) -> dict:
        """
        Phase 2 라우팅 결과를 받아, 해당 트랙에 필요한 심층 검증을 자동 실행.
        run.py에서 매수 직전에 이 함수 하나만 호출하면 된다.

        반환: {
            "verdict": "PASS|REDUCE|REJECT",
            "checks": {"segment": {...}, "ceo_trust": {...}, "moat": {...}},
            "summary": "한줄 요약"
        }
        """
        checks = {}
        final_verdict = self.PASS

        # Track A: 테마 검증만 (D+0 단타이므로 가벼운 검증)
        if track == "A" and theme:
            seg = self.check_segment(stock_name, ticker, theme)
            checks["segment"] = seg
            if seg["verdict"] == self.REJECT:
                final_verdict = self.REJECT
            elif seg["verdict"] == self.REDUCE:
                final_verdict = self.REDUCE

        # Track B: 세그먼트 팩트체크
        elif track == "B":
            if theme:
                seg = self.check_segment(stock_name, ticker, theme)
                checks["segment"] = seg
                if seg["verdict"] == self.REJECT:
                    final_verdict = self.REJECT
                elif seg["verdict"] == self.REDUCE:
                    final_verdict = self.REDUCE

        # Track C: CEO 신뢰도 (오버나잇 리스크)
        elif track == "C":
            ceo = self.check_ceo_trust(stock_name, ticker)
            checks["ceo_trust"] = ceo
            if ceo["verdict"] == self.REJECT:
                final_verdict = self.REJECT

        # Track D: CEO 신뢰도 + 해자/변곡점 (풀 스캔)
        elif track == "D":
            ceo = self.check_ceo_trust(stock_name, ticker)
            checks["ceo_trust"] = ceo
            if ceo["verdict"] == self.REJECT:
                final_verdict = self.REJECT
            else:
                moat = self.check_moat_and_turnaround(stock_name, ticker)
                checks["moat"] = moat
                if moat["verdict"] == self.REJECT:
                    final_verdict = self.REJECT
                elif moat["verdict"] == self.REDUCE and final_verdict != self.REJECT:
                    final_verdict = self.REDUCE

        # Track E: 상폐 리스크 스캔만 (폭락주 특성상 기업 성장성은 무시)
        elif track == "E":
            delist = self.check_delisting_risk(stock_name, ticker)
            checks["delisting"] = delist
            if delist["verdict"] == self.REJECT:
                final_verdict = self.REJECT

        # Track F: 메가 트렌드 시대 중심주 팩트체크 (장기 비중 배팅 전 필수 검증)
        elif track == "F":
            mega = self.check_mega_trend(stock_name, ticker)
            checks["mega_trend"] = mega
            if mega["verdict"] == self.REJECT:
                final_verdict = self.REJECT

        # ML 승률 예측 관문 (정량적 데이터가 있을 경우)
        if quant_features:
            ml_result = self.ml_predict_gate(stock_name, ticker, quant_features)
            checks["ml_predict"] = ml_result
            if ml_result["verdict"] == self.REJECT:
                final_verdict = self.REJECT
                # ML에서 REJECT된 경우 바로 리턴하여 불필요한 LLM 호출(또는 요약 생성) 방지 가능하나,
                # 아래 요약에 반영하기 위해 계속 진행
                
        # 요약 생성
        check_names = list(checks.keys())
        summary = f"Phase 3 검증 완료 [{', '.join(check_names) or '해당없음'}] -> {final_verdict}"

        # Fail-Close 최종 관문: 모든 검증 통과(PASS) 후에도 AI가 최종 검토
        if final_verdict == self.PASS:
            fail_close = self.fail_close_final_gate(
                stock_name, ticker, track, quant_summary)
            checks["fail_close"] = fail_close
            if fail_close["verdict"] == self.REJECT:
                final_verdict = self.REJECT
                summary += f" -> Fail-Close REJECT (확신도={fail_close.get('confidence', 0)}%)"
                logger.warning(
                    f"[Gate] {stock_name} 퀀트 PASS였으나 Fail-Close에서 REJECT! "
                    f"사유: {fail_close.get('reason', '')[:100]}")

        logger.info(f"[Gate] {stock_name} Track {track} -> {final_verdict} ({summary})")
        return {
            "verdict": final_verdict,
            "checks": checks,
            "summary": summary,
        }

    # ══════════════════════════════════════════
    # 글로벌(미국장) 테마 분석 및 수혜주 매핑
    # ══════════════════════════════════════════
    def update_daily_themes(self) -> dict:
        """
        전일/금주 미국 증시 주도 섹터를 분석하여 한국 증시 관련 대장주를 추출.
        결과를 data/daily_theme.json에 저장하고 반환한다.
        """
        import datetime
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        cache_path = "data/daily_theme.json"
        
        # 이미 오늘 분석을 마쳤으면 캐시 반환
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cache_data = json.load(f)
                    if cache_data.get("date") == today:
                        logger.info("[Theme] 금일 미국장 연동 테마 캐시를 로드했습니다.")
                        return cache_data
            except Exception as e:
                logger.warning(f"[Theme] 캐시 로드 실패: {e}")
        
        logger.info("[Theme] 미국장 주도 섹터 및 한국장 수혜주 AI 리서치 시작...")
        
        prompt = f"""
당신은 글로벌 매크로 및 주식 시장 분석 전문가입니다.
오늘 날짜는 {today} 입니다.

최근 1~2거래일 또는 이번 주 미국 증시(나스닥, S&P 500)에서 가장 강하게 상승한(돈이 몰린) 주도 섹터 3개를 선정하고, 각 섹터의 상승 사유를 설명해 주세요.
그리고 해당 미국 주도 섹터들과 연동되어 **한국 증시(KOSPI/KOSDAQ)에서 수혜를 받을 수 있는 대표 대장주(종목코드 6자리 포함)**를 섹터별로 3~5개씩, 총 10~15개 추천해 주세요.
시총이 너무 작거나(1000억 미만) 잡주는 제외하고, 시장의 수급이 몰릴 만한 주도주 위주로 선정하세요.

반드시 아래 JSON 포맷으로만 응답하세요. (마크다운 백틱 제외, 오직 JSON만)
{{
    "date": "{today}",
    "themes": [
        {{
            "us_sector": "섹터명 (예: AI 반도체)",
            "reason": "상승 사유 요약 (1~2문장)",
            "kr_stocks": [
                {{"ticker": "000660", "name": "SK하이닉스"}},
                {{"ticker": "042700", "name": "한미반도체"}}
            ]
        }}
    ],
    "briefing": "텔레그램으로 전송될 오늘의 글로벌 시황 및 추천 테마 요약 (3문장 이내)"
}}
"""
        raw_resp = self._call_gemini_with_search(prompt, use_pro=True)
        
        if not raw_resp:
            logger.error("[Theme] AI 시황 분석 응답 없음.")
            return {}
            
        parsed = self._parse_json(raw_resp)
        if not parsed or "themes" not in parsed:
            logger.error(f"[Theme] 분석 결과 JSON 파싱 실패: {raw_resp[:200]}")
            return {}
            
        # 모든 Ticker 수집 (편의를 위해 평탄화)
        all_tickers = []
        for theme in parsed.get("themes", []):
            for stock in theme.get("kr_stocks", []):
                all_tickers.append(stock["ticker"])
        
        parsed["all_tickers"] = list(set(all_tickers))
        
        # 캐시 저장
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(parsed, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.warning(f"[Theme] 캐시 저장 실패: {e}")
            
        logger.info(f"[Theme] 리서치 완료. {len(parsed['all_tickers'])}개 주도주 후보 확보.")
        return parsed
