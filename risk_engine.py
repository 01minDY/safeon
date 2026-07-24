"""Pure SafeON safety calculations.

The functions are independent from FastAPI, MQTT and SQLite so the exact
specification boundaries can be unit-tested.
"""

from __future__ import annotations

import math

import config


def risk_level_for_distance(distance_m: float | None) -> str:
    """Return SAFE/CAUTION/DANGER/OFFLINE using the final three-stage rule."""
    if distance_m is None:
        return "OFFLINE"
    if distance_m <= config.DISTANCE_DANGER_M:
        return "DANGER"
    if distance_m <= config.DISTANCE_CAUTION_M:
        return "CAUTION"
    return "SAFE"


def normalize_risk_level(value, distance_m: float | None = None) -> str:
    """Normalize canonical strings and legacy numeric levels.

    Legacy 0/1 values become SAFE/CAUTION and legacy 2/3 values become DANGER.
    When no value is supplied, the server calculates from distance.
    """
    if value is None:
        return risk_level_for_distance(distance_m)
    if isinstance(value, str):
        text = value.strip().upper()
        if text in config.RISK_LEVELS:
            return text
        if text.isdigit():
            value = int(text)
    if isinstance(value, (int, float)):
        numeric = int(value)
        if numeric <= 0:
            return "SAFE"
        if numeric == 1:
            return "CAUTION"
        return "DANGER"
    raise ValueError(f"지원하지 않는 risk_level: {value!r}")


def wet_bulb_temperature(temperature_c: float, humidity_pct: float) -> float:
    """Calculate Tw with the formula provided in the implementation spec."""
    ta = float(temperature_c)
    rh = min(100.0, max(0.0, float(humidity_pct)))
    return (
        ta * math.atan(0.151977 * math.sqrt(rh + 8.313659))
        + math.atan(ta + rh)
        - math.atan(rh - 1.67633)
        + 0.00391838 * (rh ** 1.5) * math.atan(0.023101 * rh)
        - 4.686035
    )


def apparent_temperature(temperature_c: float, humidity_pct: float) -> float:
    """Calculate the Korean summer apparent temperature from Ta and Tw."""
    ta = float(temperature_c)
    tw = wet_bulb_temperature(ta, humidity_pct)
    value = (
        -0.2442
        + 0.55399 * tw
        + 0.45535 * ta
        - 0.0022 * (tw ** 2)
        + 0.00278 * tw * ta
        + 3.0
    )
    return round(value, 1)


def heat_level_for(apparent_temp_c: float) -> str:
    if apparent_temp_c < 31:
        return "NORMAL"
    if apparent_temp_c < 33:
        return "HEAT_CAUTION"
    if apparent_temp_c < 35:
        return "REST_REQUIRED"
    if apparent_temp_c < 38:
        return "STOP_RECOMMENDED"
    return "EMERGENCY_STOP"


def heat_guidance(level: str) -> dict:
    """Return dashboard wording and worker-device output for a heat stage."""
    return {
        "NORMAL": {
            "message": "정상 모니터링",
            "legal_basis": "normal",
            "led": "green",
            "sound": "none",
        },
        "HEAT_CAUTION": {
            "message": "폭염작업 후보: 냉방·통풍·시간조정·휴식을 검토하세요.",
            "legal_basis": "preventive",
            "led": "yellow_blink",
            "sound": "short_once",
        },
        "REST_REQUIRED": {
            "message": "폭염작업 시 2시간 이내 20분 이상 휴식 기준을 적용하세요.",
            "legal_basis": "legal_standard",
            "led": "orange_blink",
            "sound": "triple_with_vibration",
        },
        "STOP_RECOMMENDED": {
            "message": "행정지침상 14~17시 옥외작업 중지를 권고합니다.",
            "legal_basis": "administrative_recommendation",
            "led": "red_blink",
            "sound": "strong_repeat_with_vibration",
        },
        "EMERGENCY_STOP": {
            "message": "행정지침상 긴급조치 외 옥외작업 중지를 권고합니다.",
            "legal_basis": "administrative_recommendation",
            "led": "red_fast_blink",
            "sound": "long_repeat",
        },
    }[level]


def proximity_alert(level: str, distance_m: float | None = None) -> dict:
    distance_text = "--" if distance_m is None else f"{distance_m:.1f}m"
    return {
        "SAFE": {
            "led": "green",
            "buzzer": "none",
            "vibration": False,
            "display": "SAFE",
        },
        "CAUTION": {
            "led": "yellow",
            "buzzer": "slow",
            "vibration": False,
            "display": f"CAUTION {distance_text}",
        },
        "DANGER": {
            "led": "red",
            "buzzer": "fast",
            "vibration": True,
            "display": f"DANGER {distance_text}",
        },
        "OFFLINE": {
            "led": "offline_pattern",
            "buzzer": "offline_pattern",
            "vibration": False,
            "display": "OFFLINE",
        },
    }[level]
