"""노드 A 수위 주의(310 초과)·위험(320 초과, FLOOD 신호·확정 후보) → 담당자 텔레그램 단체방 알림. 실제 텔레그램은 부르지 않는다(보낸 것만 모음).
서버 모듈을 불러오면 DB를 만들기 때문에 다른 테스트처럼 별도 프로세스에서 임시 DB로 돌린다."""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r"""
import json, time
from webapp import server as s

sent = []
s.TELEGRAM.token = "TEST"
s.TELEGRAM.call = lambda method, payload=None, timeout=15: (sent.append(payload["text"]), {"ok": True, "result": {"message_id": len(sent)}})[1]
s.TELEGRAM.staff._run = lambda fn, *a: fn(*a)   # 스레드 없이 바로 보냄(순서 확인용)
s.database.put_record("tg_room", "-100", {"chat_id": "-100", "kind": "staff", "title": "담당자방", "linked_at": time.time() - 60})

clock = [time.time() - 6]   # 1초 간격 측정처럼 시각을 0.5초씩 늘린다(묶음끼리 순서가 섞이지 않게)

def batch(depths, events=()):
    rows = []
    for d in depths:
        clock[0] += 0.5
        rows.append({"node": "A", "sensor": "depth", "value": d, "ts": clock[0]})
    return s.ingest_sensor_batch({"readings": rows, "events": [dict(e, ts=clock[0]) for e in events]})

out = {}
out["calm"] = batch([100])                                                   # 정상: 알림 없음
out["flood"] = batch([372], [{"node": "A", "code": "FLOOD", "seq": 1}])      # FLOOD 신호 → 바로 알림
out["again"] = batch([375, 380, 390])                                        # 확정 후보지만 이미 알림 → 다시 안 보냄
out["drain"] = batch([100], [{"node": "A", "code": "DRAIN", "seq": 2}])      # 내려감 → ✅ 한 번
out["lost"] = batch([380, 385, 390])                                         # FLOOD 신호가 유실돼도 확정 후보면 알림
ev = {e["code"]: e for e in s.SENSORS.status()["events"]}
print(json.dumps({"sent": sent, "notes": {k: v.get("flood_alert") for k, v in out.items()},
                  "flood_event": ev.get("FLOOD"), "drain_event": ev.get("DRAIN")}, ensure_ascii=False))
"""


class TestFloodAlert(unittest.TestCase):
    def test_flood_signal_alerts_staff_room_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "DISASTER_DB_PATH": str(Path(tmp) / "t.db"), "LLM_MODE": "mock",
                   "PYTHONIOENCODING": "utf-8", "TELEGRAM_BOT_TOKEN": ""}
            p = subprocess.run([sys.executable, "-c", SCRIPT], cwd=ROOT, env=env, capture_output=True, timeout=60,
                               encoding="utf-8", errors="replace")
            self.assertEqual(p.returncode, 0, p.stderr)
            r = json.loads(p.stdout.strip().splitlines()[-1])
        sent = r["sent"]
        self.assertEqual(len(sent), 3, sent)   # 위험 · 내려감 · (유실 뒤) 다시 위험
        self.assertTrue(sent[0].startswith("🚨 침수 센서 위험 · 노드 A"))
        self.assertIn("수심 372", sent[0]); self.assertIn("수심 100", sent[1]); self.assertIn("위험 기준 320", sent[0]); self.assertIn("배수 모터", sent[0])
        self.assertTrue(sent[1].startswith("✅ 침수 센서: 수위가 내려갔어요"))
        self.assertTrue(sent[2].startswith("🚨 침수 센서 위험 · 노드 A"))
        self.assertEqual((r["notes"]["calm"], r["notes"]["again"]), (None, None))
        self.assertEqual(r["notes"]["flood"], "담당자 텔레그램 방에 알림")
        self.assertTrue(r["flood_event"]["urgent"])
        self.assertEqual(r["flood_event"]["note"], "담당자 텔레그램 방에 알림")
        self.assertEqual(r["drain_event"]["label"], "수위 내려감 · 배수 모터 끔")


if __name__ == "__main__":
    unittest.main()


STEPS = r"""
import json, time
from webapp import server as s

sent = []
s.TELEGRAM.token = "TEST"
s.TELEGRAM.call = lambda method, payload=None, timeout=15: (sent.append(payload["text"]), {"ok": True, "result": {"message_id": len(sent)}})[1]
s.TELEGRAM.staff._run = lambda fn, *a: fn(*a)
s.database.put_record("tg_room", "-100", {"chat_id": "-100", "kind": "staff", "title": "담당자방", "linked_at": time.time() - 60})
clock = [time.time() - 10]
out = []
for d in [315, 318, 325, 315, 305, 302, 280]:
    clock[0] += 0.5
    n = len(sent)
    s.ingest_sensor_batch({"readings": [{"node": "A", "sensor": "depth", "value": d, "ts": clock[0]}], "events": []})
    out.append([d, sent[n].splitlines()[0] if len(sent) > n else None])
print(json.dumps(out, ensure_ascii=False))
"""


class TestCautionAlert(unittest.TestCase):
    def test_caution_then_danger_then_down_with_hysteresis(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "DISASTER_DB_PATH": str(Path(tmp) / "t.db"), "LLM_MODE": "mock",
                   "PYTHONIOENCODING": "utf-8", "TELEGRAM_BOT_TOKEN": ""}
            p = subprocess.run([sys.executable, "-c", STEPS], cwd=ROOT, env=env, capture_output=True, timeout=60,
                               encoding="utf-8", errors="replace")
            self.assertEqual(p.returncode, 0, p.stderr)
            steps = json.loads(p.stdout.strip().splitlines()[-1])
            got = {}
            for d, head in steps:   # 같은 값이 두 번 나오면 처음 것
                got.setdefault(d, head)
        self.assertTrue(got[315].startswith("⚠️ 침수 센서 주의"))            # 310 넘음 → 주의
        self.assertIsNone(got[318])                                          # 주의가 이어지면 다시 안 보냄
        self.assertTrue(got[325].startswith("🚨 침수 센서 위험"))             # 320 넘음 → 바로 위험
        self.assertEqual(steps[3], [315, None])                              # 기준 근처 흔들림(310 위)은 그대로
        self.assertTrue(got[305].startswith("⬇️ 침수 센서: 위험 기준 아래로"))
        self.assertIsNone(got[302])                                          # 300 위: 아직 주의
        self.assertTrue(got[280].startswith("✅ 침수 센서: 수위가 내려갔어요"))


PERSONAL = r"""
import json, time
from webapp import server as s

sent = []
s.TELEGRAM.token = "TEST"
s.TELEGRAM.call = lambda method, payload=None, timeout=15: (sent.append([payload["chat_id"], payload["text"]]), {"ok": True, "result": {"message_id": len(sent)}})[1]
s.TELEGRAM.staff._run = lambda fn, *a: fn(*a)
s.database.put_record("tg_sub", "777", {"chat_id": 777, "since": time.time()})   # 담당자 방 없이 봇에 /start 한 개인만
t = time.time()
r1 = s.ingest_sensor_batch({"readings": [{"node": "A", "sensor": "depth", "value": 315, "ts": t}], "events": []})
r2 = s.ingest_sensor_batch({"readings": [{"node": "A", "sensor": "depth", "value": 350, "ts": t + 1}], "events": []})
print(json.dumps({"sent": sent, "notes": [r1.get("flood_alert"), r2.get("flood_alert")]}, ensure_ascii=False))
"""


class TestPersonalAlert(unittest.TestCase):
    def test_personal_chat_gets_caution_and_danger(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "DISASTER_DB_PATH": str(Path(tmp) / "t.db"), "LLM_MODE": "mock",
                   "PYTHONIOENCODING": "utf-8", "TELEGRAM_BOT_TOKEN": ""}
            p = subprocess.run([sys.executable, "-c", PERSONAL], cwd=ROOT, env=env, capture_output=True, timeout=60,
                               encoding="utf-8", errors="replace")
            self.assertEqual(p.returncode, 0, p.stderr)
            r = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual([c for c, _ in r["sent"]], [777, 777])
        self.assertTrue(r["sent"][0][1].startswith("⚠️ 침수 센서 주의"))
        self.assertTrue(r["sent"][1][1].startswith("🚨 침수 센서 위험"))
        self.assertIn("119", r["sent"][1][1]); self.assertNotIn("관리자 화면", r["sent"][1][1])   # 개인에게는 관리자 안내 대신 119
        self.assertEqual(r["notes"], ["개인 텔레그램 1명에 알림"] * 2)
