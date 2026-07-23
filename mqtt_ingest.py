"""MQTT 수신 처리 — 구현 세부 명세서 기준.

토픽 규약 (HW팀 공유용):
  safeon/worker/{worker_id}/status    1초   근접위험 데이터 (A)
  safeon/worker/{worker_id}/event     발생시 낙상 등
  safeon/equip/{equipment_id}/status  1초   근접위험 데이터 (A, 중장비측)
  safeon/equip/{equipment_id}/env     30분  온습도 (B)
  safeon/equip/{equipment_id}/camera  분석시 후방 카메라 (C)
  safeon/batch                        필요시 오프라인 저장분 배열

근접위험 페이로드 (A):
  {"timestamp","worker_id","equipment_id","distance_m","risk_level",
   "near_miss","sequence"}   risk_level: SAFE|CAUTION|DANGER (엣지 판정)

단계별 동작 (명세서):
  SAFE    → 기록 없음 (실시간 표시만)
  CAUTION → 대시보드 수신·경보·관제 기록 시작 (이벤트 로그)
  DANGER  → + 아차사고 보고서(Incident) 생성
"""
import json
import time
from datetime import datetime, timedelta

import paho.mqtt.client as mqtt

import config
import db
import risk_engine
from risk_engine import PairTracker


def _ts_of(payload) -> str:
    """기기 timestamp(ISO 8601) 우선, 없거나 파싱 불가면 서버 시각."""
    ts = payload.get("timestamp")
    if ts:
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00")) \
                .replace(tzinfo=None).isoformat(timespec="seconds")
        except ValueError:
            pass
    return datetime.now().isoformat(timespec="seconds")


def _distance_m(payload):
    """distance_m(m) 우선, 구버전 distance_cm도 수용."""
    if payload.get("distance_m") is not None:
        return float(payload["distance_m"])
    if payload.get("distance_cm") is not None:
        return float(payload["distance_cm"]) / 100.0
    return None


class Ingest:
    def __init__(self, conn, on_update=None):
        self.conn = conn
        self.on_update = on_update or (lambda p: None)
        self.trackers: dict[tuple, PairTracker] = {}
        self.env_latest: dict[str, dict] = {}
        self.camera_latest: dict[str, dict] = {}
        # 장치 레지스트리: device_id -> {type, last_seen, battery, status, ...}
        self.devices: dict[str, dict] = {}
        # 진행 중 사건: (equipment_id, worker_id) -> event_uid
        self.open_incidents: dict[tuple, str] = {}
        self.heat_stage_prev: dict[str, str] = {}
        self.stats = {"received": 0, "events": 0, "incidents": 0, "batch_records": 0}

    # ---------- 장치 레지스트리 ----------
    def touch_device(self, device_id, dtype, payload=None, interval_type="distance"):
        if not device_id:
            return
        d = self.devices.setdefault(device_id, {
            "device_id": device_id, "type": dtype, "interval_type": interval_type,
            "status": "ONLINE", "battery": None, "error_code": None, "fw": None})
        d["last_seen"] = time.time()
        d["status"] = "ONLINE"
        if payload:
            if payload.get("battery") is not None:
                d["battery"] = payload["battery"]
            if payload.get("sensor_status"):
                d["error_code"] = None if payload["sensor_status"] == "NORMAL" \
                    else payload["sensor_status"]
            if payload.get("fw_version"):
                d["fw"] = payload["fw_version"]

    def refresh_device_status(self):
        """명세서 권고: 거리장치 2초→DEGRADED, 5초→OFFLINE / 온습도 2회 누락→OFFLINE.
        주기 호출(서버 watchdog). 상태 변화 시 True 반환."""
        now = time.time()
        changed = False
        for d in self.devices.values():
            gap = now - d.get("last_seen", 0)
            if d["interval_type"] == "env":
                new = "OFFLINE" if gap > config.ENV_INTERVAL_SEC * config.ENV_MISS_LIMIT \
                    else "ONLINE"
            else:
                new = "OFFLINE" if gap >= config.OFFLINE_SEC \
                    else "DEGRADED" if gap >= config.DEGRADED_SEC else "ONLINE"
            if new != d["status"]:
                d["status"] = new
                changed = True
        return changed

    def device_list(self):
        now = time.time()
        return [{**d, "last_seen_ago_sec": round(now - d.get("last_seen", 0), 1)}
                for d in self.devices.values()]

    # ---------- A. 근접위험 데이터 ----------
    def handle_status(self, payload, ts=None, realtime=True):
        worker_id = payload.get("worker_id")
        equipment_id = payload.get("equipment_id") or payload.get("equip_id")
        dist = _distance_m(payload)
        if dist is None:
            return
        ts = ts or _ts_of(payload)
        edge_level = payload.get("risk_level")
        if isinstance(edge_level, str):
            edge_level = edge_level.upper()

        key = (equipment_id or "?", worker_id or "?")
        tracker = self.trackers.setdefault(key, PairTracker())
        filtered, level, changed = tracker.update(dist, edge_level)

        event_recorded = False
        if realtime:
            # CAUTION 이상 '진입' 시 관제 기록 (디바운스)
            if changed and level in (config.RISK_CAUTION, config.RISK_DANGER):
                db.insert_event(self.conn, "nearmiss", equipment_id, worker_id,
                                filtered, level, ts=ts)
                self.stats["events"] += 1
                event_recorded = True
            # DANGER 진입 → 사건(아차사고 보고서) 시작 / 해제 → 종료
            self._incident_flow(key, equipment_id, worker_id, filtered, level,
                                changed, ts)

        self.on_update({
            "type": "live", "equipment_id": equipment_id, "worker_id": worker_id,
            "distance_m": round(filtered, 2), "risk_level": level,
            "risk_label": config.RISK_LABELS.get(level, level),
            "near_miss": risk_engine.is_near_miss(level),
            "battery": payload.get("battery"), "sequence": payload.get("sequence"),
            "edge_judged": edge_level is not None,
            "ts": time.time(), "event_recorded": event_recorded,
        })

    def _incident_flow(self, key, equipment_id, worker_id, dist, level, changed, ts):
        uid = self.open_incidents.get(key)
        if level == config.RISK_DANGER:
            if uid is None:
                uid = db.open_incident(self.conn, equipment_id, worker_id,
                                       start_ts=ts, distance_m=dist)
                self.open_incidents[key] = uid
                self.stats["incidents"] += 1
                self.on_update({"type": "incident", "action": "open",
                                "event_uid": uid, "equipment_id": equipment_id,
                                "worker_id": worker_id, "ts": time.time()})
            else:
                db.update_incident_distance(self.conn, uid, dist)
        elif uid is not None and changed:
            # DANGER 해제 → 사건 종료 + 규칙 기반 개선 권고 생성
            since = (datetime.now() - timedelta(
                seconds=config.INCIDENT_REPEAT_WINDOW_SEC)).isoformat(timespec="seconds")
            repeat = db.count_recent_incidents(self.conn, equipment_id, worker_id, since)
            row = [i for i in db.query_incidents(self.conn, limit=1000)
                   if i["event_uid"] == uid]
            min_d = row[0]["min_distance_m"] if row else dist
            dur = db.close_incident(self.conn, uid, end_ts=ts, recommendation="")
            rec = risk_engine.recommend(dur or 0, min_d, repeat)
            db.close_incident(self.conn, uid, end_ts=ts, recommendation=rec)
            del self.open_incidents[key]
            self.on_update({"type": "incident", "action": "close",
                            "event_uid": uid, "duration_sec": dur,
                            "recommendation": rec, "ts": time.time()})

    # ---------- 근로자 이벤트 (낙상 등) ----------
    def handle_worker_event(self, worker_id, payload, ts=None):
        etype = payload.get("type", "fall")
        detail = payload.get("detail", "")
        if etype == "fall":
            db.insert_event(self.conn, "fall", None, worker_id, None,
                            config.RISK_DANGER,
                            detail=detail or "헬멧 자이로 낙상 감지",
                            ts=ts or _ts_of(payload))
            self.stats["events"] += 1
            self.on_update({"type": "fall", "worker_id": worker_id,
                            "detail": detail, "ts": time.time()})

    # ---------- B. 온습도 데이터 (30분 주기) ----------
    def handle_env(self, equipment_id, payload, ts=None):
        temp = payload.get("temperature_c", payload.get("temp_c"))
        hum = payload.get("humidity_pct")
        sensor_status = payload.get("sensor_status", "NORMAL")
        ts = ts or _ts_of(payload)

        apparent = stage = advice = None
        if temp is not None and hum is not None and sensor_status == "NORMAL":
            apparent = risk_engine.apparent_temp(float(temp), float(hum))
            stage, advice = risk_engine.heat_stage(apparent)

        db.insert_env(self.conn, equipment_id, temp, hum, apparent, stage,
                      sensor_status, ts=ts)

        # 단계 상승 시 이벤트 기록 (HEAT_CAUTION 이상)
        prev = self.heat_stage_prev.get(equipment_id, "NORMAL")
        if stage and stage != "NORMAL" and stage != prev:
            db.insert_event(self.conn, "env", equipment_id, None, None,
                            config.RISK_CAUTION,
                            detail=f"체감온도 {apparent:.1f}°C {config.HEAT_LABELS[stage]} — {advice}",
                            ts=ts)
            self.stats["events"] += 1
        if stage:
            self.heat_stage_prev[equipment_id] = stage

        self.env_latest[equipment_id] = {
            "equipment_id": equipment_id, "temperature_c": temp,
            "humidity_pct": hum,
            "apparent_c": round(apparent, 1) if apparent is not None else None,
            "heat_stage": stage, "heat_label": config.HEAT_LABELS.get(stage),
            "advice": advice, "sensor_status": sensor_status, "ts": ts}
        self.on_update({"type": "env", **self.env_latest[equipment_id]})

    # ---------- C. 후방 카메라 데이터 ----------
    def handle_camera(self, equipment_id, payload, ts=None):
        detected = bool(payload.get("person_detected"))
        conf = payload.get("confidence")
        cam_status = payload.get("camera_status", "ONLINE")
        ts = ts or _ts_of(payload)

        prev = self.camera_latest.get(equipment_id, {})
        # 미검출→검출 전환 시에만 이벤트 기록 (중복 방지)
        if detected and not prev.get("person_detected"):
            db.insert_event(self.conn, "camera", equipment_id, None, None,
                            config.RISK_CAUTION,
                            detail=f"후방 사람 감지 (신뢰도 {conf})", ts=ts)
            self.stats["events"] += 1

        self.camera_latest[equipment_id] = {
            "equipment_id": equipment_id, "person_detected": detected,
            "confidence": conf, "camera_status": cam_status, "ts": ts}
        self.on_update({"type": "camera", **self.camera_latest[equipment_id]})

    # ---------- 배치 업로드 (백업 플랜) ----------
    def handle_batch(self, records):
        """오프라인 저장분 일괄 입력. 각 레코드는 timestamp 필수."""
        n = 0
        for r in records:
            kind = r.get("kind", "status")
            ts = _ts_of(r)
            if kind == "status":
                lv = str(r.get("risk_level", "SAFE")).upper()
                if lv in (config.RISK_CAUTION, config.RISK_DANGER):
                    db.insert_event(self.conn, "nearmiss",
                                    r.get("equipment_id") or r.get("equip_id"),
                                    r.get("worker_id"), _distance_m(r), lv,
                                    detail="배치 업로드", ts=ts)
                    n += 1
            elif kind == "env":
                self.handle_env(r.get("equipment_id") or r.get("equip_id"), r, ts=ts)
                n += 1
            elif kind == "camera":
                self.handle_camera(r.get("equipment_id"), r, ts=ts)
                n += 1
            elif kind == "event":
                self.handle_worker_event(r.get("worker_id"), r, ts=ts)
                n += 1
        self.stats["batch_records"] += n
        self.on_update({"type": "batch", "count": n, "ts": time.time()})
        return n

    # ---------- MQTT ----------
    def on_message(self, client, userdata, msg):
        self.stats["received"] += 1
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:
            return
        parts = msg.topic.split("/")
        try:
            if parts[1] == "worker" and parts[3] == "status":
                payload.setdefault("worker_id", parts[2])
                self.touch_device(parts[2], "WORKER", payload)
                self.handle_status(payload)
            elif parts[1] == "worker" and parts[3] == "event":
                self.touch_device(parts[2], "WORKER", payload)
                self.handle_worker_event(parts[2], payload)
            elif parts[1] == "equip" and parts[3] == "status":
                payload.setdefault("equipment_id", parts[2])
                self.touch_device(parts[2], "EQUIPMENT", payload)
                self.handle_status(payload)
            elif parts[1] == "equip" and parts[3] == "env":
                self.touch_device(f"{parts[2]}-ENV", "EQUIPMENT", payload,
                                  interval_type="env")
                self.handle_env(parts[2], payload)
            elif parts[1] == "equip" and parts[3] == "camera":
                self.touch_device(f"{parts[2]}-CAM", "CAMERA", payload)
                self.handle_camera(parts[2], payload)
            elif parts[1] == "batch":
                self.handle_batch(payload if isinstance(payload, list) else [payload])
        except (IndexError, KeyError):
            pass

    def start(self, host=None, port=None):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id="safeon-server", clean_session=True)
        client.on_message = self.on_message
        client.on_connect = lambda c, u, f, rc, props=None: c.subscribe(f"{config.MQTT_BASE}/#")
        client.connect(host or config.MQTT_HOST, port or config.MQTT_PORT, keepalive=30)
        client.loop_start()
        self.client = client
        return client
