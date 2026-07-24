"""SafeON control server: MQTT ingestion, REST API and live dashboard."""

from __future__ import annotations

import asyncio
import csv
import io
import json
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse

import config
import db
import risk_engine
from models import (
    CameraReading,
    EnvironmentReading,
    ImprovementActionCreate,
    ImprovementActionUpdate,
    IncidentActionUpdate,
    ProximityReading,
)
from mqtt_ingest import Ingest


conn = db.get_conn()
ws_clients: set[WebSocket] = set()
loop_ref: dict[str, asyncio.AbstractEventLoop] = {}


def broadcast_sync(payload: dict):
    loop = loop_ref.get("loop")
    if loop is None:
        return
    message = json.dumps(payload, ensure_ascii=False, default=str)

    async def send():
        dead = []
        for client in tuple(ws_clients):
            try:
                await client.send_text(message)
            except Exception:
                dead.append(client)
        for client in dead:
            ws_clients.discard(client)

    asyncio.run_coroutine_threadsafe(send(), loop)


ingest = Ingest(conn, on_update=broadcast_sync)


async def monitor_devices():
    while True:
        await asyncio.sleep(1)
        for item in db.evaluate_device_health(conn):
            broadcast_sync({"type": "device", **item})
        for item in db.close_stale_incidents(conn):
            broadcast_sync({"type": "incident", **item})


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop_ref["loop"] = asyncio.get_running_loop()
    mqtt_error = None
    try:
        ingest.start()
    except Exception as exc:
        mqtt_error = str(exc)
        print(f"[SafeON] MQTT 연결 실패, HTTP 백업으로 기동: {exc}")
    app.state.mqtt_error = mqtt_error
    monitor_task = asyncio.create_task(monitor_devices())
    try:
        yield
    finally:
        monitor_task.cancel()
        ingest.stop()


app = FastAPI(
    title="SafeON 안전관제 API",
    version="2.0.0",
    lifespan=lifespan,
)


@app.post("/api/ingest/proximity")
def ingest_proximity(reading: ProximityReading):
    return ingest.handle_proximity(reading.model_dump(mode="json"), transport="http")


@app.post("/api/ingest/environment")
def ingest_environment(reading: EnvironmentReading):
    return ingest.handle_environment(
        reading.model_dump(mode="json"), transport="http"
    )


@app.post("/api/ingest/camera")
def ingest_camera(reading: CameraReading):
    return ingest.handle_camera(reading.model_dump(mode="json"), transport="http")


@app.post("/api/batch")
def ingest_batch(records: list[dict]):
    return ingest.handle_batch(records, transport="http")


@app.get("/api/live")
def live():
    devices = db.list_devices(conn)
    status_by_device = {
        (item["device_id"], item["device_type"]): item["status"]
        for item in devices
    }
    proximity = []
    for source in ingest.proximity_latest.values():
        item = dict(source)
        worker_status = status_by_device.get((item["worker_id"], "WORKER"))
        if worker_status == "OFFLINE":
            item.update(
                {
                    "risk_level": "OFFLINE",
                    "risk_label": config.RISK_LABELS["OFFLINE"],
                    "near_miss": False,
                    "alert": risk_engine.proximity_alert("OFFLINE"),
                    "risk_mismatch": True,
                }
            )
        proximity.append(item)

    cameras = []
    for source in (
        list(ingest.camera_latest.values()) or db.latest_cameras(conn)
    ):
        item = dict(source)
        camera_id = f"CAM-{item['equipment_id']}"
        if status_by_device.get((camera_id, "CAMERA")) == "OFFLINE":
            item["camera_status"] = "OFFLINE"
        cameras.append(item)

    return {
        "proximity": proximity,
        "environment": (
            list(ingest.environment_latest.values())
            or db.latest_environment(conn)
        ),
        "cameras": cameras,
    }


@app.get("/api/incidents")
def incidents(
    date_str: str | None = None,
    action_status: str | None = None,
    equipment_id: str | None = None,
    worker_id: str | None = None,
    active_only: bool = False,
    limit: int = Query(default=200, ge=1, le=1000),
):
    return db.list_incidents(
        conn,
        date_str=date_str,
        action_status=action_status,
        equipment_id=equipment_id,
        worker_id=worker_id,
        active_only=active_only,
        limit=limit,
    )


@app.patch("/api/incidents/{event_id}/action")
def incident_action(event_id: str, update: IncidentActionUpdate):
    item = db.update_incident_action(conn, event_id, update.action_status)
    if item is None:
        raise HTTPException(status_code=404, detail="사건을 찾을 수 없습니다.")
    broadcast_sync({"type": "incident_action", **item})
    return item


@app.get("/api/devices")
def devices():
    return db.list_devices(conn)


@app.get("/api/status")
def legacy_status():
    return db.list_devices(conn)


@app.get("/api/environment")
@app.get("/api/env")
def environment():
    return list(ingest.environment_latest.values()) or db.latest_environment(conn)


@app.get("/api/cameras")
def cameras():
    return list(ingest.camera_latest.values()) or db.latest_cameras(conn)


def _report_summary(result: dict) -> str:
    count = result["total_incidents"]
    if count == 0:
        return (
            f"{result['start']}~{result['end']} 기간 중 기록된 위험 사건이 없습니다. "
            f"장치 온라인율은 {result['device_online_rate']:.1f}%입니다."
        )
    minimum = result["minimum_distance_m"]
    exposure = result["total_exposure_seconds"]
    summary = (
        f"위험 사건 {count}건, 총 노출 {exposure:.1f}초가 기록됐습니다. "
        f"최소 접근거리는 {minimum:.2f}m이며 장치 온라인율은 "
        f"{result['device_online_rate']:.1f}%입니다."
    )
    if result["active_incidents"]:
        summary += f" 현재 진행 중인 사건이 {result['active_incidents']}건 있습니다."
    if result["action_close_rate"] < 100:
        summary += (
            f" 조치 완료율은 {result['action_close_rate']:.1f}%로 미완료 사건을 "
            "확인해야 합니다."
        )
    return summary


@app.get("/api/report/daily")
def daily_report(date_str: str | None = None):
    result = db.daily_report(conn, date_str)
    result["summary"] = _report_summary(result)
    return result


@app.get("/api/report/weekly")
def weekly_report(end_date: str | None = None):
    result = db.weekly_report(conn, end_date)
    result["summary"] = _report_summary(result)
    return result


@app.get("/api/report/incidents.csv")
def incident_csv(
    start_date: str | None = None,
    end_date: str | None = None,
):
    end = end_date or date.today().isoformat()
    start = start_date or end
    rows = db.report(conn, start, end)["incidents"]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "event_id",
            "start_ts",
            "end_ts",
            "worker_id",
            "equipment_id",
            "min_distance_m",
            "exposure_seconds",
            "online_rate",
            "action_status",
            "recommendation",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.get("event_id"),
                row.get("start_ts"),
                row.get("end_ts"),
                row.get("worker_id"),
                row.get("equipment_id"),
                row.get("min_distance_m"),
                row.get("exposure_seconds"),
                row.get("online_rate"),
                row.get("action_status"),
                row.get("recommendation"),
            ]
        )
    content = "\ufeff" + buffer.getvalue()
    return StreamingResponse(
        iter([content.encode("utf-8")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=safeon-incidents.csv"},
    )


@app.get("/api/actions")
def actions(status: str | None = None, limit: int = Query(200, ge=1, le=1000)):
    return db.list_improvement_actions(conn, status, limit)


@app.post("/api/actions")
def create_action(payload: ImprovementActionCreate):
    item = db.create_improvement_action(conn, payload.model_dump())
    broadcast_sync({"type": "improvement_action", **item})
    return item


@app.patch("/api/actions/{action_id}")
def update_action(action_id: str, payload: ImprovementActionUpdate):
    item = db.update_improvement_action(
        conn, action_id, payload.model_dump(exclude_none=True)
    )
    if item is None:
        raise HTTPException(status_code=404, detail="개선조치를 찾을 수 없습니다.")
    broadcast_sync({"type": "improvement_action", **item})
    return item


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "mqtt": ingest.stats,
        "mqtt_error": getattr(app.state, "mqtt_error", None),
        "websocket_clients": len(ws_clients),
    }


@app.websocket("/ws/live")
async def websocket_live(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_clients.discard(ws)


@app.get("/")
def index():
    return FileResponse("static/dashboard.html")
