"""마이크로비트 게이트웨이(gateway/microbit_gateway.py)와 서버 쪽 센서 수신(webapp/sensors.py). 실제 기기·네트워크 없음."""
import tempfile, unittest
from pathlib import Path

from database import db
from gateway import microbit_gateway as G
from webapp.sensors import SensorHub


class TestParse(unittest.TestCase):
    def test_parse_line(self):
        self.assertEqual(G.parse_line("S,A,water_low,523\r\n", 10.0),
                         {"node": "A", "sensor": "water_low", "value": 523.0, "ts": 10.0})
        for bad in ("", "hello", "S,A,water_low", "S,A,water_low,abc", "S,A;rm,x,1", "X,A,water_low,5",
                    "S,A,water_low,1,2", "S,AAAAAAAAAAAAAAAAAAAAA,x,1"):
            self.assertIsNone(G.parse_line(bad, 0), bad)

    def test_simulator_scenarios(self):
        flood = [G.parse_line(l, 0) for l in G.Simulator("flood").tick()]
        self.assertTrue(all(r["value"] > 400 for r in flood if r["node"] == "A"))
        drop = G.Simulator("dropout"); drop.tick()
        self.assertFalse(any(l.startswith("S,B") for l in drop.tick()))
        with self.assertRaises(ValueError):
            G.Simulator("tsunami")


class TestGatewayBatching(unittest.TestCase):
    def test_retry_after_network_error_and_drop_on_auth_error(self):
        calls, logs = [], []
        responses = [OSError("connection refused"), (200, {"output": {"status": "정상", "level": "정상"}}),
                     (401, {"error": "토큰"})]
        def sender(server, token, batch, rain):
            calls.append(len(batch))
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        gw = G.Gateway("http://x", "t" * 20, sender=sender, out=logs.append, clock=lambda: 0)
        gw.feed("S,A,water_low,10"); gw.feed("garbage"); gw.flush()
        self.assertEqual((len(gw.pending), gw.ignored), (1, 1))     # 실패분은 보관
        gw.feed("S,A,water_high,10"); gw.flush()
        self.assertEqual((calls[-1], gw.pending), (2, []))           # 다음 번에 함께 전송
        gw.feed("S,A,water_low,10"); gw.flush()
        self.assertEqual(gw.pending, [])                             # 인증 오류는 재시도해도 같으므로 버림
        self.assertIn("HTTP 401", logs[-1])

    def test_pending_is_bounded(self):
        gw = G.Gateway("http://x", "t", out=lambda m: None, clock=lambda: 0)
        for i in range(G.MAX_PENDING + 50):
            gw.feed(f"S,A,water_low,{i}")
        self.assertEqual(len(gw.pending), G.MAX_PENDING)
        self.assertEqual(gw.pending[-1]["value"], G.MAX_PENDING + 49)   # 오래된 값부터 버림


class TestSensorHub(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sensor-hub-")
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_db()
        self.now = 1_000_000.0
        self.hub = SensorHub(db, clock=lambda: self.now)

    def tearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    def send(self, scenario, ticks):
        sim, res = G.Simulator(scenario), None
        for _ in range(ticks):
            res = self.hub.ingest([G.parse_line(l, self.now) for l in sim.tick()])
            self.now += 1
        return res

    def test_flood_needs_sustained_readings_and_queues_once(self):
        early = self.send("flood", 2)                                          # 연속 3회 전에는 확정 안 함
        self.assertNotEqual(early["output"]["status"], "확정후보")
        self.assertIsNone(early["approval_id"])
        res = self.send("flood", 1)
        self.assertEqual((res["output"]["status"], res["output"]["hazards"]), ("확정후보", ["침수"]))
        self.assertTrue(res["approval_id"])
        again = self.send("flood", 5)                                          # 계속 높아도 승인 요청은 한 번만
        self.assertEqual((again["approval_id"], again["duplicate_of"]), (None, res["approval_id"]))
        self.assertEqual(sum(a["kind"] == "monitor_stage" for a in db.list_pending_approvals()), 1)

    def test_confirmed_stage_not_requeued_but_new_hazard_is(self):
        aid = self.send("flood", 3)["approval_id"]
        db.resolve_approval(aid, True, "담당자", "확인")
        db.put_record("stage", "current", {"level": "경계", "hazards": ["침수"], "confirmed": True})
        self.assertEqual(self.send("flood", 2)["duplicate_of"], "confirmed")
        slope = self.send("slope", 3)                                          # 다른 위험(사면)은 새로 올린다
        self.assertEqual(slope["output"]["hazards"], ["사면"])
        self.assertEqual([a["item"]["hazards"] for a in db.list_pending_approvals() if a["kind"] == "monitor_stage"], [["사면"]])

    def test_single_sensor_and_dropout_hold(self):
        self.assertEqual(self.send("single", 4)["output"]["status"], "보류")
        self.assertEqual(db.list_pending_approvals(), [])

    def test_bad_and_skewed_readings_rejected(self):
        res = self.hub.ingest([{"node": "A", "sensor": "water_low", "value": 1, "ts": self.now + 3600},   # 시계 크게 틀림
                               {"node": "A", "sensor": "water_low", "value": float("nan")},
                               {"node": "A", "sensor": "water_low", "value": True},
                               {"node": "A" * 30, "sensor": "x", "value": 1},
                               "S,A,water_low,1",
                               {"node": "A", "sensor": "water_low", "value": 5, "ts": self.now + 2}])  # 조금 앞섬 → 서버 시각
        self.assertEqual((res["accepted"], res["rejected"]), (1, 5))
        self.assertEqual(self.hub.buffer[-1]["ts"], self.now)
        with self.assertRaises(ValueError):
            self.hub.ingest("not a list")
        with self.assertRaises(ValueError):
            self.hub.ingest([], rain_stage=9)

    def test_status_shows_latest_and_offline_nodes(self):
        self.send("dropout", 3)
        self.now += 40   # stale_sec(30) 넘게 아무것도 안 옴
        st = self.hub.status()
        self.assertEqual({n["node"]: n["online"] for n in st["nodes"]}, {"A": False, "B": False})
        self.assertEqual(next(s for s in st["sensors"] if s["key"] == "B.tilt")["value"], 0)


if __name__ == "__main__":
    unittest.main()
