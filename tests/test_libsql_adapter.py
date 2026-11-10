"""Turso(libsql) 어댑터가 sqlite3와 같게 동작하는지(행 이름 접근·중복 오류·되돌리기). libsql이 없으면 건너뛴다.
Turso 서버에는 연결하지 않고 같은 어댑터를 로컬 파일로 시험한다(전체 시험도 DISASTER_DB_BACKEND=libsql로 돌릴 수 있다)."""
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import db


@unittest.skipUnless(importlib.util.find_spec("libsql"), "libsql 미설치(Render 배포에서만 필요)")
class TestLibsqlAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (db.BACKEND, db.DB_PATH)
        db.BACKEND, db.DB_PATH = "libsql", Path(self.tmp.name) / "t.db"
        db.init_db()

    def tearDown(self):
        if db._LIBSQL["conn"] is not None:
            db._LIBSQL["conn"].raw.close()
            db._LIBSQL["conn"] = None
        db.BACKEND, db.DB_PATH = self.saved
        self.tmp.cleanup()

    def test_behaves_like_sqlite3(self):
        uid = db.create_user("담당자1", "password123", "operator")
        with self.assertRaises(ValueError):                       # 같은 이름 → IntegrityError → ValueError
            db.create_user("담당자1", "password123", "operator")
        self.assertEqual(db.authenticate("담당자1", "password123")["id"], uid)
        with db.connect() as conn:
            row = conn.execute("SELECT id, username FROM users WHERE id=?", (uid,)).fetchone()
            self.assertEqual((row["username"], row[0], dict(row)["id"]), ("담당자1", uid, uid))
        with self.assertRaises(RuntimeError):                     # 예외가 나면 바깥 with 전체를 되돌린다
            with db.connect() as conn:
                db.put_record("volunteer", "V-X", {"id": "V-X"})
                raise RuntimeError
        self.assertIsNone(db.get_record("volunteer", "V-X"))
        db.put_record("volunteer", "V-Y", {"id": "V-Y", "name": "삽"})
        self.assertEqual(db.get_record("volunteer", "V-Y")["name"], "삽")
        with self.assertRaises(sqlite3.OperationalError):
            with db.connect() as conn:
                conn.execute("SELECT * FROM no_such_table")


if __name__ == "__main__":
    unittest.main()
