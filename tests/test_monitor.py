import unittest
from agents.monitor import Monitor, load_config

NOW = 100.0
def r(node, sensor, v, ts): return {"node": node, "sensor": sensor, "value": v, "ts": ts}
def calm():  # 두 노드 모두 정상값
    return ([r("A", s, 100, NOW - i) for s in ("water_low", "water_high") for i in range(5)] +
            [r("B", "tilt", 0, NOW - i) for i in range(5)] +
            [r("B", "vibration", 50, NOW - i) for i in range(5)])
def mon(): return Monitor(load_config("config/monitor.json"))


class TestMonitor(unittest.TestCase):
    def test_calm(self):
        res = mon().evaluate(calm(), NOW)
        self.assertEqual(res.output["status"], "정상"); self.assertFalse(res.needs_approval)

    def test_single_sensor_is_hold(self):
        d = [x for x in calm() if not (x["sensor"] == "water_high")] + \
            [r("A", "water_high", 900, NOW - i) for i in range(5)]
        res = mon().evaluate(d, NOW)
        self.assertEqual(res.output["status"], "보류"); self.assertFalse(res.needs_approval)

    def test_spike_ignored(self):
        d = calm() + [r("A", "water_high", 900, NOW - 10)]   # 한 번 튐, 최근 3회는 정상
        res = mon().evaluate(d, NOW)
        self.assertEqual(res.output["status"], "정상")
        self.assertTrue(any("튐" in l for l in res.output["false_alarm_log"]))

    def test_flood_confirmed(self):
        d = [x for x in calm() if x["node"] != "A"] + \
            [r("A", s, 900, NOW - i) for s in ("water_low", "water_high") for i in range(5)]
        res = mon().evaluate(d, NOW)
        self.assertEqual((res.output["status"], res.output["level"]), ("확정후보", "경계"))
        self.assertTrue(res.needs_approval)                 # 확정은 담당자
        self.assertEqual(res.output["hazards"], ["침수"])

    def test_rain_stage_raises_to_danger(self):
        d = [x for x in calm() if x["node"] != "A"] + \
            [r("A", s, 900, NOW - i) for s in ("water_low", "water_high") for i in range(5)]
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
            r("A", s, 900, NOW - 50) for s in ("water_low", "water_high") for _ in range(5)]
        res = mon().evaluate(d, NOW)
        self.assertNotIn("침수", res.output["hazards"]); self.assertIn("A", res.output["sensor_faults"])
        res = mon().evaluate(calm() + [r("A", "water_high", 9999, NOW + 100)], NOW)
        self.assertNotIn("침수", res.output["hazards"])

    def test_stale_required_sensor_causes_hold(self):
        d = [x for x in calm() if not (x["node"] == "A" and x["sensor"] == "water_high")]
        d += [r("A", "water_high", 900, NOW - 50)]
        res = mon().evaluate(d, NOW)
        self.assertEqual(res.output["status"], "보류")
        self.assertIn("A.water_high", res.output["stale_sensors"])

    def test_summarizer_failure_falls_back(self):
        d = [x for x in calm() if x["node"] != "A"] + \
            [r("A", s, 900, NOW - i) for s in ("water_low", "water_high") for i in range(5)]
        def bad(*a): raise RuntimeError("down")
        res = Monitor(load_config("config/monitor.json"), summarizer=bad).evaluate(d, NOW)
        self.assertFalse(res.ai_used); self.assertEqual(res.output["status"], "확정후보")
