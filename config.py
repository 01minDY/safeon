"""SafeON runtime configuration.

The values in this module mirror the implementation specification.  Hardware
firmware and the control server must use the same distance thresholds.
Environment variables are supported for deployment without editing source.
"""

from __future__ import annotations

import os


# MQTT
MQTT_HOST = os.getenv("SAFEON_MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("SAFEON_MQTT_PORT", "1883"))
MQTT_BASE = os.getenv("SAFEON_MQTT_BASE", "safeon")

# Distance stages (metres)
DISTANCE_DANGER_M = 1.0
DISTANCE_CAUTION_M = 3.0

# Device health
PROXIMITY_DEGRADED_SEC = 2.0
PROXIMITY_OFFLINE_SEC = 5.0
ENV_INTERVAL_SEC = 30 * 60
ENV_OFFLINE_MISSES = 2
CAMERA_DEGRADED_SEC = 5.0
CAMERA_OFFLINE_SEC = 15.0

# Incident rules
LONG_EXPOSURE_SEC = 30.0
REPEAT_INCIDENT_COUNT = 3

# Storage
DB_PATH = os.getenv("SAFEON_DB_PATH", "safeon.db")

RISK_LEVELS = ("SAFE", "CAUTION", "DANGER", "OFFLINE")
RISK_LABELS = {
    "SAFE": "안전",
    "CAUTION": "주의",
    "DANGER": "위험",
    "OFFLINE": "통신장애",
}

HEAT_LEVELS = (
    "NORMAL",
    "HEAT_CAUTION",
    "REST_REQUIRED",
    "STOP_RECOMMENDED",
    "EMERGENCY_STOP",
)
HEAT_LABELS = {
    "NORMAL": "정상",
    "HEAT_CAUTION": "온열 주의",
    "REST_REQUIRED": "휴식 필요",
    "STOP_RECOMMENDED": "작업중지 권고",
    "EMERGENCY_STOP": "긴급 작업중지",
}

ACTION_STATES = ("OPEN", "ACK", "CLOSED")
