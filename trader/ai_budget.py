"""
ai_budget.py - Gemini API 일일 호출 예산 가드 (과금 폭탄 방지)

비싼 Pro 모델 호출을 하루 단위로 카운트하고, 안전 상한을 넘으면
호출을 막아 Flash로 우회시킨다. (Tier1 RPD 한도 초과로 봇이 죽는 것도 방지)

- DAILY_PRO_LIMIT: 하루 Pro 호출 안전 상한 (Tier1 RPD 250보다 충분히 낮게)
- 날짜가 바뀌면 자동으로 카운터 리셋
- 환경변수 GEMINI_DAILY_PRO_LIMIT 로 오버라이드 가능
"""
import datetime
import logging
import os

logger = logging.getLogger("ssangbong.budget")

# 하루 Pro 호출 안전 상한 (환경변수로 조정 가능)
try:
    DAILY_PRO_LIMIT = int(os.environ.get("GEMINI_DAILY_PRO_LIMIT", "60"))
except (ValueError, TypeError):
    DAILY_PRO_LIMIT = 60

_state = {"date": None, "pro_calls": 0}


def _today() -> str:
    return datetime.date.today().isoformat()


def _reset_if_needed() -> None:
    d = _today()
    if _state["date"] != d:
        _state["date"] = d
        _state["pro_calls"] = 0


def allow_pro() -> bool:
    """오늘 Pro 호출 여유가 남아 있는지."""
    _reset_if_needed()
    return _state["pro_calls"] < DAILY_PRO_LIMIT


def record_pro() -> None:
    """Pro 호출 1회 성공 기록."""
    _reset_if_needed()
    _state["pro_calls"] += 1
    if _state["pro_calls"] in (DAILY_PRO_LIMIT // 2, DAILY_PRO_LIMIT - 5, DAILY_PRO_LIMIT):
        logger.warning(
            f"[Budget] 금일 Pro 호출 {_state['pro_calls']}/{DAILY_PRO_LIMIT}회"
        )


def pro_calls_today() -> int:
    _reset_if_needed()
    return _state["pro_calls"]
