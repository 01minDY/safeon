"""SafeON 30-second fallback demonstration simulator.

The default mode sends deterministic mock sensor readings directly to the
running FastAPI server over HTTP.  It is designed for a screen-recorded demo
when field MQTT communication or physical sensors are unavailable.

Demo timeline:
    0-8s   SAFE     worker is outside the caution zone
    8-16s  CAUTION  worker approaches the equipment
    16-22s DANGER   a near-miss incident is created
    22-26s CAUTION  worker begins moving away; the incident is closed
    26-30s SAFE     a stable safe distance is restored
"""

from __future__ import annotations

import argparse
import json
import math
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime

try:
    import paho.mqtt.client as mqtt
except ImportError:  # HTTP demo mode does not require an MQTT client.
    mqtt = None

import config
import risk_engine


WORKER_ID = "W01"
EQUIPMENT_ID = "E01"
DEMO_DURATION_SECONDS = 30.0


@dataclass(frozen=True)
class DistanceKeyframe:
    second: float
    distance_m: float


# Continuous distance curve with enough dwell time in every risk stage.
DISTANCE_KEYFRAMES = (
    DistanceKeyframe(0.0, 4.80),
    DistanceKeyframe(4.0, 4.30),
    DistanceKeyframe(7.0, 3.20),
    DistanceKeyframe(8.0, 2.80),
    DistanceKeyframe(12.0, 2.00),
    DistanceKeyframe(15.0, 1.15),
    DistanceKeyframe(16.0, 0.92),
    DistanceKeyframe(19.0, 0.58),
    DistanceKeyframe(21.0, 0.78),
    DistanceKeyframe(22.0, 1.25),
    DistanceKeyframe(25.0, 2.75),
    DistanceKeyframe(26.0, 3.25),
    DistanceKeyframe(30.0, 4.60),
)

STAGE_KO = {
    "SAFE": "안전",
    "CAUTION": "주의",
    "DANGER": "위험",
    "OFFLINE": "통신장애",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _smoothstep(value: float) -> float:
    """Ease motion so the simulated worker does not change speed abruptly."""
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def scenario_at(elapsed: float) -> float:
    """Return a continuous, realistic distance for a demo timestamp."""
    elapsed = max(0.0, min(DEMO_DURATION_SECONDS, float(elapsed)))
    for start, end in zip(DISTANCE_KEYFRAMES, DISTANCE_KEYFRAMES[1:]):
        if start.second <= elapsed <= end.second:
            progress = (elapsed - start.second) / (end.second - start.second)
            eased = _smoothstep(progress)
            return start.distance_m + (
                end.distance_m - start.distance_m
            ) * eased
    return DISTANCE_KEYFRAMES[-1].distance_m


def environment_at(elapsed: float, heat: bool = False) -> tuple[float, float]:
    """Return slowly changing summer-site temperature and humidity."""
    temperature = 30.6 + elapsed * 0.018 + math.sin(elapsed / 4.2) * 0.12
    humidity = 63.5 + elapsed * 0.055 + math.sin(elapsed / 5.5) * 0.35
    if heat:
        temperature += 5.8
    return round(temperature, 1), round(humidity, 1)


def camera_confidence(distance_m: float) -> float:
    """Correlate camera confidence with the worker's approach distance."""
    if distance_m > 3.2:
        return 0.12
    confidence = 0.72 + (3.2 - distance_m) / 3.2 * 0.25
    return round(min(0.98, confidence), 2)


def mqtt_client(host: str, port: int):
    if mqtt is None:
        raise RuntimeError(
            "MQTT 모드는 paho-mqtt가 필요합니다. 기본 HTTP 모드를 사용하세요."
        )
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"safeon-demo-{random.randint(1000, 9999)}",
    )
    client.connect(host, port, keepalive=30)
    client.loop_start()
    return client


def _http_request(
    server: str,
    path: str,
    *,
    payload: dict | list[dict] | None = None,
    attempts: int = 3,
):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{server.rstrip('/')}{path}",
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "SafeON-Demo-Simulator/3.0",
        },
        method="GET" if payload is None else "POST",
    )
    last_error = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.15 * (attempt + 1))
    raise ConnectionError(
        f"{server.rstrip('/')}{path} 연결 실패: {last_error}"
    ) from last_error


def http_post(server: str, path: str, payload: dict | list[dict]):
    return _http_request(server, path, payload=payload)


def publish(client, topic: str, payload: dict | list[dict]):
    info = client.publish(
        topic,
        json.dumps(payload, ensure_ascii=False),
        qos=1,
    )
    info.wait_for_publish(timeout=2)


def send(args, client, kind: str, payload: dict):
    if args.transport == "http":
        path = {
            "proximity": "/api/ingest/proximity",
            "environment": "/api/ingest/environment",
            "camera": "/api/ingest/camera",
        }[kind]
        return http_post(args.server, path, payload)

    topic_id = (
        payload["worker_id"] if kind == "proximity" else payload["equipment_id"]
    )
    topic = f"{config.MQTT_BASE}/{kind}/{topic_id}"
    publish(client, topic, payload)
    return {}


def prepare_transport(args):
    if args.transport == "http":
        health = _http_request(args.server, "/api/health", attempts=2)
        print(
            f"[연결] {args.server} · 서버 상태 {health.get('status', 'unknown')}"
        )
        print("[모드] 실제 장치 없이 HTTP 목업 데이터로 시연합니다.")
        return None

    client = mqtt_client(args.broker, args.port)
    print(f"[연결] MQTT {args.broker}:{args.port}")
    return client


def _proximity_payload(
    *,
    timestamp: str,
    sequence: int,
    distance_m: float,
    level: str,
    elapsed: float,
) -> dict:
    return {
        "timestamp": timestamp,
        "worker_id": WORKER_ID,
        "equipment_id": EQUIPMENT_ID,
        "distance_m": distance_m,
        "risk_level": level,
        "near_miss": level == "DANGER",
        "sequence": sequence,
        "battery_pct": round(max(86.0, 94.0 - elapsed * 0.025), 1),
        "equipment_battery_pct": round(
            max(82.0, 91.0 - elapsed * 0.018), 1
        ),
        "firmware_version": "3.0.0-demo",
    }


def _environment_payload(
    *, timestamp: str, elapsed: float, heat: bool
) -> dict:
    temperature, humidity = environment_at(elapsed, heat)
    return {
        "timestamp": timestamp,
        "equipment_id": EQUIPMENT_ID,
        "temperature_c": temperature,
        "humidity_pct": humidity,
        "sensor_status": "NORMAL",
        "firmware_version": "1.3.0-demo",
    }


def _camera_payload(
    *, timestamp: str, distance_m: float
) -> dict:
    return {
        "timestamp": timestamp,
        "equipment_id": EQUIPMENT_ID,
        "person_detected": distance_m <= 3.2,
        "confidence": camera_confidence(distance_m),
        "camera_status": "ONLINE",
        "firmware_version": "1.5.0-demo",
    }


def run_once(args, client, cycle: int = 1) -> str | None:
    """Run one 30-second scenario and return the created event ID."""
    started = time.monotonic()
    next_tick = started
    next_environment = 0.0
    next_camera = 0.0
    previous_level = None
    event_id = None
    sequence_base = int(time.time() * 1000) + cycle * 100_000
    sample_index = 0
    randomizer = random.Random(args.seed + cycle)

    print()
    print("SafeON 30초 시연 시작")
    print("안전 → 주의 → 위험(사건 생성) → 주의 → 안전")
    print("-" * 66)

    while True:
        elapsed = time.monotonic() - started
        if elapsed >= DEMO_DURATION_SECONDS:
            break

        timestamp = now_iso()
        distance = scenario_at(elapsed)
        if args.noise:
            distance += randomizer.uniform(-0.025, 0.025)
        distance = round(max(0.0, distance), 2)
        level = risk_engine.risk_level_for_distance(distance)
        sequence = sequence_base + sample_index

        # Send an environmental snapshot at danger entry so the event report
        # has temperature and humidity from the same moment.
        environment_due = elapsed >= next_environment
        danger_entry = level == "DANGER" and previous_level != "DANGER"
        if environment_due or danger_entry:
            send(
                args,
                client,
                "environment",
                _environment_payload(
                    timestamp=timestamp,
                    elapsed=elapsed,
                    heat=args.heat,
                ),
            )
            next_environment = elapsed + args.env_interval

        result = send(
            args,
            client,
            "proximity",
            _proximity_payload(
                timestamp=timestamp,
                sequence=sequence,
                distance_m=distance,
                level=level,
                elapsed=elapsed,
            ),
        )

        if elapsed >= next_camera or level != previous_level:
            send(
                args,
                client,
                "camera",
                _camera_payload(
                    timestamp=timestamp,
                    distance_m=distance,
                ),
            )
            next_camera = elapsed + args.camera_interval

        transition = result.get("incident_transition")
        incident = result.get("incident") or {}
        if incident.get("event_id"):
            event_id = incident["event_id"]

        if level != previous_level:
            print()
            print(
                f"[{elapsed:05.1f}초] 단계 전환 → "
                f"{STAGE_KO[level]} ({level})"
            )
        if transition == "STARTED":
            print(f"          사건 생성 → {event_id}")
        elif transition == "ENDED":
            print(f"          사건 종료 → {event_id}")

        print(
            f"  {elapsed:05.1f}초 | {distance:>4.2f}m | "
            f"{STAGE_KO[level]:<4} | seq={sequence}",
            flush=True,
        )

        previous_level = level
        sample_index += 1
        next_tick += args.interval
        remaining = next_tick - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    print("-" * 66)
    print("30초 시연 완료 · 안전거리 복귀")
    if event_id:
        print(
            f"대시보드의 위험사건에서 {event_id}를 눌러 "
            "개별사건 리포트를 확인하세요."
        )
    return event_id


def live_demo(args):
    client = prepare_transport(args)
    try:
        cycle = 1
        while True:
            run_once(args, client, cycle)
            if not args.repeat:
                break
            cycle += 1
            print("\n3초 후 시나리오를 반복합니다.")
            time.sleep(3)
    finally:
        if client is not None:
            client.loop_stop()
            client.disconnect()


def batch_demo(args):
    """Insert the same scenario instantly for setup checks, not video capture."""
    records = []
    sequence_base = int(time.time() * 1000)
    base = time.time() - DEMO_DURATION_SECONDS
    for second in range(int(DEMO_DURATION_SECONDS) + 1):
        timestamp = (
            datetime.fromtimestamp(base + second)
            .astimezone()
            .isoformat(timespec="milliseconds")
        )
        distance = round(scenario_at(second), 2)
        level = risk_engine.risk_level_for_distance(distance)
        if second in {0, 16, 24}:
            temperature, humidity = environment_at(second, args.heat)
            records.append(
                {
                    "kind": "environment",
                    "timestamp": timestamp,
                    "equipment_id": EQUIPMENT_ID,
                    "temperature_c": temperature,
                    "humidity_pct": humidity,
                    "sensor_status": "NORMAL",
                }
            )
        records.append(
            {
                "kind": "proximity",
                "timestamp": timestamp,
                "worker_id": WORKER_ID,
                "equipment_id": EQUIPMENT_ID,
                "distance_m": distance,
                "risk_level": level,
                "near_miss": level == "DANGER",
                "sequence": sequence_base + second,
            }
        )

    if args.transport == "http":
        _http_request(args.server, "/api/health", attempts=2)
        result = http_post(args.server, "/api/batch", records)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    client = mqtt_client(args.broker, args.port)
    try:
        publish(client, f"{config.MQTT_BASE}/batch", records)
        print(f"MQTT 배치 {len(records)}건 전송 완료")
    finally:
        client.loop_stop()
        client.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "SafeON 대시보드용 30초 목업 시연. 기본값은 MQTT 없이 "
            "localhost:8000으로 HTTP 데이터를 전송합니다."
        )
    )
    parser.add_argument("--broker", default=config.MQTT_HOST)
    parser.add_argument("--port", type=int, default=config.MQTT_PORT)
    parser.add_argument("--server", default="http://localhost:8000")
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument(
        "--http",
        dest="transport",
        action="store_const",
        const="http",
        help="FastAPI 서버로 직접 전송(기본값)",
    )
    transport.add_argument(
        "--mqtt",
        dest="transport",
        action="store_const",
        const="mqtt",
        help="MQTT 브로커로 전송",
    )
    parser.set_defaults(transport="http")
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="거리 센서 전송 간격(기본 0.5초)",
    )
    parser.add_argument(
        "--env-interval",
        type=float,
        default=6.0,
        help="온습도 전송 간격(기본 6초)",
    )
    parser.add_argument(
        "--camera-interval",
        type=float,
        default=1.0,
        help="카메라 전송 간격(기본 1초)",
    )
    parser.add_argument(
        "--noise",
        action="store_true",
        help="±2.5cm의 미세한 거리 센서 노이즈 추가",
    )
    parser.add_argument("--heat", action="store_true", help="고온 환경으로 시연")
    parser.add_argument("--seed", type=int, default=20260725)
    loop = parser.add_mutually_exclusive_group()
    loop.add_argument("--repeat", action="store_true", help="30초 시나리오 반복")
    loop.add_argument(
        "--once",
        dest="repeat",
        action="store_false",
        help="한 번만 실행(기본값, 기존 명령 호환)",
    )
    parser.set_defaults(repeat=False)
    parser.add_argument(
        "--batch-demo",
        action="store_true",
        help="영상용 실시간 재생 대신 전체 시나리오를 즉시 적재",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval은 0보다 커야 합니다.")
    if args.env_interval <= 0:
        parser.error("--env-interval은 0보다 커야 합니다.")
    if args.camera_interval <= 0:
        parser.error("--camera-interval은 0보다 커야 합니다.")

    try:
        if args.batch_demo:
            batch_demo(args)
        else:
            live_demo(args)
    except KeyboardInterrupt:
        print("\n시뮬레이터 종료")
        return 130
    except (ConnectionError, RuntimeError, OSError) as exc:
        print(f"\n[실행 실패] {exc}")
        if args.transport == "http":
            print(
                "먼저 다른 터미널에서 "
                "'uvicorn main:app --host 0.0.0.0 --port 8000'을 실행하세요."
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
