"""시연 준비 스크립트(scripts/demo_setup.py): 미리 넣는 신고가 기대한 유형으로 분류되고, 운영 DB는 지우지 않는다."""
import tempfile, unittest
from pathlib import Path

from database import db
from scripts import demo_setup as S


class TestDemoSetup(unittest.TestCase):
    def test_seed_and_reset(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "demo.db"
            pw, results = S.setup(path, password="demo-password-1")
            self.assertEqual([r["output"]["type"] for r in results], [t for _, t in S.REPORTS])
            self.assertEqual(results[0]["output"]["urgency"], 1)
            with self.assertRaises(SystemExit):
                S.setup(path)                                            # 이미 있으면 --reset 없이는 안 지움
            S.setup(path, reset=True, password="demo-password-2")       # 다시 만들면 계정·신고가 처음 상태
            old = db.DB_PATH; db.DB_PATH = path
            try:
                self.assertIsNotNone(db.authenticate(S.DEMO_USER, "demo-password-2"))
                self.assertEqual(len(db.list_incidents()), len(S.REPORTS))
            finally:
                db.DB_PATH = old

    def test_refuses_to_delete_non_demo_db(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "disaster.db"
            path.write_bytes(b"")
            with self.assertRaises(SystemExit):
                S.setup(path, reset=True)
            self.assertTrue(path.exists())
