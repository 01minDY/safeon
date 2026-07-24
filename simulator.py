"""SafeON specification simulator.

Publishes canonical proximity, environment and rear-camera messages.  The same
payloads can be sent through HTTP when MQTT is unavailable.
"""

from __future__ import annotations

import argparse
import json
import random
import time
import urllib.request
from datetime import datetime

import paho.mqtt.client as mqtt

import config
import risk_engine


WORKER_ID = "W01"
EQUIPMENT_ID = "E01"

SCENARIO = [
    (5, 4.5, 4.0),   # SAFE
    (7, 3.0, 1.3),   # CAUTION
    (7, 1.0, 0.45),  # DANGER
    (5, 0.45, 1.8),  # DANGER -> CAUTION
    (5, 1.8, 4.2),   # CAUTION -> SAFE
]
TOTAL_SECONDS = sum(segment[0] for segment in SCENARIO)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def scenario_at(elapsed: float):
    cursor = 0.0
    for duration, start, end in SCENARIO:
        if cursor <= elapsed < cursor + duration:
            ratio = (elapsed - cursor) / duration
            return start + (end - start) * ratio
        cursor += duration
    return SCENARIO[-1][-1]


def mqtt_client(host, port):
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"safeon-sim-{random.randint(1000, 9999)}",
    )
    client.connect(host, port, keepalive=30)
    client.loop_start()
    return client


def http_post(server: str, path: str, payload):
    request = urllib.request.Request(
        f"{server.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def publish(client, topic: str, payload: dict):
    client.publish(topic, json.dumps(payload, ensure_ascii=False))


def send(args, client, kind, payload):
    if args.http:
        path = {
            "proximity": "/api/ingest/proximity",
            "environment": "/api/ingest/environment",
            "camera": "/api/ingest/camera",
        }[kind]
        http_post(args.server, path, payload)
    else:
        topic = {
            "proximity": f"{config.MQTT_BASE}/proximity/{WORKER_ID}",
            "environment": f"{config.MQTT_BASE}/environment/{EQUIPMENT_ID}",
            "camera": f"{config.MQTT_BASE}/camera/{EQUIPMENT_ID}",
        }[kind]
        publish(client, topic, payload)


def live_demo(args):
    client = None if args.http else mqtt_client(args.broker, args.port)
    sequence = 0
    last_environment = 0.0
    last_camera = 0.0
    try:
        while True:
            started = time.time()
            while time.time() - started < TOTAL_SECONDS:
                elapsed = time.time() - started
                distance = scenario_at(elapsed)
                if args.noise:
                    if random.random() < 0.05:
                        time.sleep(args.interval)
                        continue
                    distance *= random.uniform(0.96, 1.04)
                distance = round(max(0.0, distance), 2)
                level = risk_engine.risk_level_for_distance(distance)
                sequence += 1
                proximity = {
                    "timestamp": now_iso(),
                    "worker_id": WORKER_ID,
                    "equipment_id": EQUIPMENT_ID,
                    "distance_m": distance,
                    "risk_level": level,
                    "near_miss": level == "DANGER",
                    "sequence": sequence,
                    "battery_pct": max(65, 96 - sequence // 120),
                    "firmware_version": "2.0.0-sim",
                }
                send(args, client, "proximity", proximity)

                if time.time() - last_camera >= args.camera_interval:
                    last_camera = time.time()
                    camera = {
                        "timestamp": now_iso(),
                        "equipment_id": EQUIPMENT_ID,
                        "person_detected": distance <= 3.0,
                        "confidence": round(
                            random.uniform(0.86, 0.98) if distance <= 3 else 0.12,
                            2,
                        ),
                        "camera_status": "ONLINE",
                        "firmware_version": "1.4.0-sim",
                    }
                    send(args, client, "camera", camera)

                if time.time() - last_environment >= args.env_interval:
                    last_environment = time.time()
                    temperature = random.uniform(29, 32)
                    if args.heat:
                        temperature += 7
                    environment = {
                        "timestamp": now_iso(),
                        "equipment_id": EQUIPMENT_ID,
                        "temperature_c": round(temperature, 1),
                        "humidity_pct": round(random.uniform(58, 74), 1),
                        "sensor_status": "NORMAL",
                        "firmware_version": "1.2.0-sim",
                    }
                    send(args, client, "environment", environment)

                print(
                    f"{proximity['timestamp']}  {distance:>4.2f}m  "
                    f"{level:<7} seq={sequence}"
                )
                time.sleep(args.interval)
            if args.once:
                break
            print("--- 시나리오 반복 ---")
    finally:
        if client is not None:
            client.loop_stop()
            client.disconnect()


def batch_demo(args):
    records = []
    sequence = 0
    base = time.time() - 300
    for second in range(TOTAL_SECONDS):
        distance = round(scenario_at(second), 2)
        level = risk_engine.risk_level_for_distance(distance)
        sequence += 1
        records.append(
            {
                "kind": "proximity",
                "timestamp": datetime.fromtimestamp(base + second)
                .astimezone()
                .isoformat(timespec="seconds"),
                "worker_id": WORKER_ID,
                "equipment_id": EQUIPMENT_ID,
                "distance_m": distance,
                "risk_level": level,
                "near_miss": level == "DANGER",
                "sequence": sequence,
            }
        )
    records.extend(
        [
            {
                "kind": "environment",
                "timestamp": now_iso(),
                "equipment_id": EQUIPMENT_ID,
                "temperature_c": 36.2,
                "humidity_pct": 72.0,
                "sensor_status": "NORMAL",
            },
            {
                "kind": "camera",
                "timestamp": now_iso(),
                "equipment_id": EQUIPMENT_ID,
                "person_detected": True,
                "confidence": 0.93,
                "camera_status": "ONLINE",
            },
        ]
    )
    if args.http:
        result = http_post(args.server, "/api/batch", records)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        client = mqtt_client(args.broker, args.port)
        publish(client, f"{config.MQTT_BASE}/batch", records)
        time.sleep(1)
        client.loop_stop()
        client.disconnect()
        print(f"MQTT 배치 {len(records)}건 전송 완료")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker", default=config.MQTT_HOST)
    parser.add_argument("--port", type=int, default=config.MQTT_PORT)
    parser.add_argument("--server", default="http://localhost:8000")
    parser.add_argument("--http", action="store_true", help="MQTT 대신 HTTP 사용")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--env-interval", type=float, default=20.0)
    parser.add_argument("--camera-interval", type=float, default=2.0)
    parser.add_argument("--noise", action="store_true")
    parser.add_argument("--heat", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--batch-demo", action="store_true")
    arguments = parser.parse_args()
    try:
        if arguments.batch_demo:
            batch_demo(arguments)
        else:
            live_demo(arguments)
    except KeyboardInterrupt:
        print("\n시뮬레이터 종료")
