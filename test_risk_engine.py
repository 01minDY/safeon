"""엔진 단위 테스트 (명세서 기준): python test_risk_engine.py"""
import risk_engine as re
from risk_engine import PairTracker


def test_classify_3levels():
    # 명세서: SAFE >3m / CAUTION 1~3m / DANGER ≤1m
    assert re.classify(5.0) == "SAFE"
    assert re.classify(3.01) == "SAFE"
    assert re.classify(3.0) == "CAUTION"
    assert re.classify(1.01) == "CAUTION"
    assert re.classify(1.0) == "DANGER"
    assert re.classify(0.3) == "DANGER"


def test_near_miss_rule():
    # 명세서 권고: DANGER이면 near_miss = true
    assert re.is_near_miss("DANGER") is True
    assert re.is_near_miss("CAUTION") is False
    assert re.is_near_miss("SAFE") is False


def test_tracker_filter_and_debounce():
    t = PairTracker()
    _, lv, ch = t.update(5.0, now=0.0)
    assert lv == "SAFE" and not ch
    t.update(0.8, now=1.0)
    t.update(0.8, now=2.0)
    _, lv, ch = t.update(0.8, now=3.0)
    assert lv == "DANGER"          # 필터 수렴 후 위험
    _, lv2, ch2 = t.update(0.8, now=4.0)
    assert lv2 == lv and not ch2   # 유지 시 changed=False (디바운스)


def test_edge_level_priority():
    t = PairTracker()
    # 엣지(ESP32) 판정값이 오면 서버 계산보다 우선
    _, lv, _ = t.update(5.0, edge_level="DANGER", now=0.0)
    assert lv == "DANGER"


def test_outlier_smoothing():
    t = PairTracker()
    t.update(4.0, now=0.0)
    t.update(4.0, now=1.0)
    filtered, _, _ = t.update(0.4, now=2.0)   # 이상치 1개
    assert filtered > 1.0                      # 이동평균으로 완화


def test_apparent_temp_reasonable():
    # 기상청 여름철 체감온도: 33°C/70% → 대략 35~38°C 범위
    ac = re.apparent_temp(33.0, 70.0)
    assert 34.0 < ac < 40.0
    # 습도 낮으면 체감온도도 낮아짐
    assert re.apparent_temp(33.0, 30.0) < ac


def test_heat_stages():
    assert re.heat_stage(28.0)[0] == "NORMAL"
    assert re.heat_stage(31.5)[0] == "HEAT_CAUTION"
    assert re.heat_stage(33.0)[0] == "REST_REQUIRED"
    assert re.heat_stage(35.0)[0] == "STOP_RECOMMENDED"
    assert re.heat_stage(38.0)[0] == "EMERGENCY_STOP"


def test_recommend_rules():
    r = re.recommend(duration_sec=8.0, min_distance_m=0.4, repeat_count=3)
    assert "초근접" in r and "지속" in r and "반복" in r
    r2 = re.recommend(duration_sec=1.0, min_distance_m=0.9, repeat_count=1)
    assert "단발성" in r2


if __name__ == "__main__":
    fns = [v for k, v in globals().items() if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)}개 테스트 전부 통과")
