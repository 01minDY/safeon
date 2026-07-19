"""위험도 엔진 단위 테스트: python -m pytest test_risk_engine.py -v (또는 python test_risk_engine.py)"""
import risk_engine as re
from risk_engine import PairTracker


def test_base_level():
    assert re.base_level(500) == 0
    assert re.base_level(300) == 0
    assert re.base_level(299) == 1
    assert re.base_level(150) == 1
    assert re.base_level(149) == 2
    assert re.base_level(80) == 2
    assert re.base_level(79) == 3
    assert re.base_level(0) == 3


def test_state_adjust():
    assert re.state_adjust(1, "reverse") == 2
    assert re.state_adjust(1, "working") == 2
    assert re.state_adjust(1, "forward") == 1
    assert re.state_adjust(0, "reverse") == 0   # 안전이면 보정 없음
    assert re.state_adjust(3, "reverse") == 3   # 상한 3


def test_approach_speed():
    # 2초 동안 300 -> 100cm 접근 = 100cm/s
    samples = [(0.0, 300.0), (2.0, 100.0)]
    assert re.approach_speed(samples) == 100.0
    # 멀어지면 음수
    assert re.approach_speed([(0.0, 100.0), (2.0, 300.0)]) == -100.0
    assert re.approach_speed([(0.0, 100.0)]) == 0.0


def test_speed_adjust():
    assert re.speed_adjust(1, 100.0) == 2
    assert re.speed_adjust(1, 10.0) == 1
    assert re.speed_adjust(0, 100.0) == 0


def test_tracker_filter_and_debounce():
    t = PairTracker()
    _, lv, _, ch = t.update(500, "idle", now=0.0)
    assert lv == 0 and not ch
    # 근접 샘플이 누적되면 필터값이 수렴하여 등급 상승
    t.update(100, "reverse", now=1.0)
    t.update(100, "reverse", now=2.0)
    _, lv, _, ch = t.update(100, "reverse", now=3.0)
    assert lv >= 2  # 필터 수렴 후 경고 이상
    # 동일 조건 유지 시 changed=False (디바운스)
    _, lv2, _, ch2 = t.update(100, "reverse", now=4.0)
    assert lv2 == lv and not ch2


def test_outlier_smoothing():
    t = PairTracker()
    t.update(300, "idle", now=0.0)
    t.update(300, "idle", now=1.0)
    filtered, _, _, _ = t.update(30, "idle", now=2.0)  # 이상치 1개
    assert filtered > 100  # 이동평균으로 완화됨


def test_alert_mapping():
    assert re.alert_for(0)["led"] == "green"
    assert re.alert_for(3) == {"buzzer": True, "vibration": True, "led": "red"}


if __name__ == "__main__":
    fns = [v for k, v in globals().items() if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)}개 테스트 전부 통과")
