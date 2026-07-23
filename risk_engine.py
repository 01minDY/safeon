"""위험도·체감온도 엔진 — 명세서 기준 순수 로직 (단위 테스트 대상)."""
import math
import time
from collections import deque
from dataclasses import dataclass, field

import config


# ---------- 거리 3단계 판정 (명세서) ----------
def classify(distance_m: float) -> str:
    """SAFE: 3m 초과 / CAUTION: 1~3m / DANGER: 1m 이하"""
    if distance_m > config.DIST_CAUTION_M:
        return config.RISK_SAFE
    if distance_m > config.DIST_DANGER_M:
        return config.RISK_CAUTION
    return config.RISK_DANGER


def is_near_miss(risk_level: str) -> bool:
    """명세서 권고: DANGER이면 near_miss = true (아차사고 보고서 생성)"""
    return risk_level == config.RISK_DANGER


@dataclass
class PairTracker:
    """(중장비, 근로자) 쌍의 시계열: 이동평균 필터 + 단계 변화 감지."""
    raw: deque = field(default_factory=lambda: deque(maxlen=config.FILTER_WINDOW))
    level: str = config.RISK_SAFE
    level_since: float = 0.0
    last_seen: float = 0.0

    def update(self, distance_m: float, edge_level: str | None = None,
               now: float | None = None):
        """반환: (filtered_m, level, changed). 엣지 판정값이 오면 우선 사용."""
        now = now if now is not None else time.time()
        self.last_seen = now
        self.raw.append(distance_m)
        filtered = sum(self.raw) / len(self.raw)
        level = edge_level if edge_level in config.RISK_ORDER else classify(filtered)
        changed = level != self.level
        if changed:
            self.level = level
            self.level_since = now
        return filtered, level, changed

    def dwell_seconds(self, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        return now - self.level_since if self.level_since else 0.0


# ---------- 체감온도 (기상청 여름철 공식) ----------
def wet_bulb(ta: float, rh: float) -> float:
    """Stull 습구온도 근사식. ta: 기온(℃), rh: 상대습도(%)"""
    return (ta * math.atan(0.151977 * math.sqrt(rh + 8.313659))
            + math.atan(ta + rh)
            - math.atan(rh - 1.67633)
            + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh)
            - 4.686035)


def apparent_temp(ta: float, rh: float) -> float:
    """기상청 여름철 체감온도(℃)."""
    tw = wet_bulb(ta, rh)
    return (-0.2442 + 0.55399 * tw + 0.45535 * ta
            - 0.0022 * tw ** 2 + 0.00278 * tw * ta + 3.0)


def heat_stage(apparent_c: float) -> tuple[str, str]:
    """체감온도 → (단계, 권고문구). 2026 고용노동부 대응지침 기준."""
    for threshold, stage, advice in config.HEAT_STAGES:
        if threshold is None or apparent_c >= threshold:
            return stage, advice
    return "NORMAL", "정상 모니터링"


# ---------- 사건(Incident) 개선 권고 (규칙 기반) ----------
def recommend(duration_sec: float, min_distance_m: float, repeat_count: int) -> str:
    parts = []
    if min_distance_m is not None and min_distance_m <= config.INCIDENT_VERY_CLOSE_M:
        parts.append(f"최소 접근거리 {min_distance_m:.2f}m 초근접 — 즉시 작업중지 기준 및 안전교육 점검")
    if duration_sec >= config.INCIDENT_LONG_EXPOSURE_SEC:
        parts.append(f"위험 노출 {duration_sec:.0f}초 지속 — 작업 동선 분리 및 유도자 배치 검토")
    if repeat_count >= 2:
        parts.append(f"동일 장비-근로자 쌍 {repeat_count}회 반복 접근 — 해당 구역 출입통제 필요")
    if not parts:
        parts.append("단발성 접근 — 해당 시간대 작업계획 확인 권고")
    return " / ".join(parts)
