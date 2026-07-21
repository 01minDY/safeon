"""SafeON 설정 — 대회장에서 실측 후 이 파일만 수정하면 됨."""

# 거리 임계값 (cm)
DIST_SAFE = 300      # 이상: 안전
DIST_CAUTION = 150   # 150~300: 주의
DIST_WARNING = 80    # 80~150: 경고, 미만: 위험

# 위험 상태로 간주하는 장비 상태 (+1 등급 보정)
HIGH_RISK_STATES = {"reverse", "working"}

# 접근 속도 보정: 최근 N개 샘플에서 접근 속도(cm/s)가 임계 초과 시 +1 등급
SPEED_WINDOW = 5          # 샘플 개수
SPEED_THRESHOLD = 60.0    # cm/s

# 거리값 이동평균 필터 윈도우
FILTER_WINDOW = 3

# Near-miss로 기록하는 최소 등급 (2 = 경고)
NEARMISS_MIN_LEVEL = 2

# 태그가 오프라인으로 간주되는 무통신 시간 (초)
OFFLINE_TIMEOUT = 5.0

# DB 경로
DB_PATH = "safeon.db"

RISK_LABELS = {0: "안전", 1: "주의", 2: "경고", 3: "위험"}
