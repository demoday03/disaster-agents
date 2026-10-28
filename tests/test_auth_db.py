import tempfile
import time
import unittest
from pathlib import Path
from database import db


class TestUsersAndSessions(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="disaster-auth-unit-")
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "test.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()

    def test_password_is_hashed_and_checked(self):
        db.create_user("담당자", "correct-horse-1", "operator")
        with db.connect() as conn:
            stored = conn.execute("SELECT password_hash FROM users").fetchone()[0]
        self.assertNotIn("correct-horse-1", stored)
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertEqual(db.authenticate("담당자", "correct-horse-1")["role"], "operator")
        self.assertIsNone(db.authenticate("담당자", "wrong-password"))
        self.assertIsNone(db.authenticate("없는사람", "correct-horse-1"))

    def test_create_user_validation(self):
        db.create_user("a", "12345678", "admin")
        for args in (("a", "12345678", "admin"), ("b", "short", "operator"),
                     ("c", "12345678", "superuser"), ("", "12345678", "operator")):
            with self.assertRaises(ValueError, msg=args):
                db.create_user(*args)
        self.assertEqual(db.count_users(), 1)

    def test_session_lifecycle_and_expiry(self):
        uid = db.create_user("담당자", "12345678", "operator")
        token, csrf = db.create_session(uid)
        s = db.get_session(token)
        self.assertEqual((s["username"], s["csrf"]), ("담당자", csrf))
        with db.connect() as conn:   # 토큰 원문은 DB에 없다
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions WHERE token_hash=?", (token,)).fetchone()[0], 0)
        db.delete_session(token)
        self.assertIsNone(db.get_session(token))
        old, _ = db.create_session(uid, ttl_s=-1)
        self.assertIsNone(db.get_session(old))
        self.assertIsNone(db.get_session(None))


if __name__ == "__main__":
    unittest.main()
