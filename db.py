"""SQLite 저장소 — Near-miss/낙상/환경 이벤트 + 온습도 로그."""

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
    event_type TEXT NOT NULL DEFAULT 'nearmiss',
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


def insert_event(
    conn,
    event_type,
    equip_id,
    worker_id,
    distance_cm,
    equip_state,
    risk_level,
    speed_cms=0.0,
    detail="",
    ts=None,
):
    """이벤트 저장. ts 지정 시 배치 데이터의 기존 시각을 사용."""
    with _DB_LOCK:
        try:
            conn.execute(
                """
                INSERT INTO events (
                    ts,
                    event_type,
                    equip_id,
                    worker_id,
                    distance_cm,
                    equip_state,
                    risk_level,
                    speed_cms,
                    detail
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts or datetime.now().isoformat(timespec="seconds"),
                    event_type,
                    equip_id,
                    worker_id,
                    round(distance_cm, 1)
                    if distance_cm is not None
                    else None,
                    equip_state,
                    risk_level,
                    round(speed_cms, 1),
                    detail,
                ),
            )
            conn.commit()

        except sqlite3.Error:
            conn.rollback()
            raise


def insert_env(conn, equip_id, temp_c, humidity_pct, ts=None):
    """온습도 기록 저장."""
    with _DB_LOCK:
        try:
            conn.execute(
                """
                INSERT INTO env_logs (
                    ts,
                    equip_id,
                    temp_c,
                    humidity_pct
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    ts or datetime.now().isoformat(timespec="seconds"),
                    equip_id,
                    temp_c,
                    humidity_pct,
                ),
            )
            conn.commit()

        except sqlite3.Error:
            conn.rollback()
            raise


def latest_env(conn):
    """장비별 최신 온습도."""
    with _DB_LOCK:
        rows = conn.execute(
            """
            SELECT e.*
            FROM env_logs e
            JOIN (
                SELECT equip_id, MAX(id) AS mid
                FROM env_logs
                GROUP BY equip_id
            ) m
            ON e.id = m.mid
            """
        ).fetchall()

        return [dict(row) for row in rows]


def query_events(
    conn,
    date_str=None,
    min_level=None,
    equip_id=None,
    event_type=None,
    limit=200,
):
    """조건에 맞는 이벤트 목록 조회."""
    query = "SELECT * FROM events WHERE 1=1"
    args = []

    if date_str:
        query += " AND ts LIKE ?"
        args.append(f"{date_str}%")

    if min_level is not None:
        query += " AND risk_level >= ?"
        args.append(min_level)

    if equip_id:
        query += " AND equip_id = ?"
        args.append(equip_id)

    if event_type:
        query += " AND event_type = ?"
        args.append(event_type)

    query += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(limit)

    with _DB_LOCK:
        rows = conn.execute(query, args).fetchall()
        return [dict(row) for row in rows]


def _stats_where(dates):
    """날짜별 ts LIKE 조건 생성."""
    where = "(" + " OR ".join("ts LIKE ?" for _ in dates) + ")"
    args = [f"{day}%" for day in dates]
    return where, args


def range_stats(conn, dates: list[str]):
    """지정 날짜 목록의 이벤트 통계."""
    where, args = _stats_where(dates)

    with _DB_LOCK:

        def query(sql, extra=()):
            cursor = conn.execute(
                sql.format(w=where),
                list(args) + list(extra),
            )
            return cursor.fetchall()

        total_rows = query(
            "SELECT COUNT(*) AS c FROM events WHERE {w}"
        )
        total = total_rows[0]["c"]

        by_level = {
            row["risk_level"]: row["c"]
            for row in query(
                """
                SELECT risk_level, COUNT(*) AS c
                FROM events
                WHERE {w}
                  AND event_type = 'nearmiss'
                GROUP BY risk_level
                """
            )
        }

        by_type = {
            row["event_type"]: row["c"]
            for row in query(
                """
                SELECT event_type, COUNT(*) AS c
                FROM events
                WHERE {w}
                GROUP BY event_type
                """
            )
        }

        by_equip = [
            dict(row)
            for row in query(
                """
                SELECT equip_id, COUNT(*) AS c
                FROM events
                WHERE {w}
                  AND equip_id IS NOT NULL
                GROUP BY equip_id
                ORDER BY c DESC
                """
            )
        ]

        by_hour = {
            row["h"]: row["c"]
            for row in query(
                """
                SELECT substr(ts, 12, 2) AS h, COUNT(*) AS c
                FROM events
                WHERE {w}
                GROUP BY h
                """
            )
        }

        by_day = {
            row["d"]: row["c"]
            for row in query(
                """
                SELECT substr(ts, 1, 10) AS d, COUNT(*) AS c
                FROM events
                WHERE {w}
                GROUP BY d
                """
            )
        }

        by_worker = [
            dict(row)
            for row in query(
                """
                SELECT worker_id, COUNT(*) AS c
                FROM events
                WHERE {w}
                  AND worker_id IS NOT NULL
                GROUP BY worker_id
                ORDER BY c DESC
                """
            )
        ]

        return {
            "total": total,
            "by_level": by_level,
            "by_type": by_type,
            "by_equip": by_equip,
            "by_hour": by_hour,
            "by_day": by_day,
            "by_worker": by_worker,
        }


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