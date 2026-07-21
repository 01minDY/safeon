# SafeON — 스마트 안전관제 시스템 (관제 서버 + 대시보드)

중장비-근로자 충돌 위험을 실시간 감지·경보·기록하는 해커톤 프로젝트의 SW 파트입니다.
**MQTT 기반**으로 ESP32 2대(근로자 태그 / 중장비 장치)와 통신하며,
경보는 **엣지(ESP32)에서 즉시 판정**하고 서버는 관제·기록·리포트를 담당합니다.

## 아키텍처

```
[근로자 태그 ESP32]──┐                          ┌─ 위험 판정 검증·필터·디바운스
 UWB(dwm1001)·자이로  ├─ MQTT ──> [브로커] ──> [FastAPI 관제 서버] ─ SQLite
[중장비 장치 ESP32]──┘  (1초)      :1883            │
 상태·온습도(30분)                                  └─ WebSocket ──> [대시보드]
[simulator.py 목업] ── 동일 토픽·JSON ──┘
                     ── (백업) HTTP POST /api/batch : 오프라인 저장분 일괄 업로드
```

| 파일 | 역할 |
|---|---|
| `main.py` | FastAPI 서버 (WebSocket, 조회/리포트 API, HTTP 배치 백업) |
| `mqtt_ingest.py` | MQTT 구독·토픽 처리 (status/event/env/batch) |
| `risk_engine.py` | 서버측 위험도 검증 엔진 (필터·디바운스·엣지 판정 수용) |
| `db.py` | SQLite (Near-miss·낙상·이상환경 이벤트 + 온습도 로그, 일일/주간 통계) |
| `config.py` | 임계값·MQTT 설정 — **대회장 실측 후 이 파일만 튜닝** |
| `broker.py` | 내장 MQTT 브로커 (mosquitto 없을 때) |
| `static/dashboard.html` | 관제 대시보드 |
| `simulator.py` | ESP32 목업 (동일 토픽/JSON 발행, 노이즈·낙상·배치 모드) |
| `test_risk_engine.py` | 단위 테스트 |

## 실행 (데모)

```bash
pip install -r requirements.txt

# 터미널 1: MQTT 브로커 (mosquitto 있으면 mosquitto 실행 권장)
python broker.py

# 터미널 2: 관제 서버
uvicorn main:app --host 0.0.0.0 --port 8000

# 터미널 3: ESP32 목업 시뮬레이터
python simulator.py --env-interval 20 --fall-at 12   # 온습도 20초 가속 + 12초에 낙상
python simulator.py --noise                          # 현장 무선환경(노이즈·유실·지연) 리허설
python simulator.py --batch-demo                     # 통신두절→로컬저장→일괄업로드 백업 플랜 시연
```

대시보드: http://localhost:8000/

## 대회 당일 ESP32 전환 (이것만 하면 됨)

1. 관제 노트북에서 브로커·서버 실행, 노트북 IP 확인 (예: 192.168.0.10)
2. ESP32 펌웨어에서 MQTT 브로커 주소를 노트북 IP로 설정
3. 아래 토픽/JSON 규약대로 발행 — **시뮬레이터와 완전히 동일하므로 서버·대시보드는 수정 불필요**
4. 거리 임계값 실측 튜닝은 `config.py`만 수정

## MQTT 토픽 규약 (HW팀 공유용)

| 토픽 | 주기 | 페이로드 |
|---|---|---|
| `safeon/worker/{worker_id}/status` | 1초 | `{"equip_id":"FORKLIFT-01","distance_cm":250,"risk_level":1,"battery":87,"seq":1024}` |
| `safeon/worker/{worker_id}/event` | 발생 시 | `{"type":"fall","detail":"헬멧 자이로 급가속 감지"}` |
| `safeon/equip/{equip_id}/status` | 1초 | `{"worker_id":"WORKER-01","state":"reverse","distance_cm":250,"risk_level":1}` |
| `safeon/equip/{equip_id}/env` | **30분** | `{"temp_c":31.2,"humidity_pct":58.0}` |
| `safeon/batch` | 필요 시 | 오프라인 저장 레코드 배열 (아래 참고) |

- `state`: `idle` \| `forward` \| `reverse` \| `working`
- `risk_level`: **엣지(ESP32)에서 판정한 값** (0 안전/1 주의/2 경고/3 위험). 페이로드에 없으면 서버가 재계산(백업).
- 타임스탬프는 서버 수신 시각 사용 → ESP32 시계 동기화 불필요 (배치 업로드만 `ts` 필수)

### 엣지 판정 로직 (ESP32 펌웨어와 서버 공통, config.py와 동일 값)

1. 거리: ≥300cm 안전 / 150–300 주의 / 80–150 경고 / <80 위험
2. 장비 상태 `reverse`·`working`이면 +1 등급
3. 판정 즉시 로컬 경보 (부저·LED·진동) → 서버 왕복 지연 없음

### 백업 플랜 (통신 두절 시)

Wi-Fi 실시간 전송 실패 시 ESP32는 레코드를 로컬 저장 후, 복구되면 일괄 업로드:

```jsonc
// MQTT safeon/batch 또는 HTTP POST /api/batch — 형식 동일
[
  {"kind":"status","ts":"2026-07-24T10:31:05","equip_id":"FORKLIFT-01",
   "worker_id":"WORKER-01","distance_cm":95.0,"equip_state":"reverse","risk_level":3},
  {"kind":"env","ts":"2026-07-24T10:30:00","equip_id":"FORKLIFT-01","temp_c":36.5,"humidity_pct":62.0},
  {"kind":"event","ts":"2026-07-24T10:32:11","worker_id":"WORKER-01","type":"fall"}
]
```

업로드된 데이터는 일일/주간 리포트와 통계에 자동 반영됩니다.

## 조회 API

| 엔드포인트 | 용도 |
|---|---|
| `WS /ws/live` | 실시간 push (근접·낙상·환경) |
| `GET /api/events?event_type=&min_level=&date_str=` | 이벤트 로그 |
| `GET /api/env` | 장비별 최신 온습도 |
| `GET /api/report/daily` / `GET /api/report/weekly` | 일일/주간 리포트 (자동 요약 포함) |
| `GET /api/status` | 태그 온라인/등급 현황 |
| `GET /api/health` | MQTT 수신 통계 |

## 테스트

```bash
python test_risk_engine.py
```
