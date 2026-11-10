"""시연 DB(scripts/demo_setup.py)로 서버를 켜면 미리 넣은 신고도 에이전트에 연결되는지 시험. mock 모드, 실제 API 호출 없음.
실행: python -m unittest webapp.test_demo_seed -v"""
import os, subprocess, sys, tempfile, time, unittest, urllib.request
from pathlib import Path
from scripts import demo_setup
from webapp.test_server import Client, ROOT


class TestDemoSeedLinked(unittest.TestCase):
    port = 18776

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="disaster-seed-test-")
        db_path = Path(cls.tmp.name) / "demo.db"
        cls.password, _ = demo_setup.setup(db_path)
        env = {**os.environ, "WEBAPP_PORT": str(cls.port), "LLM_MODE": "mock", "DISASTER_DB_PATH": str(db_path)}
        cls.proc = subprocess.Popen([sys.executable, "webapp/server.py"], cwd=ROOT, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.base = f"http://127.0.0.1:{cls.port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(cls.base + "/api/health", timeout=.2).read(); break
            except Exception:
                time.sleep(.1)
        cls.staff = Client(cls.base)
        assert cls.staff.login(demo_setup.DEMO_USER, cls.password)[0] == 200

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try: cls.proc.wait(timeout=3)
        except subprocess.TimeoutExpired: cls.proc.kill()
        cls.tmp.cleanup()

    def test_seeded_reports_are_linked_on_startup(self):
        end, items = time.time() + 10, []
        while time.time() < end:
            items = {x["output"]["id"]: x["output"] for x in self.staff.get("/api/incidents")[1]["items"]}
            if all(o.get("state") for o in items.values()) and "guidance" in items["R-DEMO0001"].get("linked", {}):
                break
            time.sleep(.2)
        self.assertEqual(len(items), 6)
        self.assertTrue(all(o.get("state") == "접수" for o in items.values()), {k: o.get("state") for k, o in items.items()})
        rescue = items["R-DEMO0001"]["linked"]
        self.assertTrue(rescue.get("dispatch") and "guidance" in rescue)           # 인명구조 → 배치 추천·안내문
        self.assertTrue(items["R-DEMO0005"]["linked"].get("supply"))                 # 물품 요청 → ⑧
        _, q = self.staff.get("/api/approvals")
        self.assertIn(rescue["dispatch"][0], [a["id"] for a in q["items"]])


if __name__ == "__main__":
    unittest.main()
