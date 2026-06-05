"""
telegram_bot.py - 텔레그램 지휘 통제실
매뉴얼 Section 6 구현

기능:
  - 매수/매도 체결 알림
  - AI 트랙 전환 사유 보고
  - /상태, /비상정지, /재개시 명령어
"""
import logging
import os
import time
import threading
from typing import Optional, Callable

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("ssangbong.telegram")


class TelegramBot:
    """텔레그램 알림 + 명령어 수신"""

    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self._last_update_id = 0
        self._command_handlers: dict[str, Callable] = {}

    # ──────────────────────────────────────────
    # 메시지 전송
    # ──────────────────────────────────────────
    def send(self, text: str, parse_mode: str = "HTML", reply_markup: dict = None) -> bool:
        """텔레그램 메시지 전송"""
        if not self.token or not self.chat_id:
            logger.warning("[TG] 토큰/채팅ID 미설정")
            return False

        try:
            # 텔레그램 메시지 길이 제한 (최대 4096자)
            if len(text) > 4090:
                text = text[:4087] + "..."

            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup

            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json=payload,
                timeout=10,
            )
            if resp.status_code != 200:
                logger.error(f"[TG] 전송 실패: {resp.text}")
                return False
            return True
        except Exception as e:
            logger.error(f"[TG] 전송 에러: {e}")
            return False

    def send_menu(self):
        """하단 고정형 탭다운 메뉴(ReplyKeyboardMarkup) 전송"""
        keyboard = {
            "keyboard": [
                [
                    {"text": "/상태"},
                    {"text": "/수익"},
                    {"text": "/매매일지"}
                ],
                [
                    {"text": "/긴급익절"},
                    {"text": "/비상청산"},
                    {"text": "/추가매수"}
                ],
                [
                    {"text": "/비상정지"},
                    {"text": "/재개시"}
                ]
            ],
            "resize_keyboard": True,
            "persistent": True
        }
        self.send("🤖 <b>쌍봉봇 메인 제어판</b>\n하단 메뉴를 이용해 명령을 내려주세요.", reply_markup=keyboard)

    def send_confirm_inline(self, message: str, confirm_data: str, cancel_data: str = "cancel_action"):
        """2단계 확인을 위한 인라인 키보드 (Yes/No)"""
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ 예, 실행합니다", "callback_data": confirm_data},
                    {"text": "❌ 아니오, 취소합니다", "callback_data": cancel_data}
                ]
            ]
        }
        self.send(f"⚠️ <b>[안전장치 확인]</b>\n\n{message}", reply_markup=keyboard)

    def send_position_keyboard(self, positions: dict, action_prefix: str, text: str):
        """보유 종목별 2단계 확인 버튼 전송 (비상청산 또는 추가매수 용)"""
        if not positions:
            self.send(f"⚠️ 현재 보유 중인 종목이 없습니다.")
            return

        keyboard = {"inline_keyboard": []}
        for ticker, pos in positions.items():
            name = pos.get("name", ticker)
            keyboard["inline_keyboard"].append([
                {"text": f"{name} ({ticker})", "callback_data": f"{action_prefix}_{ticker}"}
            ])
            
        # 마지막 줄에 취소 버튼 추가
        keyboard["inline_keyboard"].append([
            {"text": "❌ 취소", "callback_data": "cancel_action"}
        ])
        
        self.send(text, reply_markup=keyboard)


    # ──────────────────────────────────────────
    # 매매 알림 포맷
    # ──────────────────────────────────────────
    def notify_buy(self, data: dict):
        """매수 체결 알림"""
        god = " [GOD MODE]" if data.get("god_mode") else ""
        t_info = data.get("track_info", {})
        track_name = t_info.get("name", "")
        sl_pct = t_info.get("sl_pct", 0) * 100
        tp_pct = t_info.get("tp_pct", 0) * 100
        
        # 지정가/시장가 구분
        order_type_str = "시장가 진입" if t_info.get("order_type") == "market" else "지정가 대기"
        
        msg = (
            f"🟢 <b>매수 주문 전송 ({order_type_str}){god}</b>\n"
            f"━━━━━━━━━━━━━\n"
            f"종목: {data.get('name', '')} ({data.get('ticker', '?')})\n"
            f"트랙: Track {data.get('track', '?')} ({track_name})\n"
            f"수량: {data.get('quantity', 0):,}주\n"
            f"진입가: {data.get('price', 0):,}원\n"
            f"목표 익절: +{tp_pct:.1f}%\n"
            f"예상 손절: -{sl_pct:.1f}%\n"
            f"━━━━━━━━━━━━━\n"
            f"<b>[AI 진입 근거]</b>\n"
        )
        reason = data.get('reason', '-')
        if len(reason) > 500:
            reason = reason[:497] + "..."
        msg += reason
        self.send(msg)

    def notify_sell(self, data: dict):
        """매도 체결 알림"""
        pnl = data.get("pnl", 0)
        entry_price = data.get("entry_price", 0)
        sell_price = data.get("sell_price", 0)
        
        # 수익률/손실률 계산
        pnl_pct = (sell_price - entry_price) / entry_price * 100 if entry_price > 0 else 0.0
        
        emoji = "🔴" if pnl < 0 else "🟡"
        trigger = data.get("trigger", "")
        trigger_label = {
            "STOP_LOSS": "손절",
            "TAKE_PROFIT": "익절",
            "TIMEOUT": "시간초과",
            "TRAILING_STOP": "트레일링스탑",
            "BREAK_EVEN": "본절보존",
            "MIDDAY_ALL_KILL": "점심수익확정",
            "MIDDAY_LIQUIDATION": "점심정리",
            "EOD_LIQUIDATION": "장마감정리",
        }.get(trigger, data.get("sell_reason", data.get("reason", "")))

        ticker = data.get("ticker", "?")
        name = data.get("name", "")
        
        # 이름이 없거나 티커와 동일할 경우 pykrx로 이름 조회
        if not name or name == ticker:
            try:
                from pykrx import stock
                krx_name = stock.get_market_ticker_name(ticker)
                if krx_name:
                    name = krx_name
            except Exception:
                pass

        stock_label = f"{name} ({ticker})" if name and name != ticker else ticker

        msg = (
            f"{emoji} <b>매도 체결 ({trigger_label})</b>\n"
            f"━━━━━━━━━━━━━\n"
            f"종목: {stock_label}\n"
            f"트랙: Track {data.get('track', '?')}\n"
            f"수량: {data.get('quantity', 0):,}주\n"
            f"진입가: {entry_price:,}원\n"
            f"매도가: {sell_price:,}원\n"
            f"손익: {pnl:+,.0f}원 ({pnl_pct:+.2f}%)\n"
            f"━━━━━━━━━━━━━"
        )
        self.send(msg)

    def notify_track_change(self, ticker: str, old_track: str, new_track: str, reason: str):
        """AI 트랙 전환 보고"""
        msg = (
            f"🔄 <b>트랙 전환</b>\n"
            f"━━━━━━━━━━━━━\n"
            f"종목: {ticker}\n"
            f"변경: Track {old_track} -> Track {new_track}\n"
            f"사유: {reason}"
        )
        self.send(msg)

    def notify_status(self, status: dict):
        """포트폴리오 상태 보고"""
        details = status.get("details", [])
        pos_lines = []
        for d in details:
            emoji = "📈" if d["change_pct"] >= 0 else "📉"
            pos_lines.append(
                f"  {emoji} {d['name']}({d['ticker']}) "
                f"Track {d['track']} | {d['change_pct']:+.2f}% "
                f"({d['pnl']:+,.0f}원)"
            )

        positions_text = "\n".join(pos_lines) if pos_lines else "  (보유 종목 없음)"
        kill_status = "🔴 정지" if status.get("kill_switch") else "🟢 가동"
        pending_count = status.get("pending_count", 0)
        pending_str = f"\n체결대기: {pending_count}개 종목" if pending_count > 0 else ""

        total_capital = status.get("total_capital", 0)
        cumulative_pnl = status.get("cumulative_pnl", 0)

        msg = (
            f"📊 <b>쌍봉봇 상태 보고</b>\n"
            f"━━━━━━━━━━━━━\n"
            f"시스템: {kill_status}\n"
            f"총 시드: {total_capital:,.0f}원\n"
            f"보유: {status.get('total_positions', 0)}개 종목{pending_str}\n"
            f"평가액: {status.get('total_value', 0):,.0f}원\n"
            f"평가손익: {status.get('total_pnl', 0):+,.0f}원\n"
            f"당일실현: {status.get('daily_pnl', 0):+,.0f}원\n"
            f"누적수익: {cumulative_pnl:+,.0f}원\n"
            f"━━━━━━━━━━━━━\n"
            f"<b>[보유 종목]</b>\n"
            f"{positions_text}"
        )
        self.send(msg)

    def notify_system(self, message: str):
        """시스템 알림"""
        self.send(f"⚙️ <b>시스템</b>: {message}")

    # ──────────────────────────────────────────
    # 명령어 수신 (폴링 및 콜백)
    # ──────────────────────────────────────────
    def register_command(self, command: str, handler: Callable):
        """명령어 및 콜백 핸들러 등록"""
        self._command_handlers[command] = handler

    def poll_commands(self) -> list:
        """새 명령어 및 버튼 클릭(Callback Query) 확인 (non-blocking)"""
        if not self.token:
            return []

        try:
            resp = requests.get(
                f"{self.base_url}/getUpdates",
                params={"offset": self._last_update_id + 1, "timeout": 1},
                timeout=5,
            )
            if resp.status_code != 200:
                return []

            updates = resp.json().get("result", [])
            actions = []

            for update in updates:
                self._last_update_id = update["update_id"]
                
                # 1) 일반 메시지 처리
                if "message" in update:
                    msg = update["message"]
                    text = msg.get("text", "").strip()
                    chat_id = str(msg.get("chat", {}).get("id", ""))

                    if chat_id != self.chat_id:
                        continue

                    # 슬래시가 있거나, 키보드 텍스트 자체를 명령어로 인식
                    if text.startswith("/"):
                        parts = text.split()
                        cmd = parts[0].replace("/", "")
                        actions.append(cmd)
                        if cmd in self._command_handlers:
                            try:
                                if len(parts) > 1:
                                    param = parts[1]
                                    self._command_handlers[cmd](param)
                                else:
                                    self._command_handlers[cmd]()
                            except TypeError:
                                # 파라미터 개수가 안 맞는 경우의 안전장치
                                try:
                                    self._command_handlers[cmd]()
                                except Exception as e2:
                                    logger.error(f"[TG] 명령어 파라미터 매칭 에러 ({cmd}): {e2}")
                            except Exception as e:
                                logger.error(f"[TG] 명령어 처리 에러 ({cmd}): {e}")
                        else:
                            self.send("⚠️ 알 수 없는 명령어입니다. 하단 메뉴를 이용해주세요.")
                    elif text:
                        # 일반 텍스트 입력 시 안내 메시지
                        self.send("🤖 저는 정해진 명령어와 하단 메뉴 버튼으로만 동작합니다.\n자연어 대화는 지원하지 않으니 하단 메뉴를 이용해주세요!")

                
                # 2) 버튼 클릭 (Callback Query) 처리
                elif "callback_query" in update:
                    cb = update["callback_query"]
                    cb_data = cb.get("data", "")
                    chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                    msg_id = cb.get("message", {}).get("message_id")

                    if chat_id != self.chat_id:
                        continue
                    
                    # 로딩 상태 응답
                    requests.post(f"{self.base_url}/answerCallbackQuery", json={"callback_query_id": cb.get("id")}, timeout=5)

                    if cb_data == "cancel_action":
                        # 취소 버튼 누름: 메시지 수정으로 키보드 날리기
                        requests.post(
                            f"{self.base_url}/editMessageText",
                            json={"chat_id": chat_id, "message_id": msg_id, "text": "✅ 작업이 취소되었습니다."},
                            timeout=5
                        )
                        continue

                    # 버튼 클릭 시 원본 메시지의 키보드를 제거하여 중복 클릭 방지
                    requests.post(
                        f"{self.base_url}/editMessageReplyMarkup",
                        json={"chat_id": chat_id, "message_id": msg_id, "reply_markup": {"inline_keyboard": []}},
                        timeout=5
                    )

                    logger.info(f"[TG] 콜백 버튼 클릭: {cb_data}")
                    actions.append(cb_data)
                    
                    # 핸들러 찾기 (파라미터가 있는 경우 처리: ex. sell_005930)
                    for registered_cmd, handler in self._command_handlers.items():
                        if cb_data == registered_cmd or cb_data.startswith(f"{registered_cmd}_"):
                            try:
                                # 데이터 전달이 필요한 경우 대비
                                if "_" in cb_data and cb_data.startswith(f"{registered_cmd}_"):
                                    param = cb_data[len(registered_cmd) + 1:]
                                    handler(param)
                                else:
                                    handler()
                            except TypeError:
                                # 파라미터 안받는 함수일경우
                                handler()
                            except Exception as e:
                                logger.error(f"[TG] 콜백 처리 에러 ({cb_data}): {e}")
                            break

            return actions

        except Exception as e:
            logger.error(f"[TG] 폴링 에러: {e}")
            return []
