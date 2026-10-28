import unittest
from agents import recovery as R
from common.llm import SolarClient

RC = R.load_rules("config/support_rules.json")
def fake(c): return SolarClient(transport=lambda u, h, b, t: {"choices": [{"message": {"content": c}}]})
def ids(hh): return {c["id"] for c in R.match_support(hh, RC).output["candidates"]}

class TestRecovery(unittest.TestCase):
    def test_matching_answer_key(self):   # 정답표 시험
        self.assertEqual(ids({"id": "H1", "area": "거제시", "damage_types": ["주택침수"]}), {"R1", "R2", "R3", "R5"})
        self.assertEqual(ids({"id": "H2", "area": "거제시", "damage_types": ["사면유실"], "housing": "공동주택"}), {"R2", "R3", "R4"})
        self.assertEqual(ids({"id": "H3", "area": "거제시", "damage_types": ["주택침수"], "long_term_evacuation": True, "lease_signed": True}),
                         {"R1", "R2", "R3", "R5", "R6"})
        self.assertEqual(ids({"id": "H4", "area": "타지역", "damage_types": ["주택침수"]}), {"R1"})
        self.assertEqual(ids({"id": "H5", "area": "거제시", "damage_types": []}), {"R2", "R3"})

    def test_unconfirmed_deadline_never_displayed(self):
        m = R.match_support({"id": "H1", "area": "거제시", "damage_types": ["주택침수"]}, RC)
        self.assertNotIn("R7", {c["id"] for c in m.output["candidates"]})

    def test_support_text_marks_unconfirmed_and_notice(self):
        m = R.match_support({"id": "H1", "area": "거제시", "damage_types": ["주택침수"]}, RC)
        draft, ev = R.support_text(m)
        self.assertIn("(확인 필요)", draft); self.assertIn("참고용", draft); self.assertTrue(m.output["requires_verification"])

    def test_extract_and_manual_form(self):
        r = R.extract_fields("피해확인서 ... 연락처 010-1111-2222", fake('{"zone":"Z3","householder":"가구 A-03","damage_type":"주택침수"}'))
        self.assertEqual((r.ai_used, r.output["manual_form_required"]), (True, False))
        r = R.extract_fields("흐릿한 서류", fake('{"zone":null,"householder":null,"damage_type":"주택침수"}'))
        self.assertEqual(r.output["missing"], ["zone", "householder"]); self.assertTrue(r.output["manual_form_required"])
        r = R.extract_fields("서류", None); self.assertTrue(r.output["manual_form_required"])   # 파싱 실패 → 수동 폼

    def test_draft_needs_human_and_case_flow(self):
        d = R.draft_report("H1", {}, ["주택침수"], RC)
        self.assertTrue(d.needs_approval); self.assertIn("피해 사진", d.output["checklist"])
        c = R.RecoveryCase("C1", "H1")
        with self.assertRaises(PermissionError): c.advance("청소", "victim", 1)
        with self.assertRaises(ValueError): c.advance("수리", "staff", 1)       # 단계 건너뛰기 불가
        self.assertEqual(c.advance("청소", "staff", 1), "청소")

    def test_aggregate(self):
        t = R.aggregate([{"zone": "Z1", "damage_type": "주택침수"}, {"zone": "Z1", "damage_type": "주택침수"},
                         {"zone": "Z2", "damage_type": "사면유실"}])
        self.assertEqual(t, {"by_zone": {"Z1": {"주택침수": 2}, "Z2": {"사면유실": 1}}, "total": 3})
