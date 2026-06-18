# 디버깅 참조: Gemini API 과금 절감 (2026-06-18 적용)

> 작성 대상: **안티(anti-user)** — 디버깅/배포 시 참고. 배포는 안티가 진행.

## 1. 왜 바꿨나 (과금 폭탄 원인)
- `gemini-3.1-pro-preview`(고가)를 **모든 추론의 1차 기본값**으로 사용.
- 60초마다 최대 13종목 라우팅 + 통과 종목당 Phase 3 게이트가 Pro 2~4회 추가 호출(항상 `fail_close` 포함).
- 타이밍 게이트 `continue`가 캐시 미설정 → Track A/C/D 종목이 매 60초 Pro 재호출.
- 결과: AI Studio Tier1 **Pro RPD 219/250 (한도 임박)** → 비용 폭증 + 한도 초과 시 봇 다운.

## 2. 무엇을 바꿨나
| 위치 | 변경 |
|------|------|
| `trader/ai_budget.py` (신규) | 일일 **Pro 호출 상한 가드**. 기본 60회/일, 초과 시 Pro 차단→Flash 우회 |
| `trader/ai_router.py` | 라우팅 1차 모델 **Pro→Flash** (Pro는 예산 내 폴백). `maxOutputTokens` 4096→2048 |
| `trader/ai_fundamental.py` | segment/ceo/moat/devils/delisting/mega 검증 **Flash**로. `fail_close`·일일테마만 Pro. `maxOutputTokens` 4096→1024 |
| `run.py` | `SCAN_INTERVAL` 60→120초, `AI_SKIP_TTL` 900→1800초, 라우팅 대상 13→6, 타이밍 게이트 continue에 `ai_skip_cache` 등록 |

## 3. 튜닝 노브 (코드 수정 없이 .env로)
- `GEMINI_DAILY_PRO_LIMIT=60` → 더 줄이려면 `30` 등으로 낮춤 (`ai_budget.py` 기본 60).
- 라우팅 품질을 다시 올리려면 `trader/ai_router.py`의 `model_primary`를 Pro로 되돌리되 **예산 가드는 유지**.
- 호출 빈도 더 줄이려면 `run.py`의 `SCAN_INTERVAL` ↑, `analysis_targets` 슬라이스 ↓.

## 4. 로그로 동작 확인하기 (grep 키워드)
- `[Budget] 금일 Pro 호출 N/60회` — Pro 사용량 추적 (절반/임박/도달 시 출력).
- `[AI] Pro 일일 상한(60) 도달 → ... 건너뜀` — 라우팅에서 Pro 차단됨.
- `[Fundamental] Pro 일일 상한(60) 도달 → ... 건너뜀` — Phase 3에서 Pro 차단됨.
- `[AI] 폴백 모델(...) 추론 성공` — Flash 실패로 Pro 폴백이 실제 발생한 경우(드물어야 정상).
- `[AI Cache] ... 재분석 생략`, `[Scan] ... 볼륨 확증 미달` — 캐시/타이밍 게이트로 재호출 차단됨.

## 5. 정상 동작 기대치
- AI Studio 대시보드에서 **Pro RPD ≤ 60**(캡), 대부분 트래픽이 **Flash RPD**(한도 10K, 저렴)로 이동.
- Pro 폴백 로그가 거의 안 보여야 함. 자주 보이면 Flash가 계속 실패 중 → API 키/네트워크/모델명 점검.

## 6. 디버깅 시 흔한 함정
- **모델명**: `gemini-3.1-pro-preview` / `gemini-3.5-flash` 하드코딩(`ai_router.py:118-119`, `ai_fundamental.py:43-44`). 모델 종료/이름 변경 시 전부 실패하므로 여기부터 확인.
- **예산 카운터는 프로세스 메모리**: 봇 재시작하면 카운트 0으로 리셋됨. 재시작 루프가 잦으면 일일 상한이 사실상 무력화되니 크래시 루프부터 잡을 것.
- **Flash로 내려도 품질 저하로 SKIP/REJECT가 늘 수 있음** → 매수 빈도 감소는 비용 절감의 정상 부작용. 너무 안 사면 신뢰도 임계치(`confidence`)나 모델을 조정.
- **bs4/pykrx 미설치**: `trader/signals.py`가 `bs4`, `pykrx_scanner.py`가 `pykrx`를 import. `pip install -r requirements.txt`로 먼저 환경 맞출 것 (requirements 갱신됨).
- **ML 게이트**: `data/ml_brain.pkl` 없으면 `ml_predict_gate`가 confidence 50 고정 → Watchlist Sniper가 룰 기반 폴백으로 격발(`run.py` check_watchlist_sniper). joblib/scikit-learn 필요.

## 7. 롤백
- 비용 변경만 되돌리려면: `ai_router.py`/`ai_fundamental.py`의 모델/`use_pro`/`maxOutputTokens`, `run.py`의 상수 4개를 이전 값으로. `ai_budget.py`는 삭제하지 말고 상한만 크게(예: 10000) 두면 사실상 무효화.
