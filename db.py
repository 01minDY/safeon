"""SQLite 저장소 — 이벤트·사건(Incident)·온습도 로그 (명세서 기준)."""
import sqlite3
import threading
from datetime import datetime, date, timedelta

import config


# FastAPI 요청 스레드와 MQTT 스레드가 같은 연결을 동시에 쓰지 않도록 보호
_DB_LOCK = threading.RLock()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'nearmiss',   -- nearmiss | fall | env | camera
    equipment_id TEXT,
    worker_id TEXT,
    distance_m REAL,
    risk_level TEXT NOT NULL DEFAULT 'SAFE',       -- SAFE | CAUTION | DANGER
    detail TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

-- 아차사고 보고서: DANGER 진입~해제를 하나의 사건으로 관리 (명세서 '관제팀 계산')
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uid TEXT UNIQUE NOT NULL,                -- 예: EVT-20260724-0001
    equipment_id TEXT,
    worker_id TEXT,
    start_ts TEXT NOT NULL,
    end_ts TEXT,
    min_distance_m REAL,
    duration_sec REAL,
    action_status TEXT NOT NULL DEFAULT 'OPEN',    -- OPEN | ACK | CLOSED
    recommendation TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS env_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    temperature_c REAL,
    humidity_pct REAL,
    apparent_c REAL,
    heat_stage TEXT,
    sensor_status TEXT DEFAULT 'NORMAL'
);

CREATE INDEX IF NOT EXISTS idx_env_ts ON env_logs(ts);
"""


def get_conn(path: str = config.DB_PATH) -> sqlite3.Connection:
    """SQLite 연결 생성."""
    conn = sqlite3.connect(
        path,
        check_same_thread=False,
        timeout=10.0,
    )
    conn.row_factory = sqlite3.Row

    with _DB_LOCK:
        conn.executescript(_SCHEMA)

        # 동시 읽기 성능 및 잠금 충돌 완화
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")

    return conn


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ---------- 이벤트 ----------
def insert_event(conn, event_type, equipment_id, worker_id, distance_m,
                 risk_level, detail="", ts=None):
    conn.execute(
        "INSERT INTO events (ts, event_type, equipment_id, worker_id, distance_m,"
        " risk_level, detail) VALUES (?,?,?,?,?,?,?)",
        (ts or _now(), event_type, equipment_id, worker_id,
         round(distance_m, 2) if distance_m is not None else None,
         risk_level, detail))
    conn.commit()


def query_events(conn, date_str=None, risk_level=None, equipment_id=None,
                 event_type=None, limit=200):
    q, args = "SELECT * FROM events WHERE 1=1", []
    if date_str:
        q += " AND ts LIKE ?"; args.append(f"{date_str}%")
    if risk_level:
        q += " AND risk_level = ?"; args.append(risk_level)
    if equipment_id:
        q += " AND equipment_id = ?"; args.append(equipment_id)
    if event_type:
        q += " AND event_type = ?"; args.append(event_type)
    q += " ORDER BY ts DESC, id DESC LIMIT ?"; args.append(limit)
    return [dict(r) for r in conn.execute(q, args).fetchall()]


# ---------- 사건(Incident) ----------
def next_event_uid(conn, ts=None) -> str:
    d = (ts or _now())[:10].replace("-", "")
    n = conn.execute("SELECT COUNT(*) c FROM incidents WHERE event_uid LIKE ?",
                     (f"EVT-{d}-%",)).fetchone()["c"]
    return f"EVT-{d}-{n + 1:04d}"


def open_incident(conn, equipment_id, worker_id, start_ts=None, distance_m=None):
    uid = next_event_uid(conn, start_ts)
    conn.execute(
        "INSERT INTO incidents (event_uid, equipment_id, worker_id, start_ts,"
        " min_distance_m) VALUES (?,?,?,?,?)",
        (uid, equipment_id, worker_id, start_ts or _now(),
         round(distance_m, 2) if distance_m is not None else None))
    conn.commit()
    return uid


def update_incident_distance(conn, event_uid, distance_m):
    conn.execute(
        "UPDATE incidents SET min_distance_m = MIN(COALESCE(min_distance_m, 1e9), ?)"
        " WHERE event_uid = ?", (round(distance_m, 2), event_uid))
    conn.commit()


def close_incident(conn, event_uid, end_ts=None, recommendation=""):
    end_ts = end_ts or _now()
    row = conn.execute("SELECT start_ts FROM incidents WHERE event_uid = ?",
                       (event_uid,)).fetchone()
    dur = None
    if row:
        try:
            dur = (datetime.fromisoformat(end_ts)
                   - datetime.fromisoformat(row["start_ts"])).total_seconds()
        except ValueError:
            pass
    conn.execute(
        "UPDATE incidents SET end_ts = ?, duration_sec = ?, recommendation = ?"
        " WHERE event_uid = ?", (end_ts, dur, recommendation, event_uid))
    conn.commit()
    return dur


def set_incident_status(conn, event_uid, status):
    if status not in ("OPEN", "ACK", "CLOSED"):
        return False
    conn.execute("UPDATE incidents SET action_status = ? WHERE event_uid = ?",
                 (status, event_uid))
    conn.commit()
    return True


def query_incidents(conn, date_str=None, status=None, limit=100):
    q, args = "SELECT * FROM incidents WHERE 1=1", []
    if date_str:
        q += " AND start_ts LIKE ?"; args.append(f"{date_str}%")
    if status:
        q += " AND action_status = ?"; args.append(status)
    q += " ORDER BY start_ts DESC, id DESC LIMIT ?"; args.append(limit)
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def count_recent_incidents(conn, equipment_id, worker_id, since_ts) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM incidents WHERE equipment_id=? AND worker_id=?"
        " AND start_ts >= ?", (equipment_id, worker_id, since_ts)).fetchone()["c"]


# ---------- 온습도 ----------
def insert_env(conn, equipment_id, temperature_c, humidity_pct, apparent_c=None,
               heat_stage=None, sensor_status="NORMAL", ts=None):
    conn.execute(
        "INSERT INTO env_logs (ts, equipment_id, temperature_c, humidity_pct,"
        " apparent_c, heat_stage, sensor_status) VALUES (?,?,?,?,?,?,?)",
        (ts or _now(), equipment_id, temperature_c, humidity_pct,
         round(apparent_c, 1) if apparent_c is not None else None,
         heat_stage, sensor_status))
    conn.commit()


def latest_env(conn):
    rows = conn.execute(
        "SELECT e.* FROM env_logs e JOIN (SELECT equipment_id, MAX(id) mid"
        " FROM env_logs GROUP BY equipment_id) m ON e.id = m.mid").fetchall()
    return [dict(r) for r in rows]


# ---------- 통계 ----------
def range_stats(conn, dates: list[str]):
    where = "(" + " OR ".join("ts LIKE ?" for _ in dates) + ")"
    args = [f"{d}%" for d in dates]

    def q(sql, table="events"):
        return conn.execute(sql.format(w=where, t=table), args).fetchall()

    total = q("SELECT COUNT(*) c FROM {t} WHERE {w}")[0]["c"]
    by_level = {r["risk_level"]: r["c"] for r in q(
        "SELECT risk_level, COUNT(*) c FROM {t} WHERE {w} AND event_type='nearmiss' GROUP BY risk_level")}
    by_type = {r["event_type"]: r["c"] for r in q(
        "SELECT event_type, COUNT(*) c FROM {t} WHERE {w} GROUP BY event_type")}
    by_equip = [dict(r) for r in q(
        "SELECT equipment_id, COUNT(*) c FROM {t} WHERE {w} AND equipment_id IS NOT NULL"
        " GROUP BY equipment_id ORDER BY c DESC")]
    by_hour = {r["h"]: r["c"] for r in q(
        "SELECT substr(ts,12,2) h, COUNT(*) c FROM {t} WHERE {w} GROUP BY h")}
    by_day = {r["d"]: r["c"] for r in q(
        "SELECT substr(ts,1,10) d, COUNT(*) c FROM {t} WHERE {w} GROUP BY d")}
    by_worker = [dict(r) for r in q(
        "SELECT worker_id, COUNT(*) c FROM {t} WHERE {w} AND worker_id IS NOT NULL"
        " GROUP BY worker_id ORDER BY c DESC")]

    inc_where = where.replace("ts LIKE", "start_ts LIKE")
    inc = conn.execute(
        f"SELECT COUNT(*) c, AVG(duration_sec) avg_dur, MIN(min_distance_m) min_d"
        f" FROM incidents WHERE {inc_where}", args).fetchone()
    inc_open = conn.execute(
        f"SELECT COUNT(*) c FROM incidents WHERE {inc_where} AND action_status='OPEN'",
        args).fetchone()["c"]

    return {"total": total, "by_level": by_level, "by_type": by_type,
            "by_equip": by_equip, "by_hour": by_hour, "by_day": by_day,
            "by_worker": by_worker,
            "incidents": {"count": inc["c"], "open": inc_open,
                          "avg_duration_sec": round(inc["avg_dur"], 1) if inc["avg_dur"] else None,
                          "min_distance_m": inc["min_d"]}}


def daily_stats(conn, date_str=None):
    """일일 이벤트 통계."""
    selected_date = date_str or date.today().isoformat()

    return {
        "date": selected_date,
        **range_stats(conn, [selected_date]),
    }


def weekly_stats(conn, end_date=None):
    """최근 7일 이벤트 통계."""
    end = (
        date.fromisoformat(end_date)
        if end_date
        else date.today()
    )

    days = [
        (end - timedelta(days=i)).isoformat()
        for i in range(6, -1, -1)
    ]

    return {
        "start": days[0],
        "end": days[-1],
        "days": days,
        **range_stats(conn, days),
    }