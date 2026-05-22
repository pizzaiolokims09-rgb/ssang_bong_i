import json
import os
from datetime import datetime

positions_file = "data/positions.json"
os.makedirs("data", exist_ok=True)

# 복구할 하드코딩 데이터
positions = {
    "003280": {
        "name": "흥아해운",
        "entry_price": 3010,
        "quantity": 1672,
        "track": "B",
        "track_info": {"name": "눌림목 단기 스윙", "order_type": "limit"},
        "entry_time": datetime.now().isoformat(),
        "reason": "데이터 복구",
        "sl_pct": 0.03,
        "tp_pct": 0.09,
        "god_mode": False
    },
    "024060": {
        "name": "흥구석유",
        "entry_price": 16400,
        "quantity": 307,
        "track": "B",
        "track_info": {"name": "눌림목 단기 스윙", "order_type": "limit"},
        "entry_time": datetime.now().isoformat(),
        "reason": "데이터 복구",
        "sl_pct": 0.025,
        "tp_pct": 0.08,
        "god_mode": False
    },
    "007610": {
        "name": "선도전기",
        "entry_price": 15800,
        "quantity": 455,
        "track": "A",
        "track_info": {"name": "데이트레이딩 & 상한가 따라잡기", "order_type": "market"},
        "entry_time": datetime.now().isoformat(),
        "reason": "데이터 복구",
        "sl_pct": 0.03,
        "tp_pct": 0.12,
        "god_mode": False
    }
}

with open(positions_file, "w", encoding="utf-8") as f:
    json.dump(positions, f, ensure_ascii=False, indent=2)

print("포지션 데이터 복구 완료!")
