"""위험도 판단 엔진 — 순수 로직, HW/서버와 무관하게 단위 테스트 가능."""
from collections import deque
from dataclasses import dataclass, field
import time

import config


def base_level(distance_cm: float) -> int:
    """거리 기반 기본 등급. 0 안전 / 1 주의 / 2 경고 / 3 위험"""
    if distance_cm >= config.DIST_SAFE:
        return 0
    if distance_cm >= config.DIST_CAUTION:
        return 1
    if distance_cm >= config.DIST_WARNING:
        return 2
    return 3


def state_adjust(level: int, equip_state: str) -> int:
    """후진/작업중이면 +1 등급 (안전 상태는 보정하지 않음)."""
    if level > 0 and equip_state in config.HIGH_RISK_STATES:
        return min(level + 1, 3)
    return level


def approach_speed(samples: list[tuple[float, float]]) -> float:
    """(timestamp, distance) 샘플에서 접근 속도(cm/s, 양수=접근)를 계산."""
    if len(samples) < 2:
        return 0.0
    (t0, d0), (t1, d1) = samples[0], samples[-1]
    dt = t1 - t0
    if dt <= 0:
        return 0.0
    return (d0 - d1) / dt


def speed_adjust(level: int, speed: float) -> int:
    if level > 0 and speed > config.SPEED_THRESHOLD:
        return min(level + 1, 3)
    return level


def alert_for(level: int) -> dict:
    """등급별 경보 지시 — 태그(ESP32)로 그대로 전달."""
    return {
        0: {"buzzer": False, "vibration": False, "led": "green"},
        1: {"buzzer": False, "vibration": True, "led": "yellow"},
        2: {"buzzer": True, "vibration": True, "led": "orange"},
        3: {"buzzer": True, "vibration": True, "led": "red"},
    }[level]


@dataclass
class PairTracker:
    """(장비, 작업자) 쌍의 시계열 상태: 필터·속도·등급 변화 추적."""
    raw: deque = field(default_factory=lambda: deque(maxlen=config.FILTER_WINDOW))
    history: deque = field(default_factory=lambda: deque(maxlen=config.SPEED_WINDOW))
    level: int = 0
    level_since: float = 0.0
    last_seen: float = 0.0

    def update(self, distance_cm: float, equip_state: str, now: float | None = None):
        """새 샘플 반영. 반환: (filtered_distance, level, speed, level_changed)"""
        now = now if now is not None else time.time()
        self.last_seen = now

        # 이동평균 필터
        self.raw.append(distance_cm)
        filtered = sum(self.raw) / len(self.raw)

        # 접근 속도
        self.history.append((now, filtered))
        speed = approach_speed(list(self.history))

        # 등급 판정
        level = base_level(filtered)
        level = state_adjust(level, equip_state)
        level = speed_adjust(level, speed)

        changed = level != self.level
        if changed:
            self.level = level
            self.level_since = now
        return filtered, level, speed, changed

    def dwell_seconds(self, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        return now - self.level_since if self.level_since else 0.0
