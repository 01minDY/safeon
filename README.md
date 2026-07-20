# SafeON — 스마트 안전관제 시스템 데모(관제 서버 + 대시보드)

중장비-근로자 충돌 위험을 실시간 감지·경보·기록하는 해커톤 프로젝트의 SW 파트입니다.
ESP32 태그(HW팀)로부터 HTTP로 데이터를 수신하고, 위험도 판단 → 양방향 경보 지시 → Near-miss 기록 → 웹 대시보드를 제공합니다.

## 구조

```
[근로자/중장비 태그 ESP32] ── HTTP POST /api/report ──> [FastAPI 서버]
[simulator.py (목업)]      ── 동일 경로 ──────────────>     │
                                              위험도 엔진 · SQLite · WebSocket
                                                            │
                                                     [웹 대시보드 (/)]
```

| 파일 | 역할 |
|---|---|
| `main.py` | FastAPI 서버 (수신 API, WebSocket, 통계/리포트 API) |
| `risk_engine.py` | 위험도 판단 엔진 (거리 등급 + 장비상태/접근속도 보정, 필터, 디바운스) |
| `db.py` | SQLite 이벤트 저장/조회 |
| `config.py` | 임계값 설정 — **대회장 실측 후 이 파일만 튜닝** |
| `static/dashboard.html` | 관제 대시보드 (실시간 카드, 로그, 차트, 일일 리포트) |
| `simulator.py` | ESP32 목업 시뮬레이터 (시나리오 재생, 노이즈 모드) |
| `test_risk_engine.py` | 위험도 엔진 단위 테스트 |

## Start

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

대시보드: http://localhost:8000/

다른 터미널에서 시뮬레이터 실행:

```bash
python simulator.py            # 기본 데모 시나리오 (반복)
python simulator.py --noise    # 노이즈·패킷유실·지연 주입 (현장 조건 리허설)
python simulator.py --once     # 1회만 재생
```

테스트:

```bash
python test_risk_engine.py
```

## ESP32 연동 스펙

1초 간격으로 전송:

```
POST http://<서버IP>:8000/api/report
Content-Type: application/json

{
  "equip_id": "FORKLIFT-01",
  "worker_id": "WORKER-01",
  "distance_cm": 250,
  "equip_state": "reverse",   // idle | forward | reverse | working
  "battery": 87,              // 선택
  "seq": 1024                 // 선택
}
```

응답 (경보 지시 — 태그에서 그대로 구동):

```json
{ "risk_level": 2, "alert": { "buzzer": true, "vibration": true, "led": "orange" } }
```

- 위험 등급: 0 안전 / 1 주의 / 2 경고 / 3 위험
- 타임스탬프는 서버 시각을 사용하므로 ESP32에서 보낼 필요 없음
- curl 검증 예시:

```bash
curl -X POST http://localhost:8000/api/report -H "Content-Type: application/json" \
  -d '{"equip_id":"FORKLIFT-01","worker_id":"WORKER-01","distance_cm":90,"equip_state":"reverse"}'
```

## 위험도 판단 로직

1. 거리 기본 등급: ≥300cm 안전 / 150–300 주의 / 80–150 경고 / <80 위험
2. 장비 상태 보정: `reverse`·`working`이면 +1 등급
3. 접근 속도 보정: 60cm/s 초과 접근 시 +1 등급
4. 거리값 3샘플 이동평균 필터 + 등급 변화 시에만 이벤트 기록 (디바운스)

임계값은 전부 `config.py`에서 조정.
