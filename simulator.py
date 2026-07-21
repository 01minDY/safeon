"""ESP32 목업 시뮬레이터 (MQTT) — 실제 ESP32와 동일한 토픽/JSON으로 발행.

가상 장치 2대:
  [1] 근로자 태그 (WORKER-01)  → safeon/worker/WORKER-01/status, .../event(낙상)
  [2] 중장비 장치 (FORKLIFT-01) → safeon/equip/FORKLIFT-01/status, .../env(온습도)

ESP32는 엣지에서 위험등급을 즉시 판정해 경보를 울리고, 판정 결과를 함께 발행한다.
이 시뮬레이터도 동일하게 엣지 판정을 수행한다 (config.py와 동일 임계값).

실행 예:
    python simulator.py                          # 기본 데모 (반복)
    python simulator.py --noise                  # 노이즈·유실·지연 주입
    python simulator.py --env-interval 20        # 온습도 20초 주기 (실제 30분의 가속판)
    python simulator.py --fall-at 12             # 12초 시점에 낙상 이벤트 발생
    python simulator.py --batch-demo             # 오프라인 저장→일괄 업로드 백업 플랜 시연
    python simulator.py --broker 192.168.0.10
"""
import argparse
import json
import random
import time
import urllib.request

import paho.mqtt.client as mqtt

import config

WORKER = "WORKER-01"
EQUIP = "FORKLIFT-01"

# 시나리오: (구간 지속시간(s), 시작거리, 끝거리, 장비상태)
SCENARIO = [
    (4, 500, 500, "idle"),      # 안전 대기
    (5, 500, 200, "forward"),   # 접근 → 주의
    (4, 200, 100, "reverse"),   # 후진 중 접근 → 경고
    (3, 100, 60, "reverse"),    # 위험 진입
    (5, 60, 400, "forward"),    # 이탈
    (4, 400, 400, "idle"),
]
TOTAL = sum(s[0] for s in SCENARIO)


def edge_risk(distance_cm: float, state: str) -> int:
    """엣지(ESP32)에서 수행하는 즉시 판정 — 펌웨어와 동일 로직."""
    if distance_cm >= config.DIST_SAFE:
        lv = 0
    elif distance_cm >= config.DIST_CAUTION:
        lv = 1
    elif distance_cm >= config.DIST_WARNING:
        lv = 2
    else:
        lv = 3
    if lv > 0 and state in config.HIGH_RISK_STATES:
        lv = min(lv + 1, 3)
    return lv


def scenario_at(t: float):
    acc = 0
    for dur, d0, d1, state in SCENARIO:
        if acc <= t < acc + dur:
            return d0 + (d1 - d0) * (t - acc) / dur, state
        acc += dur
    return None, None


def make_client(broker, port):
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                    client_id=f"sim-{random.randint(1000,9999)}")
    c.connect(broker, port, keepalive=30)
    c.loop_start()
    return c


def run_live(a):
    c = make_client(a.broker, a.port)
    seq = 0
    last_env = 0.0
    fall_sent = False
    print(f"브로커 {a.broker}:{a.port} 연결. 시나리오 재생 시작 (Ctrl+C 종료)")
    while True:
        t0 = time.time()
        fall_sent = False
        while time.time() - t0 < TOTAL:
            now = time.time() - t0
            dist, state = scenario_at(now)
            if dist is None:
                break

            # 노이즈 모드: 현장 무선 환경 재현
            d = dist
            if a.noise:
                if random.random() < 0.05:
                    time.sleep(a.interval)
                    continue                      # 5% 패킷 유실
                d *= random.uniform(0.9, 1.1)     # ±10% 거리 노이즈
                if random.random() < 0.3:
                    time.sleep(random.uniform(0, 0.3))  # 전송 지연

            seq += 1
            lv = edge_risk(d, state)

            # [1] 근로자 태그: 거리 + 엣지 판정 결과
            c.publish(f"{config.MQTT_BASE}/worker/{WORKER}/status", json.dumps({
                "equip_id": EQUIP, "distance_cm": round(d, 1),
                "risk_level": lv, "battery": random.randint(70, 100), "seq": seq,
            }))
            # [2] 중장비 장치: 장비 상태
            c.publish(f"{config.MQTT_BASE}/equip/{EQUIP}/status", json.dumps({
                "worker_id": WORKER, "state": state,
                "distance_cm": round(d, 1), "risk_level": lv,
            }))
            print(f"t={now:5.1f}s  {d:7.1f}cm  {state:8s} -> 엣지판정 level {lv}")

            # [3] 온습도: 30분 주기 (가속판: --env-interval)
            if time.time() - last_env >= a.env_interval:
                last_env = time.time()
                temp = round(random.uniform(28, 33) + (5 if a.heat else 0), 1)
                hum = round(random.uniform(50, 70), 1)
                c.publish(f"{config.MQTT_BASE}/equip/{EQUIP}/env", json.dumps({
                    "temp_c": temp, "humidity_pct": hum,
                }))
                print(f"        [env] {temp}°C / {hum}%")

            # [4] 낙상 이벤트 (옵션)
            if a.fall_at is not None and not fall_sent and now >= a.fall_at:
                fall_sent = True
                c.publish(f"{config.MQTT_BASE}/worker/{WORKER}/event", json.dumps({
                    "type": "fall", "detail": "헬멧 자이로 급가속 감지",
                }))
                print("        [fall] 낙상 이벤트 발행!")

            time.sleep(a.interval)
        if a.once:
            break
        print("--- 시나리오 반복 ---")
    c.loop_stop()


def run_batch_demo(a):
    """백업 플랜 시연: 통신 두절 동안 로컬 저장했다 복구 후 일괄 업로드."""
    print("통신 두절 상황 가정 — 센서 데이터 로컬 저장 중...")
    records = []
    base = time.time() - 3600  # 1시간 전 데이터로 가정
    t = 0.0
    while t < TOTAL:
        dist, state = scenario_at(t)
        if dist is not None:
            lv = edge_risk(dist, state)
            ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(base + t))
            records.append({"kind": "status", "ts": ts, "equip_id": EQUIP,
                            "worker_id": WORKER, "distance_cm": round(dist, 1),
                            "equip_state": state, "risk_level": lv})
        t += 1.0
    records.append({"kind": "env", "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(base)),
                    "equip_id": EQUIP, "temp_c": 36.5, "humidity_pct": 62.0})
    print(f"로컬 저장 레코드 {len(records)}건 생성. 통신 복구 → 일괄 업로드...")

    # 방법 1: MQTT safeon/batch  /  방법 2: HTTP POST /api/batch (둘 다 지원)
    if a.batch_http:
        req = urllib.request.Request(
            f"{a.server}/api/batch", data=json.dumps(records).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as res:
            print("HTTP 업로드 결과:", res.read().decode())
    else:
        c = make_client(a.broker, a.port)
        c.publish(f"{config.MQTT_BASE}/batch", json.dumps(records))
        time.sleep(1)
        c.loop_stop()
        print("MQTT 배치 업로드 완료 → 대시보드 리포트에서 확인하세요.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--broker", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--server", default="http://localhost:8000", help="HTTP 배치 업로드용")
    p.add_argument("--interval", type=float, default=1.0, help="status 발행 주기(초)")
    p.add_argument("--env-interval", type=float, default=30.0,
                   help="온습도 주기(초). 실제 30분(1800)의 데모 가속판")
    p.add_argument("--noise", action="store_true", help="노이즈·유실·지연 주입")
    p.add_argument("--heat", action="store_true", help="폭염 상황 (이상온도 경보 유발)")
    p.add_argument("--fall-at", type=float, default=None, help="N초 시점 낙상 이벤트")
    p.add_argument("--once", action="store_true", help="1회만 재생")
    p.add_argument("--batch-demo", action="store_true", help="오프라인→일괄 업로드 백업 플랜 시연")
    p.add_argument("--batch-http", action="store_true", help="배치를 MQTT 대신 HTTP로 업로드")
    a = p.parse_args()
    try:
        if a.batch_demo or a.batch_http:
            run_batch_demo(a)
        else:
            run_live(a)
    except KeyboardInterrupt:
        print("\n종료")
