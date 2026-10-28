"""오류 안내문 세트(제안서 11.3) 오프라인 시험. 실제 Groundedness 모델의 차단율은
scripts/eval_verify.py로 따로 잰다. 여기서는 세트 형식과 규칙 선검사·실패 시 동작만 확인한다."""
import unittest
from collections import Counter
from common import eval_guidance as EG
from common.llm import SolarClient


def fake(content):
    return SolarClient(transport=lambda u, h, b, t: {"choices": [{"message": {"content": content}}]})


class TestGuidanceErrorSet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.docs = EG.load()

    def test_set_shape(self):
        errs = self.data["errors"]
        self.assertEqual(len(errs), 20)
        ids = [e["id"] for e in errs + self.data["controls"]]
        self.assertEqual(len(ids), len(set(ids)))
        cats = Counter(e["category"].split("(")[0] for e in errs)
        for c in ("숫자", "없는 제도", "위험한 행동"):   # 11.3의 세 유형이 모두 들어 있어야 함
            self.assertGreater(cats[c], 0, c)
        for it in errs + self.data["controls"]:
            for d in it["evidence"]:
                self.assertIn(d, self.docs, f"{it['id']}: 근거 {d} 없음")

    def test_number_precheck_even_if_model_says_grounded(self):
        # 최악의 경우(모델이 전부 grounded라고 답함)에도 숫자 오류는 규칙으로 차단된다.
        res = EG.evaluate(fake("grounded"), self.data, self.docs)
        for item, r in zip(self.data["errors"], res["errors"]):
            want = "notGrounded" if item["rule_catch"] else "grounded"
            self.assertEqual(r["verdict"], want, item["id"])
        # 규칙만으로 막히는 비율. 목표 90%에 못 미치므로 나머지는 모델 판정에 의존한다.
        self.assertEqual(res["hard_block_rate"], 7 / 20)

    def test_controls_not_blocked_by_number_precheck(self):
        res = EG.evaluate(fake("grounded"), self.data, self.docs)
        self.assertEqual(res["control_pass_rate"], 1.0)

    def test_without_verifier_nothing_is_grounded(self):
        res = EG.evaluate(None, self.data, self.docs)
        self.assertEqual(res["block_rate"], 1.0)
        self.assertTrue(all(r["verdict"] != "grounded" for r in res["controls"]))

    def test_model_failure_is_held(self):
        def boom(*a): raise RuntimeError("net")
        res = EG.evaluate(SolarClient(transport=boom), self.data, self.docs)
        self.assertEqual(res["block_rate"], 1.0)
        self.assertTrue(all(r["verdict"] == "notSure" for r in res["errors"] if r["category"] != "숫자"))


if __name__ == "__main__":
    unittest.main()
