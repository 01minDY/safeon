"""SafeON 관제 서버 — FastAPI

실행: uvicorn main:app --host 0.0.0.0 --port 8000
대시보드: http://localhost:8000/
태그(ESP32/시뮬레이터) 보고: POST /api/report
"""
import asyncio
import json
import time
from datetime import date

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import config
import db
import risk_engine
from risk_engine import PairTracker

app = FastAPI(title="SafeON 관제 서버")

conn = db.get_conn()
trackers: dict[tuple[str, str], PairTracker] = {}   # (equip_id, worker_id) -> tracker
equip_states: dict[str, str] = {}                    # equip_id -> 마지막 상태
ws_clients: set[WebSocket] = set()


class Report(BaseModel):
    equip_id: str
    worker_id: str
    distance_cm: float = Field(ge=0)
    equip_state: str = "idle"       # idle | forward | reverse | working
    rssi: int | None = None
    battery: int | None = None
    seq: int | None = None


async def broadcast(payload: dict):
    dead = []
    msg = json.dumps(payload, ensure_ascii=False)
    for ws in ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.discard(ws)


@app.post("/api/report")
async def report(r: Report):
    key = (r.equip_id, r.worker_id)
    tracker = trackers.setdefault(key, PairTracker())
    equip_states[r.equip_id] = r.equip_state

    filtered, level, speed, changed = tracker.update(r.distance_cm, r.equip_state)

    # Near-miss 기록: 경고 이상 등급으로 '진입'하는 순간만 (디바운스)
    if changed and level >= config.NEARMISS_MIN_LEVEL:
        db.insert_event(conn, r.equip_id, r.worker_id, filtered, r.equip_state, level, speed)

    await broadcast({
        "type": "live",
        "equip_id": r.equip_id,
        "worker_id": r.worker_id,
        "distance_cm": round(filtered, 1),
        "raw_distance_cm": r.distance_cm,
        "equip_state": r.equip_state,
        "risk_level": level,
        "risk_label": config.RISK_LABELS[level],
        "speed_cms": round(speed, 1),
        "battery": r.battery,
        "ts": time.time(),
        "event_recorded": changed and level >= config.NEARMISS_MIN_LEVEL,
    })

    return {"risk_level": level, "alert": risk_engine.alert_for(level)}


@app.get("/api/events")
def events(date_str: str | None = None, min_level: int | None = None,
           equip_id: str | None = None, limit: int = 200):
    return db.query_events(conn, date_str, min_level, equip_id, limit)


@app.get("/api/stats/daily")
def stats(date_str: str | None = None):
    return db.daily_stats(conn, date_str)


@app.get("/api/report/daily")
def daily_report(date_str: str | None = None):
    s = db.daily_stats(conn, date_str)
    worst_equip = s["by_equip"][0]["equip_id"] if s["by_equip"] else None
    worst_hour = max(s["by_hour"], key=s["by_hour"].get) if s["by_hour"] else None
    danger = s["by_level"].get(3, 0)
    warning = s["by_level"].get(2, 0)
    summary = (
        f"{s['date']} 총 Near-miss {s['total']}건 (경고 {warning}건, 위험 {danger}건). "
        + (f"최다 발생 장비는 {worst_equip}, " if worst_equip else "")
        + (f"집중 시간대는 {worst_hour}시입니다. " if worst_hour else "")
        + ("위험 등급 이벤트가 발생했으므로 해당 구역 작업 동선 점검을 권장합니다."
           if danger else "위험 등급 이벤트는 없었습니다.")
    )
    return {**s, "summary": summary}


@app.get("/api/status")
def status():
    """현재 태그별 상태 (오프라인 감지 포함)."""
    now = time.time()
    out = []
    for (equip_id, worker_id), t in trackers.items():
        out.append({
            "equip_id": equip_id, "worker_id": worker_id,
            "risk_level": t.level, "risk_label": config.RISK_LABELS[t.level],
            "online": (now - t.last_seen) < config.OFFLINE_TIMEOUT,
            "dwell_sec": round(t.dwell_seconds(now), 1),
        })
    return out


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # keepalive용, 내용 무시
    except WebSocketDisconnect:
        ws_clients.discard(ws)


@app.get("/")
def index():
    return FileResponse("static/dashboard.html")
