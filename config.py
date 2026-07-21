"""SafeON 설정 — 대회장에서 실측 후 이 파일만 수정하면 됨."""

# ---------- MQTT ----------
MQTT_HOST = "localhost"     # 브로커 주소 (대회장: 관제 노트북 IP)
MQTT_PORT = 1883
MQTT_BASE = "safeon"        # 토픽 프리픽스

# 구독 토픽 (서버)
#   safeon/worker/{worker_id}/status  근로자 태그 상태 (1초 주기)
#   safeon/worker/{worker_id}/event   낙상 등 이벤트 (발생 시)
#   safeon/equip/{equip_id}/status    중장비 장치 상태 (1초 주기)
#   safeon/equip/{equip_id}/env       온습도 (30분 주기)
#   safeon/batch                      오프라인 저장 데이터 일괄 업로드 (백업 플랜)

# ---------- 거리 임계값 (cm) — 엣지(ESP32)와 동일 값 유지 ----------
DIST_SAFE = 300      # 이상: 안전
DIST_CAUTION = 150   # 150~300: 주의
DIST_WARNING = 80    # 80~150: 경고, 미만: 위험

# 위험 상태로 간주하는 장비 상태 (+1 등급 보정)
HIGH_RISK_STATES = {"reverse", "working"}

# 접근 속도 보정
SPEED_WINDOW = 5
SPEED_THRESHOLD = 60.0    # cm/s

# 거리값 이동평균 필터 윈도우
FILTER_WINDOW = 3

# Near-miss로 기록하는 최소 등급 (2 = 경고)
NEARMISS_MIN_LEVEL = 2

# ---------- 환경 센서 (중장비 온습도, 30분 주기) ----------
ENV_INTERVAL_SEC = 30 * 60   # 실제 주기: 30분 (시뮬레이터는 --env-interval로 가속)
TEMP_MAX = 35.0              # 초과 시 이상온도 경보 (폭염)
TEMP_MIN = -5.0              # 미만 시 이상온도 경보 (한파)
HUMIDITY_MAX = 90.0

# ---------- 기타 ----------
OFFLINE_TIMEOUT = 5.0   # 태그 무통신 오프라인 간주 (초)
DB_PATH = "safeon.db"

RISK_LABELS = {0: "안전", 1: "주의", 2: "경고", 3: "위험"}
EVENT_TYPES = {"nearmiss": "근접위험", "fall": "낙상감지", "env": "이상환경"}
