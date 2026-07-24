# SafeON — 스마트 안전관제 시스템 데모(관제 서버 + 대시보드)

중장비-근로자 충돌 위험을 실시간 감지·경보·기록하는 해커톤 프로젝트의 SW 파트입니다.
**구현 세부 명세서 기준**으로 MQTT 통신, 3단계 위험판정(엣지 즉시판정), 아차사고 보고서(Incident),
장치상태 관제, 기상청 체감온도 기반 폭염 대응을 구현합니다.

## 위험 단계 (명세서)

| 단계 | 거리 | 시스템 동작 |
|---|---|---|
| SAFE | 3m 초과 | 정상 표시, 경보·기록 없음 |
| CAUTION | 1m 초과~3m 이하 | 황색 경보, 관제 기록 시작 |
| DANGER | 1m 이하 | 적색 경보, **아차사고 보고서 자동 생성** (near_miss=true) |
| OFFLINE | — | 미수신 지속 시 관제센터 자체 판정 |

근거: WorkSafe Victoria 3m 배제구역 권고 / 고용노동부·안전보건공단 2025 스마트 안전장치 최소 감지성능 1m.

## 아키텍처

```
[근로자 태그 W01]────┐                      ┌ 사건(EVT) 관리·장치상태·체감온도
 UWB·자이로(낙상)     ├ MQTT ─> [브로커] ──> [FastAPI 관제 서버] ─ SQLite
[중장비 장치 E01]────┘ (1초)     :1883          │
 상태·온습도(30분)·후방카메라                   └ WebSocket ──> [대시보드]
[simulator.py 목업] ── 동일 토픽·JSON ──┘
                    ── (백업) MQTT safeon/batch 또는 HTTP POST /api/batch
```

| 파일 | 역할 |
|---|---|
| `main.py` | FastAPI 서버 (WebSocket, 사건 조치, 장치 watchdog, 리포트) |
| `mqtt_ingest.py` | MQTT 수신·사건 흐름·장치 레지스트리·체감온도 처리 |
| `risk_engine.py` | 3단계 판정, 필터, 기상청 체감온도, 규칙 기반 개선 권고 |
| `db.py` | SQLite (events / incidents / env_logs, 일일·주간 통계) |
| `config.py` | 임계값·MQTT 설정 — **대회장 실측 후 이 파일만 튜닝** |
| `mosquitto.conf` | mosquitto 브로커 설정 (1순위 권장) |
| `broker.py` | 내장 MQTT 브로커 (mosquitto 설치 불가 시 백업) |
| `static/dashboard.html` | 관제 대시보드 |
| `simulator.py` | ESP32 목업 (명세서 JSON 그대로 발행) |
| `test_risk_engine.py` | 단위 테스트 8종 |

## 실행 (데모)

```bash
pip install -r requirements.txt

# 터미널 1: MQTT 브로커 — mosquitto 권장 (1순위)
mosquitto -c mosquitto.conf -v
# mosquitto가 없으면 (백업): python broker.py

# 터미널 2: 관제 서버
uvicorn main:app --host 0.0.0.0 --port 8000

# 터미널 3: ESP32 목업 시뮬레이터
python simulator.py --env-interval 20 --heat 34 --camera --fall-at 12
#   --heat 34      : 폭염 상황 (체감온도 REST_REQUIRED 이상 경보)
#   --camera       : 후진 시 후방카메라 사람감지 발행
#   --fall-at 12   : 12초 시점 낙상 이벤트
python simulator.py --noise            # 현장 무선환경(노이즈·유실·지연) 리허설
python simulator.py --offline-at 15    # 통신두절 재현 → OFFLINE 자체판정 확인
python simulator.py --batch-demo       # 통신두절→로컬저장→일괄업로드 백업 플랜 시연
```

대시보드: http://localhost:8000/

### mosquitto 설치·설정 가이드

- **Windows**: https://mosquitto.org/download/ 에서 설치 → `mosquitto.exe`를 이 프로젝트의 `mosquitto.conf`로 실행. 방화벽 허용 창에서 **개인/공용 모두 허용** (안 하면 ESP32 접속 불가).
- **macOS**: `brew install mosquitto` / **Linux**: `apt install mosquitto`
- **핵심**: mosquitto 2.x는 기본 localhost 전용 → 반드시 `-c mosquitto.conf`로 실행 (`listener 1883` + `allow_anonymous true`).
- `-v` 모드는 수신 토픽을 콘솔에 출력 — ESP32 발행 확인용 당일 디버깅 1차 도구.
- 접속 테스트: `mosquitto_sub -h <노트북IP> -t "safeon/#" -v`

## ESP32 전환

1. 관제 노트북에서 `mosquitto -c mosquitto.conf -v` + 서버 실행, 노트북 IP 확인
2. ESP32 펌웨어에서 브로커 주소를 노트북 IP로 설정
3. 아래 토픽/JSON 규약대로 발행 — **시뮬레이터와 완전히 동일하므로 서버·대시보드 수정 불필요**
4. 거리 임계값 튜닝은 `config.py`만 수정

## MQTT 토픽·JSON 규약

| 토픽 | 주기 | 내용 |
|---|---|---|
| `safeon/worker/{worker_id}/status` | 1초 | A. 근접위험 데이터 |
| `safeon/worker/{worker_id}/event` | 발생 시 | 낙상 등 `{"type":"fall","detail":...}` |
| `safeon/equip/{equipment_id}/status` | 1초 | A. 근접위험 데이터 (중장비측) |
| `safeon/equip/{equipment_id}/env` | **30분** | B. 온습도 데이터 |
| `safeon/equip/{equipment_id}/camera` | 분석 시 | C. 후방 카메라 데이터 |
| `safeon/batch` | 필요 시 | 오프라인 저장분 배열 (백업) |

### A. 근접위험 데이터

```json
{
  "timestamp": "2026-07-24T18:30:00+09:00",
  "worker_id": "W01",
  "equipment_id": "E01",
  "distance_m": 0.82,
  "risk_level": "DANGER",
  "near_miss": true,
  "sequence": 1254
}
```

- `risk_level`은 **엣지(ESP32) 즉시 판정값** (SAFE/CAUTION/DANGER). 판정 즉시 로컬 경보(LED·부저·진동·OLED) → 서버 왕복 지연 없음. 없으면 서버가 재계산(백업).
- `near_miss`: DANGER이면 true (명세서 권고 기준)

### B. 온습도 데이터 (30분 주기)

```json
{
  "timestamp": "2026-07-24T18:30:00+09:00",
  "equipment_id": "E01",
  "temperature_c": 31.4,
  "humidity_pct": 68.0,
  "sensor_status": "NORMAL"
}
```

수신 시 서버가 **기상청 여름철 체감온도**(Stull 습구온도 기반)를 계산해 폭염 단계 판정:

| 단계 | 체감온도 | 처리 (2026 고용노동부 대응지침) |
|---|---|---|
| NORMAL | 31℃ 미만 | 정상 모니터링 |
| HEAT_CAUTION | 31~33℃ | 냉방·통풍·시간조정·휴식 검토 |
| REST_REQUIRED | 33~35℃ | 2시간 이내 20분 이상 휴식 (법적 기준) |
| STOP_RECOMMENDED | 35~38℃ | 14~17시 옥외작업 중지 권고 |
| EMERGENCY_STOP | 38℃ 이상 | 긴급조치 외 옥외작업 중지 권고 |

### C. 후방 카메라 데이터

```json
{
  "timestamp": "2026-07-24T18:30:00+09:00",
  "equipment_id": "E01",
  "person_detected": true,
  "confidence": 0.91,
  "camera_status": "ONLINE"
}
```

(영상 연산은 별도 노트북에서 수행 → 결과만 이 형식으로 발행)

## 관제센터 자체 계산 (명세서 '안전관제팀 구현' 항목)

- **아차사고 보고서(Incident)**: DANGER 진입 시 자동 생성 — 사건 ID(`EVT-YYYYMMDD-0001`), 시작/종료시각, 최소 접근거리, 위험 노출지속시간, 조치상태(OPEN→ACK→CLOSED, 대시보드에서 변경), 규칙 기반 개선 권고(반복 접근·장시간 노출·초근접).
- **장치상태**: 장치 ID/유형(WORKER·EQUIPMENT·CAMERA)/배터리/마지막 수신. 별도 OFFLINE 메시지 없이도 자체 판정 — 거리장치 2초 미수신 → DEGRADED, 5초 → OFFLINE / 온습도 2회 연속 누락 → OFFLINE.

## 백업 플랜 (통신 두절 시)

ESP32는 로컬 저장 후 복구 시 일괄 업로드 (MQTT `safeon/batch` 또는 HTTP `POST /api/batch`, 형식 동일):

```jsonc
[
  {"kind":"status","timestamp":"2026-07-24T10:31:05+09:00","equipment_id":"E01",
   "worker_id":"W01","distance_m":0.95,"risk_level":"DANGER","near_miss":true,"sequence":88},
  {"kind":"env","timestamp":"2026-07-24T10:30:00+09:00","equipment_id":"E01",
   "temperature_c":34.5,"humidity_pct":70.0,"sensor_status":"NORMAL"},
  {"kind":"event","timestamp":"2026-07-24T10:32:11+09:00","worker_id":"W01","type":"fall"}
]
```

## 조회 API

| 엔드포인트 | 용도 |
|---|---|
| `WS /ws/live` | 실시간 push (근접·사건·환경·카메라·장치상태) |
| `GET /api/incidents` / `PATCH /api/incidents/{uid}?status=ACK` | 아차사고 보고서 / 조치상태 변경 |
| `GET /api/devices` | 장치상태 (ONLINE/DEGRADED/OFFLINE) |
| `GET /api/events` / `GET /api/env` / `GET /api/camera` | 이벤트 로그 / 온습도 / 카메라 |
| `GET /api/report/daily` / `GET /api/report/weekly` | 일일/주간 리포트 (자동 요약) |
| `GET /api/health` | 수신 통계 |

## 테스트

```bash
python test_risk_engine.py   # 판정·필터·체감온도·권고 8종
```
