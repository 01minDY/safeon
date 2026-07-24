"""Validated HTTP/MQTT payload models for the SafeON specification."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def local_now() -> datetime:
    return datetime.now().astimezone()


RiskLevel = Literal["SAFE", "CAUTION", "DANGER", "OFFLINE"]
SensorStatus = Literal["NORMAL", "ERROR", "OFFLINE"]
CameraStatus = Literal["ONLINE", "ERROR", "OFFLINE"]
ActionState = Literal["OPEN", "ACK", "CLOSED"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class ProximityReading(StrictModel):
    timestamp: datetime = Field(default_factory=local_now)
    worker_id: str = Field(min_length=1, max_length=80)
    equipment_id: str = Field(min_length=1, max_length=80)
    distance_m: float = Field(ge=0)
    risk_level: RiskLevel | None = None
    near_miss: bool | None = None
    sequence: int = Field(ge=0)
    battery_pct: float | None = Field(default=None, ge=0, le=100)
    equipment_battery_pct: float | None = Field(default=None, ge=0, le=100)
    sensor_error_code: str | None = Field(default=None, max_length=120)
    firmware_version: str | None = Field(default=None, max_length=80)


class EnvironmentReading(StrictModel):
    timestamp: datetime = Field(default_factory=local_now)
    equipment_id: str = Field(min_length=1, max_length=80)
    temperature_c: float = Field(ge=-80, le=100)
    humidity_pct: float = Field(ge=0, le=100)
    sensor_status: SensorStatus = "NORMAL"
    sensor_error_code: str | None = Field(default=None, max_length=120)
    firmware_version: str | None = Field(default=None, max_length=80)


class CameraReading(StrictModel):
    timestamp: datetime = Field(default_factory=local_now)
    equipment_id: str = Field(min_length=1, max_length=80)
    person_detected: bool
    confidence: float = Field(ge=0, le=1)
    camera_status: CameraStatus = "ONLINE"
    sensor_error_code: str | None = Field(default=None, max_length=120)
    firmware_version: str | None = Field(default=None, max_length=80)


class IncidentActionUpdate(StrictModel):
    action_status: ActionState


class ImprovementActionCreate(StrictModel):
    event_id: str | None = Field(default=None, max_length=40)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    priority: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"
    assignee: str | None = Field(default=None, max_length=80)
    due_date: str | None = Field(default=None, max_length=10)


class ImprovementActionUpdate(StrictModel):
    status: Literal["OPEN", "IN_PROGRESS", "CLOSED"] | None = None
    assignee: str | None = Field(default=None, max_length=80)
    due_date: str | None = Field(default=None, max_length=10)


class RecommendationApprovalUpdate(StrictModel):
    approved: bool
