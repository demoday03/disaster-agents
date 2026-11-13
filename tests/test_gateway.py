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
        self.assertEqual(G.parse_event("E,C,+1,42\r\n", 5.0), {"node": "C", "code": "+1", "seq": 42, "ts": 5.0})
        for bad in ("E,E,SOS", "E,E,SOS,x", "E,E,SOS,1000", "E,E,S;OS,1", "S,E,temp,3", "E,E,TOOLONGCODE,1"):
            self.assertIsNone(G.parse_event(bad, 0), bad)

    def test_parse_bluetooth_module_lines(self):
        """G 노드 외부 모듈(블루투스)이 보내는 여러 모양의 줄."""
        row = lambda sensor, value, node="G": {"node": node, "sensor": sensor, "value": value, "ts": 1.0}
        self.assertEqual(G.parse_module_line("S,G,temp,23.5\r\n", "G", 1.0), [row("temp", 23.5)])
        self.assertEqual(G.parse_module_line("temp=23.5,humidity=61", "G", 1.0), [row("temp", 23.5), row("humidity", 61.0)])
        self.assertEqual(G.parse_module_line("T:23.5 H:61; gas = 312", "G", 1.0), [row("T", 23.5), row("H", 61.0), row("gas", 312.0)])
        self.assertEqual(G.parse_module_line("온도=19", "G", 1.0), [row("온도", 19.0)])
        self.assertEqual(G.parse_module_line("23.5,61", "G", 1.0, ["temp", "humidity"]), [row("temp", 23.5), row("humidity", 61.0)])
        self.assertEqual(G.parse_module_line("23.5 61 7", "M", 1.0, ["temp"]), [row("temp", 23.5, "M"), row("v2", 61.0, "M"), row("v3", 7.0, "M")])
        for bad in ("", "hello", "AT+OK", "temp=abc", "1.2.3", "temp=1234567"):
            self.assertEqual(G.parse_module_line(bad, "G", 1.0), [], bad)
        sim = G.ModuleSimulator()
        self.assertEqual([r["sensor"] for r in G.parse_module_line(sim.line(), "G", 1.0)], ["temp", "humidity", "gas"])

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
        def sender(server, token, batch, rain, events):
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

    def test_serial_reconnects_after_usb_drop(self):
        class FakeSerial:
            def __init__(self, lines): self.lines = list(lines)
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def readline(self):
                r = self.lines.pop(0)
                if isinstance(r, Exception):
                    raise r
                return r
        opens = [OSError("포트 없음"),                                        # 처음부터 연결 실패
                 FakeSerial([b"S,A,water_low,5\r\n", OSError("장치가 명령을 인식하지 않습니다")]),
                 OSError("포트 없음"),
                 FakeSerial([b"S,B,tilt,1\r\n"])]
        def opener():
            r = opens.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        logs, sleeps = [], []
        got = []
        for line in G.serial_lines("COM9", 115200, retry_s=2, opener=opener, out=logs.append, sleep=sleeps.append):
            if line:
                got.append(line.strip())
            if len(got) == 2:
                break
        self.assertEqual(got, ["S,A,water_low,5", "S,B,tilt,1"])
        self.assertEqual(sleeps, [2, 2, 2])
        self.assertEqual(sum("[연결 끊김]" in m for m in logs), 2)                # 끊길 때마다 한 번만 알림
        self.assertEqual(sum("[재연결]" in m for m in logs), 2)

    def test_event_is_acked_deduped_and_sent_with_readings(self):
        written, sent = [], []
        def sender(server, token, batch, rain, events):
            sent.append((len(batch), [e["code"] for e in events]))
            return 200, {"output": {"status": "정상", "level": "정상"}, "events": [{"node": "E", "label": "SOS 요청"}]}
        gw = G.Gateway("http://x", "t" * 20, sender=sender, out=lambda m: None, clock=lambda: 0,
                       writer=lambda m: written.append(m) or True)
        for line in ("E,E,SOS,7", "E,E,SOS,7", "S,E,temp,300", "E,C,+1,7"):   # 두 번째는 무선 재전송
            gw.feed(line)
        self.assertEqual(written, ["K,E,7", "K,E,7", "K,C,7"])                 # 재전송에도 확인은 다시 보낸다
        gw.flush()
        self.assertEqual(sent, [(1, ["SOS", "+1"])])
        self.assertEqual(gw.pending_events, [])
        gw.flush()
        self.assertEqual(len(sent), 1)                                        # 보낼 것이 없으면 요청하지 않음

    def test_downlink_broadcasts_confirmed_level_and_safety_check(self):
        now = [0.0]
        written, logs = [], []
        replies = [(200, {"level": 0, "level_name": "정상", "safety_check": False}),
                   (200, {"level": 2, "level_name": "경계", "safety_check": True}),
                   (200, {"level": 2, "level_name": "경계", "safety_check": True}),
                   OSError("connection refused"), OSError("connection refused"),
                   (200, {"level": 2, "level_name": "경계", "safety_check": False})]
        def fetcher(server, token):
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        gw = G.Gateway("http://x", "t" * 20, out=logs.append, clock=lambda: now[0],
                       writer=lambda m: written.append(m) or True, fetcher=fetcher)
        self.assertEqual(gw.poll_downlink(), ["X"])                            # 시작할 때 현장도 정상으로 맞춤
        self.assertIsNone(gw.poll_downlink())                                  # DOWNLINK_S 안에는 묻지 않음
        now[0] += G.DOWNLINK_S
        self.assertEqual(gw.poll_downlink(), ["L2", "Q,D"])
        now[0] += G.DOWNLINK_S
        self.assertEqual(gw.poll_downlink(), [])                               # 바뀐 것 없음 → 보내지 않음
        for _ in range(2):
            now[0] += G.DOWNLINK_S
            self.assertIsNone(gw.poll_downlink())
        self.assertEqual(sum("[내려보내기 실패]" in m for m in logs), 1)
        now[0] += G.RESEND_LEVEL_S
        self.assertEqual(gw.poll_downlink(), ["L2"])                           # 유실 대비 주기적 재방송
        self.assertFalse(gw.sent_check)                                        # 안부 확인이 끝나면 다음 시작 때 다시 Q
        sim = G.Gateway("http://x", "t", out=lambda m: None, fetcher=fetcher)  # 기기 없는(가상 센서) 모드는 묻지 않음
        self.assertIsNone(sim.poll_downlink())

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

    def test_field_events_dedup_ack_and_never_change_stage(self):
        ev = lambda code, seq, node="E": {"node": node, "code": code, "seq": seq, "ts": self.now}
        res = self.hub.ingest([], events=[ev("SOS", 1), ev("SOS", 1), ev("IN", 2), ev("BOOM", 3), ev("SOS", True),
                                          "E,E,SOS,1", ev("SOS", 1000)])
        self.assertEqual([e["code"] for e in res["events"]], ["SOS", "IN"])     # 중복·모르는 코드·틀린 형식은 버림
        self.assertNotEqual(res["output"]["status"], "확정후보")                 # 현장 신호는 단계 판정에 쓰지 않음
        self.assertEqual(db.list_pending_approvals(), [])
        st = self.hub.status()
        self.assertEqual((st["open_urgent"], [e["code"] for e in st["events"]]), (1, ["IN", "SOS"]))   # 최신 순
        self.assertTrue(next(n for n in st["nodes"] if n["node"] == "E")["online"])
        sos = next(e for e in st["events"] if e["code"] == "SOS")
        self.assertEqual(self.hub.ack_event(sos["id"], "담당자")["acked_by"], "담당자")
        self.assertEqual(self.hub.status()["open_urgent"], 0)
        with self.assertRaises(KeyError):
            self.hub.ack_event("EV999", "담당자")
        self.now += 61                                                          # 재전송 판단 시간이 지나면 같은 번호도 새 신호
        self.assertEqual(len(self.hub.ingest([], events=[ev("SOS", 1)])["events"]), 1)
        with self.assertRaises(ValueError):
            self.hub.ingest([], events="E,E,SOS,1")
        self.hub.annotate(sos["id"], applied=True, note="봉사 탭으로 넘김")
        self.assertEqual(next(e for e in self.hub.status()["events"] if e["id"] == sos["id"])["note"], "봉사 탭으로 넘김")

    def test_window_gives_recent_max(self):
        for v in (50, 320, 90):
            self.hub.ingest([{"node": "A", "sensor": "water_low", "value": v, "ts": self.now}])
            self.now += 10
        w = self.hub.window(25)                                                 # 마지막 25초: 320(20초 전)·90(10초 전)
        self.assertEqual((w["A.water_low"]["max"], w["A.water_low"]["age_s"]), (320, 10))
        self.assertEqual(self.hub.window(5), {})
        self.assertEqual(self.hub.thresholds()["A.water_low"], 200)

    def test_downlink_only_confirmed_stage_and_safety_check(self):
        self.assertEqual(self.hub.downlink(), {"level": 0, "level_name": "정상", "safety_check": False})
        db.put_record("stage", "current", {"level": "위험", "hazards": ["침수"], "confirmed": False})
        self.assertEqual(self.hub.downlink()["level"], 0)                       # 확정 전 후보는 내려보내지 않음
        db.put_record("stage", "current", {"level": "경계", "hazards": ["침수"], "confirmed": True})
        db.put_record("check", "H1", {"household": "H1", "state": "대기"})
        self.assertEqual(self.hub.downlink(), {"level": 2, "level_name": "경계", "safety_check": True})
        db.put_record("check", "H1", {"household": "H1", "state": "안전"})
        self.assertFalse(self.hub.downlink()["safety_check"])

    def test_external_module_values_shown_not_judged(self):
        """감시 설정에 없는 외부 모듈 값(G 노드 블루투스)은 받아서 보여 주되 판정에는 쓰지 않는다."""
        sent = []
        gw = G.Gateway("http://x", "t" * 20, sender=lambda *a: (sent.append(a[2]), (200, {}))[1], out=lambda *a: None, clock=lambda: self.now)
        self.assertEqual(gw.feed_module("temp=31.5,gas=420"), 2)
        self.assertEqual(gw.feed_module("AT+OK"), 0)
        gw.flush()
        base = self.hub.ingest([])["output"]   # G 값이 없을 때의 판정(A·B 값이 없어 보류)
        res = self.hub.ingest(sent[0])
        self.assertEqual(res["accepted"], 2)
        st = self.hub.status()
        self.assertEqual([(x["key"], x["value"], x["online"]) for x in st["extra"]], [("G.gas", 420.0, True), ("G.temp", 31.5, True)])
        self.assertIn("G", [n["node"] for n in st["nodes"]])
        self.assertNotIn("G.temp", [s["key"] for s in st["sensors"]])
        self.assertEqual((res["output"]["level"], res["output"]["status"]), (base["level"], base["status"]))   # 판정은 그대로
        self.now += 40
        self.assertFalse(any(x["online"] for x in self.hub.status()["extra"]))

    def test_status_shows_latest_and_offline_nodes(self):
        self.send("dropout", 3)
        self.now += 40   # stale_sec(30) 넘게 아무것도 안 옴
        st = self.hub.status()
        self.assertEqual({n["node"]: n["online"] for n in st["nodes"]}, {"A": False, "B": False})
        self.assertEqual(next(s for s in st["sensors"] if s["key"] == "B.tilt")["value"], 0)


if __name__ == "__main__":
    unittest.main()
