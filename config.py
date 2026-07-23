"""SafeON 설정 — 구현 세부 명세서 기준. 대회장 실측 후 이 파일만 수정."""

# ---------- MQTT ----------
MQTT_HOST = "localhost"     # 브로커 주소 (대회장: 관제 노트북 IP)
MQTT_PORT = 1883
MQTT_BASE = "safeon"

# 토픽:
#   safeon/worker/{worker_id}/status      근접위험 데이터 (1초)
#   safeon/worker/{worker_id}/event       낙상 등 이벤트
#   safeon/equip/{equipment_id}/status    중장비측 근접위험 데이터 (1초)
#   safeon/equip/{equipment_id}/env       온습도 (30분)
#   safeon/equip/{equipment_id}/camera    후방 카메라 분석 결과
#   safeon/batch                          오프라인 저장분 일괄 업로드

# ---------- 거리 단계 (명세서: 3단계, 단위 m) ----------
# 근거: WorkSafe Victoria 3m 배제구역 / 공단 스마트 안전장치 최소 감지성능 1m
DIST_CAUTION_M = 3.0    # 초과: SAFE / 1~3m: CAUTION
DIST_DANGER_M = 1.0     # 이하: DANGER

RISK_SAFE = "SAFE"
RISK_CAUTION = "CAUTION"
RISK_DANGER = "DANGER"
RISK_OFFLINE = "OFFLINE"
RISK_ORDER = {RISK_SAFE: 0, RISK_CAUTION: 1, RISK_DANGER: 2}
RISK_LABELS = {RISK_SAFE: "안전", RISK_CAUTION: "주의", RISK_DANGER: "위험",
               RISK_OFFLINE: "통신장애"}

# 거리값 이동평균 필터 윈도우
FILTER_WINDOW = 3

# ---------- 장치상태 판정 (명세서 권고 기준) ----------
DEGRADED_SEC = 2.0      # 거리장치 2초 이상 미수신 → DEGRADED
OFFLINE_SEC = 5.0       # 5초 이상 미수신 → OFFLINE
ENV_INTERVAL_SEC = 30 * 60   # 온습도 전송 주기 (30분)
ENV_MISS_LIMIT = 2           # 예정 시각 2회 연속 누락 → OFFLINE

# ---------- 체감온도 단계 (2026 고용노동부 대응지침, ℃) ----------
HEAT_STAGES = [
    (38.0, "EMERGENCY_STOP",   "긴급조치 외 옥외작업 중지 권고"),
    (35.0, "STOP_RECOMMENDED", "14~17시 옥외작업 중지 권고"),
    (33.0, "REST_REQUIRED",    "폭염작업 시 2시간 이내 20분 이상 휴식 (법적 기준)"),
    (31.0, "HEAT_CAUTION",     "폭염작업 후보 — 냉방·통풍·시간조정·휴식 검토"),
    (None, "NORMAL",           "정상 모니터링"),
]
HEAT_LABELS = {"NORMAL": "정상", "HEAT_CAUTION": "폭염주의",
               "REST_REQUIRED": "휴식필요", "STOP_RECOMMENDED": "작업중지권고",
               "EMERGENCY_STOP": "긴급중지"}

# ---------- 사건(Incident) 규칙 기반 개선 권고 ----------
INCIDENT_LONG_EXPOSURE_SEC = 5.0    # 위험 노출 지속 시 '장시간' 판단
INCIDENT_REPEAT_WINDOW_SEC = 600    # 동일 쌍 반복 접근 판정 윈도우 (10분)
INCIDENT_VERY_CLOSE_M = 0.5        # 초근접 기준

# ---------- 기타 ----------
DB_PATH = "safeon.db"
EVENT_TYPES = {"nearmiss": "근접위험", "fall": "낙상감지", "env": "이상환경",
               "camera": "카메라감지"}
