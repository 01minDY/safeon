"""SafeON 관제 서버 — FastAPI + MQTT

실행:
    1) 브로커: mosquitto  (없으면: python broker.py)
    2) 서버:   uvicorn main:app --host 0.0.0.0 --port 8000
    3) 대시보드: http://localhost:8000/

수신 경로:
    - MQTT  safeon/#            (실시간, 1순위)
    - HTTP  POST /api/batch     (오프라인 저장분 일괄 업로드, 백업 플랜)
"""
import asyncio
import json
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

import config
import db
from mqtt_ingest import Ingest

app = FastAPI(title="SafeON 관제 서버")

conn = db.get_conn()
ws_clients: set[WebSocket] = set()
loop_ref: dict = {}


def broadcast_sync(payload: dict):
    """MQTT 스레드(동기)에서 WebSocket(비동기)으로 안전하게 전달."""
    loop = loop_ref.get("loop")
    if loop is None:
        return
    msg = json.dumps(payload, ensure_ascii=False)

    async def _send():
        dead = []
        for ws in ws_clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            ws_clients.discard(ws)

    asyncio.run_coroutine_threadsafe(_send(), loop)


ingest = Ingest(conn, on_update=broadcast_sync)


@app.on_event("startup")
async def startup():
    loop_ref["loop"] = asyncio.get_running_loop()
    try:
        ingest.start()
        print(f"[SafeON] MQTT 연결: {config.MQTT_HOST}:{config.MQTT_PORT} → {config.MQTT_BASE}/#")
    except Exception as e:
        print(f"[SafeON] !! MQTT 브로커 연결 실패: {e}")
        print("[SafeON]    브로커 없이 기동합니다. HTTP /api/batch 백업 경로는 사용 가능.")


# ---------- 백업 플랜: 오프라인 저장분 일괄 업로드 ----------
@app.post("/api/batch")
async def batch(records: list[dict]):
    n = ingest.handle_batch(records)
    return {"stored": n}


# ---------- 조회 API ----------
@app.get("/api/events")
def events(date_str: str | None = None, min_level: int | None = None,
           equip_id: str | None = None, event_type: str | None = None, limit: int = 200):
    return db.query_events(conn, date_str, min_level, equip_id, event_type, limit)


@app.get("/api/env")
def env():
    latest = list(ingest.env_latest.values())
    return latest if latest else db.latest_env(conn)


@app.get("/api/stats/daily")
def stats_daily(date_str: str | None = None):
    return db.daily_stats(conn, date_str)


@app.get("/api/report/daily")
def report_daily(date_str: str | None = None):
    s = db.daily_stats(conn, date_str)
    return {**s, "summary": _summary(s, f"{s['date']}")}


@app.get("/api/report/weekly")
def report_weekly(end_date: str | None = None):
    s = db.weekly_stats(conn, end_date)
    return {**s, "summary": _summary(s, f"{s['start']} ~ {s['end']} 주간")}


def _summary(s: dict, label: str) -> str:
    danger = s["by_level"].get(3, 0)
    warning = s["by_level"].get(2, 0)
    falls = s["by_type"].get("fall", 0)
    envs = s["by_type"].get("env", 0)
    worst_equip = s["by_equip"][0]["equip_id"] if s["by_equip"] else None
    worst_hour = max(s["by_hour"], key=s["by_hour"].get) if s["by_hour"] else None
    parts = [f"{label} 총 이벤트 {s['total']}건 (근접 경고 {warning}·위험 {danger}건"]
    parts.append(f", 낙상 {falls}건" if falls else "")
    parts.append(f", 이상환경 {envs}건" if envs else "")
    parts.append("). ")
    if worst_equip:
        parts.append(f"최다 발생 장비는 {worst_equip}")
        parts.append(f", 집중 시간대는 {worst_hour}시입니다. " if worst_hour else "입니다. ")
    if falls:
        parts.append("낙상 이벤트가 발생했으므로 해당 근로자 안전 확인 및 작업환경 점검이 필요합니다. ")
    if danger:
        parts.append("위험 등급 근접 이벤트가 발생했으므로 작업 동선 분리를 권장합니다.")
    elif s["total"] == 0:
        parts.append("기록된 위험 이벤트가 없습니다.")
    return "".join(parts)


@app.get("/api/status")
def status():
    now = time.time()
    out = []
    for (equip_id, worker_id), t in ingest.trackers.items():
        out.append({
            "equip_id": equip_id, "worker_id": worker_id,
            "risk_level": t.level, "risk_label": config.RISK_LABELS[t.level],
            "online": (now - t.last_seen) < config.OFFLINE_TIMEOUT,
            "dwell_sec": round(t.dwell_seconds(now), 1),
        })
    return out


@app.get("/api/health")
def health():
    return {"mqtt": ingest.stats, "ws_clients": len(ws_clients)}


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # keepalive
    except WebSocketDisconnect:
        ws_clients.discard(ws)


@app.get("/")
def index():
    return FileResponse("static/dashboard.html")
