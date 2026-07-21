"""SQLite 이벤트 저장소."""
import sqlite3
from datetime import datetime, date

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    equip_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    distance_cm REAL NOT NULL,
    equip_state TEXT NOT NULL,
    risk_level INTEGER NOT NULL,
    speed_cms REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


def get_conn(path: str = config.DB_PATH) -> sqlite3.Connection:
    # FastAPI는 멀티스레드로 sync 엔드포인트를 실행하므로 check_same_thread=False 필요
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def insert_event(conn, equip_id, worker_id, distance_cm, equip_state, risk_level, speed_cms=0.0):
    conn.execute(
        "INSERT INTO events (ts, equip_id, worker_id, distance_cm, equip_state, risk_level, speed_cms)"
        " VALUES (?,?,?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), equip_id, worker_id,
         round(distance_cm, 1), equip_state, risk_level, round(speed_cms, 1)),
    )
    conn.commit()


def query_events(conn, date_str=None, min_level=None, equip_id=None, limit=200):
    q = "SELECT * FROM events WHERE 1=1"
    args = []
    if date_str:
        q += " AND ts LIKE ?"
        args.append(f"{date_str}%")
    if min_level is not None:
        q += " AND risk_level >= ?"
        args.append(min_level)
    if equip_id:
        q += " AND equip_id = ?"
        args.append(equip_id)
    q += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def daily_stats(conn, date_str=None):
    d = date_str or date.today().isoformat()
    like = f"{d}%"
    total = conn.execute("SELECT COUNT(*) c FROM events WHERE ts LIKE ?", (like,)).fetchone()["c"]
    by_level = {r["risk_level"]: r["c"] for r in conn.execute(
        "SELECT risk_level, COUNT(*) c FROM events WHERE ts LIKE ? GROUP BY risk_level", (like,))}
    by_equip = [dict(r) for r in conn.execute(
        "SELECT equip_id, COUNT(*) c FROM events WHERE ts LIKE ? GROUP BY equip_id ORDER BY c DESC", (like,))]
    by_hour = {r["h"]: r["c"] for r in conn.execute(
        "SELECT substr(ts,12,2) h, COUNT(*) c FROM events WHERE ts LIKE ? GROUP BY h", (like,))}
    by_worker = [dict(r) for r in conn.execute(
        "SELECT worker_id, COUNT(*) c FROM events WHERE ts LIKE ? GROUP BY worker_id ORDER BY c DESC", (like,))]
    return {"date": d, "total": total, "by_level": by_level,
            "by_equip": by_equip, "by_hour": by_hour, "by_worker": by_worker}
