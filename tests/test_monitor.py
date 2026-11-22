import unittest
from agents.monitor import Monitor, load_config

NOW = 100.0
def r(node, sensor, v, ts): return {"node": node, "sensor": sensor, "value": v, "ts": ts}
def calm():  # 두 노드 모두 정상값(수심은 주의 기준 310 아래)
    return ([r("A", "depth", 100, NOW - i) for i in range(5)] +
            [r("B", "tilt", 0, NOW - i) for i in range(5)] +
            [r("B", "vibration", 50, NOW - i) for i in range(5)])
def mon(): return Monitor(load_config("config/monitor.json"))


class TestMonitor(unittest.TestCase):
    def test_calm(self):
        res = mon().evaluate(calm(), NOW)
        self.assertEqual(res.output["status"], "정상"); self.assertFalse(res.needs_approval)

    def test_only_node_a_installed(self):
        """노드 B(선택 노드)를 설치하지 않아 값이 하나도 없으면 끊김·보류로 보지 않는다. 수심만으로 판정."""
        a_only = [x for x in calm() if x["node"] == "A"]
        res = mon().evaluate(a_only, NOW)
        self.assertEqual((res.output["status"], res.output["sensor_faults"], res.output["stale_sensors"]), ("정상", [], []))
        flood = [r("A", "depth", 400, NOW - i) for i in range(5)]
        res = mon().evaluate(flood, NOW)
        self.assertEqual((res.output["status"], res.output["hazards"]), ("확정후보", ["침수"])); self.assertTrue(res.needs_approval)

    def test_node_a_missing_is_still_a_fault(self):
        """필수 노드 A가 끊기면 예전처럼 센서 이상(보류)."""
        b_only = [x for x in calm() if x["node"] == "B"]
        res = mon().evaluate(b_only, NOW)
        self.assertEqual((res.output["status"], res.output["sensor_faults"]), ("보류", ["A"]))

    def test_depth_over_caution_only_is_watch(self):
        """수심이 주의 기준(310)만 연속으로 넘으면 '보류(주의)', 승인 요청은 없다."""
        d = [x for x in calm() if x["node"] != "A"] + [r("A", "depth", 325, NOW - i) for i in range(5)]
        res = mon().evaluate(d, NOW)
        self.assertEqual((res.output["status"], res.output["level"]), ("보류", "주의")); self.assertFalse(res.needs_approval)
        self.assertEqual(res.output["caution_sensors"], ["A.depth"])
        self.assertIn("주의 기준", res.output["summary"])

    def test_depth_caution_must_be_sustained(self):
        d = calm() + [r("A", "depth", 325, NOW - 0.5)]   # 마지막 한 번만 주의 기준 위
        self.assertEqual(mon().evaluate(d, NOW).output["status"], "정상")

    def test_single_sensor_of_pair_is_hold(self):
        """두 센서가 함께 넘어야 하는 규칙(사면)은 하나만 넘으면 보류."""
        d = [x for x in calm() if x["node"] != "B"] + [r("B", "tilt", 1, NOW - i) for i in range(5)] + \
            [r("B", "vibration", 50, NOW - i) for i in range(5)]
        res = mon().evaluate(d, NOW)
        self.assertEqual(res.output["status"], "보류"); self.assertFalse(res.needs_approval)

    def test_spike_ignored(self):
        d = calm() + [r("A", "depth", 900, NOW - 9.5)]   # 한 번 튐, 최근 3회는 정상
        res = mon().evaluate(d, NOW)
        self.assertEqual(res.output["status"], "정상")
        self.assertTrue(any("튐" in l for l in res.output["false_alarm_log"]))

    def test_flood_confirmed(self):
        d = [x for x in calm() if x["node"] != "A"] + \
            [r("A", "depth", 900, NOW - i) for i in range(5)]
        res = mon().evaluate(d, NOW)
        self.assertEqual((res.output["status"], res.output["level"]), ("확정후보", "경계"))
        self.assertTrue(res.needs_approval)                 # 확정은 담당자
        self.assertEqual(res.output["hazards"], ["침수"])

    def test_rain_stage_raises_to_danger(self):
        d = [x for x in calm() if x["node"] != "A"] + \
            [r("A", "depth", 900, NOW - i) for i in range(5)]
        self.assertEqual(mon().evaluate(d, NOW, rain_stage=3).output["level"], "위험")

    def test_slope_needs_both_in_window(self):
        d = [x for x in calm() if x["node"] != "B"] + \
            [r("B", "tilt", 1, NOW - 2), r("B", "vibration", 600, NOW - 3)]
        self.assertEqual(mon().evaluate(d, NOW).output["hazards"], ["사면"])
        d2 = [x for x in calm() if x["node"] != "B"] + [r("B", "tilt", 1, NOW - 2), r("B", "vibration", 50, NOW - 3)]
        self.assertEqual(mon().evaluate(d2, NOW).output["status"], "보류")

    def test_stale_node_is_fault(self):
        d = [x for x in calm() if x["node"] != "A"]          # A 응답 없음
        res = mon().evaluate(d, NOW)
        self.assertEqual(res.output["sensor_faults"], ["A"])

    def test_old_or_future_samples_cannot_trigger_hazard(self):
        d = [x for x in calm() if x["node"] != "A"] + [
            r("A", "depth", 900, NOW - 50) for _ in range(5)]
        res = mon().evaluate(d, NOW)
        self.assertNotIn("침수", res.output["hazards"]); self.assertIn("A", res.output["sensor_faults"])
        res = mon().evaluate(calm() + [r("A", "depth", 9999, NOW + 100)], NOW)
        self.assertNotIn("침수", res.output["hazards"])

    def test_stale_required_sensor_causes_hold(self):
        d = [x for x in calm() if not (x["node"] == "B" and x["sensor"] == "vibration")]
        d += [r("B", "vibration", 900, NOW - 50)]
        res = mon().evaluate(d, NOW)
        self.assertEqual(res.output["status"], "보류")
        self.assertIn("B.vibration", res.output["stale_sensors"])

    def test_summarizer_failure_falls_back(self):
        d = [x for x in calm() if x["node"] != "A"] + \
            [r("A", "depth", 900, NOW - i) for i in range(5)]
        def bad(*a): raise RuntimeError("down")
        res = Monitor(load_config("config/monitor.json"), summarizer=bad).evaluate(d, NOW)
        self.assertFalse(res.ai_used); self.assertEqual(res.output["status"], "확정후보")
