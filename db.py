"""SQLite persistence for SafeON incidents, devices and sensor observations."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

import config


_DB_LOCK = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    start_ts TEXT NOT NULL,
    end_ts TEXT,
    worker_id TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    min_distance_m REAL NOT NULL,
    last_distance_m REAL NOT NULL,
    exposure_seconds REAL NOT NULL DEFAULT 0,
    sample_count INTEGER NOT NULL DEFAULT 1,
    online_rate REAL NOT NULL DEFAULT 100,
    near_miss INTEGER NOT NULL DEFAULT 1,
    action_status TEXT NOT NULL DEFAULT 'OPEN',
    recommendation TEXT NOT NULL DEFAULT '',
    created_ts TEXT NOT NULL,
    updated_ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_start ON incidents(start_ts);
CREATE INDEX IF NOT EXISTS idx_incidents_pair ON incidents(equipment_id, worker_id);
CREATE INDEX IF NOT EXISTS idx_incidents_action ON incidents(action_status);

CREATE TABLE IF NOT EXISTS env_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    temperature_c REAL NOT NULL,
    humidity_pct REAL NOT NULL,
    apparent_temperature_c REAL NOT NULL,
    heat_level TEXT NOT NULL,
    sensor_status TEXT NOT NULL,
    guidance TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_env_timestamp ON env_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_env_equipment ON env_logs(equipment_id);

CREATE TABLE IF NOT EXISTS camera_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    person_detected INTEGER NOT NULL,
    confidence REAL NOT NULL,
    camera_status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_camera_timestamp ON camera_observations(timestamp);

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT NOT NULL,
    device_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ONLINE',
    reported_status TEXT NOT NULL DEFAULT 'NORMAL',
    last_seen TEXT NOT NULL,
    last_seen_epoch REAL NOT NULL,
    first_seen_epoch REAL NOT NULL,
    expected_interval_sec REAL NOT NULL,
    mqtt_connected INTEGER NOT NULL DEFAULT 1,
    battery_pct REAL,
    sensor_error_code TEXT,
    firmware_version TEXT,
    message_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (device_id, device_type)
);
CREATE INDEX IF NOT EXISTS idx_devices_status ON devices(status);

CREATE TABLE IF NOT EXISTS improvement_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL UNIQUE,
    event_id TEXT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL DEFAULT 'MEDIUM',
    assignee TEXT,
    due_date TEXT,
    status TEXT NOT NULL DEFAULT 'OPEN',
    created_ts TEXT NOT NULL,
    updated_ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON improvement_actions(status);
"""


def get_conn(path: str = config.DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    with _DB_LOCK:
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _as_iso(value=None) -> str:
    if value is None:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    if isinstance(value, datetime):
        return value.astimezone().isoformat(timespec="seconds")
    return str(value)


def _as_epoch(value=None) -> float:
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        return value.timestamp()
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def _row(row):
    return dict(row) if row is not None else None


def _event_id(conn, timestamp: str) -> str:
    day = timestamp[:10].replace("-", "")
    prefix = f"EVT-{day}-"
    row = conn.execute(
        "SELECT event_id FROM incidents WHERE event_id LIKE ? "
        "ORDER BY event_id DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    number = int(row["event_id"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}{number:04d}"


def _action_id(event_id: str) -> str:
    return "ACT-" + event_id.removeprefix("EVT-")


def _upsert_device(
    conn,
    device_id: str,
    device_type: str,
    timestamp,
    expected_interval_sec: float,
    transport: str = "mqtt",
    battery_pct=None,
    sensor_error_code=None,
    firmware_version=None,
    reported_status: str = "NORMAL",
):
    if not device_id:
        return
    ts = _as_iso(timestamp)
    epoch = _as_epoch(timestamp)
    if reported_status == "OFFLINE":
        state = "OFFLINE"
    elif reported_status == "ERROR":
        state = "DEGRADED"
    else:
        state = "ONLINE"
    conn.execute(
        """
        INSERT INTO devices (
            device_id, device_type, status, reported_status, last_seen,
            last_seen_epoch, first_seen_epoch, expected_interval_sec,
            mqtt_connected, battery_pct, sensor_error_code,
            firmware_version, message_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(device_id, device_type) DO UPDATE SET
            status=excluded.status,
            reported_status=excluded.reported_status,
            last_seen=excluded.last_seen,
            last_seen_epoch=excluded.last_seen_epoch,
            expected_interval_sec=MIN(
                devices.expected_interval_sec,
                excluded.expected_interval_sec
            ),
            mqtt_connected=excluded.mqtt_connected,
            battery_pct=COALESCE(excluded.battery_pct, devices.battery_pct),
            sensor_error_code=excluded.sensor_error_code,
            firmware_version=COALESCE(
                excluded.firmware_version,
                devices.firmware_version
            ),
            message_count=devices.message_count + 1
        """,
        (
            device_id,
            device_type,
            state,
            reported_status,
            ts,
            epoch,
            epoch,
            expected_interval_sec,
            1 if transport == "mqtt" else 0,
            battery_pct,
            sensor_error_code,
            firmware_version,
        ),
    )


def record_proximity(conn, reading: dict, transport: str = "mqtt") -> dict:
    """Update device state and open/update/finish a DANGER incident."""
    timestamp = _as_iso(reading.get("timestamp"))
    epoch = _as_epoch(reading.get("timestamp"))
    worker_id = reading["worker_id"]
    equipment_id = reading["equipment_id"]
    distance_m = float(reading["distance_m"])
    level = reading["risk_level"]
    near_miss = bool(reading.get("near_miss") or level == "DANGER")

    with _DB_LOCK:
        try:
            _upsert_device(
                conn,
                worker_id,
                "WORKER",
                reading.get("timestamp"),
                1.0,
                transport,
                reading.get("battery_pct"),
                reading.get("sensor_error_code"),
                reading.get("firmware_version"),
            )
            _upsert_device(
                conn,
                equipment_id,
                "EQUIPMENT",
                reading.get("timestamp"),
                1.0,
                transport,
                reading.get("equipment_battery_pct"),
                None,
                None,
            )
            active = conn.execute(
                """
                SELECT * FROM incidents
                WHERE worker_id=? AND equipment_id=? AND end_ts IS NULL
                ORDER BY id DESC LIMIT 1
                """,
                (worker_id, equipment_id),
            ).fetchone()
            result = {"transition": None, "incident": _row(active)}

            if level == "DANGER":
                if active is None:
                    event_id = _event_id(conn, timestamp)
                    repeated = conn.execute(
                        """
                        SELECT COUNT(*) AS c FROM incidents
                        WHERE worker_id=? AND equipment_id=?
                          AND start_ts >= ?
                        """,
                        (
                            worker_id,
                            equipment_id,
                            (datetime.fromtimestamp(epoch).astimezone()
                             - timedelta(hours=24)).isoformat(timespec="seconds"),
                        ),
                    ).fetchone()["c"]
                    recommendation = (
                        "즉시 작업 동선을 분리하고 운전자·근로자 경보 작동 여부를 "
                        "확인하세요."
                    )
                    if repeated + 1 >= config.REPEAT_INCIDENT_COUNT:
                        recommendation += (
                            " 동일 장비·근로자 조합의 반복 접근이 확인되어 유도자 "
                            "배치와 동선 재설계를 권고합니다."
                        )
                    conn.execute(
                        """
                        INSERT INTO incidents (
                            event_id, start_ts, worker_id, equipment_id,
                            min_distance_m, last_distance_m, near_miss,
                            recommendation, created_ts, updated_ts
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            timestamp,
                            worker_id,
                            equipment_id,
                            distance_m,
                            distance_m,
                            1 if near_miss else 0,
                            recommendation,
                            timestamp,
                            timestamp,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO improvement_actions (
                            action_id, event_id, title, description, priority,
                            status, created_ts, updated_ts
                        ) VALUES (?, ?, ?, ?, 'HIGH', 'OPEN', ?, ?)
                        """,
                        (
                            _action_id(event_id),
                            event_id,
                            "충돌 위험구역 및 경보장치 점검",
                            recommendation,
                            timestamp,
                            timestamp,
                        ),
                    )
                    result["transition"] = "STARTED"
                    result["incident"] = _row(
                        conn.execute(
                            "SELECT * FROM incidents WHERE event_id=?",
                            (event_id,),
                        ).fetchone()
                    )
                else:
                    start_epoch = _as_epoch(active["start_ts"])
                    duration = max(0.0, epoch - start_epoch)
                    samples = active["sample_count"] + 1
                    online_rate = min(100.0, samples / max(1.0, duration + 1) * 100)
                    recommendation = active["recommendation"]
                    if (
                        duration >= config.LONG_EXPOSURE_SEC
                        and "장시간" not in recommendation
                    ):
                        recommendation += (
                            " 장시간 위험 노출이 확인되어 해당 작업을 중지하고 "
                            "현장 관리자가 즉시 점검하세요."
                        )
                    conn.execute(
                        """
                        UPDATE incidents
                        SET min_distance_m=MIN(min_distance_m, ?),
                            last_distance_m=?, exposure_seconds=?,
                            sample_count=?, online_rate=?, near_miss=?,
                            recommendation=?, updated_ts=?
                        WHERE id=?
                        """,
                        (
                            distance_m,
                            distance_m,
                            round(duration, 1),
                            samples,
                            round(online_rate, 1),
                            1 if near_miss else active["near_miss"],
                            recommendation,
                            timestamp,
                            active["id"],
                        ),
                    )
                    result["transition"] = "UPDATED"
                    result["incident"] = _row(
                        conn.execute(
                            "SELECT * FROM incidents WHERE id=?",
                            (active["id"],),
                        ).fetchone()
                    )
            elif active is not None:
                start_epoch = _as_epoch(active["start_ts"])
                duration = max(0.0, epoch - start_epoch)
                samples = active["sample_count"]
                online_rate = min(100.0, samples / max(1.0, duration + 1) * 100)
                recommendation = active["recommendation"]
                if level == "OFFLINE":
                    recommendation += (
                        " 위험상태 이탈 확인 전에 통신이 중단되어 장치와 작업자 "
                        "상태를 직접 확인하세요."
                    )
                conn.execute(
                    """
                    UPDATE incidents
                    SET end_ts=?, exposure_seconds=?, online_rate=?,
                        recommendation=?, updated_ts=?
                    WHERE id=?
                    """,
                    (
                        timestamp,
                        round(duration, 1),
                        round(online_rate, 1),
                        recommendation,
                        timestamp,
                        active["id"],
                    ),
                )
                result["transition"] = "ENDED"
                result["incident"] = _row(
                    conn.execute(
                        "SELECT * FROM incidents WHERE id=?",
                        (active["id"],),
                    ).fetchone()
                )

            conn.commit()
            return result
        except sqlite3.Error:
            conn.rollback()
            raise


def record_environment(conn, reading: dict, transport: str = "mqtt") -> dict:
    with _DB_LOCK:
        try:
            _upsert_device(
                conn,
                reading["equipment_id"],
                "EQUIPMENT",
                reading.get("timestamp"),
                config.ENV_INTERVAL_SEC,
                transport,
                sensor_error_code=reading.get("sensor_error_code"),
                firmware_version=reading.get("firmware_version"),
                reported_status=reading["sensor_status"],
            )
            conn.execute(
                """
                INSERT INTO env_logs (
                    timestamp, equipment_id, temperature_c, humidity_pct,
                    apparent_temperature_c, heat_level, sensor_status, guidance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _as_iso(reading.get("timestamp")),
                    reading["equipment_id"],
                    reading["temperature_c"],
                    reading["humidity_pct"],
                    reading["apparent_temperature_c"],
                    reading["heat_level"],
                    reading["sensor_status"],
                    reading["guidance"],
                ),
            )
            conn.commit()
            return reading
        except sqlite3.Error:
            conn.rollback()
            raise


def record_camera(conn, reading: dict, transport: str = "mqtt") -> dict:
    reported = {
        "ONLINE": "NORMAL",
        "ERROR": "ERROR",
        "OFFLINE": "OFFLINE",
    }[reading["camera_status"]]
    with _DB_LOCK:
        try:
            _upsert_device(
                conn,
                f"CAM-{reading['equipment_id']}",
                "CAMERA",
                reading.get("timestamp"),
                1.0,
                transport,
                sensor_error_code=reading.get("sensor_error_code"),
                firmware_version=reading.get("firmware_version"),
                reported_status=reported,
            )
            conn.execute(
                """
                INSERT INTO camera_observations (
                    timestamp, equipment_id, person_detected, confidence,
                    camera_status
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    _as_iso(reading.get("timestamp")),
                    reading["equipment_id"],
                    1 if reading["person_detected"] else 0,
                    reading["confidence"],
                    reading["camera_status"],
                ),
            )
            conn.commit()
            return reading
        except sqlite3.Error:
            conn.rollback()
            raise


def evaluate_device_health(conn, now: float | None = None) -> list[dict]:
    now = now if now is not None else time.time()
    changed = []
    with _DB_LOCK:
        rows = conn.execute("SELECT * FROM devices").fetchall()
        for row in rows:
            age = max(0.0, now - row["last_seen_epoch"])
            expected = row["expected_interval_sec"]
            if row["reported_status"] == "OFFLINE":
                status = "OFFLINE"
            elif row["reported_status"] == "ERROR":
                status = "DEGRADED"
            elif row["device_type"] == "CAMERA":
                status = (
                    "OFFLINE"
                    if age >= config.CAMERA_OFFLINE_SEC
                    else "DEGRADED"
                    if age >= config.CAMERA_DEGRADED_SEC
                    else "ONLINE"
                )
            elif expected >= config.ENV_INTERVAL_SEC:
                status = (
                    "OFFLINE"
                    if age >= expected * config.ENV_OFFLINE_MISSES
                    else "DEGRADED"
                    if age >= expected
                    else "ONLINE"
                )
            else:
                status = (
                    "OFFLINE"
                    if age >= config.PROXIMITY_OFFLINE_SEC
                    else "DEGRADED"
                    if age >= config.PROXIMITY_DEGRADED_SEC
                    else "ONLINE"
                )
            if status != row["status"]:
                conn.execute(
                    """
                    UPDATE devices
                    SET status=?, mqtt_connected=?
                    WHERE device_id=? AND device_type=?
                    """,
                    (
                        status,
                        0 if status == "OFFLINE" else row["mqtt_connected"],
                        row["device_id"],
                        row["device_type"],
                    ),
                )
                changed.append(
                    {
                        "device_id": row["device_id"],
                        "device_type": row["device_type"],
                        "status": status,
                        "age_seconds": round(age, 1),
                    }
                )
        conn.commit()
    return changed


def close_stale_incidents(conn, now: float | None = None) -> list[dict]:
    now = now if now is not None else time.time()
    closed = []
    with _DB_LOCK:
        rows = conn.execute(
            """
            SELECT i.*, d.last_seen, d.last_seen_epoch
            FROM incidents i
            LEFT JOIN devices d
              ON d.device_id=i.worker_id AND d.device_type='WORKER'
            WHERE i.end_ts IS NULL
            """
        ).fetchall()
        for row in rows:
            if row["last_seen_epoch"] is None:
                continue
            if now - row["last_seen_epoch"] < config.PROXIMITY_OFFLINE_SEC:
                continue
            duration = max(0.0, row["last_seen_epoch"] - _as_epoch(row["start_ts"]))
            recommendation = row["recommendation"]
            if "통신" not in recommendation:
                recommendation += (
                    " 위험상태에서 통신이 두절되어 작업자·장비 상태를 직접 "
                    "확인하고 통신장치를 점검하세요."
                )
            conn.execute(
                """
                UPDATE incidents
                SET end_ts=?, exposure_seconds=?, recommendation=?, updated_ts=?
                WHERE id=?
                """,
                (
                    row["last_seen"],
                    round(duration, 1),
                    recommendation,
                    row["last_seen"],
                    row["id"],
                ),
            )
            closed.append({"event_id": row["event_id"], "transition": "ENDED"})
        conn.commit()
    return closed


def _device_dict(row, now=None):
    now = now if now is not None else time.time()
    item = dict(row)
    elapsed = max(
        item["expected_interval_sec"],
        now - item["first_seen_epoch"] + item["expected_interval_sec"],
    )
    expected_samples = max(1.0, elapsed / item["expected_interval_sec"])
    item["online_rate"] = round(
        min(100.0, item["message_count"] / expected_samples * 100),
        1,
    )
    item["age_seconds"] = round(max(0.0, now - item["last_seen_epoch"]), 1)
    item["mqtt_connected"] = bool(item["mqtt_connected"])
    return item


def list_devices(conn) -> list[dict]:
    evaluate_device_health(conn)
    with _DB_LOCK:
        rows = conn.execute(
            "SELECT * FROM devices ORDER BY device_type, device_id"
        ).fetchall()
        return [_device_dict(row) for row in rows]


def list_incidents(
    conn,
    date_str=None,
    action_status=None,
    equipment_id=None,
    worker_id=None,
    active_only=False,
    limit=200,
) -> list[dict]:
    query = "SELECT * FROM incidents WHERE 1=1"
    args = []
    if date_str:
        query += " AND start_ts LIKE ?"
        args.append(f"{date_str}%")
    if action_status:
        query += " AND action_status=?"
        args.append(action_status)
    if equipment_id:
        query += " AND equipment_id=?"
        args.append(equipment_id)
    if worker_id:
        query += " AND worker_id=?"
        args.append(worker_id)
    if active_only:
        query += " AND end_ts IS NULL"
    query += " ORDER BY start_ts DESC, id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 1000)))
    with _DB_LOCK:
        return [dict(r) for r in conn.execute(query, args).fetchall()]


def update_incident_action(conn, event_id: str, status: str) -> dict | None:
    if status not in config.ACTION_STATES:
        raise ValueError("지원하지 않는 조치상태입니다.")
    now = _as_iso()
    with _DB_LOCK:
        conn.execute(
            "UPDATE incidents SET action_status=?, updated_ts=? WHERE event_id=?",
            (status, now, event_id),
        )
        if status == "CLOSED":
            conn.execute(
                """
                UPDATE improvement_actions
                SET status='CLOSED', updated_ts=?
                WHERE event_id=?
                """,
                (now, event_id),
            )
        elif status == "ACK":
            conn.execute(
                """
                UPDATE improvement_actions
                SET status='IN_PROGRESS', updated_ts=?
                WHERE event_id=? AND status='OPEN'
                """,
                (now, event_id),
            )
        conn.commit()
        return _row(
            conn.execute(
                "SELECT * FROM incidents WHERE event_id=?", (event_id,)
            ).fetchone()
        )


def latest_environment(conn) -> list[dict]:
    with _DB_LOCK:
        rows = conn.execute(
            """
            SELECT e.* FROM env_logs e
            JOIN (
                SELECT equipment_id, MAX(id) AS max_id
                FROM env_logs GROUP BY equipment_id
            ) latest ON latest.max_id=e.id
            ORDER BY e.equipment_id
            """
        ).fetchall()
        return [dict(r) for r in rows]


def latest_cameras(conn) -> list[dict]:
    with _DB_LOCK:
        rows = conn.execute(
            """
            SELECT c.* FROM camera_observations c
            JOIN (
                SELECT equipment_id, MAX(id) AS max_id
                FROM camera_observations GROUP BY equipment_id
            ) latest ON latest.max_id=c.id
            ORDER BY c.equipment_id
            """
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["person_detected"] = bool(item["person_detected"])
            out.append(item)
        return out


def report(conn, start_date: str, end_date: str) -> dict:
    with _DB_LOCK:
        incidents = conn.execute(
            """
            SELECT * FROM incidents
            WHERE substr(start_ts, 1, 10) BETWEEN ? AND ?
            ORDER BY start_ts
            """,
            (start_date, end_date),
        ).fetchall()
        env = conn.execute(
            """
            SELECT * FROM env_logs
            WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
            ORDER BY timestamp
            """,
            (start_date, end_date),
        ).fetchall()
    incident_rows = [dict(r) for r in incidents]
    env_rows = [dict(r) for r in env]
    by_day = {}
    by_hour = {}
    for item in incident_rows:
        day = item["start_ts"][:10]
        hour = item["start_ts"][11:13]
        by_day[day] = by_day.get(day, 0) + 1
        by_hour[hour] = by_hour.get(hour, 0) + 1
    devices = list_devices(conn)
    closed_count = sum(i["action_status"] == "CLOSED" for i in incident_rows)
    return {
        "start": start_date,
        "end": end_date,
        "total_incidents": len(incident_rows),
        "active_incidents": sum(i["end_ts"] is None for i in incident_rows),
        "total_exposure_seconds": round(
            sum(i["exposure_seconds"] for i in incident_rows), 1
        ),
        "minimum_distance_m": (
            min(i["min_distance_m"] for i in incident_rows)
            if incident_rows
            else None
        ),
        "action_close_rate": round(
            closed_count / len(incident_rows) * 100, 1
        )
        if incident_rows
        else 100.0,
        "device_online_rate": round(
            sum(d["online_rate"] for d in devices) / len(devices), 1
        )
        if devices
        else 100.0,
        "max_apparent_temperature_c": (
            max(e["apparent_temperature_c"] for e in env_rows)
            if env_rows
            else None
        ),
        "by_day": by_day,
        "by_hour": by_hour,
        "incidents": list(reversed(incident_rows[-100:])),
    }


def create_improvement_action(conn, payload: dict) -> dict:
    now = _as_iso()
    with _DB_LOCK:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM improvement_actions"
        ).fetchone()["c"]
        action_id = f"ACT-MANUAL-{count + 1:04d}"
        conn.execute(
            """
            INSERT INTO improvement_actions (
                action_id, event_id, title, description, priority, assignee,
                due_date, status, created_ts, updated_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
            """,
            (
                action_id,
                payload.get("event_id"),
                payload["title"],
                payload.get("description", ""),
                payload.get("priority", "MEDIUM"),
                payload.get("assignee"),
                payload.get("due_date"),
                now,
                now,
            ),
        )
        conn.commit()
        return _row(
            conn.execute(
                "SELECT * FROM improvement_actions WHERE action_id=?",
                (action_id,),
            ).fetchone()
        )


def list_improvement_actions(conn, status=None, limit=200) -> list[dict]:
    query = "SELECT * FROM improvement_actions"
    args = []
    if status:
        query += " WHERE status=?"
        args.append(status)
    query += " ORDER BY created_ts DESC, id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 1000)))
    with _DB_LOCK:
        return [dict(r) for r in conn.execute(query, args).fetchall()]


def update_improvement_action(conn, action_id: str, payload: dict) -> dict | None:
    allowed = {
        key: value
        for key, value in payload.items()
        if key in {"status", "assignee", "due_date"} and value is not None
    }
    if not allowed:
        with _DB_LOCK:
            return _row(
                conn.execute(
                    "SELECT * FROM improvement_actions WHERE action_id=?",
                    (action_id,),
                ).fetchone()
            )
    fields = ", ".join(f"{key}=?" for key in allowed)
    args = list(allowed.values()) + [_as_iso(), action_id]
    with _DB_LOCK:
        conn.execute(
            f"UPDATE improvement_actions SET {fields}, updated_ts=? "
            "WHERE action_id=?",
            args,
        )
        conn.commit()
        return _row(
            conn.execute(
                "SELECT * FROM improvement_actions WHERE action_id=?",
                (action_id,),
            ).fetchone()
        )


def daily_report(conn, date_str=None) -> dict:
    selected = date_str or date.today().isoformat()
    return report(conn, selected, selected)


def weekly_report(conn, end_date=None) -> dict:
    end = date.fromisoformat(end_date) if end_date else date.today()
    start = end - timedelta(days=6)
    return report(conn, start.isoformat(), end.isoformat())
