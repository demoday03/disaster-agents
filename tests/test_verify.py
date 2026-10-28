import unittest
from agents import verify as V
from common.llm import SolarClient

def fake(c):
    calls = []
    def tr(u, h, b, t): calls.append(b); return {"choices": [{"message": {"content": c}}]}
    cl = SolarClient(transport=tr); cl.calls_log = calls; return cl
EVID = "주택 침수 세대당 재난지원금 350만원. 참고용입니다."

class TestVerify(unittest.TestCase):
    def test_number_precheck_blocks_without_api(self):
        v, why = V.verify_text("재난지원금 500만원이 지급됩니다", EVID, client=None)
        self.assertEqual(v, "notGrounded"); self.assertIn("500", why)

    def test_verdicts(self):
        d = "주택 침수 가구는 재난지원금 350만원 대상일 수 있습니다."
        self.assertEqual(V.verify_text(d, EVID, fake("grounded"))[0], "grounded")
        self.assertEqual(V.verify_text(d, EVID, fake("notGrounded"))[0], "notGrounded")   # 'grounded'가 들어 있어도 구분
        self.assertEqual(V.verify_text(d, EVID, fake("notSure"))[0], "notSure")
        self.assertEqual(V.verify_text(d, EVID, fake("???"))[0], "notSure")
        self.assertEqual(V.verify_text(d, EVID, None)[0], "notSure")

    def test_api_failure_is_not_sure(self):
        def boom(*a): raise RuntimeError("net")
        self.assertEqual(V.verify_text("안내", "근거", SolarClient(transport=boom))[0], "notSure")

    def test_message_roles_and_truncation(self):
        c = fake("grounded"); V.verify_text("안내", "근거", c)
        self.assertEqual([m["role"] for m in c.calls_log[0]["messages"]], ["user", "assistant"])
        self.assertEqual(V.verify_text("안내", "가" * 50, fake("grounded"), max_chars=10)[0], "notSure")

    def test_verify_routes(self):
        docs = {"R1": EVID}
        ok = V.verify("재난지원금 350만원 안내", ["R1"], docs, fake("grounded"))
        self.assertEqual((ok.output["route"], ok.needs_approval), ("승인 대기열", True))
        blk = V.verify("재난지원금 999만원", ["R1"], docs, fake("grounded"))
        self.assertTrue(blk.output["blocked"]); self.assertFalse(blk.needs_approval)
        self.assertEqual(V.verify("안내", ["R9"], docs, fake("grounded")).output["route"], "담당자 확인 요청")

    def test_error_set_blocking_rate(self):   # 오류 문구 세트 차단·보류율(목표 90%)
        errs = ["재난지원금 500만원", "지원금 100만원 지급", "신청 기한은 30일", "25가지가 아니라 40가지", "월 80만원 지원"]
        res = [V.verify_text(e, EVID, fake("grounded"))[0] != "grounded" for e in errs]
        self.assertEqual(sum(res), len(errs))

    def test_ambiguous_verdict_text_is_not_grounded(self):
        # 부분 문자열로 grounded를 인정하면 안 된다(fail-open 회귀 방지)
        d = "주택 침수 가구는 재난지원금 350만원 대상일 수 있습니다."
        for raw in ["not grounded", "ungrounded", "Not Grounded", "grounded? notSure",
                    "It is grounded.", "Grounded", ""]:
            self.assertEqual(V.verify_text(d, EVID, fake(raw))[0], "notSure", raw)
        for raw, want in [(" grounded\n", "grounded"), ('"notGrounded"', "notGrounded")]:
            self.assertEqual(V.verify_text(d, EVID, fake(raw))[0], want, raw)

    def test_judge_mode_without_groundedness_model(self):
        # GROUNDEDNESS_MODEL이 없으면 채팅 모델이 지시문을 받고 같은 세 판정으로 답한다
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"SOLAR_MODEL": "solar-pro4"}):
            os.environ.pop("GROUNDEDNESS_MODEL", None)
            c = V.make_client()
        self.assertEqual((c.model, c.verify_mode), ("solar-pro4", "judge"))
        calls = []
        c.transport = lambda u, h, b, t: calls.append(b) or {"choices": [{"message": {"content": "notGrounded"}}]}
        self.assertEqual(V.verify_text("대피소는 학교입니다", "대피소는 체육관", c)[0], "notGrounded")
        msgs = calls[0]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["system", "user"])
        self.assertIn("[근거]\n대피소는 체육관", msgs[1]["content"])
        with mock.patch.dict(os.environ, {"GROUNDEDNESS_MODEL": "groundedness-check"}):
            g = V.make_client()
        self.assertEqual((g.model, g.verify_mode), ("groundedness-check", "groundedness"))
