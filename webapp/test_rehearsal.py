"""시연 리허설 스크립트(scripts/rehearse_demo.py)를 mock 모드로 끝까지 돌려 모든 장면이 통과하는지 확인한다.
실행: python -m unittest webapp.test_rehearsal -v   (약 10초, 실제 API 호출 없음)"""
import unittest
from scripts import rehearse_demo


class TestRehearsal(unittest.TestCase):
    def test_all_scenes_pass_in_mock_mode(self):
        self.assertEqual(rehearse_demo.main(["--mock", "--port", "18801"]), 0)


if __name__ == "__main__":
    unittest.main()
