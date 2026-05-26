Ssangbong_Bot_Manual_V2.md
1. System Overview (시스템 개요)
Bot Name: Ssangbong Bot V2 (AI 기반 하이브리드 자동매매 봇)
Core Goal: 데이트레이딩(5~7% 복리)을 베이스로 하되, 기술적 모멘텀과 심층 펀더멘털 분석을 결합하여 수익을 극대화한다.
Advanced Logic:
1차: 통합 검색망(Base Screener)으로 주도주 포착.
2차: AI Router(Flash)가 차트/수급 기반 트랙(Track A~F) 결정.
3차: AI Fundamental(Pro)이 DART/뉴스 기반 심층 재무 & 리스크 스캔 진행 (오버나잇/스윙의 경우).
Infrastructure: Python 3.11+, Linux Server, KIS API, Telegram.

2. Architecture & Modules (모듈 아키텍처)
kis_client.py: KIS API 통신, 토큰 갱신 및 잔고 조회 (체결강도, 상장일, 네이버 재무 크롤링 지원).
telegram_bot.py: 매매 알림 및 2단계 컨펌(/비상정지 등).
signals.py: Phase 1 통합 베이스 조건검색, 트랙별 영웅문 정량 필터 및 보조 지표 연산(VWAP, ATR).
quant_indicators.py: Wilder's Smoothing 정통 퀀트 지표 엔진 (RSI/ATR/ADX/MACD). TradingView 동일 계산 방식 적용. 4대 매도 트리거 통합 판정 함수 제공.
ai_router.py: Phase 2 분봉/호가 분석 및 Track A~F 동적 라우팅 + ATR 기반 동적 TP/SL 산출 (Gemini 3.1 Pro Preview 메인, Gemini 3.5 Flash 폴백).
ai_fundamental.py: Phase 3 공시 및 재무 심층 검증 + Fail-Close 최종 관문 (Gemini 3.1 Pro Preview 메인, Gemini 3.5 Flash 폴백).
orders.py: 포지션 사이징, 5대 절대 방어장치 및 일일 손절/재진입 쿨다운 제어. ATR TP/SL 데이터 포지션 내 저장.
monitor.py: 수익률 감시, 4대 매도 트리거(패닉셀/MACD/데드크로스/RSI), ATR 2차 안전장치, Track A 정오(12:00) 올킬 로직, 거미줄 피라미딩 감시.
journal.py: SQLite3 매매일지 저장 및 AI 기반 복기.
run.py: 논블로킹 1초 이벤트 메인 루프 (시간대별 진입 필터링, 상한가 차단, Phase 3 gate_check 통합).

3. Trading Hours Policy (매매 시간 정책)
09:00 ~ 09:30: 관망 및 주도 테마 탐색 (단타 진입 강력 차단).
09:30 ~ 14:30: Track A (단타) 집중 진입 시기 (오후 14:30 이후 스캔 대기).
12:00 ~ 12:30: Track A 전체 합산 수익 시 '올킬' 익절 (손실 시 본절/약손절 탈출 시도).
14:00 ~ 15:20: Track D (매집주) 스캔 개시.
14:45 ~ 15:20: Track C (종가 베팅) 1차/2차 집중 스캔 및 집행.
24시간 가동: Track E (폭락주) 상시 모니터링 및 거미줄 매수 대기.
24시간 가동: Track F (메가트렌드) 상시 모니터링 및 150/200일선 도달 감지.

4. Signal Logic: 3-Phase Routing & Verification
[Phase 1: Base Screener & Track Pre-Filter]
제외 종목: 관리종목, 우선주, ETF, 스펙(SPAC) 원천 제외.
[Phase 2: AI Dynamic Routing (Track A~E)]
Track A: 데이트레이딩 (엔벨로프 20, 12.5% 발산 돌파) → God Mode 폐지, AI 검증 2중 통과 및 당일 VWAP 라인 이상 조건 필수.
Track B: 눌림목 단기 스윙 (거래량 20% 이하 급감, 20일선 근접, 정배열).
Track C: 종가 베팅 (14:45~ 수급 폭발, 체결강도 50%+, 최근 거래량 100%+ 급증).
Track D: 세력주 매집 (20봉 신저가, PER 1+ , 유보율 200%+, 자산증감 10%+).
Track E: 폭락주 스나이핑 (250거래일 이상 상장주, 바닥 대비 300%+ 대시세 이력 필수).
Track F: 메가 트렌드 장기 눌림목 (60일 내 50%+ 시세분출 + 150/200일선 근접 + 시대중심 섹터 우량주). God Mode 금지, 종가 분할매집.
[Phase 3: AI Fundamental Deep Scan + Fail-Close]
Track B, C, D, E, F '오버나잇' 전 필수 통과.
상폐 리스크 스캔 (Track E 전용): 횡령, 배임, 자본잠식 등 6대 악재 실시간 체크.
메가 트렌드 시대 중심주 팩트체크 (Track F 전용): 단순 테마주가 아닌 시대 중심 섹터(반도체/2차전지/로봇/전력/태양광) 우량주인지 AI 검증. 부적합 시 REJECT.
Fail-Close 최종 관문 (전 트랙): 월스트리트 20년 수석 트레이더 페르소나가 거시경제/뉴스 감성/데이터 모순/변동성 리스크를 최종 검토. 확신 70% 미만 시 무조건 REJECT.

5. Security & Risk Management (5대 절대 방어 + 퀀트 방어 체계)
Volume Confirm: 최근 20봉 평균 대비 1.5배 이상 거래량 터치 필수.
Track E Hard Lock: 한 종목 및 전체 Track E 비중이 원금의 10%를 절대 초과하지 않도록 락.
Spider Web Buying (Track E): 200일 최고가 기준 48% / 39% / 34% / 30% 지지선 도달 시 3.75%씩 기계적 분할 매수.
Absolute Hard SL: 단타는 ATR 기반 동적 손절(pivot_low - ATR * 1.5, 최소 -2% ~ 최대 -5% 제한), 스윙 -7%, 폭락주 평단 대비 +5% 도달 시 기계적 전량 익절.
ATR 2차 안전장치: 진입 시 Wilder ATR(14)을 기준으로 atr_sl_price(진입가 - 2*ATR), atr_tp_price(진입가 + 3*ATR) 자동 산출. 기존 동적 SL(5분봉 눌림목 저점)을 1차로 사용하고, ATR SL을 최대 허용 손실 한도(2차 안전장치)로 운용하는 이중 방어 구조.
4대 매도 트리거 (분할익절 후 잔여 포지션 청산): (1) 패닉셀 - 거래량 3배 폭발 음봉 → 무조건 즉시 청산, (2) MACD 음전환 → 추세 전환 시그널, (3) 데드크로스 → ADX 약세(< 20)일 때만 실행, (4) RSI 과매수 → ADX 약세(< 20)일 때만 실행.
Track F Weight Lock: 한 종목 및 전체 Track F 비중이 원금의 20%를 절대 초과하지 않도록 락. 200일선 하향 이탈 시 기계적 손절.
Kill Switch: 일일 손실 한도 도달 시 전체 가동 중단.
Risk Hard Guard: 상한가(+25% 이상) 종목 진입 원천 차단, 손절 후 60분 쿨다운 강제(Revenge Trading 방지), Track A 일일 손절 4회 누적 시 당일 Track A 비활성화.

6. Execution Instruction (운용 지침)
Track E 스캔 시 Pandas rolling max 연산을 통해 200일 최고가를 정확히 산출한다.
모든 트랙은 '영웅문 정량 필터'를 먼저 통과한 종목에 대해서만 AI 라우팅을 진행한다 (Hard Filter 정책).
Track C는 14:45분에 1차 검색을 수행하고, 미충족 시 15:00에 최종 검색하여 종목을 발굴한다.
12:00 정오에는 Track A(단타) 포지션에 대해 '합산 수익 올킬' 로직이 가동되어 수익을 확정한다.

7. Update Log (업데이트 내역)
[2026-05-26] 베개 매매법 아키텍처 통합 (Phase 1~3)
- Phase 1: Wilder's Smoothing 정통 퀀트 지표 엔진 신규 구축 (quant_indicators.py). RSI/ATR/ADX/MACD를 TradingView 동일 방식으로 계산.
- Phase 2: 4대 매도 트리거(패닉셀/MACD/데드크로스/RSI) monitor.py에 삽입. 50% 분할익절 후 잔여 포지션의 청산 판단에 활용.
- Phase 2: ATR 기반 동적 TP/SL을 ai_router → orders → monitor 전 라인에 걸쳐 통합. 이중 방어 구조(1차: 5분봉 동적SL, 2차: ATR 최대손실 한도).
- Phase 3: Fail-Close 최종 관문 신설 (ai_fundamental.py). 월스트리트 수석 트레이더 페르소나가 매수 직전 최종 검토, 확신 70% 미만 시 무조건 REJECT.
- Phase 3: run.py에 FundamentalScanner gate_check 통합. 모든 매수 전 트랙별 심층 검증 + Fail-Close 자동 실행.
- Phase 4 (FastAPI 전환): 현재 안정적 구조 유지 결정, 미시행.

[2026-05-23] 쌍봉봇 전체 모듈 정밀 디버깅 및 무결성 패치
- 일일 손절 카운터(Track A) 자정 리셋 로직 보완 (무한 차단 버그 해결).
- 지정가 체결(confirm_pending) 시 동적 손절 데이터(pivot_low, dynamic_sl_price 등) 정상 상속 처리.
- SMC(스마트머니컨셉) 스윙 로우(pivot_low) 계산 로직을 정상 반환하도록 수정하여 AI의 동적 손절 기능 복구.
- 볼륨 확증 필터(Volume Confirm Filter)의 임계값을 기존 0.1(10%)에서 1.5(150%)로 상향 보정하여 실제 돌파 판별력 강화.
- Track E 추가매수 하드락 비중을 초기 할당량과 동일하게 10%로 하향 통일.
- JSON 시간 데이터 복원 및 Gemini AI 응답 None 처리 등 잠재적 크래시(TypeError) 발생 위험 구간 전면 방어 코드 추가.