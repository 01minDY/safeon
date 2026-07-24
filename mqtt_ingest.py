"""MQTT/HTTP ingestion for the SafeON sensor contract."""

from __future__ import annotations

import json
import time
from datetime import datetime

import paho.mqtt.client as mqtt
from pydantic import ValidationError

import config
import db
import risk_engine
from models import CameraReading, EnvironmentReading, ProximityReading


class Ingest:
    def __init__(self, conn, on_update=None):
        self.conn = conn
        self.on_update = on_update or (lambda payload: None)
        self.proximity_latest: dict[tuple[str, str], dict] = {}
        self.environment_latest: dict[str, dict] = {}
        self.camera_latest: dict[str, dict] = {}
        self.last_sequence: dict[tuple[str, str], int] = {}
        self.stats = {
            "received": 0,
            "accepted": 0,
            "invalid": 0,
            "duplicates": 0,
            "incidents_started": 0,
            "batch_records": 0,
        }

    @staticmethod
    def _timestamp(value=None):
        if isinstance(value, datetime):
            return value.astimezone().isoformat(timespec="seconds")
        if value:
            return str(value)
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _legacy_proximity(payload: dict, worker_id=None, equipment_id=None):
        result = dict(payload)
        result["worker_id"] = result.get("worker_id") or worker_id
        result["equipment_id"] = (
            result.get("equipment_id")
            or result.get("equip_id")
            or equipment_id
        )
        if "distance_m" not in result and result.get("distance_cm") is not None:
            result["distance_m"] = float(result["distance_cm"]) / 100.0
        result["timestamp"] = (
            result.get("timestamp") or result.get("ts") or datetime.now().astimezone()
        )
        if "sequence" not in result:
            result["sequence"] = result.get("seq")
        if result.get("sequence") is None:
            result["sequence"] = int(time.time() * 1000)
        if "battery_pct" not in result:
            result["battery_pct"] = result.get("battery")
        if result.get("risk_level") is not None:
            result["risk_level"] = risk_engine.normalize_risk_level(
                result["risk_level"], result.get("distance_m")
            )
        return result

    @staticmethod
    def _legacy_environment(payload: dict, equipment_id=None):
        result = dict(payload)
        result["equipment_id"] = (
            result.get("equipment_id")
            or result.get("equip_id")
            or equipment_id
        )
        result["timestamp"] = (
            result.get("timestamp") or result.get("ts") or datetime.now().astimezone()
        )
        if "temperature_c" not in result:
            result["temperature_c"] = result.get("temp_c")
        result.setdefault("sensor_status", "NORMAL")
        return result

    def handle_proximity(
        self,
        payload: dict,
        *,
        worker_id=None,
        equipment_id=None,
        transport="mqtt",
    ) -> dict:
        raw = self._legacy_proximity(payload, worker_id, equipment_id)
        model = ProximityReading.model_validate(raw)
        item = model.model_dump(mode="json")
        item["timestamp"] = self._timestamp(model.timestamp)

        key = (item["equipment_id"], item["worker_id"])
        sequence = item["sequence"]
        previous = self.last_sequence.get(key)
        if previous is not None and sequence <= previous:
            self.stats["duplicates"] += 1
            return {
                "accepted": False,
                "duplicate": True,
                "sequence": sequence,
                "last_sequence": previous,
            }
        self.last_sequence[key] = sequence

        edge_level = risk_engine.normalize_risk_level(
            item.get("risk_level"), item["distance_m"]
        )
        calculated_level = risk_engine.risk_level_for_distance(item["distance_m"])
        level = "OFFLINE" if edge_level == "OFFLINE" else calculated_level
        item.update(
            {
                "risk_level": level,
                "risk_label": config.RISK_LABELS[level],
                "near_miss": level == "DANGER",
                "edge_risk_level": edge_level,
                "risk_mismatch": edge_level not in {level, "OFFLINE"},
                "alert": risk_engine.proximity_alert(level, item["distance_m"]),
            }
        )

        incident = db.record_proximity(self.conn, item, transport)
        item["incident_transition"] = incident["transition"]
        item["incident"] = incident["incident"]
        self.proximity_latest[key] = item
        self.stats["accepted"] += 1
        if incident["transition"] == "STARTED":
            self.stats["incidents_started"] += 1
        self.on_update({"type": "proximity", **item})
        return {"accepted": True, **item}

    def handle_environment(
        self,
        payload: dict,
        *,
        equipment_id=None,
        transport="mqtt",
    ) -> dict:
        raw = self._legacy_environment(payload, equipment_id)
        model = EnvironmentReading.model_validate(raw)
        item = model.model_dump(mode="json")
        item["timestamp"] = self._timestamp(model.timestamp)
        apparent = risk_engine.apparent_temperature(
            item["temperature_c"], item["humidity_pct"]
        )
        heat_level = risk_engine.heat_level_for(apparent)
        guidance = risk_engine.heat_guidance(heat_level)
        item.update(
            {
                "apparent_temperature_c": apparent,
                "heat_level": heat_level,
                "heat_label": config.HEAT_LABELS[heat_level],
                "guidance": guidance["message"],
                "legal_basis": guidance["legal_basis"],
                "worker_alert": guidance,
            }
        )
        db.record_environment(self.conn, item, transport)
        self.environment_latest[item["equipment_id"]] = item
        self.stats["accepted"] += 1
        self.on_update({"type": "environment", **item})
        return {"accepted": True, **item}

    def handle_camera(
        self,
        payload: dict,
        *,
        equipment_id=None,
        transport="mqtt",
    ) -> dict:
        raw = dict(payload)
        raw["equipment_id"] = (
            raw.get("equipment_id") or raw.get("equip_id") or equipment_id
        )
        raw["timestamp"] = (
            raw.get("timestamp") or raw.get("ts") or datetime.now().astimezone()
        )
        model = CameraReading.model_validate(raw)
        item = model.model_dump(mode="json")
        item["timestamp"] = self._timestamp(model.timestamp)
        db.record_camera(self.conn, item, transport)
        self.camera_latest[item["equipment_id"]] = item
        self.stats["accepted"] += 1
        self.on_update({"type": "camera", **item})
        return {"accepted": True, **item}

    def handle_batch(self, records: list[dict], transport="http") -> dict:
        accepted = 0
        rejected = 0
        duplicates = 0
        for record in records:
            kind = str(record.get("kind", "proximity")).lower()
            try:
                if kind in {"status", "proximity"}:
                    result = self.handle_proximity(record, transport=transport)
                elif kind in {"env", "environment"}:
                    result = self.handle_environment(record, transport=transport)
                elif kind in {"camera", "detection"}:
                    result = self.handle_camera(record, transport=transport)
                else:
                    rejected += 1
                    continue
                if result.get("duplicate"):
                    duplicates += 1
                else:
                    accepted += 1
            except (ValidationError, ValueError, TypeError, KeyError):
                self.stats["invalid"] += 1
                rejected += 1
        self.stats["batch_records"] += accepted
        payload = {
            "type": "batch",
            "accepted": accepted,
            "rejected": rejected,
            "duplicates": duplicates,
            "timestamp": self._timestamp(),
        }
        self.on_update(payload)
        return payload

    def on_message(self, client, userdata, msg):
        self.stats["received"] += 1
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            parts = msg.topic.split("/")
            if parts[:2] == [config.MQTT_BASE, "batch"]:
                self.handle_batch(
                    payload if isinstance(payload, list) else [payload],
                    transport="mqtt",
                )
            elif len(parts) >= 3 and parts[1] == "proximity":
                self.handle_proximity(payload, worker_id=parts[2])
            elif len(parts) >= 3 and parts[1] == "environment":
                self.handle_environment(payload, equipment_id=parts[2])
            elif len(parts) >= 3 and parts[1] == "camera":
                self.handle_camera(payload, equipment_id=parts[2])
            elif len(parts) >= 4 and parts[1] == "worker" and parts[3] == "status":
                self.handle_proximity(payload, worker_id=parts[2])
            elif len(parts) >= 4 and parts[1] == "equip" and parts[3] == "env":
                self.handle_environment(payload, equipment_id=parts[2])
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError):
            self.stats["invalid"] += 1

    def start(self, host=None, port=None):
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="safeon-server",
            clean_session=True,
        )
        client.on_message = self.on_message
        client.on_connect = (
            lambda c, u, f, rc, props=None: c.subscribe(f"{config.MQTT_BASE}/#")
        )
        client.connect(
            host or config.MQTT_HOST,
            port or config.MQTT_PORT,
            keepalive=30,
        )
        client.loop_start()
        self.client = client
        return client

    def stop(self):
        client = getattr(self, "client", None)
        if client is not None:
            client.loop_stop()
            client.disconnect()
