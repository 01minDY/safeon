"""ESP32 목업 시뮬레이터 (MQTT) — 구현 세부 명세서의 토픽/JSON과 동일하게 발행.

가상 장치:
  [1] 근로자 태그 W01     → safeon/worker/W01/status, .../event(낙상)
  [2] 중장비 장치 E01     → safeon/equip/E01/status, .../env(온습도), .../camera(후방)

실행 예:
    python simulator.py                          # 기본 데모 (반복)
    python simulator.py --noise                  # 노이즈·유실·지연 주입
    python simulator.py --env-interval 20        # 온습도 20초 주기 (실제 30분의 가속판)
    python simulator.py --heat 34                # 기온 34°C 폭염 상황 (체감온도 경보 유발)
    python simulator.py --fall-at 12             # 12초 시점 낙상 이벤트
    python simulator.py --camera                 # 후진 시 후방카메라 사람감지 발행
    python simulator.py --offline-at 15          # 15초 시점 통신두절 재현 (OFFLINE 판정 확인)
    python simulator.py --batch-demo             # 오프라인 저장→일괄 업로드 백업 플랜 시연
"""
import argparse
import json
import random
import time
import urllib.request
from datetime import datetime, timezone, timedelta

import paho.mqtt.client as mqtt

import config
import risk_engine

WORKER = "W01"
EQUIP = "E01"
KST = timezone(timedelta(hours=9))

# 시나리오: (구간 지속시간(s), 시작거리(m), 끝거리(m), 장비상태)
SCENARIO = [
    (4, 5.0, 5.0, "idle"),      # SAFE
    (5, 5.0, 2.0, "forward"),   # 접근 → CAUTION (3m 진입)
    (4, 2.0, 1.2, "reverse"),   # 후진 접근
    (4, 1.2, 0.6, "reverse"),   # DANGER 진입 (1m 이하)
    (5, 0.6, 4.0, "forward"),   # 이탈 → 사건 종료
    (4, 4.0, 4.0, "idle"),
]
TOTAL = sum(s[0] for s in SCENARIO)


def now_iso():
    return datetime.now(KST).isoformat(timespec="seconds")


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


def status_payload(dist_m, seq):
    """명세서 A. 근접위험 데이터 — ESP32 펌웨어와 동일 형식."""
    level = risk_engine.classify(dist_m)   # 엣지 즉시 판정
    return {
        "timestamp": now_iso(),
        "worker_id": WORKER,
        "equipment_id": EQUIP,
        "distance_m": round(dist_m, 2),
        "risk_level": level,
        "near_miss": level == "DANGER",
        "sequence": seq,
        "battery": random.randint(70, 100),
    }, level


def run_live(a):
    c = make_client(a.broker, a.port)
    seq = 0
    last_env = 0.0
    print(f"브로커 {a.broker}:{a.port} 연결. 시나리오 재생 (Ctrl+C 종료)")
    while True:
        t0 = time.time()
        fall_sent = False
        offline_until = None
        while time.time() - t0 < TOTAL:
            now = time.time() - t0
            dist, state = scenario_at(now)
            if dist is None:
                break

            # 통신두절 재현: --offline-at N 시점부터 5초간 발행 중단
            if a.offline_at is not None and a.offline_at <= now < a.offline_at + 6:
                if offline_until is None:
                    offline_until = now + 6
                    print(f"t={now:5.1f}s  !! 통신두절 시작 (6초) — 관제 OFFLINE 판정 확인")
                time.sleep(a.interval)
                continue

            d = dist
            if a.noise:
                if random.random() < 0.05:
                    time.sleep(a.interval); continue
                d *= random.uniform(0.92, 1.08)
                if random.random() < 0.3:
                    time.sleep(random.uniform(0, 0.3))

            seq += 1
            payload, level = status_payload(d, seq)

            # [1] 근로자 태그 + [2] 중장비 장치 (동일 측정, 양측 발행)
            c.publish(f"{config.MQTT_BASE}/worker/{WORKER}/status", json.dumps(payload))
            c.publish(f"{config.MQTT_BASE}/equip/{EQUIP}/status", json.dumps(payload))
            print(f"t={now:5.1f}s  {d:5.2f}m  {state:8s} -> {level}"
                  + ("  [near_miss]" if payload["near_miss"] else ""))

            # [3] 온습도 (30분 주기의 가속판)
            if time.time() - last_env >= a.env_interval:
                last_env = time.time()
                temp = round(random.uniform(a.heat - 1, a.heat + 1), 1)
                hum = round(random.uniform(55, 75), 1)
                c.publish(f"{config.MQTT_BASE}/equip/{EQUIP}/env", json.dumps({
                    "timestamp": now_iso(), "equipment_id": EQUIP,
                    "temperature_c": temp, "humidity_pct": hum,
                    "sensor_status": "NORMAL"}))
                ac = risk_engine.apparent_temp(temp, hum)
                print(f"        [env] {temp}°C/{hum}% → 체감 {ac:.1f}°C "
                      f"{risk_engine.heat_stage(ac)[0]}")

            # [4] 후방 카메라: 후진 중 사람 감지 (옵션)
            if a.camera and state == "reverse" and random.random() < 0.5:
                c.publish(f"{config.MQTT_BASE}/equip/{EQUIP}/camera", json.dumps({
                    "timestamp": now_iso(), "equipment_id": EQUIP,
                    "person_detected": True,
                    "confidence": round(random.uniform(0.75, 0.98), 2),
                    "camera_status": "ONLINE"}))
                print("        [camera] 후방 사람 감지 발행")

            # [5] 낙상 이벤트 (옵션)
            if a.fall_at is not None and not fall_sent and now >= a.fall_at:
                fall_sent = True
                c.publish(f"{config.MQTT_BASE}/worker/{WORKER}/event", json.dumps({
                    "timestamp": now_iso(), "type": "fall",
                    "detail": "헬멧 자이로 급가속 감지"}))
                print("        [fall] 낙상 이벤트 발행!")

            time.sleep(a.interval)
        if a.once:
            break
        print("--- 시나리오 반복 ---")
    c.loop_stop()


def run_batch_demo(a):
    """백업 플랜: 통신 두절 동안 로컬 저장 → 복구 후 일괄 업로드."""
    print("통신 두절 가정 — 센서 데이터 로컬 저장 중...")
    records = []
    base = datetime.now(KST) - timedelta(hours=1)
    t = 0.0
    seq = 0
    while t < TOTAL:
        dist, state = scenario_at(t)
        if dist is not None:
            seq += 1
            level = risk_engine.classify(dist)
            records.append({
                "kind": "status",
                "timestamp": (base + timedelta(seconds=t)).isoformat(timespec="seconds"),
                "worker_id": WORKER, "equipment_id": EQUIP,
                "distance_m": round(dist, 2), "risk_level": level,
                "near_miss": level == "DANGER", "sequence": seq})
        t += 1.0
    records.append({"kind": "env", "timestamp": base.isoformat(timespec="seconds"),
                    "equipment_id": EQUIP, "temperature_c": 34.5,
                    "humidity_pct": 70.0, "sensor_status": "NORMAL"})
    print(f"로컬 저장 레코드 {len(records)}건. 통신 복구 → 일괄 업로드...")

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
    p.add_argument("--server", default="http://localhost:8000")
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--env-interval", type=float, default=30.0,
                   help="온습도 주기(초). 실제 30분(1800)의 데모 가속판")
    p.add_argument("--heat", type=float, default=30.0, help="기온(°C). 34 이상이면 폭염 경보 유발")
    p.add_argument("--noise", action="store_true")
    p.add_argument("--camera", action="store_true", help="후진 시 후방카메라 감지 발행")
    p.add_argument("--fall-at", type=float, default=None)
    p.add_argument("--offline-at", type=float, default=None, help="N초 시점 6초간 통신두절 재현")
    p.add_argument("--once", action="store_true")
    p.add_argument("--batch-demo", action="store_true")
    p.add_argument("--batch-http", action="store_true")
    a = p.parse_args()
    try:
        if a.batch_demo or a.batch_http:
            run_batch_demo(a)
        else:
            run_live(a)
    except KeyboardInterrupt:
        print("\n종료")
