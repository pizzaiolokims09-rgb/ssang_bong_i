"""
journal.py - 자동 매매일지 작성 및 AI 최적화 루틴
매일 매매 내역을 SQLite에 저장하고, Gemini AI로 복기 및 고도화를 진행합니다.
"""
import sqlite3
import logging
from datetime import datetime
import json

logger = logging.getLogger("ssangbong.journal")

class TradeJournal:
    def __init__(self, ai_router):
        self.db_path = "data/journal.db"
        self.ai = ai_router
        self._init_db()

    def _init_db(self):
        """SQLite 데이터베이스 초기화"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        # 개별 매매일지 테이블
        c.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                ticker TEXT,
                name TEXT,
                track TEXT,
                reason TEXT,
                entry_price INTEGER,
                sell_price INTEGER,
                quantity INTEGER,
                pnl INTEGER,
                return_pct REAL,
                total_capital_pct REAL,
                ai_review TEXT
            )
        ''')
        
        # 고도화 요약 테이블
        c.execute('''
            CREATE TABLE IF NOT EXISTS optimizations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                summary TEXT,
                max_profit_cond TEXT,
                max_loss_cond TEXT
            )
        ''')
        conn.commit()
        conn.close()

    def record_trade(self, trade_data: dict, total_capital: float):
        """매도 완료 시 매매일지 기록"""
        entry = trade_data["entry_price"]
        sell = trade_data["sell_price"]
        pnl = trade_data["pnl"]
        return_pct = (sell - entry) / entry * 100 if entry > 0 else 0
        total_cap_pct = (pnl / total_capital) * 100 if total_capital > 0 else 0

        # AI 매매 복기 작성
        prompt = f"""
        당신은 프로 데이트레이더입니다. 방금 종료된 매매를 복기해주세요.
        - 종목: {trade_data.get('name', trade_data['ticker'])}
        - 진입 트랙: {trade_data['track']}
        - 진입 이유: {trade_data.get('reason', '알 수 없음')}
        - 매수/매도가: {entry:,}원 -> {sell:,}원
        - 수익률: {return_pct:+.2f}% ({pnl:+,.0f}원)
        - 매도 이유: {trade_data.get('sell_reason', '알 수 없음')}
        
        간단명료하게 3문장 이내로 '이 매매의 패인 또는 승인'과 '다음 매매 시 개선점'을 적어주세요.
        """
        review = self.ai._call_gemini(prompt, use_thinking=False) or "복기 실패"

        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''
            INSERT INTO trades (date, ticker, name, track, reason, entry_price, sell_price, quantity, pnl, return_pct, total_capital_pct, ai_review)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            trade_data['ticker'],
            trade_data.get('name', ''),
            trade_data['track'],
            trade_data.get('reason', ''),
            entry, sell, trade_data['quantity'], pnl, return_pct, total_cap_pct, review
        ))
        conn.commit()
        conn.close()
        
        return review

    def generate_daily_summary(self):
        """당일 매매 요약 리포트 생성"""
        today = datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT name, track, return_pct, pnl, ai_review FROM trades WHERE date LIKE ?", (f"{today}%",))
        trades = c.fetchall()
        conn.close()

        if not trades:
            return "오늘 진행된 매매가 없습니다."

        total_pnl = sum(t[3] for t in trades)
        summary = f"📝 <b>[오늘의 매매일지 요약]</b>\n총 {len(trades)}건 매매 / 실현 손익: {total_pnl:+,.0f}원\n\n"
        
        for name, track, ret, pnl, review in trades:
            emoji = "🟢" if pnl > 0 else "🔴"
            summary += f"{emoji} <b>{name}</b> (Track {track}) : {ret:+.2f}%\n"
            summary += f"💡 복기: {review}\n\n"
            
        return summary

    def get_daily_pnl(self) -> int:
        """당일 실현 손익 총합 (DB 기반)"""
        today = datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT SUM(pnl) FROM trades WHERE date LIKE ?", (f"{today}%",))
        result = c.fetchone()[0]
        conn.close()
        return int(result) if result else 0

    def get_cumulative_pnl(self) -> int:
        """누적 실현 손익 총합 (DB 기반)"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT SUM(pnl) FROM trades")
        result = c.fetchone()[0]
        conn.close()
        return int(result) if result else 0

    def optimize_brain_if_needed(self):
        """
        일지가 30개 쌓이면, 가장 오래된 10개를 분석하여 고도화 요약을 만들고 
        트레이딩 두뇌의 승률을 높이는 규칙을 추출함
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM trades")
        count = c.fetchone()[0]

        if count >= 30:
            logger.info("[Journal] 매매일지 30개 도달! 고도화 분석 루틴 시작")
            c.execute("SELECT * FROM trades ORDER BY id ASC LIMIT 10")
            old_trades = c.fetchall()
            
            # 분석용 데이터 문자열 구성
            trade_data_str = ""
            for t in old_trades:
                trade_data_str += f"- [{t[3]}] Track {t[4]}, 수익: {t[10]:+.2f}%, 이유: {t[5]}, 복기: {t[12]}\n"
                
            prompt = f"""
            당신은 매매 시스템을 최적화하는 AI 코어입니다. 아래는 과거 10개의 매매 내역입니다.
            {trade_data_str}
            
            이 데이터를 분석하여 봇의 승률을 높이기 위한 규칙을 도출하세요.
            반드시 아래 JSON 형식으로 반환하세요.
            {{
                "summary": "과거 10건에 대한 1문장 요약",
                "max_profit_cond": "가장 수익이 좋았던 진입 패턴과 조건",
                "max_loss_cond": "가장 손실이 컸거나 실패했던 패턴과 회피 방법"
            }}
            """
            result_str = self.ai._call_gemini(prompt, use_thinking=True)
            
            if not result_str:
                logger.warning("[Journal] 고도화 분석용 AI 응답 없음 → 다음 기회에 재시도")
                conn.close()
                return None

            try:
                if "```json" in result_str:
                    result_str = result_str.split("```json")[1].split("```")[0]
                elif "```" in result_str:
                    result_str = result_str.split("```")[1].split("```")[0]
                analysis = json.loads(result_str.strip())
                
                # 고도화 DB 저장
                c.execute('''
                    INSERT INTO optimizations (date, summary, max_profit_cond, max_loss_cond)
                    VALUES (?, ?, ?, ?)
                ''', (
                    datetime.now().strftime("%Y-%m-%d"),
                    analysis.get("summary", ""),
                    analysis.get("max_profit_cond", ""),
                    analysis.get("max_loss_cond", "")
                ))
                
                # 분석 끝난 10개는 삭제 (또는 아카이브 테이블로 이동 가능)
                c.execute("DELETE FROM trades WHERE id IN (SELECT id FROM trades ORDER BY id ASC LIMIT 10)")
                conn.commit()
                conn.close()
                
                return f"🧠 <b>[트레이딩 두뇌 고도화 완료]</b>\n\n📌 <b>핵심 요약</b>: {analysis.get('summary')}\n🏆 <b>최고 수익 패턴</b>: {analysis.get('max_profit_cond')}\n⛔ <b>최대 손실 회피</b>: {analysis.get('max_loss_cond')}"

            except Exception as e:
                logger.error(f"[Journal] 분석 JSON 파싱 에러: {e}")
                conn.close()
                return None
        
        conn.close()
        return None
