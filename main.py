"""SafeON 관제 서버 — FastAPI + MQTT (구현 세부 명세서 기준)

실행:
    1) 브로커: mosquitto -c mosquitto.conf -v   (없으면: python broker.py)
    2) 서버:   uvicorn main:app --host 0.0.0.0 --port 8000
    3) 대시보드: http://localhost:8000/
"""
import asyncio
import json
import time

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

import config
import db
from mqtt_ingest import Ingest

app = FastAPI(title="SafeON 관제 서버")

conn = db.get_conn()
ws_clients: set[WebSocket] = set()
loop_ref: dict = {}


def broadcast_sync(payload: dict):
    """MQTT 스레드(동기) → WebSocket(비동기) 전달."""
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


async def device_watchdog():
    """명세서: 미수신 시간 기반 DEGRADED/OFFLINE 자체 판정 (1초 주기)."""
    while True:
        await asyncio.sleep(1.0)
        if ingest.refresh_device_status():
            broadcast_sync({"type": "devices", "devices": ingest.device_list(),
                            "ts": time.time()})


@app.on_event("startup")
async def startup():
    loop_ref["loop"] = asyncio.get_running_loop()
    asyncio.create_task(device_watchdog())
    try:
        ingest.start()
        print(f"[SafeON] MQTT 연결: {config.MQTT_HOST}:{config.MQTT_PORT} → {config.MQTT_BASE}/#")
    except Exception as e:
        print(f"[SafeON] !! MQTT 브로커 연결 실패: {e}")
        print("[SafeON]    브로커 없이 기동. HTTP /api/batch 백업 경로는 사용 가능.")


# ---------- 백업: 오프라인 저장분 일괄 업로드 ----------
@app.post("/api/batch")
async def batch(records: list[dict]):
    return {"stored": ingest.handle_batch(records)}


# ---------- 조회 ----------
@app.get("/api/events")
def events(date_str: str | None = None, risk_level: str | None = None,
           equipment_id: str | None = None, event_type: str | None = None,
           limit: int = 200):
    return db.query_events(conn, date_str, risk_level, equipment_id, event_type, limit)


@app.get("/api/incidents")
def incidents(date_str: str | None = None, status: str | None = None, limit: int = 100):
    return db.query_incidents(conn, date_str, status, limit)


@app.patch("/api/incidents/{event_uid}")
def incident_action(event_uid: str, status: str):
    """조치상태 변경: OPEN → ACK → CLOSED"""
    if not db.set_incident_status(conn, event_uid, status.upper()):
        raise HTTPException(400, "status는 OPEN/ACK/CLOSED 중 하나")
    broadcast_sync({"type": "incident", "action": "status",
                    "event_uid": event_uid, "status": status.upper(),
                    "ts": time.time()})
    return {"event_uid": event_uid, "action_status": status.upper()}


@app.get("/api/devices")
def devices():
    """장치상태: ONLINE/DEGRADED/OFFLINE, 마지막 통신, 배터리 등."""
    return ingest.device_list()


@app.get("/api/env")
def env():
    latest = list(ingest.env_latest.values())
    return latest if latest else db.latest_env(conn)


@app.get("/api/camera")
def camera():
    return list(ingest.camera_latest.values())


@app.get("/api/stats/daily")
def stats_daily(date_str: str | None = None):
    return db.daily_stats(conn, date_str)


@app.get("/api/report/daily")
def report_daily(date_str: str | None = None):
    s = db.daily_stats(conn, date_str)
    return {**s, "summary": _summary(s, s["date"])}


@app.get("/api/report/weekly")
def report_weekly(end_date: str | None = None):
    s = db.weekly_stats(conn, end_date)
    return {**s, "summary": _summary(s, f"{s['start']} ~ {s['end']} 주간")}


def _summary(s: dict, label: str) -> str:
    caution = s["by_level"].get(config.RISK_CAUTION, 0)
    danger = s["by_level"].get(config.RISK_DANGER, 0)
    falls = s["by_type"].get("fall", 0)
    envs = s["by_type"].get("env", 0)
    cams = s["by_type"].get("camera", 0)
    inc = s["incidents"]
    worst_equip = s["by_equip"][0]["equipment_id"] if s["by_equip"] else None
    worst_hour = max(s["by_hour"], key=s["by_hour"].get) if s["by_hour"] else None

    parts = [f"{label} 총 이벤트 {s['total']}건 (주의 진입 {caution}·위험 진입 {danger}건"]
    parts.append(f", 낙상 {falls}건" if falls else "")
    parts.append(f", 폭염경보 {envs}건" if envs else "")
    parts.append(f", 카메라 감지 {cams}건" if cams else "")
    parts.append("). ")
    if inc["count"]:
        parts.append(f"아차사고 보고서 {inc['count']}건 생성(미조치 {inc['open']}건)")
        if inc["min_distance_m"] is not None:
            parts.append(f", 최소 접근거리 {inc['min_distance_m']}m")
        if inc["avg_duration_sec"] is not None:
            parts.append(f", 평균 위험 노출 {inc['avg_duration_sec']}초")
        parts.append(". ")
    if worst_equip:
        parts.append(f"최다 발생 장비는 {worst_equip}")
        parts.append(f", 집중 시간대는 {worst_hour}시입니다. " if worst_hour else "입니다. ")
    if falls:
        parts.append("낙상 발생 — 해당 근로자 안전 확인 및 작업환경 점검 필요. ")
    if danger:
        parts.append("위험(DANGER) 진입 발생 — 작업 동선 분리를 권장합니다.")
    elif s["total"] == 0:
        parts.append("기록된 위험 이벤트가 없습니다.")
    return "".join(parts)


@app.get("/api/status")
def status():
    """(장비, 근로자) 쌍별 현재 단계 — 미수신 시 OFFLINE 표시 (명세서)."""
    now = time.time()
    out = []
    for (equipment_id, worker_id), t in ingest.trackers.items():
        gap = now - t.last_seen
        level = config.RISK_OFFLINE if gap >= config.OFFLINE_SEC else t.level
        out.append({
            "equipment_id": equipment_id, "worker_id": worker_id,
            "risk_level": level, "risk_label": config.RISK_LABELS[level],
            "dwell_sec": round(t.dwell_seconds(now), 1),
        })
    return out


@app.get("/api/health")
def health():
    return {"mqtt": ingest.stats, "ws_clients": len(ws_clients),
            "devices": len(ingest.devices)}


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
