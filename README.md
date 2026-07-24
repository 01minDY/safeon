# SafeON — 중장비·근로자 스마트 안전관제

SafeON은 중장비와 근로자의 접근 거리, 중장비 온습도, 후방 카메라
분석 결과를 MQTT로 수집해 실시간 경보·사건 관리·보고서·후속조치를
제공하는 해커톤용 관제 시스템입니다.

이 버전은 `SafeOn 구현 세부 명세서`의 데이터 계약과 판정 기준을
기준으로 구현되어 있습니다.

## 핵심 동작

```text
[근로자 거리장치] ─┐
[중장비 환경센서] ─┼─ MQTT safeon/# ─> [FastAPI 관제 서버] ─> SQLite
[후방 카메라]    ─┘                              │
                                        WebSocket 실시간 전송
                                                  │
                                          [관제 대시보드]

통신 장애 시 HTTP /api/ingest/* 또는 /api/batch 사용
```

- 거리 단계: `SAFE`(3m 초과), `CAUTION`(1m 초과~3m 이하),
  `DANGER`(1m 이하), `OFFLINE`
- `DANGER` 진입 시 `EVT-YYYYMMDD-0001` 형식의 사건 생성
- 사건별 시작/종료시각, 최소거리, 위험 노출시간, 온라인율 계산
- 위험성평가는 목업 기준인 가능성 `상(4)`·중대성 `대(3)`로 고정해
  `12점·상`으로 산정하며, 중장비는 `지게차`로 가정
- 사건마다 긴급 2건·우선 3건·정기 1건의 지게차 개선권고 자동 생성
- 조치상태 `OPEN → ACK → CLOSED` 및 개선조치 추적
- 거리장치 2초 미수신 `DEGRADED`, 5초 미수신 `OFFLINE`
- 온습도장치 예정 전송시각 2회 연속 누락 시 `OFFLINE`
- 명세서의 여름철 체감온도 공식과 5단계 대응기준 적용
- 후방 카메라의 사람 감지 여부·신뢰도·상태 수신

## 센서 데이터 계약

### 근접위험

토픽: `safeon/proximity/{worker_id}`

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

관제 서버는 센서의 `risk_level`을 검증하고 `distance_m` 기준으로 최종
단계를 계산합니다. `sequence`가 이전 값과 같거나 작으면 중복
메시지로 제외합니다.

### 온습도

토픽: `safeon/environment/{equipment_id}` (운영 주기 30분)

```json
{
  "timestamp": "2026-07-24T18:30:00+09:00",
  "equipment_id": "E01",
  "temperature_c": 31.4,
  "humidity_pct": 68.0,
  "sensor_status": "NORMAL"
}
```

### 후방 카메라

토픽: `safeon/camera/{equipment_id}`

```json
{
  "timestamp": "2026-07-24T18:30:00+09:00",
  "equipment_id": "E01",
  "person_detected": true,
  "confidence": 0.91,
  "camera_status": "ONLINE"
}
```

### 배치 백업

`safeon/batch` 또는 `POST /api/batch`로 위 레코드에
`kind: proximity | environment | camera`를 추가한 배열을 전송합니다.

## 실행

Python 3.11 이상을 권장합니다.

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

터미널 1 — MQTT 브로커:

```bash
mosquitto -c mosquitto.conf -v

# Mosquitto가 없을 때 개발용 내장 브로커
python broker.py
```

터미널 2 — 관제 서버:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

터미널 3 — 센서 시뮬레이터:

```bash
python simulator.py --once --env-interval 10
python simulator.py --once --heat
python simulator.py --batch-demo

# MQTT 없이 HTTP 백업경로로 시연
python simulator.py --http --once
```

대시보드: <http://localhost:8000/>

API 문서: <http://localhost:8000/docs>

## 주요 API

| API | 기능 |
|---|---|
| `POST /api/ingest/proximity` | 근접위험 HTTP 백업 수신 |
| `POST /api/ingest/environment` | 온습도 HTTP 백업 수신 |
| `POST /api/ingest/camera` | 후방 카메라 HTTP 백업 수신 |
| `POST /api/batch` | 오프라인 저장분 일괄 수신 |
| `GET /api/live` | 최신 센서 상태 |
| `GET /api/incidents` | 위험사건 조회 |
| `PATCH /api/incidents/{event_id}/action` | OPEN/ACK/CLOSED 변경 |
| `GET /api/devices` | 장치상태·온라인율 조회 |
| `GET /api/report/daily` | 일일 안전운영 보고서 |
| `GET /api/report/weekly` | 최근 7일 보고서 |
| `GET/POST /api/actions` | 개선조치 조회·등록 |
| `GET /api/recommendations/latest` | 최신 사건의 평가·개선권고 조회 |
| `GET /api/recommendations/{event_id}` | 사건별 평가·개선권고 조회 |
| `POST /api/incidents/{event_id}/recommendations` | 고정 목업 기준 권고 생성 |
| `PATCH /api/incidents/{event_id}/recommendations/approval` | 권고 승인상태 변경 |
| `WS /ws/live` | 대시보드 실시간 이벤트 |

## 테스트

```bash
python test_risk_engine.py
```

거리 경계값, 체감온도 단계, 근로자 장치 출력, 사건 생명주기, 장치
오프라인 판정, 조치상태 연동, 고정 위험성평가 및 6개 지게차 권고
생성을 검증합니다.

## 구성

| 파일 | 역할 |
|---|---|
| `models.py` | 명세서 기반 Pydantic 센서 모델 |
| `risk_engine.py` | 거리·체감온도·경보 계산 |
| `recommendation_engine.py` | 고정 위험성평가·지게차 개선권고 생성 |
| `mqtt_ingest.py` | MQTT/HTTP 공통 수신 및 중복 제거 |
| `db.py` | 사건·환경·카메라·장치·개선조치 저장 |
| `main.py` | FastAPI, WebSocket, 보고서 API |
| `static/dashboard.html` | 실시간 관제·보고서·개선조치 SPA |
| `simulator.py` | 명세서 형식 센서 시뮬레이터 |

## 배포 전 보안 주의

동봉된 `mosquitto.conf`는 행사장 LAN 시연을 위해 익명 접속을
허용합니다. 실제 현장에서는 MQTT 계정·ACL·TLS와 API 인증, 방화벽,
데이터 보존·백업 정책을 반드시 추가해야 합니다.
