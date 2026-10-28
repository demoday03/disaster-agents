import unittest
from agents.dispatch import (recommend, plan, weighted_score, load_config, Board)

CFG = load_config("config/dispatch.json")
W = CFG["weights"]["volunteer"]


class TestScoring(unittest.TestCase):
    def test_proposal_example(self):   # 제안서 5.7 계산 예
        ex = {"B-02": (1, .8, 1, 1, .95), "B-07": (1, .4, 1, .5, .75), "B-05": (.6, 1, 0, 1, .63)}
        for _, (c, d, e, t, total) in ex.items():
            self.assertAlmostEqual(weighted_score(
                {"capability": c, "distance": d, "equipment": e, "time": t}, W), total, places=4)


def vols():
    return [{"id": "B-02", "zone": "Z1", "skills": ["토사"], "equipment": ["삽"], "available_min": 300},
            {"id": "B-07", "zone": "Z2", "skills": ["토사"], "equipment": ["삽"], "available_min": 90},
            {"id": "B-05", "zone": "Z1", "skills": [], "equipment": [], "available_min": 300}]

CTX = {"travel": {("Z1", "Z3"): {"minutes": 12, "via_blocked": False},
                  ("Z2", "Z3"): {"minutes": 36, "via_blocked": False}}}
T1 = {"id": "T1", "zone": "Z3", "skills": ["토사"], "equipment": ["삽"], "hours": 3, "urgency": 2}


class TestDispatch(unittest.TestCase):
    def test_ranking_and_shape(self):
        res = recommend("volunteer", T1, vols(), CTX, CFG)
        o = res.output
        self.assertEqual(o["top"]["id"], "B-02"); self.assertEqual(len(o["alternatives"]), 2)
        self.assertTrue(res.needs_approval); self.assertIn("역량", o["reason"])

    def test_reproducible(self):
        a = recommend("volunteer", T1, vols(), CTX, CFG).output
        b = recommend("volunteer", T1, list(reversed(vols())), CTX, CFG).output
        self.assertEqual([a["top"]["id"]] + [x["id"] for x in a["alternatives"]],
                         [b["top"]["id"]] + [x["id"] for x in b["alternatives"]])

    def test_hard_constraints(self):
        c = vols(); c[0]["rest_needed"] = True
        d = {**T1, "must_have_equipment": ["삽"]}
        o = recommend("volunteer", d, c, CTX, CFG).output
        ex = {e["id"]: e["reasons"] for e in o["excluded"]}
        self.assertIn("휴식 필요", ex["B-02"]); self.assertIn("필수 장비 없음", ex["B-05"])
        self.assertEqual(o["top"]["id"], "B-07")

    def test_blocked_route_and_hours_cap(self):
        ctx = {"travel": {("Z1", "Z3"): {"minutes": 12, "via_blocked": True}}}
        o = recommend("volunteer", T1, vols()[:1], ctx, CFG).output
        self.assertTrue(o["shortage"]); self.assertIn("통제 구간 경유", o["excluded"][0]["reasons"])
        cfg = {**CFG, "max_hours_per_volunteer": 6}
        c = vols(); c[0]["hours_today"] = 5
        o = recommend("volunteer", T1, c, CTX, cfg).output
        self.assertNotEqual(o["top"]["id"], "B-02")

    def test_shelter_capacity(self):
        sh = [{"id": "S1", "zone": "Z1", "capacity": 50, "capacity_left": 0, "amenities": ["엘리베이터"]},
              {"id": "S2", "zone": "Z1", "capacity": 80, "capacity_left": 3, "amenities": []},
              {"id": "S3", "zone": "Z1", "capacity": 60, "capacity_left": 30, "amenities": ["엘리베이터"]}]
        d = {"id": "H1", "zone": "Z1", "people": 4, "amenities": ["엘리베이터"]}
        o = recommend("shelter", d, sh, {}, CFG).output
        self.assertEqual(o["top"]["id"], "S3")
        self.assertEqual({e["id"] for e in o["excluded"]}, {"S1", "S2"})

    def test_goods_never_medicine_or_expired(self):
        g = [{"id": "G1", "zone": "Z1", "qty": 100, "category": "의약품"},
             {"id": "G2", "zone": "Z1", "qty": 100, "expired": True},
             {"id": "G3", "zone": "Z1", "qty": 100, "days_left": 3, "category": "식수"}]
        o = recommend("goods", {"id": "R1", "zone": "Z1", "qty": 20}, g, {}, CFG).output
        self.assertEqual(o["top"]["id"], "G3"); self.assertEqual(len(o["excluded"]), 2)

    def test_plan_urgent_first_no_double_booking(self):
        t2 = {**T1, "id": "T2", "urgency": 1}
        res = plan("volunteer", [T1, t2], vols(), CTX, CFG)
        tops = [r.output["top"]["id"] for r in res if r.output["top"]]
        self.assertEqual(res[0].output["demand_id"], "T2")      # 긴급도 1 먼저
        self.assertEqual(len(tops), len(set(tops)))

    def test_board_flow(self):
        b = Board("volunteer", vols(), CTX, CFG)
        b.submit(T1, recommend("volunteer", T1, vols(), CTX, CFG))
        self.assertEqual(b.applied, {})                          # 승인 전엔 반영 없음
        new = b.reject("T1", "담당자A", "다른 현장 우선")
        self.assertEqual(new.output["top"]["id"], "B-07")        # 거절 사유 반영 재추천
        b.modify("T1", "B-05", "담당자A")
        self.assertEqual(b.applied["T1"], "B-05")
        self.assertIsNotNone(b.history[-1]["score_diff"])
        c = vols(); c[0]["rest_needed"] = True
        b2 = Board("volunteer", c, CTX, CFG); b2.submit(T1, recommend("volunteer", T1, c, CTX, CFG))
        with self.assertRaises(ValueError): b2.direct("T1", "B-02", "담당자A")
        with self.assertRaises(ValueError): b2.direct("T1", "B-02", "담당자A", override=True)
        warnings = b2.direct("T1", "B-02", "담당자A", override=True, reason="현장 책임자 예외 승인")
        self.assertIn("휴식 필요", warnings); self.assertTrue(b2.history[-1]["override"])

    def test_board_rejects_missing_or_invalid_candidates(self):
        b = Board("volunteer", vols(), CTX, CFG)
        b.submit(T1, recommend("volunteer", T1, [], CTX, CFG))
        with self.assertRaises(ValueError): b.approve("T1", "담당자A")
        with self.assertRaises(ValueError): b.reject("T1", "담당자A", "no candidate")
        with self.assertRaises(ValueError): b.modify("T1", "UNKNOWN", "담당자A")
        with self.assertRaises(ValueError): b.direct("T1", "UNKNOWN", "담당자A")

    def test_explainer_failure_keeps_scores(self):
        def bad(*a): raise RuntimeError("x")
        r = recommend("volunteer", T1, vols(), CTX, CFG, explainer=bad)
        self.assertFalse(r.ai_used); self.assertIsNotNone(r.output["top"])

if __name__ == "__main__":
    unittest.main()
