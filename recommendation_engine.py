"""Fixed mock risk assessment and forklift improvement recommendations."""

from __future__ import annotations

from datetime import datetime, timedelta

import config


RECOMMENDATION_BLUEPRINTS = (
    {
        "category": "URGENT",
        "priority": "HIGH",
        "title": "근로자 통로와 지게차 운행경로 즉시 분리",
        "description": (
            "교차지점에 차단대·바닥표시·일방통행 동선을 적용하고 분리가 "
            "어려운 구간은 유도자를 배치합니다."
        ),
        "legal_basis": (
            "접촉 위험구역 출입통제 및 작업지휘자·유도자 배치 원칙"
        ),
        "due_days": 1,
    },
    {
        "category": "URGENT",
        "priority": "HIGH",
        "title": "지게차 제동·조종·경보장치 긴급 점검",
        "description": (
            "브레이크, 조향, 경광등, 후진경보, 방향지시기와 SAFEON "
            "경보장치를 작업 재개 전 확인합니다."
        ),
        "legal_basis": (
            "지게차 작업 전 제동·조종·등화·경보장치 기능 점검"
        ),
        "due_days": 1,
    },
    {
        "category": "PRIORITY",
        "priority": "MEDIUM",
        "title": "현장 제한속도와 감속구간 재설정",
        "description": (
            "출입구·적재구역·교차로의 제한속도를 낮추고 시야가 불량한 "
            "구간에 정지선과 일시정지 절차를 둡니다."
        ),
        "legal_basis": (
            "현장별 제한속도·작업방법 설정 및 반복 위험구간 개선"
        ),
        "due_days": 3,
    },
    {
        "category": "PRIORITY",
        "priority": "MEDIUM",
        "title": "후방 사각지대 보조수단 보강",
        "description": (
            "후방카메라, 반사경, 접근경보 또는 유도자를 추가하고 적재물이 "
            "시야를 가릴 때의 후진 기준을 강화합니다."
        ),
        "legal_basis": "근로자 접촉 방지와 경보장치 기능 확보",
        "due_days": 3,
    },
    {
        "category": "PRIORITY",
        "priority": "MEDIUM",
        "title": "반복 발생 조합 대상 맞춤 안전교육",
        "description": "",
        "legal_basis": "",
        "due_days": 7,
    },
    {
        "category": "REGULAR",
        "priority": "LOW",
        "title": "하역장치·유압장치·바퀴 상태 정기점검",
        "description": (
            "포크, 마스트, 체인, 유압누유, 타이어 손상과 체결 상태를 "
            "체크리스트로 기록합니다."
        ),
        "legal_basis": (
            "지게차 작업 전 하역·유압장치 및 바퀴 이상 유무 점검"
        ),
        "due_days": 30,
    },
)


def fixed_assessment(event_id: str, created_ts: str) -> dict:
    """Return the fixed mock values requested for the risk assessment."""
    risk_score = (
        config.MOCK_LIKELIHOOD_SCORE * config.MOCK_SEVERITY_SCORE
    )
    return {
        "event_id": event_id,
        "likelihood_label": config.MOCK_LIKELIHOOD_LABEL,
        "likelihood_score": config.MOCK_LIKELIHOOD_SCORE,
        "severity_label": config.MOCK_SEVERITY_LABEL,
        "severity_score": config.MOCK_SEVERITY_SCORE,
        "risk_score": risk_score,
        "risk_grade": config.MOCK_RISK_GRADE,
        "equipment_kind": config.MOCK_EQUIPMENT_KIND,
        "risk_type": f"{config.MOCK_EQUIPMENT_KIND}-근로자 충돌",
        "due_within_hours": config.MOCK_RECOMMENDATION_DUE_HOURS,
        "created_ts": created_ts,
    }


def build_recommendations(
    event_id: str,
    equipment_id: str,
    worker_id: str,
    repeated_count: int,
    created_ts: str,
) -> list[dict]:
    """Build the six idempotent forklift recommendations from the mockup."""
    base = datetime.fromisoformat(created_ts.replace("Z", "+00:00"))
    action_prefix = event_id.removeprefix("EVT-")
    recommendations = []
    for order, blueprint in enumerate(RECOMMENDATION_BLUEPRINTS, start=1):
        item = dict(blueprint)
        item["action_id"] = f"ACT-{action_prefix}-{order:02d}"
        item["event_id"] = event_id
        item["sort_order"] = order
        item["due_date"] = (
            base + timedelta(days=item.pop("due_days"))
        ).date().isoformat()
        if order == 5:
            item["description"] = (
                f"지게차 {equipment_id} 운전자와 근로자 {worker_id}에게 "
                "위험구역, 신호방법, 우선통행 규칙을 재교육하고 기록을 "
                "남깁니다."
            )
            item["legal_basis"] = (
                "동일 장비·근로자 조합 반복 접근 "
                f"{max(1, repeated_count)}회 감지"
            )
        recommendations.append(item)
    return recommendations
