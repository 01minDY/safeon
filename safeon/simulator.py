"""목업 시뮬레이터 — 실제 ESP32와 동일한 HTTP 경로로 데이터 전송.

용도: 사전 개발 테스트 / 데모 리허설 / HW 장애 시 발표 백업.

실행 예:
    python simulator.py                        # 기본 데모 시나리오
    python simulator.py --noise               # 노이즈+패킷유실 모드
    python simulator.py --server http://192.168.0.10:8000
"""
import argparse
import random
import time
import urllib.request
import json

# 시나리오: (구간 지속시간(s), 시작거리, 끝거리, 장비상태)
SCENARIOS = {
    "FORKLIFT-01|WORKER-01": [
        (4, 500, 500, "idle"),      # 안전 대기
        (5, 500, 200, "forward"),   # 접근 → 주의
        (4, 200, 100, "reverse"),   # 후진 중 접근 → 경고/위험
        (3, 100, 60, "reverse"),    # 위험 진입
        (5, 60, 400, "forward"),    # 이탈 → 안전
        (4, 400, 400, "idle"),
    ],
    "EXCAVATOR-01|WORKER-02": [
        (6, 600, 600, "idle"),
        (6, 600, 250, "working"),   # 작업중 접근 → 경고
        (4, 250, 130, "working"),
        (6, 130, 500, "idle"),      # 이탈
        (3, 500, 500, "idle"),
    ],
}


def send(server, payload, timeout=2):
    req = urllib.request.Request(
        f"{server}/api/report",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read())


def run(server: str, noise: bool, interval: float, loop: bool):
    seqs = {k: 0 for k in SCENARIOS}
    while True:
        # 각 쌍의 시나리오를 (경과시간 → 거리) 함수로 변환해 동시 재생
        t0 = time.time()
        durations = {k: sum(seg[0] for seg in v) for k, v in SCENARIOS.items()}
        total = max(durations.values())

        while time.time() - t0 < total:
            now = time.time() - t0
            for key, segs in SCENARIOS.items():
                equip_id, worker_id = key.split("|")
                # 현재 구간 찾기
                acc = 0
                cur = None
                for dur, d0, d1, state in segs:
                    if acc <= now < acc + dur:
                        frac = (now - acc) / dur
                        cur = (d0 + (d1 - d0) * frac, state)
                        break
                    acc += dur
                if cur is None:
                    continue
                dist, state = cur

                if noise:
                    if random.random() < 0.05:   # 5% 패킷 유실
                        continue
                    dist *= random.uniform(0.9, 1.1)   # ±10% 노이즈
                    if random.random() < 0.3:
                        time.sleep(random.uniform(0, 0.3))  # 지연

                seqs[key] += 1
                payload = {
                    "equip_id": equip_id,
                    "worker_id": worker_id,
                    "distance_cm": round(dist, 1),
                    "equip_state": state,
                    "battery": random.randint(70, 100),
                    "seq": seqs[key],
                }
                try:
                    res = send(server, payload)
                    print(f"[{equip_id}->{worker_id}] {payload['distance_cm']:7.1f}cm "
                          f"{state:8s} -> level {res['risk_level']}")
                except Exception as e:
                    print(f"!! 전송 실패: {e}")
            time.sleep(interval)

        if not loop:
            break
        print("--- 시나리오 반복 ---")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--server", default="http://localhost:8000")
    p.add_argument("--noise", action="store_true", help="노이즈/유실/지연 주입")
    p.add_argument("--interval", type=float, default=1.0, help="전송 간격(초)")
    p.add_argument("--once", action="store_true", help="1회만 재생 (기본: 반복)")
    a = p.parse_args()
    try:
        run(a.server, a.noise, a.interval, loop=not a.once)
    except KeyboardInterrupt:
        print("\n종료")
