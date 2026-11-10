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

    def test_rule_extraction_without_ai(self):
        doc = "피해 신고서\n구역: Z3\n세대주: 가구 H05\n연락처: 010-1234-5678\n피해 내용: 주택침수 (반지하)"
        r = R.extract_fields(doc, None)
        self.assertFalse(r.ai_used)
        self.assertEqual(r.output["fields"], {"zone": "Z3", "householder": "가구 H05", "damage_type": "주택침수"})
        self.assertEqual(R.extract_fields("Z12구역 확인서", None).output["fields"]["zone"], "Z12")   # 한글이 바로 붙어도
        self.assertEqual(R.extract_fields("7구역 뒷산 토사 유출", None).output["fields"],
                         {"zone": "Z7", "householder": None, "damage_type": "사면유실"})

    def test_ai_fields_must_be_short_strings(self):
        r = R.extract_fields("서류", fake('{"zone":3,"householder":"%s","damage_type":"주택침수"}' % ("가" * 60)))
        self.assertEqual(r.output["missing"], ["zone", "householder"])

    def test_draft_needs_human_and_case_flow(self):
        d = R.draft_report("H1", {}, ["주택침수"], RC)
        self.assertTrue(d.needs_approval); self.assertIn("피해 사진", d.output["checklist"])
        c = R.RecoveryCase("C1", "H1")
        with self.assertRaises(PermissionError): c.advance("청소", "victim", 1)
        with self.assertRaises(ValueError): c.advance("수리", "staff", 1)       # 단계 건너뛰기 불가
        self.assertEqual(c.advance("청소", "staff", 1), "청소")

    def test_five_sample_documents_answer_key(self):   # D11 가상 서류 5종, 제안서 11.2 목표: 핵심 필드 90% 이상
        key = {"피해신고서_예시.txt": ("Z3", "가구 H05", "주택침수"), "임대차계약서_예시.txt": ("Z8", "가구 H16", "사면유실"),
               "피해확인서_예시.txt": ("Z5", "가구 H10", "주택파손"), "피해사진_설명_예시.txt": ("Z1", "가구 H02", "주택침수"),
               "체류확인서_예시.txt": ("Z7", "가구 H14", "주택침수")}
        hit = total = 0
        for name, want in key.items():
            with open(f"data/demo/{name}", encoding="utf-8") as fp:
                got = R.extract_fields(fp.read()).output["fields"]
            for k, w in zip(("zone", "householder", "damage_type"), want):
                total += 1; hit += got.get(k) == w
        self.assertGreaterEqual(hit / total, 0.9, f"{hit}/{total}")

    def test_aggregate(self):
        t = R.aggregate([{"zone": "Z1", "damage_type": "주택침수"}, {"zone": "Z1", "damage_type": "주택침수"},
                         {"zone": "Z2", "damage_type": "사면유실"}])
        self.assertEqual(t, {"by_zone": {"Z1": {"주택침수": 2}, "Z2": {"사면유실": 1}}, "total": 3})
