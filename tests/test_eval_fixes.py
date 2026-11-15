"""외부 평가(2026-11-15)에서 나온 오류 고친 것 시험. 실행: python -m unittest tests.test_eval_fixes -v"""
import unittest
from datetime import datetime, timedelta, timezone

from common import clock, scenario


class TestOutageSkills(unittest.TestCase):
    """정전 신고에 급수팀을 1순위로 추천하던 문제: 단전·단수를 글로 나눠 필요한 역량을 정한다."""
    def setUp(self):
        self.sc = scenario.load()

    def skills(self, text):
        return scenario.demand_skills({"id": "R-1", "type": "단전·단수", "zone": "Z9", "urgency": 2, "text": text}, self.sc)

    def test_power_water_split(self):
        self.assertEqual(self.skills("아파트 정전으로 엘리베이터가 멈췄어요"), ["전기"])
        self.assertEqual(self.skills("어제부터 단수라 수도에서 물이 안 나와요"), ["급수"])
        self.assertEqual(self.skills("정전에 단수까지 됐어요"), ["전기", "급수"])
        self.assertEqual(self.skills("무슨 일인지 모르겠어요"), ["전기"])                      # 단서가 없으면 기본값
        water = next(p for p in self.sc["public"] if p["id"] == "P09")
        self.assertNotIn("전기", water["skills"])                                             # 급수팀은 전기 역량이 아니다
        self.assertEqual(scenario.demand_skills({"type": "침수", "text": "정전"}, self.sc), ["배수"])   # 다른 유형은 그대로


class TestKoreaTime(unittest.TestCase):
    """서버가 UTC로 돌아도 화면에 나가는 시각은 한국 시각."""
    def test_kst(self):
        utc = datetime.now(timezone.utc).replace(tzinfo=None)
        self.assertAlmostEqual((clock.now_kst() - utc).total_seconds(), 9 * 3600, delta=5)
        self.assertEqual(clock.kst_text(60, "%H:%M"), "09:01")                                   # 1970-01-01 00:01 UTC = 09:01 KST
        self.assertEqual(clock.kst_text(None), "")


if __name__ == "__main__":
    unittest.main()
