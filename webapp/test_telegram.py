"""텔레그램 봇 알림 시험. 실제 텔레그램 대신 이 PC에 가짜 텔레그램 API를 띄워 서버가 보내는 요청을 받아 본다.
실행: python -m unittest webapp.test_telegram -v"""
import json, re, threading, time, unittest, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from webapp.test_server import Client, ServerBase

FAKE_PORT, APP_PORT = 18790, 18791
CALLS = []   # (method, payload)
TIMES = []


class FakeTelegram(BaseHTTPRequestHandler):
    def do_POST(self):
        method = self.path.rsplit("/", 1)[-1]
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        CALLS.append((method, payload)); TIMES.append(time.time())
        if method == "getMe":
            body = {"ok": True, "result": {"id": 1, "is_bot": True, "username": "test_disaster_bot"}}
        elif method == "sendMessage" and payload.get("chat_id") == 999:   # 봇을 차단한 사용자
            self.send_response(403); self.end_headers(); self.wfile.write(b'{"ok":false,"error_code":403}'); return
        elif method == "sendMessage":   # 진짜 텔레그램처럼 보낸 메시지 번호를 돌려준다(답장 확인용)
            body = {"ok": True, "result": {"message_id": 1000 + len(CALLS)}}
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
                 "TELEGRAM_API_BASE": f"http://127.0.0.1:{FAKE_PORT}", "TELEGRAM_WEBHOOK_URL": f"http://127.0.0.1:{APP_PORT}",
                 "TELEGRAM_WATCH_S": "0.3"}   # 신고 진행 알림을 빨리 확인

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
        for _ in range(80):
            hit = [c for c in CALLS if pred(*c)]
            if hit:
                return hit
            time.sleep(0.1)
        self.fail(f"가짜 텔레그램이 받지 못함: {what}\n받은 것: " + "\n".join(f"{t:.2f} {m} {json.dumps(p, ensure_ascii=False)[:120]}" for (m, p), t in list(zip(CALLS, TIMES))[-8:]))

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

    def secret(self):
        if not hasattr(type(self), "_secret"):
            type(self)._secret = self.wait_for(lambda m, p: m == "setWebhook", "웹훅 등록")[0][1]["secret_token"]
        return type(self)._secret

    def press(self, chat, data, mid=50):
        cq = {"id": f"cq-{chat}-{time.time()}", "data": data, "message": {"message_id": mid, "chat": {"id": chat}}}
        self.assertEqual(self.update({"update_id": 7, "callback_query": cq}, self.secret()), 200)
        return cq["id"]

    def sent_to(self, chat, pred, what, method="sendMessage"):
        return self.wait_for(lambda m, p: m == method and p.get("chat_id") == chat and pred(p), what)[-1][1]

    def test_report_by_buttons_and_input_box(self):
        secret = self.secret()
        self.wait_for(lambda m, p: m == "setMyCommands" and any(c["command"] == "report" for c in p["commands"]), "명령 메뉴")
        # ① 메뉴의 🆘 신고하기 → 무슨 일인지 버튼(여러 개 고르기)
        self.assertEqual(self.update(self.msg(333, "🆘 신고하기"), secret), 200)
        kinds = self.sent_to(333, lambda p: "무슨 일인가요" in p["text"], "종류 고르기")
        rows = kinds["reply_markup"]["inline_keyboard"]
        fid = rows[0][0]["callback_data"].split(":")[1]
        self.press(333, f"rk:{fid}:flood"); self.press(333, f"rk:{fid}:life")
        ed = self.sent_to(333, lambda p: "고른 것: 사람이 다치거나 갇혔어요, 물이 차요" in p["text"], "고른 것 표시", "editMessageText")
        self.assertIn("119", ed["text"])                                                       # 인명 피해를 고르면 119 먼저
        self.assertTrue(ed["reply_markup"]["inline_keyboard"][0][0]["text"].startswith("✅"))
        # ② 위치: 입력창 안내문 + 지금 위치 보내기 버튼
        self.press(333, f"rn:{fid}")
        ask = self.sent_to(333, lambda p: "어디인가요" in p["text"], "위치 묻기")["reply_markup"]
        self.assertTrue(ask["keyboard"][0][0]["request_location"])
        self.assertIn("옥포동", ask["input_field_placeholder"])
        self.update(self.msg(333, "옥포동 시장 앞"), secret)
        ask = self.sent_to(333, lambda p: "더 알려 줄 것" in p["text"], "자세히 묻기")["reply_markup"]
        self.assertEqual(ask["keyboard"][0][0]["text"], "건너뛰기")
        # ③ 더 알려 줄 것 → 확인 → 보내기
        self.update(self.msg(333, "2명 갇힘, 물이 허리까지"), secret)
        conf = self.sent_to(333, lambda p: "이렇게 보낼까요" in p["text"], "확인")
        self.assertIn("위치: 옥포동 시장 앞 · 사람이 갇혔거나 다쳤어요(구조 요청) · 물이 차고 있어요(침수). 2명 갇힘", conf["text"])
        self.press(333, f"rs:{fid}", mid=77)
        rec = self.sent_to(333, lambda p: "접수번호" in p["text"] and p["message_id"] == 77, "접수 결과", "editMessageText")
        iid = re.search(r"R-[0-9A-F]{8}", rec["text"]).group(0)
        self.assertIn("인명구조", rec["text"])
        self.assertEqual([b["callback_data"] for b in rec["reply_markup"]["inline_keyboard"][0]], [f"pg:{iid}", f"cx:{iid}"])
        self.sent_to(333, lambda p: p.get("reply_markup", {}).get("keyboard", [[{}]])[0][0].get("text") == "🆘 신고하기", "메뉴 돌아오기")
        o = self.get(f"/api/incidents/{iid}")[1]["result"]["output"]
        self.assertEqual((o["type"], o["channel"]), ("인명구조", "텔레그램"))                    # 담당자 화면에 '텔레그램 신고'
        self.press(333, f"rk:{fid}:road")                                                       # 다 쓴 양식의 버튼은 안내만
        self.wait_for(lambda m, p: m == "answerCallbackQuery" and p["show_alert"] and "이전 신고 양식" in p["text"], "이전 양식")
        # 담당자가 출동을 승인하면 → 텔레그램으로 알림
        for _ in range(50):
            pend = [a for a in self.get("/api/approvals")[1]["items"]
                    if (a.get("incident_id") or a["item"].get("incident_id")) == iid and a["kind"] == "dispatch_public"]
            if pend:
                break
            time.sleep(0.1)
        self.assertEqual(self.post(f"/api/approvals/{pend[0]['id']}/resolve", {"approved": True, "reason": "확인함"})[0], 200)
        self.sent_to(333, lambda p: iid in p["text"] and "출동이 승인됐어요" in p["text"], "출동 승인 알림")
        self.update(self.msg(333, "📋 내 신고"), secret)
        self.sent_to(333, lambda p: "내 신고" in p["text"] and iid in p["text"], "내 신고")
        # 신고 취소: 한 번 더 묻고 취소
        self.press(333, f"cx:{iid}")
        ask = self.sent_to(333, lambda p: "취소할까요" in p["text"], "취소 확인")
        self.press(333, ask["reply_markup"]["inline_keyboard"][0][0]["callback_data"], mid=88)
        self.sent_to(333, lambda p: p["message_id"] == 88 and "취소했어요" in p["text"], "취소 완료", "editMessageText")
        self.assertTrue(self.get(f"/api/incidents/{iid}")[1]["result"]["output"]["cancelled_by_reporter"])
        self.press(444, f"cx:{iid}")                                                            # 남의 신고는 취소 못 함
        self.wait_for(lambda m, p: m == "answerCallbackQuery" and "이 대화방에서 보낸 신고만" in p["text"], "남의 신고")

    def test_quick_text_report_and_missing_location(self):
        secret = self.secret()
        # 그냥 글을 보내면 '이대로 신고할까요?'
        self.update(self.msg(555, "물이 차서 지하 주차장이 잠기고 있어요"), secret)
        q = self.sent_to(555, lambda p: "바로 신고할까요" in p["text"], "바로 신고 묻기")
        self.press(555, q["reply_markup"]["inline_keyboard"][0][0]["callback_data"], mid=90)
        rec = self.sent_to(555, lambda p: "접수번호" in p["text"], "접수 결과", "editMessageText")
        iid = re.search(r"R-[0-9A-F]{8}", rec["text"]).group(0)
        # 위치가 없으면 입력창(답장)을 열어 바로 묻고, 지금 위치를 보내면 보완된다
        ask = self.sent_to(555, lambda p: "정보가 빠졌어요" in p["text"], "보완 묻기")
        self.assertTrue(ask["reply_markup"]["force_reply"])
        loc = {"update_id": 8, "message": {"chat": {"id": 555, "type": "private"},
                                           "location": {"latitude": 34.88812, "longitude": 128.62345, "horizontal_accuracy": 12}}}
        self.update(loc, secret)
        self.sent_to(555, lambda p: "보완 내용을 넣었어요" in p["text"], "보완 완료")
        o = self.get(f"/api/incidents/{iid}")[1]["result"]["output"]
        self.assertIn("GPS 34.88812, 128.62345", o["text"])
        self.assertNotEqual(o["state"], "보완 필요")
        self.press(555, f"cx:{iid}")                                                            # 보완한 뒤에도 취소 버튼이 듣는다
        self.sent_to(555, lambda p: "취소할까요" in p["text"], "보완 뒤 취소 확인")

    def test_who_sent_it_household_reporter_and_questions(self):
        """누가 보냈는지: 가구 코드 연결(/code)·이름 없는 신고자 번호, 담당자가 되물으면 답이 신고에 붙는다."""
        secret, anon = self.secret(), Client(self.base)
        _, h = anon.post("/api/public/households", {"address": "옥포동 시장 뒤 주택", "floor": "반지하", "members": 2})
        # 메뉴 '🏠 우리 집 연결' → 입력창(답장)에 코드 → 연결. 틀린 코드는 안내만
        self.update(self.msg(666, "/code H-0000000000"), secret)
        self.sent_to(666, lambda p: "우리 집 번호를 찾을 수 없어요" in p["text"], "틀린 코드")
        self.update(self.msg(666, "🏠 우리 집 연결"), secret)
        ask = self.sent_to(666, lambda p: "우리 집 번호(예: H-3F9A1C2B7D)를 아래 입력창" in p["text"], "코드 묻기")
        self.assertTrue(ask["reply_markup"]["force_reply"])
        self.update(self.msg(666, h["id"][2:].lower()), secret)                                   # 소문자·H- 빠뜨려도
        self.sent_to(666, lambda p: "옥포동 시장 뒤 주택" in p["text"] and "연결했어요" in p["text"], "연결 완료")
        # 신고: 위치에 '🏠 우리 집' 버튼 → 신고문에 우리 집, 담당자에게는 가구·신고자 번호
        self.update(self.msg(666, "🆘 신고하기"), secret)
        fid = self.sent_to(666, lambda p: "무슨 일인가요" in p["text"], "종류")["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
        self.press(666, f"rk:{fid}:flood"); self.press(666, f"rn:{fid}")
        kb = self.sent_to(666, lambda p: "어디인가요" in p["text"], "위치 묻기")["reply_markup"]["keyboard"]
        self.assertEqual(kb[0][0]["text"], "🏠 우리 집")
        self.update(self.msg(666, "🏠 우리 집"), secret)
        self.sent_to(666, lambda p: "더 알려 줄 것" in p["text"], "자세히")
        self.update(self.msg(666, "건너뛰기"), secret)
        conf = self.sent_to(666, lambda p: "이렇게 보낼까요" in p["text"], "확인")
        self.assertIn("위치: 옥포동 시장 뒤 주택 · 물이 차고 있어요(침수) (우리 집 · 반지하)", conf["text"])
        self.press(666, f"rs:{fid}", mid=91)
        rec = self.sent_to(666, lambda p: p.get("message_id") == 91 and "접수번호" in p["text"], "접수", "editMessageText")
        self.assertIn(f"우리 집({h['id']})", rec["text"])
        iid = re.search(r"R-[0-9A-F]{8}", rec["text"]).group(0)
        o = self.get(f"/api/incidents/{iid}")[1]["result"]["output"]
        staff = next(x["output"] for x in self.get("/api/incidents")[1]["items"] if x["output"]["id"] == iid)
        self.assertEqual((staff["household"], staff["household_place"], staff["channel"]), (h["id"], "옥포동 시장 뒤 주택", "텔레그램"))
        self.assertRegex(staff["reporter"], r"^T-[0-9A-F]{4}$")
        for k in ("household", "reporter", "questions"):
            self.assertNotIn(k, anon.get(f"/api/incidents/{iid}")[1]["result"]["output"])          # 공개 조회에는 없음
        # 연결하지 않은 대화방도 같은 사람이면 같은 신고자 번호
        nums = set()
        for i, t in enumerate(("위치: 옥포동 다리 밑 · 길이 무너져서 차가 못 지나가요", "위치: 옥포동 다리 위 · 돌이 계속 떨어져요")):
            self.update(self.msg(777, t), secret)
            q = self.sent_to(777, lambda p, t=t: "바로 신고할까요" in p["text"] and t in p["text"], "바로 신고")
            self.press(777, q["reply_markup"]["inline_keyboard"][0][0]["callback_data"], mid=200 + i)
            r = self.sent_to(777, lambda p, i=i: p.get("message_id") == 200 + i and "접수번호" in p["text"], "접수", "editMessageText")
            rid = re.search(r"R-[0-9A-F]{8}", r["text"]).group(0)
            nums.add(next(x["output"] for x in self.get("/api/incidents")[1]["items"] if x["output"]["id"] == rid).get("reporter"))
        self.assertEqual(len(nums), 1)
        self.assertNotIn(staff["reporter"], nums)
        # 담당자가 되묻기 → 텔레그램으로 질문(입력창) → 답장하면 그 신고에 붙는다
        self.assertEqual(self.post(f"/api/incidents/{iid}/ask", {"question": ""})[0], 400)
        code, r = self.post(f"/api/incidents/{iid}/ask", {"question": "물이 어디까지 찼나요?"})
        self.assertEqual((code, r["sent_to"]), (200, 1))
        self.wait_for(lambda m, p: m == "sendMessage" and p.get("chat_id") == 666 and "물이 어디까지" in p.get("text", ""), "질문")
        idx = next(i for i, (m, p) in enumerate(CALLS) if m == "sendMessage" and p.get("chat_id") == 666 and "물이 어디까지" in p.get("text", ""))
        time.sleep(0.3)   # 질문 메시지 번호가 저장될 때까지
        reply = {"update_id": 9, "message": {"chat": {"id": 666, "type": "private"}, "text": "무릎까지요, 할머니가 2층에 계세요",
                                             "reply_to_message": {"message_id": 1000 + idx + 1}}}
        self.update(reply, secret)
        self.sent_to(666, lambda p: "답을 담당자에게 보냈어요" in p["text"], "답 전달")
        staff = next(x["output"] for x in self.get("/api/incidents")[1]["items"] if x["output"]["id"] == iid)
        self.assertEqual((staff["questions"][0]["q"], staff["questions"][0]["answers"][0]["text"]),
                         ("물이 어디까지 찼나요?", "무릎까지요, 할머니가 2층에 계세요"))
        _, web = anon.post("/api/incidents", {"text": "위치: 가람초 정문 · 길이 막혔어요 (웹 신고)"})
        self.assertEqual(self.post(f"/api/incidents/{web['output']['id']}/ask", {"question": "몇 시부터요?"})[0], 409)   # 웹 신고는 못 물음

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
        ask = self.wait_for(lambda m, p: m == "sendMessage" and p["chat_id"] == 111 and "inline_keyboard" in p.get("reply_markup", {}),
                            "안부 확인 버튼")[0][1]
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
