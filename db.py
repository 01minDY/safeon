"""SQLite 저장소 — Near-miss/낙상/환경 이벤트 + 온습도 로그."""
import sqlite3
from datetime import datetime, date, timedelta

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'nearmiss',   -- nearmiss | fall | env
    equip_id TEXT,
    worker_id TEXT,
    distance_cm REAL,
    equip_state TEXT,
    risk_level INTEGER NOT NULL DEFAULT 0,
    speed_cms REAL DEFAULT 0,
    detail TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS env_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    equip_id TEXT NOT NULL,
    temp_c REAL,
    humidity_pct REAL
);
CREATE INDEX IF NOT EXISTS idx_env_ts ON env_logs(ts);
"""


def get_conn(path: str = config.DB_PATH) -> sqlite3.Connection:
    # FastAPI는 멀티스레드로 sync 엔드포인트를 실행하므로 check_same_thread=False 필요
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def insert_event(conn, event_type, equip_id, worker_id, distance_cm, equip_state,
                 risk_level, speed_cms=0.0, detail="", ts=None):
    """ts를 지정하면 배치 업로드(오프라인 저장분)의 기기 시각으로 기록."""
    conn.execute(
        "INSERT INTO events (ts, event_type, equip_id, worker_id, distance_cm,"
        " equip_state, risk_level, speed_cms, detail) VALUES (?,?,?,?,?,?,?,?,?)",
        (ts or datetime.now().isoformat(timespec="seconds"), event_type,
         equip_id, worker_id,
         round(distance_cm, 1) if distance_cm is not None else None,
         equip_state, risk_level, round(speed_cms, 1), detail),
    )
    conn.commit()


def insert_env(conn, equip_id, temp_c, humidity_pct, ts=None):
    conn.execute(
        "INSERT INTO env_logs (ts, equip_id, temp_c, humidity_pct) VALUES (?,?,?,?)",
        (ts or datetime.now().isoformat(timespec="seconds"), equip_id, temp_c, humidity_pct),
    )
    conn.commit()


def latest_env(conn):
    """장비별 최신 온습도."""
    rows = conn.execute(
        "SELECT e.* FROM env_logs e JOIN (SELECT equip_id, MAX(id) mid FROM env_logs"
        " GROUP BY equip_id) m ON e.id = m.mid").fetchall()
    return [dict(r) for r in rows]


def query_events(conn, date_str=None, min_level=None, equip_id=None,
                 event_type=None, limit=200):
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
    if event_type:
        q += " AND event_type = ?"
        args.append(event_type)
    q += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def _stats_where(like_list):
    """ts LIKE 조건 여러 날짜 OR 결합."""
    return "(" + " OR ".join("ts LIKE ?" for _ in like_list) + ")", [f"{d}%" for d in like_list]


def range_stats(conn, dates: list[str]):
    """지정한 날짜 목록(일일=1개, 주간=7개)의 통계."""
    where, args = _stats_where(dates)
    def q(sql, extra=()):
        return conn.execute(sql.format(w=where), list(args) + list(extra)).fetchall()

    total = q("SELECT COUNT(*) c FROM events WHERE {w}")[0]["c"]
    by_level = {r["risk_level"]: r["c"] for r in q(
        "SELECT risk_level, COUNT(*) c FROM events WHERE {w} AND event_type='nearmiss' GROUP BY risk_level")}
    by_type = {r["event_type"]: r["c"] for r in q(
        "SELECT event_type, COUNT(*) c FROM events WHERE {w} GROUP BY event_type")}
    by_equip = [dict(r) for r in q(
        "SELECT equip_id, COUNT(*) c FROM events WHERE {w} AND equip_id IS NOT NULL GROUP BY equip_id ORDER BY c DESC")]
    by_hour = {r["h"]: r["c"] for r in q(
        "SELECT substr(ts,12,2) h, COUNT(*) c FROM events WHERE {w} GROUP BY h")}
    by_day = {r["d"]: r["c"] for r in q(
        "SELECT substr(ts,1,10) d, COUNT(*) c FROM events WHERE {w} GROUP BY d")}
    by_worker = [dict(r) for r in q(
        "SELECT worker_id, COUNT(*) c FROM events WHERE {w} AND worker_id IS NOT NULL GROUP BY worker_id ORDER BY c DESC")]
    return {"total": total, "by_level": by_level, "by_type": by_type,
            "by_equip": by_equip, "by_hour": by_hour, "by_day": by_day, "by_worker": by_worker}


def daily_stats(conn, date_str=None):
    d = date_str or date.today().isoformat()
    return {"date": d, **range_stats(conn, [d])}


def weekly_stats(conn, end_date=None):
    end = date.fromisoformat(end_date) if end_date else date.today()
    days = [(end - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    return {"start": days[0], "end": days[-1], "days": days, **range_stats(conn, days)}
