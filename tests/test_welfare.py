import unittest
from agents import welfare as W
from common.llm import SolarClient

CFG = W.load_cfg("config/guidance.json")
def fake(c): return SolarClient(transport=lambda u, h, b, t: {"choices": [{"message": {"content": c}}]})
PROFILE = {"flags": ["반지하", "영유아"]}


class TestSafetyChecker(unittest.TestCase):
    def test_timeout_and_vulnerable_first(self):
        s = W.SafetyChecker(CFG); s.start("H1", 0); s.start("H2", 0, vulnerable=True)
        self.assertEqual(s.tick(149), [])                         # 취약도 150초 전엔 격상 없음
        e = s.tick(150); self.assertEqual([x["household"] for x in e], ["H2"])
        e = s.tick(300); self.assertEqual([x["household"] for x in e], ["H1"])
        self.assertTrue(e[0]["dispatch_requires_approval"]); self.assertEqual(s.tick(400), [])  # 중복 격상 없음

    def test_order_and_responses(self):
        s = W.SafetyChecker(CFG)
        for h, v in [("H1", False), ("H2", True), ("H3", False)]: s.start(h, 0, v)
        s.respond("H3", 10)
        e = s.tick(1000); self.assertEqual([x["household"] for x in e], ["H2", "H1"])
        self.assertEqual(s.respond("H1", 1100), "지연 응답"); self.assertEqual(s.rows["H3"]["state"], "안전")


class TestGuidance(unittest.TestCase):
    def test_no_llm_uses_standard(self):
        r = W.make_guidance(PROFILE, "경계", ["도로A"], CFG)
        self.assertTrue(r.output["displayable"]); self.assertFalse(r.ai_used)
        self.assertIn("G-003", r.output["doc_ids"]); self.assertNotIn("G-004", r.output["doc_ids"])
        self.assertIn("RULE-CTRL", r.output["doc_ids"])

    def test_ai_draft_needs_verifier(self):
        c = fake('{"text":"반지하 가구는 지금 대피소로 이동하세요."}')
        r = W.make_guidance(PROFILE, "경계", [], CFG, client=c)
        self.assertFalse(r.output["displayable"]); self.assertEqual(r.output["status"], "검증 대기")

    def test_verdicts(self):
        c = fake('{"text":"반지하 가구는 지금 대피소로 이동하세요."}')
        g = W.make_guidance(PROFILE, "경계", [], CFG, c, verifier=lambda d, e: "grounded")
        self.assertEqual((g.output["text"], g.output["status"]), ("반지하 가구는 지금 대피소로 이동하세요.", "승인 대기"))
        for v, st in [("notGrounded", "차단"), ("notSure", "담당자 확인 요청")]:
            r = W.make_guidance(PROFILE, "경계", [], CFG, c, verifier=lambda d, e, v=v: v)
            self.assertEqual(r.output["text"], r.output["standard"])      # 실패 시 표준 원문
            self.assertTrue(r.output["status"].startswith(st))

    def test_llm_failure_falls_back(self):
        c = fake("not json"); r = W.make_guidance(PROFILE, "주의", [], CFG, c)
        self.assertFalse(r.ai_used); self.assertTrue(r.output["displayable"])
