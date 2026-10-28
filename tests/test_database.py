import tempfile
import threading
import unittest
from pathlib import Path
from database import db


class TestSQLitePersistence(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="disaster-db-unit-")
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "test.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()

    def test_migrations_are_idempotent(self):
        db.init_db()
        with db.connect() as conn:
            versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(versions.count("001_initial.sql"), 1)
        self.assertTrue({"incidents", "ai_analyses", "approvals", "resources", "audit_logs"}.issubset(tables))

    def test_incident_and_approval_persist(self):
        result = {"agent": "intake", "ai_used": False,
                  "output": {"id": "R-TEST1234", "type": "침수", "zone": "Z2", "urgency": 2, "text": "테스트"}}
        saved = db.save_incident(result)
        self.assertEqual(db.list_incidents()[0]["output"]["id"], "R-TEST1234")
        approval = db.create_approval("demo", {"stage": 2})
        self.assertEqual(db.list_pending_approvals()[0]["id"], approval["id"])
        resolved = db.resolve_approval(approval["id"], True, "테스트 담당자", "검토 완료")
        self.assertEqual(resolved["state"], "approved")
        self.assertEqual(db.list_pending_approvals(), [])
        with db.connect() as conn:
            audit_count = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        self.assertEqual(audit_count, 2)


    def test_double_resolve_and_missing(self):
        a = db.create_approval("demo", {"stage": 1})
        db.resolve_approval(a["id"], False, "담당자", "")
        with self.assertRaises(ValueError):
            db.resolve_approval(a["id"], True, "담당자", "재승인 시도")
        with self.assertRaises(KeyError):
            db.resolve_approval("AP-NOPE", True, "담당자", "x")
        with db.connect() as conn:
            row = conn.execute("SELECT state, approver_name FROM approvals WHERE id=?", (a["id"],)).fetchone()
        self.assertEqual((row[0], row[1]), ("rejected", "담당자"))

    def test_approved_guidance_lookup(self):
        db.save_incident({"agent": "intake", "output": {"id": "R-GUIDE001", "type": "침수", "text": "t"}})
        g = {"incident_id": "R-GUIDE001", "guidance": {"text": "안내", "verdict": "grounded"}}
        self.assertIsNone(db.get_approved_guidance("R-GUIDE001"))
        pending = db.create_approval("welfare_guidance", g, incident_id="R-GUIDE001")
        review = db.create_approval("welfare_guidance_review", g, incident_id="R-GUIDE001")
        db.resolve_approval(review["id"], True, "담당자", "확인")
        self.assertIsNone(db.get_approved_guidance("R-GUIDE001"))   # 대기·확인 건은 제외
        db.resolve_approval(pending["id"], True, "담당자", "승인")
        found = db.get_approved_guidance("R-GUIDE001")
        self.assertEqual(found["item"]["guidance"]["text"], "안내")
        self.assertTrue(found["approved_at"])

    def test_concurrent_resolve_only_one_wins(self):
        # 같은 항목을 동시에 처리해도 한 건만 반영되고, 나머지는 '이미 처리됨'(409)이어야 한다.
        for _ in range(10):
            a = db.create_approval("demo", {"stage": 1})
            n = 8
            barrier, results = threading.Barrier(n), []

            def worker(i):
                barrier.wait()
                try:
                    db.resolve_approval(a["id"], i % 2 == 0, f"담당자{i}", "동시 처리")
                    results.append("ok")
                except ValueError:
                    results.append("conflict")
                except Exception as e:   # 500으로 이어지는 예외(예: database is locked)
                    results.append(type(e).__name__)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
            for t in threads: t.start()
            for t in threads: t.join()
            self.assertEqual(sorted(results), ["conflict"] * (n - 1) + ["ok"])
            with db.connect() as conn:
                logs = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='approval_resolved' "
                                    "AND target_id=?", (a["id"],)).fetchone()[0]
            self.assertEqual(logs, 1)


if __name__ == "__main__":
    unittest.main()
