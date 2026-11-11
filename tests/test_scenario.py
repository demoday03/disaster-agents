import unittest
from common import scenario

SC = scenario.load()


class TestScenarioData(unittest.TestCase):
    def test_sizes_match_proposal(self):   # 제안서 7.1: 구역 10, 대피소 8, 자원 10팀
        self.assertEqual((len(SC["zones"]), len(SC["shelters"]), len(SC["public"])), (10, 8, 10))
        ids = [x["id"] for x in SC["zones"] + SC["shelters"] + SC["public"]]
        self.assertEqual(len(ids), len(set(ids)))
        zones = {z["id"] for z in SC["zones"]}
        for x in SC["shelters"] + SC["public"]:
            self.assertIn(x["zone"], zones, x["id"])
            self.assertIn("가상", x["name"])
        for s in SC["shelters"]:
            self.assertLessEqual(s["occupied"], s["capacity"])

    def test_travel_is_symmetric_and_blocks_control_zones(self):
        t = scenario.travel_ctx(SC)["travel"]
        self.assertEqual(len(t), 100)
        self.assertEqual(t[("Z1", "Z1")]["minutes"], 5)
        self.assertEqual(t[("Z1", "Z2")]["minutes"], 14)
        for (a, b), v in t.items():
            self.assertEqual(v, t[(b, a)])
        self.assertTrue(t[("Z1", "Z6")]["via_blocked"])
        self.assertFalse(t[("Z5", "Z10")]["via_blocked"])   # 비활성 통제 구간

    def test_demands(self):
        d = scenario.public_demand({"id": "R1", "type": "인명구조", "zone": "Z3", "urgency": 1}, SC)
        self.assertEqual((d["skills"], d["urgency"]), (["구조"], 1))
        for out in ({"id": "R2", "type": "물품 요청", "zone": "Z1"}, {"id": "R3", "type": "침수", "zone": "미상"}):
            with self.assertRaises(ValueError):
                scenario.public_demand(out, SC)
        # 위치를 글·GPS로만 적은 긴급 신고는 구역을 몰라도 추천 대상(긴급도 3·4는 구역 확인 후)
        self.assertEqual(scenario.public_demand({"id": "R5", "type": "인명구조", "zone": "미상", "urgency": 1}, SC)["zone"], "미상")
        with self.assertRaises(ValueError):
            scenario.public_demand({"id": "R6", "type": "침수", "zone": "미상", "urgency": 3}, SC)
        out = {"id": "R4", "type": "침수", "zone": "Z4"}
        self.assertEqual(scenario.shelter_demand(out, SC, 3, ["경사로", "경사로"])["amenities"], ["경사로"])
        for people, am in ((0, []), (True, []), (3, ["수영장"]), (3, "경사로")):
            with self.assertRaises(ValueError):
                scenario.shelter_demand(out, SC, people, am)

    def test_candidates_reflect_current_state(self):
        pub = scenario.public_candidates(SC, {"P01"})
        self.assertFalse(next(p for p in pub if p["id"] == "P01")["available"])
        sh = {s["id"]: s for s in scenario.shelter_candidates(SC, {"S1": 5})}
        self.assertEqual(sh["S1"]["capacity_left"], 120 - 40 - 5)


if __name__ == "__main__":
    unittest.main()
