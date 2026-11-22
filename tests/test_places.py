"""직접 쓴 위치에서 관리 구역 추정(common/places.py)."""
import unittest

from common import places


class TestPlaces(unittest.TestCase):
    def test_infer_zone(self):
        self.assertEqual(places.infer_zone("3구역 반지하"), "Z3")              # 구역 표기
        self.assertEqual(places.infer_zone("가람초 뒤편 상가"), "Z2")           # 가상 장소 이름
        self.assertEqual(places.infer_zone("옥포동 ○○아파트"), "Z3")           # 거제 동·면 이름은 시연용 연결표로(config/real_zones.json)
        self.assertEqual(places.infer_zone("옥포 2동 시장"), "Z3")             # 띄어 쓰거나 '동'을 빼도
        self.assertEqual(places.infer_zone("고현시장 앞"), "Z2")
        self.assertEqual(places.infer_zone("장승포항 근처"), "Z9")
        self.assertEqual(places.infer_zone("거제중학교 앞"), "미상")            # 연결표에 없는 건물 이름은 담당자가 정한다
        self.assertEqual(places.infer_zone("아주 급해요 연초에 고친 집"), "미상")  # 흔한 말(아주·연초)은 동·면으로 보지 않는다
        self.assertEqual(places.infer_zone("옥포동이랑 고현동 사이"), "미상")    # 서로 다른 구역이면 추측하지 않는다
        self.assertEqual(places.infer_zone("가람초와 한빛중 사이"), "미상")      # 단서가 서로 다르면 추측하지 않는다
        self.assertEqual(places.infer_zone("2구역인지 3구역인지"), "미상")

    def test_resolve(self):
        self.assertEqual(places.resolve({"zone": "Z4", "address": "아무 곳"}), ("Z4", "아무 곳"))   # 담당자가 정한 구역 우선
        self.assertEqual(places.resolve({"address": " 하천  상류 쪽 "}), ("Z7", "하천 상류 쪽"))
        with self.assertRaises(ValueError):
            places.resolve({})
        with self.assertRaises(ValueError):
            places.resolve({"zone": "Z99"})
        self.assertEqual(places.resolve({}, required=False), ("미상", ""))
        self.assertEqual(places.describe("Z1"), "하천 하류 주거지")
        self.assertEqual(places.describe("미상"), "위치 확인 필요")


if __name__ == "__main__":
    unittest.main()
