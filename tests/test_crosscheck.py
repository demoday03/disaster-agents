"""신고 대조 에이전트(agents/crosscheck.py): 신고 심각도 vs 같은 구역 센서. 실제 기기·서버 없음."""
import unittest

from agents import crosscheck as C

CFG = C.load_cfg()
TH = {"A.depth": 400, "B.tilt": 0, "B.vibration": 300}
CAUT = {"A.depth": 150}   # 수심 센서 1개: 주의 기준 넘음 = 1, 위험 기준 넘음 = 2
DRY = {"A.depth": {"max": 30, "age_s": 1}}
LOW_ONLY = {"A.depth": {"max": 250, "age_s": 1}}    # 주의 기준만 넘음
BOTH = {"A.depth": {"max": 520, "age_s": 1}}        # 위험 기준 넘음


def flood(text, zone="Z1", **kw):
    return {"type": "침수", "zone": zone, "text": text, "urgency": 3, "people": None, "vulnerable": False, **kw}


class TestCrosscheck(unittest.TestCase):
    def test_claimed_level_from_words(self):
        self.assertEqual(C.claimed_level("침수", "Z1 물이 허리까지 찼어요", CFG), (2, ["허리"]))
        self.assertEqual(C.claimed_level("침수", "Z1 길에 물이 발목까지 고였어요", CFG)[0], 1)
        self.assertEqual(C.claimed_level("침수", "Z1 침수됐어요", CFG), (1, []))           # 표현 없으면 가벼움으로
        self.assertEqual(C.claimed_level("산사태·토사", "Z5 뒷산이 무너져 집을 덮쳤어요", CFG)[0], 2)

    def test_verdicts(self):
        cases = [(flood("Z1 물이 허리까지 차서 집이 잠겼어요"), DRY, C.EXAGGERATED),
                 (flood("Z1 물이 허리까지 찼어요"), LOW_ONLY, C.PARTIAL),
                 (flood("Z1 물이 허리까지 찼어요"), BOTH, C.CONSISTENT),
                 (flood("Z1 길에 물이 조금 고였어요"), DRY, C.NO_EVIDENCE),
                 (flood("Z1 길에 물이 조금 고였어요"), LOW_ONLY, C.CONSISTENT),
                 (flood("Z1 길에 물이 조금 고였어요"), BOTH, C.UNDERSTATED)]
        for report, window, want in cases:
            r = C.assess(report, window, TH, CFG, cautions=CAUT)
            self.assertEqual(r["verdict"], want, (report["text"], window))
            self.assertFalse(r["changes_urgency"])                                     # 참고용

    def test_unknown_when_no_sensor_or_offline(self):
        other_zone = C.assess(flood("Z3 허리까지 잠겼어요", zone="Z3"), BOTH, TH, CFG, cautions=CAUT)
        self.assertEqual(other_zone["verdict"], C.UNKNOWN)
        self.assertIn("Z3에는 침수 센서가 없습니다", other_zone["reasons"][1])
        offline = C.assess(flood("Z1 허리까지 잠겼어요"), {}, TH, CFG, cautions=CAUT)               # 센서가 조용 ≠ 위험 없음
        self.assertEqual((offline["verdict"], offline["sensor_level"]), (C.UNKNOWN, None))
        self.assertTrue(any("값을 보내지 않았습니다" in x for x in offline["reasons"]))
        self.assertIsNone(C.assess({"type": "물품 요청", "zone": "Z1", "text": "생수"}, BOTH, TH, CFG, cautions=CAUT))

    def test_people_or_vulnerable_always_asks_field_check(self):
        r = C.assess(flood("Z1 할머니 집이 허리까지 잠겼어요", vulnerable=True), DRY, TH, CFG, cautions=CAUT)
        self.assertEqual(r["verdict"], C.EXAGGERATED)
        self.assertIn("현장 확인을 먼저", r["advice"])
        r = C.assess(flood("Z1 허리까지 잠겼어요"), DRY, TH, CFG, cautions=CAUT, similar=2)
        self.assertIn("센서 위치와 다른 곳일 수 있음", r["advice"])
        self.assertIn("최근 2건 더", r["reasons"][-1])

    def test_slope_needs_both_tilt_and_vibration(self):
        rep = {"type": "산사태·토사", "zone": "Z5", "text": "Z5 뒷산이 무너졌어요"}
        tilt_only = {"B.tilt": {"max": 1, "age_s": 1}, "B.vibration": {"max": 20, "age_s": 1}}
        both = {"B.tilt": {"max": 1, "age_s": 1}, "B.vibration": {"max": 650, "age_s": 1}}
        self.assertEqual(C.assess(rep, tilt_only, TH, CFG, cautions=CAUT)["verdict"], C.PARTIAL)
        self.assertEqual(C.assess(rep, both, TH, CFG, cautions=CAUT)["verdict"], C.CONSISTENT)


if __name__ == "__main__":
    unittest.main()
