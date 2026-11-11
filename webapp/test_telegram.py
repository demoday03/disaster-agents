"""텔레그램 봇 알림 시험. 실제 텔레그램 대신 이 PC에 가짜 텔레그램 API를 띄워 서버가 보내는 요청을 받아 본다.
실행: python -m unittest webapp.test_telegram -v"""
import json, threading, time, unittest, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from webapp.test_server import Client, ServerBase

FAKE_PORT, APP_PORT = 18790, 18791
CALLS = []   # (method, payload)


class FakeTelegram(BaseHTTPRequestHandler):
    def do_POST(self):
        method = self.path.rsplit("/", 1)[-1]
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        CALLS.append((method, payload))
        if method == "getMe":
            body = {"ok": True, "result": {"id": 1, "is_bot": True, "username": "test_disaster_bot"}}
        elif method == "sendMessage" and payload.get("chat_id") == 999:   # 봇을 차단한 사용자
            self.send_response(403); self.end_headers(); self.wfile.write(b'{"ok":false,"error_code":403}'); return
        else:
            body = {"ok": True, "result": True}
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def log_message(self, *a):
        pass


class TestTelegramBot(ServerBase):
    port = APP_PORT
    extra_env = {"WEBAPP_INCIDENT_PER_MIN": "1000", "TELEGRAM_BOT_TOKEN": "123:TEST",
                 "TELEGRAM_API_BASE": f"http://127.0.0.1:{FAKE_PORT}", "TELEGRAM_WEBHOOK_URL": f"http://127.0.0.1:{APP_PORT}"}

    @classmethod
    def setUpClass(cls):
        cls.fake = ThreadingHTTPServer(("127.0.0.1", FAKE_PORT), FakeTelegram)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.fake.shutdown()

    def wait_for(self, pred, what):
        for _ in range(60):
            hit = [c for c in CALLS if pred(*c)]
            if hit:
                return hit
            time.sleep(0.1)
        self.fail(f"가짜 텔레그램이 받지 못함: {what}")

    def update(self, body, secret):
        req = urllib.request.Request(self.base + "/api/telegram/webhook", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Telegram-Bot-Api-Secret-Token": secret})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def msg(self, chat, text):
        return {"update_id": int(time.time() * 1000) % 10**9, "message": {"chat": {"id": chat, "type": "private"}, "text": text}}

    def test_subscribe_notify_and_answer_safety_check(self):
        hook = self.wait_for(lambda m, p: m == "setWebhook", "웹훅 등록")[0][1]
        self.assertEqual(hook["url"], f"http://127.0.0.1:{APP_PORT}/api/telegram/webhook")
        secret = hook["secret_token"]
        anon = Client(self.base)
        self.assertEqual(anon.get("/api/public/status")[1]["telegram_bot"], "test_disaster_bot")   # 주민 화면의 연결 링크용
        self.assertEqual(self.update(self.msg(111, "/start"), "wrong-secret"), 403)                  # 텔레그램이 아닌 요청
        # 우리 집과 연결해 구독(주민 화면 링크 t.me/봇?start=가구ID) + 집 없이 구독
        _, h = anon.post("/api/public/households", {"address": "가람초 뒤 주택", "floor": "반지하", "members": 2})
        self.assertEqual(self.update(self.msg(111, "/start " + h["id"]), secret), 200)
        self.assertEqual(self.update(self.msg(222, "/start"), secret), 200)
        self.assertEqual(self.update(self.msg(999, "/start"), secret), 200)
        self.wait_for(lambda m, p: m == "sendMessage" and p["chat_id"] == 111 and "우리 집" in p["text"], "연결 안내")
        # 담당자 공지 → 구독자 모두에게
        self.post("/api/announcements", {"text": "하천변 산책로를 통제합니다", "audience": "all", "zone": "all", "urgent": True})
        for chat in (111, 222):
            self.wait_for(lambda m, p, c=chat: m == "sendMessage" and p["chat_id"] == c and "긴급 공지" in p["text"], f"{chat} 공지")
        self.post("/api/announcements", {"text": "봉사자 집결 안내", "audience": "volunteer", "zone": "all", "urgent": False})
        time.sleep(0.5)
        self.assertFalse([p for m, p in CALLS if m == "sendMessage" and "봉사자 집결" in p["text"]])   # 봉사자 공지는 주민에게 안 보냄
        # 안부 확인 → 연결된 집에만 '안전합니다' 버튼, 누르면 응답 기록
        self.post("/api/welfare/checks/start", {"zone": "all", "timeout_s": 600})
        ask = self.wait_for(lambda m, p: m == "sendMessage" and p["chat_id"] == 111 and "reply_markup" in p, "안부 확인 버튼")[0][1]
        data = ask["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.assertEqual(data, "safe:" + h["id"])
        other = {"update_id": 5, "callback_query": {"id": "cq0", "data": data, "message": {"chat": {"id": 222}}}}
        self.assertEqual(self.update(other, secret), 200)                                             # 남의 집 버튼은 무시
        self.assertEqual(anon.get(f"/api/public/households/{h['id']}")[1]["check"]["state"], "대기")
        mine = {"update_id": 6, "callback_query": {"id": "cq1", "data": data, "message": {"chat": {"id": 111}}}}
        self.assertEqual(self.update(mine, secret), 200)
        self.assertEqual(anon.get(f"/api/public/households/{h['id']}")[1]["check"]["state"], "안전")
        self.wait_for(lambda m, p: m == "answerCallbackQuery" and p["callback_query_id"] == "cq1", "버튼 응답")
        # 위기 단계 확정 → 알림, /상황 → 지금 상황
        self.update(self.msg(222, "/상황"), secret)
        self.wait_for(lambda m, p: m == "sendMessage" and p["chat_id"] == 222 and "위기 단계" in p["text"], "상황 답")
        # 차단한 사용자(403)는 구독에서 지우고, /stop 하면 더 보내지 않는다
        self.assertEqual(self.update(self.msg(222, "/stop"), secret), 200)
        time.sleep(0.5)
        CALLS.clear()
        self.post("/api/announcements", {"text": "두 번째 공지", "audience": "citizen", "zone": "all", "urgent": False})
        self.wait_for(lambda m, p: m == "sendMessage" and p["chat_id"] == 111 and "두 번째 공지" in p["text"], "구독자 공지")
        time.sleep(0.5)
        self.assertFalse([p for m, p in CALLS if m == "sendMessage" and p["chat_id"] in (222, 999)])
        health = anon.get("/api/health")[1]["telegram"]
        self.assertEqual((health["enabled"], health["mode"], health["subscribers"]), (True, "webhook", 1))
        self.assertNotIn("123:TEST", json.dumps(health))                                             # 토큰은 내보내지 않음


if __name__ == "__main__":
    unittest.main()
