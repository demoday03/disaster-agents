"""신고 데이터셋(제안서 D8) 형식 검사와 규칙 분류기 기준선. 목표(11.2) 충족 여부는
scripts/eval_intake.py로 따로 본다. 여기서는 세트가 설계대로인지와 성능이 기준선 아래로 떨어지지 않는지만 확인한다."""
import re
import unittest
from collections import Counter, defaultdict
from agents import intake
from common import eval_intake as EI

CFG = intake.load_cfg()


class TestIntakeEvalSet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reports = EI.load()["reports"]

    def test_size_and_distribution(self):
        self.assertEqual(len(self.reports), 80)
        self.assertEqual(len({r["id"] for r in self.reports}), 80)
        types = Counter(r["type"] for r in self.reports)
        self.assertTrue(set(types) <= set(CFG["types"]))
        # 거제 집계 비율(인명구조 약 7%, 침수 약 15%, 나머지 약 78%)에 맞춤
        self.assertEqual((types["인명구조"], types["침수"]), (6, 12))
        hard = sum(bool(r.get("hard")) for r in self.reports)
        self.assertTrue(0.15 <= hard / 80 <= 0.25, hard)

    def test_fields_are_valid_and_virtual(self):
        for r in self.reports:
            self.assertTrue(r["zone"] == "미상" or re.fullmatch(r"Z\d+", r["zone"]), r["id"])
            self.assertIsInstance(r["vulnerable"], bool)
            self.assertTrue(r["people"] is None or isinstance(r["people"], int))
            self.assertNotRegex(r["text"], r"\d{2,3}-\d{3,4}-\d{4}")   # 연락처 형식 없음

    def test_events_are_consistent(self):
        events = defaultdict(list)
        for r in self.reports:
            if "event" in r:
                events[r["event"]].append(r)
        for ev, members in events.items():
            self.assertGreaterEqual(len(members), 2, ev)
            self.assertEqual(len({(m["type"], m["zone"]) for m in members}), 1, ev)
        self.assertFalse([r["id"] for r in self.reports if r["type"] == "인명구조" and "event" in r])   # 인명구조는 병합 안 함

    def test_rule_baseline_does_not_regress(self):
        res = EI.evaluate(EI.load(), CFG)
        self.assertEqual(res["ai_used"], 0)
        # 2026-10-28 키워드 보강 후 기준선(보강 전: 유형 59/80, 인명구조 5/6, 병합 38/68).
        # 이 세트를 보고 규칙을 고쳤으므로 실제 성능보다 낙관적인 값이다. 규칙을 고쳐 올라가면 기준선도 올린다.
        self.assertGreaterEqual(res["type_accuracy"], 70 / 80)
        self.assertEqual(res["rescue_urgency1_rate"], 1.0)
        self.assertGreaterEqual(res["merge_accuracy"], 61 / 68)
        self.assertGreaterEqual(res["zone_accuracy"], 78 / 80)


if __name__ == "__main__":
    unittest.main()
