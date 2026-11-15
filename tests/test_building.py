"""같은 아파트 알아보기(이웃 신고 합치기용). 실행: python -m unittest tests.test_building -v"""
import unittest

from common.places import building, gps, near, same_building


class TestBuilding(unittest.TestCase):
    def test_reads_complex_and_dong(self):
        cases = {"위치: 옥포동 한빛아파트 101동 302호 · 물이 차요": ("한빛아파트", "101", "한빛아파트 101동"),
                 "위치: 한빛 아파트 101 동 앞": ("한빛아파트", "101", "한빛아파트 101동"),
                 "한빛아파트 지하주차장 물이 차요": ("한빛아파트", "", "한빛아파트"),
                 "위치: 옥포 e편한세상 103동": ("옥포e편한세상", "103", "옥포e편한세상 103동"),
                 "위치: 푸르지오 2차 105동 1층": ("푸르지오2차", "105", "푸르지오 2차 105동"),
                 "위치: 옥포주공 3단지 208동": ("옥포주공3단지", "208", "옥포주공 3단지 208동"),
                 "위치: 삼성빌라 B동": ("삼성빌라", "B", "삼성빌라 B동")}
        for text, (name, dong, label) in cases.items():
            b = building(text)
            self.assertEqual((b["name"], b["dong"], b["label"]), (name, dong, label), text)

    def test_does_not_guess(self):
        for text in ("우리 아파트 정전이에요", "옆 아파트에 물이 차요", "물이 넘쳐서 아파트 1층이 잠겼어요", "위치: 옥포동 시장 앞", ""):
            self.assertIsNone(building(text), text)

    def test_same_building(self):
        a = building("위치: 한빛아파트 101동")
        self.assertTrue(same_building(a, building("위치: 한빛아파트 지하주차장")))   # 동을 안 적은 이웃은 같은 단지로
        self.assertTrue(same_building(a, building("위치: 한빛 아파트 101동 502호")))
        self.assertFalse(same_building(a, building("위치: 한빛아파트 105동")))         # 동이 다르면 다른 건물
        self.assertFalse(same_building(a, building("위치: 가람아파트 101동")))
        self.assertFalse(same_building(a, None))

    def test_gps_near(self):
        a, b = gps("GPS 34.88812, 128.62345 (오차 약 12m)"), gps("위치: GPS 34.88830, 128.62360 (오차 약 20m)")
        self.assertEqual(a, (34.88812, 128.62345, 12))
        self.assertTrue(near(a, b))                                                        # 약 24m
        self.assertFalse(near(a, gps("GPS 34.89000, 128.62345 (오차 약 10m)")))            # 약 200m
        self.assertFalse(near(a, gps("GPS 34.88815, 128.62345 (오차 약 900m)")))           # 컴퓨터 위치처럼 오차가 크면 안 묶음
        self.assertFalse(near(a, gps("GPS 34.88815, 128.62345")))                          # 오차를 모르면 안 묶음


if __name__ == "__main__":
    unittest.main()
