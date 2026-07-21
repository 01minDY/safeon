"""MQTT 수신 처리 — ESP32(근로자 태그 / 중장비 장치)로부터 데이터 수신.

토픽 규약 (HW팀 공유용):
  safeon/worker/{worker_id}/status   1초 주기  {"equip_id","distance_cm","risk_level","battery","seq"}
  safeon/worker/{worker_id}/event    발생 시    {"type":"fall","detail":...}
  safeon/equip/{equip_id}/status     1초 주기  {"state","worker_id","distance_cm","risk_level"}
  safeon/equip/{equip_id}/env        30분 주기 {"temp_c","humidity_pct"}
  safeon/batch                       필요 시    [{...status/env 레코드 + "ts"}, ...]  (오프라인 백업 업로드)

설계 원칙:
- 경보는 엣지(ESP32)에서 즉시 판정·발생 → 서버는 기록/관제/리포트 담당 (지연 최소화)
- 페이로드에 risk_level이 있으면 엣지 판정을 신뢰, 없으면 서버가 재계산(백업)
"""
import json
import time

import paho.mqtt.client as mqtt

import config
import db
import risk_engine
from risk_engine import PairTracker


class Ingest:
    def __init__(self, conn, on_update=None):
        """on_update(payload: dict) — 대시보드 브로드캐스트 콜백."""
        self.conn = conn
        self.on_update = on_update or (lambda p: None)
        self.trackers: dict[tuple, PairTracker] = {}
        self.equip_states: dict[str, str] = {}
        self.env_latest: dict[str, dict] = {}
        self.stats = {"received": 0, "events": 0, "batch_records": 0}

    # ---------- 공통 처리 ----------
    def _judge(self, equip_id, worker_id, distance_cm, equip_state, edge_level):
        """엣지 판정 우선, 없으면 서버 재계산. 필터/디바운스는 서버에서 수행."""
        key = (equip_id or "?", worker_id or "?")
        tracker = self.trackers.setdefault(key, PairTracker())
        filtered, computed, speed, changed = tracker.update(
            distance_cm, equip_state or "idle")
        level = edge_level if edge_level is not None else computed
        # 엣지 판정값이 와도 등급 '변화' 감지는 서버 트래커 기준으로 디바운스
        if edge_level is not None:
            changed = level != getattr(tracker, "edge_prev", None)
            tracker.edge_prev = level
        return filtered, level, speed, changed

    def handle_status(self, worker_id, equip_id, payload, ts=None):
        distance = payload.get("distance_cm")
        state = payload.get("equip_state") or payload.get("state") \
            or self.equip_states.get(equip_id, "idle")
        if equip_id:
            self.equip_states[equip_id] = state
        if distance is None:
            return
        filtered, level, speed, changed = self._judge(
            equip_id, worker_id, float(distance), state, payload.get("risk_level"))

        recorded = False
        if changed and level >= config.NEARMISS_MIN_LEVEL:
            db.insert_event(self.conn, "nearmiss", equip_id, worker_id,
                            filtered, state, level, speed, ts=ts)
            self.stats["events"] += 1
            recorded = True

        self.on_update({
            "type": "live", "equip_id": equip_id, "worker_id": worker_id,
            "distance_cm": round(filtered, 1), "equip_state": state,
            "risk_level": level, "risk_label": config.RISK_LABELS.get(level, "?"),
            "speed_cms": round(speed, 1), "battery": payload.get("battery"),
            "edge_judged": payload.get("risk_level") is not None,
            "ts": time.time(), "event_recorded": recorded,
        })

    def handle_worker_event(self, worker_id, payload, ts=None):
        etype = payload.get("type", "fall")
        detail = payload.get("detail", "")
        if etype == "fall":
            db.insert_event(self.conn, "fall", None, worker_id, None, None, 3,
                            detail=detail or "헬멧 자이로 낙상 감지", ts=ts)
            self.stats["events"] += 1
            self.on_update({"type": "fall", "worker_id": worker_id,
                            "detail": detail, "ts": time.time()})

    def handle_env(self, equip_id, payload, ts=None):
        temp = payload.get("temp_c")
        hum = payload.get("humidity_pct")
        db.insert_env(self.conn, equip_id, temp, hum, ts=ts)
        alert = None
        if temp is not None and (temp > config.TEMP_MAX or temp < config.TEMP_MIN):
            alert = f"이상온도 {temp}°C"
        elif hum is not None and hum > config.HUMIDITY_MAX:
            alert = f"이상습도 {hum}%"
        if alert:
            db.insert_event(self.conn, "env", equip_id, None, None, None, 2,
                            detail=alert, ts=ts)
            self.stats["events"] += 1
        self.env_latest[equip_id] = {"equip_id": equip_id, "temp_c": temp,
                                     "humidity_pct": hum, "alert": alert,
                                     "ts": ts or time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.on_update({"type": "env", **self.env_latest[equip_id]})

    def handle_batch(self, records):
        """오프라인 저장분 일괄 입력 (백업 플랜 2순위). 각 레코드는 ts 필수."""
        n = 0
        for r in records:
            ts = r.get("ts")
            kind = r.get("kind", "status")
            if kind == "status":
                # 배치는 실시간 판정 불가 → 기록된 risk_level 그대로 저장 (경고 이상만)
                lv = r.get("risk_level", 0)
                if lv >= config.NEARMISS_MIN_LEVEL:
                    db.insert_event(self.conn, "nearmiss", r.get("equip_id"),
                                    r.get("worker_id"), r.get("distance_cm"),
                                    r.get("equip_state", "idle"), lv,
                                    r.get("speed_cms", 0), detail="배치 업로드", ts=ts)
                    n += 1
            elif kind == "env":
                self.handle_env(r.get("equip_id"), r, ts=ts)
                n += 1
            elif kind == "event":
                self.handle_worker_event(r.get("worker_id"), r, ts=ts)
                n += 1
        self.stats["batch_records"] += n
        self.on_update({"type": "batch", "count": n, "ts": time.time()})
        return n

    # ---------- MQTT 클라이언트 ----------
    def on_message(self, client, userdata, msg):
        self.stats["received"] += 1
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:
            return
        parts = msg.topic.split("/")
        try:
            if parts[1] == "worker" and parts[3] == "status":
                self.handle_status(parts[2], payload.get("equip_id"), payload)
            elif parts[1] == "worker" and parts[3] == "event":
                self.handle_worker_event(parts[2], payload)
            elif parts[1] == "equip" and parts[3] == "status":
                self.handle_status(payload.get("worker_id"), parts[2], payload)
            elif parts[1] == "equip" and parts[3] == "env":
                self.handle_env(parts[2], payload)
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
        client.loop_start()   # 백그라운드 스레드
        self.client = client
        return client
