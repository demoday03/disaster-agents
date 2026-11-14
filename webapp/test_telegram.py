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
        elif method == "exportChatInviteLink":   # 비공개 채널의 초대 링크
            body = {"ok": True, "result": "https://t.me/+TESTINVITE"}
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
        offer = self.sent_to(333, lambda p: "내 연락처를 보낼까요" in p["text"], "연락처 묻기(선택)")["reply_markup"]
        self.assertTrue(offer["keyboard"][0][0]["request_contact"])
        self.update(self.msg(333, "보내지 않을래요"), secret)                                     # 안 보내도 된다
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


class FakeDB:
    """records 표 흉내(담당자 방 규칙 시험용)."""
    def __init__(self):
        self.r = {}

    def put_record(self, kind, rid, data):
        self.r[(kind, str(rid))] = json.loads(json.dumps(data))
        return data

    def get_record(self, kind, rid):
        v = self.r.get((kind, str(rid)))
        return None if v is None else json.loads(json.dumps(v))

    def list_records(self, kind):
        return [json.loads(json.dumps(v)) for (k, _), v in self.r.items() if k == kind]

    def delete_record(self, kind, rid):
        return self.r.pop((kind, str(rid)), None) is not None

    def log_audit(self, *a):
        pass


class FakeBot:
    def __init__(self):
        self.db, self.username, self.public_base, self.hooks, self.calls = FakeDB(), "test_bot", "", {}, []
        self.status_fn = lambda: {"stage": {"level": "경계", "confirmed": True, "hazards": ["침수"]}}

    def call(self, method, payload=None, timeout=15):
        self.calls.append((method, payload))
        return {"ok": True, "result": {"message_id": len(self.calls)}}

    def send(self, chat, text, buttons=None, markup=None, bulk=False):
        self.calls.append(("send", {"chat_id": chat, "text": text}))

    def channel_linked(self, link):
        self.calls.append(("channel_linked", {"link": link}))


class TestStaffRoomRules(unittest.TestCase):
    """담당자 방 알림 규칙: 매우 급한 신고만 바로, 많으면 묶어서, 아무도 안 보면 한 번 더, 나머지는 요약만."""

    def setUp(self):
        from webapp.telegram_staff import StaffDesk
        self.bot = FakeBot()
        self.desk = StaffDesk(self.bot, {"TELEGRAM_STAFF_BATCH": "3", "TELEGRAM_STAFF_REMIND_S": "300", "TELEGRAM_STAFF_DIGEST_S": "1800"})
        self.items = []
        self.bot.hooks["staff_view"] = lambda: [dict(x) for x in self.items]
        code = self.desk.new_code("staff", "담당자")["code"]
        self.desk.handle_group({"chat": {"id": -100, "type": "supergroup", "title": "상황실"}, "from": {"id": 1, "first_name": "김"},
                                "text": f"/start@test_bot staff-{code}"})
        self.t0 = self.bot.db.get_record("tg_room", -100)["linked_at"]

    def inc(self, n, urgency, ts, **kw):
        x = {"id": f"R-{n:08d}", "type": "인명구조", "urgency": urgency, "ts": ts, "place": "하천 하류 주거지",
             "text": f"위치: 옥포동 {n}번지 · 사람이 갇혔어요", "channel": "텔레그램", "attended": False, "invalid": False, **kw}
        self.items.append(x)
        return x

    def sent(self, pred=lambda p: True):
        return [p for m, p in self.bot.calls if m == "sendMessage" and p.get("chat_id") == -100 and pred(p)]

    def test_link_codes(self):
        self.assertTrue(self.desk.rooms("staff"))
        code = self.desk.new_code("staff", "담당자")
        self.assertEqual(code["startgroup"], f"https://t.me/test_bot?startgroup=staff-{code['code']}")
        self.assertEqual(code["command"], f"/staff@test_bot {code['code']}")
        other = "000000" if code["code"] != "000000" else "111111"
        self.desk.handle_group({"chat": {"id": -200, "type": "group"}, "from": {"id": 2}, "text": f"/staff {other}"})
        self.assertIsNone(self.bot.db.get_record("tg_room", -200))                       # 틀린 코드
        self.desk.handle_group({"chat": {"id": -200, "type": "group"}, "from": {"id": 2}, "text": f"/staff@other_bot {code['code']}"})
        self.assertIsNone(self.bot.db.get_record("tg_room", -200))                       # 다른 봇에게 보낸 명령은 무시
        ch = self.desk.new_code("channel", "담당자")["code"]
        self.desk.handle_group({"chat": {"id": -200, "type": "group"}, "from": {"id": 2}, "text": f"/staff {ch}"})
        self.assertTrue(any("주민 공지 채널용" in p["text"] for m, p in self.bot.calls if m == "send"))   # 용도가 다른 코드
        self.desk.handle_group({"chat": {"id": -200, "type": "group"}, "from": {"id": 2}, "text": f"/staff {code['code']}"})
        self.assertEqual(self.bot.db.get_record("tg_room", -200)["kind"], "staff")
        self.desk.handle_group({"chat": {"id": -300, "type": "group"}, "from": {"id": 3}, "text": f"/staff {code['code']}"})
        self.assertIsNone(self.bot.db.get_record("tg_room", -300))                       # 한 번 쓴 코드는 끝
        for i in range(5):                                                                 # 한 방에서 5번 틀리면 그 방은 10분 무시
            self.desk.handle_group({"chat": {"id": -400, "type": "group"}, "from": {"id": 4}, "text": f"/staff 99999{i}"})
        good = self.desk.new_code("staff", "담당자")["code"]
        self.desk.handle_group({"chat": {"id": -400, "type": "group"}, "from": {"id": 4}, "text": f"/staff {good}"})
        self.assertIsNone(self.bot.db.get_record("tg_room", -400))
        for i in range(30):                                                                # 전체에서 30번 틀리면 코드를 모두 없앤다
            self.desk.handle_group({"chat": {"id": -500 - i, "type": "group"}, "from": {"id": 5}, "text": "/staff 999999"})
        self.assertEqual(self.bot.db.list_records("tg_link"), [])
        self.desk.handle_group({"chat": {"id": -100, "type": "supergroup"}, "from": {"id": 1}, "text": "물 많이 찼어요"})
        self.desk.handle_group({"chat": {"id": -100, "type": "supergroup"}, "migrate_to_chat_id": -1009})   # 슈퍼그룹으로 바뀜
        self.assertIsNotNone(self.bot.db.get_record("tg_room", -1009))

    def test_only_urgent_batched_reminded_and_closed(self):
        now = self.t0 + 10
        self.inc(1, 1, now - 400)                     # 방을 연결하기 전에 들어온 신고는 알리지 않는다
        self.items[0]["ts"] = self.t0 - 100
        self.inc(2, 1, now - 5)
        self.inc(3, 3, now - 5)                        # 보통: 바로 알리지 않는다(요약에만)
        self.inc(4, 2, now - 5)                        # 급함: 바로 알리지 않는다(5분 동안 아무도 안 보면)
        self.desk.scan(now)
        alerts = self.sent(lambda p: p["text"].startswith("🚨 매우 급한 신고 ·"))
        self.assertEqual(len(alerts), 1)
        self.assertIn("R-00000002", alerts[0]["text"])
        self.assertEqual(alerts[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "sa:R-00000002")
        self.desk.scan(now + 1)
        self.assertEqual(len(self.sent(lambda p: p["text"].startswith("🚨 매우 급한 신고"))), 1)   # 같은 알림을 두 번 보내지 않는다
        for n in range(10, 15):                                                          # 한꺼번에 5건 → 한 메시지로 묶음
            self.inc(n, 1, now + 2)
        self.desk.scan(now + 3)
        batch = self.sent(lambda p: "매우 급한 신고 5건" in p["text"])
        self.assertEqual(len(batch), 1)
        self.assertNotIn("reply_markup", batch[0])                                       # 묶음에는 확인 버튼이 없고(https 주소가 없으면 버튼 없음)
        self.assertEqual(len(self.sent(lambda p: "매우 급한 신고 ·" in p["text"])), 1)
        # 담당자가 R-2를 관리자 화면에서 확인함 → 텔레그램 알림도 '확인함'으로
        self.items[1].update(attended=True, seen={"by": "담당자", "at": now + 4})
        self.desk.scan(now + 4)
        ed = [p for m, p in self.bot.calls if m == "editMessageText"]
        self.assertEqual(len(ed), 1)
        self.assertIn("✅ 담당자 확인", ed[0]["text"])
        # 5분 지나도록 아무도 안 본 급한 신고(R-4, R-10~14) → 한 번 더(6건이라 묶음), 확인한 R-2는 빠진다
        self.desk.scan(now + 320)
        late = self.sent(lambda p: p["text"].startswith("⏰"))
        self.assertEqual(len(late), 1)
        self.assertIn("6건", late[0]["text"])
        self.assertNotIn("R-00000002", late[0]["text"])
        self.desk.scan(now + 330)
        self.assertEqual(len(self.sent(lambda p: p["text"].startswith("⏰"))), 1)
        # 30분마다 요약(새 신고가 있을 때만): 숫자와 아직 안 본 급한 신고
        self.inc(20, 4, now + 1000)
        self.desk.scan(now + 1900)
        dg = self.sent(lambda p: p["text"].startswith("📊 지난"))
        self.assertEqual(len(dg), 1)
        self.assertIn("아직 아무도 확인하지 않은 급한 신고", dg[0]["text"])
        self.assertIn("위기 단계: 경계", dg[0]["text"])
        self.desk.scan(now + 3800)                                                       # 그사이 새 신고가 없으면 요약도 없음
        self.assertEqual(len(self.sent(lambda p: p["text"].startswith("📊 지난"))), 1)


    def test_ack_while_alert_is_being_sent(self):
        """알림을 보내는 사이(메시지 번호를 저장하기 전)에 누가 확인해도, 그 알림이 '확인함'으로 바뀐다."""
        x = self.inc(30, 1, self.t0 + 20)
        self.bot.db.put_record("tg_alert", x["id"], {"iid": x["id"], "msgs": []})                # 보내기 시작(번호는 아직 없음)
        self.desk.close_alerts(x["id"], {"by": "김담당(텔레그램)", "at": self.t0 + 20})   # 그 사이에 확인됨
        self.assertFalse([p for m, p in self.bot.calls if m == "editMessageText"])
        self.desk._alert(self.desk.rooms("staff"), [x], "new", self.t0 + 21)
        ed = [p for m, p in self.bot.calls if m == "editMessageText"]
        self.assertEqual(len(ed), 1)
        self.assertIn("김담당(텔레그램) 확인", ed[0]["text"])


STAFF_FAKE, STAFF_APP = 18792, 18793


class TestStaffRoomChannelContact(ServerBase):
    """서버와 함께: 관리자 화면 코드로 담당자 방·공지 채널 연결, [👀 확인했어요], 📞 연락처 보내기."""
    port = STAFF_APP
    extra_env = {"WEBAPP_INCIDENT_PER_MIN": "1000", "TELEGRAM_BOT_TOKEN": "123:TEST",
                 "TELEGRAM_API_BASE": f"http://127.0.0.1:{STAFF_FAKE}", "TELEGRAM_WEBHOOK_URL": f"http://127.0.0.1:{STAFF_APP}",
                 "TELEGRAM_WATCH_S": "0.3"}

    @classmethod
    def setUpClass(cls):
        CALLS.clear(); TIMES.clear()
        cls.fake = ThreadingHTTPServer(("127.0.0.1", STAFF_FAKE), FakeTelegram)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.fake.shutdown()

    wait_for, update, msg, secret, press, sent_to = (TestTelegramBot.wait_for, TestTelegramBot.update, TestTelegramBot.msg,
                                                     TestTelegramBot.secret, TestTelegramBot.press, TestTelegramBot.sent_to)

    def report(self, chat, text, mid):
        self.update(self.msg(chat, text), self.secret())
        q = self.sent_to(chat, lambda p: "바로 신고할까요" in p["text"] and text in p["text"], "바로 신고")
        self.press(chat, q["reply_markup"]["inline_keyboard"][0][0]["callback_data"], mid=mid)
        r = self.sent_to(chat, lambda p: p.get("message_id") == mid and "접수번호" in p["text"], "접수", "editMessageText")
        return re.search(r"R-[0-9A-F]{8}", r["text"]).group(0)

    def test_channel_link_and_broadcast(self):
        secret, anon = self.secret(), Client(self.base)
        self.update(self.msg(801, "/start"), secret)
        self.sent_to(801, lambda p: "재난 알림을 받습니다" in p["text"], "구독")
        code = self.post("/api/telegram/link-code", {"purpose": "channel"})[1]["code"]
        staff_code = self.post("/api/telegram/link-code", {"purpose": "staff"})[1]["code"]
        post = lambda text, mid: {"update_id": 30 + mid, "channel_post": {"message_id": mid, "text": text,
                                                                          "chat": {"id": -1009, "type": "channel", "title": "거제 재난 알림"}}}
        self.update(post(f"/channel {staff_code}", 32), secret)                                # 담당자 방 코드로는 연결 안 됨
        time.sleep(0.5)
        self.assertFalse([p for m, p in CALLS if m == "sendMessage" and p.get("chat_id") == -1009])   # 채널에 틀렸다는 글을 쓰지 않음
        self.update(post(f"/channel {code}", 33), secret)
        self.wait_for(lambda m, p: m == "deleteMessage" and p == {"chat_id": -1009, "message_id": 33}, "코드 글 지우기")
        self.sent_to(-1009, lambda p: "공지 채널이에요" in p["text"], "채널 안내 글")
        moved = self.sent_to(801, lambda p: "공지 채널에 올라와요" in p["text"], "구독자에게 채널 안내")
        self.assertEqual(moved["reply_markup"]["inline_keyboard"][0][0]["url"], "https://t.me/+TESTINVITE")   # 비공개 채널 초대 링크
        self.assertEqual(anon.get("/api/public/status")[1]["telegram_channel"], "https://t.me/+TESTINVITE")
        tg = self.get("/api/telegram")[1]
        self.assertEqual([(r["kind"], r["title"]) for r in tg["rooms"]], [("channel", "거제 재난 알림")])
        # 모두에게 보내는 공지 → 채널에 한 번(1:1로는 보내지 않는다)
        CALLS.clear()
        self.post("/api/announcements", {"text": "하천변 산책로 통제", "audience": "all", "zone": "all", "urgent": True})
        self.sent_to(-1009, lambda p: "하천변 산책로 통제" in p["text"], "채널 공지")
        time.sleep(0.6)
        self.assertFalse([p for m, p in CALLS if m == "sendMessage" and p.get("chat_id") == 801 and "산책로" in p["text"]])
        self.update(self.msg(803, "/start"), secret)                                            # 새 구독자에게 채널 버튼
        self.sent_to(803, lambda p: (p.get("reply_markup") or {}).get("inline_keyboard", [[{}]])[0][0].get("url") == "https://t.me/+TESTINVITE", "채널 버튼")
        # 연결 끊기 → 다시 1:1로
        self.assertEqual(self.post("/api/telegram/rooms/-1009/unlink", {})[0], 200)
        self.post("/api/announcements", {"text": "두 번째 통제 공지", "audience": "all", "zone": "all", "urgent": False})
        self.sent_to(801, lambda p: "두 번째 통제 공지" in p["text"], "채널을 끊으면 1:1로")
        self.assertIsNone(anon.get("/api/public/status")[1]["telegram_channel"])

    def test_contact_share_is_staff_only_and_deleted(self):
        secret, anon = self.secret(), Client(self.base)
        iid = self.report(901, "위치: 옥포동 시장 앞 · 사람이 갇혔어요 2명", 301)
        offer = self.sent_to(901, lambda p: "내 연락처를 보낼까요" in p["text"], "연락처 묻기")
        self.assertTrue(offer["reply_markup"]["keyboard"][0][0]["request_contact"])
        contact = lambda uid, phone="821099998888": {"update_id": 40, "message": {"chat": {"id": 901, "type": "private"}, "from": {"id": 901},
                                                                                "contact": {"phone_number": phone, "user_id": uid}}}
        self.update(contact(12345), secret)                                                     # 남의 연락처는 받지 않는다
        self.sent_to(901, lambda p: "본인 연락처만" in p["text"], "남의 연락처 거절")
        self.update(contact(901), secret)
        ok = self.sent_to(901, lambda p: "담당자에게만 전했어요" in p["text"], "연락처 전달")
        self.assertEqual(ok["reply_markup"]["inline_keyboard"][0][0]["callback_data"], f"cd:{iid}")
        staff = next(x["output"] for x in self.get("/api/incidents")[1]["items"] if x["output"]["id"] == iid)
        self.assertEqual(len(staff["contacts"]), 1)
        self.assertNotIn("phone", staff["contacts"][0])                                        # 목록에는 번호가 없다
        self.assertRegex(staff["contacts"][0]["reporter"], r"^T-[0-9A-F]{4}$")
        self.assertNotIn("contacts", anon.get(f"/api/incidents/{iid}")[1]["result"]["output"])   # 공개 조회에도 없다
        self.assertEqual(anon.post(f"/api/incidents/{iid}/contact", {})[0], 401)               # 담당자만
        code, c = self.post(f"/api/incidents/{iid}/contact", {})                                # '번호 보기'(기록이 남는다)
        self.assertEqual((code, c["contacts"][0]["pretty"]), (200, "010-9999-8888"))
        self.press(901, f"cd:{iid}", mid=302)                                                   # 신고자가 직접 지우기
        self.sent_to(901, lambda p: p.get("message_id") == 302 and "연락처를 지웠어요" in p["text"], "연락처 지움", "editMessageText")
        self.assertEqual(self.post(f"/api/incidents/{iid}/contact", {})[0], 404)
        # 다시 보내고 신고를 취소하면 바로 지워진다
        self.update(contact(901), secret)
        for _ in range(50):
            if self.post(f"/api/incidents/{iid}/contact", {})[0] == 200:
                break
            time.sleep(0.1)
        else:
            self.fail("다시 보낸 연락처가 신고에 붙지 않음")
        self.press(901, f"cx:{iid}")
        ask = self.sent_to(901, lambda p: "취소할까요" in p["text"], "취소 확인")
        self.press(901, ask["reply_markup"]["inline_keyboard"][0][0]["callback_data"], mid=303)
        self.sent_to(901, lambda p: p.get("message_id") == 303 and "취소했어요" in p["text"], "취소", "editMessageText")
        self.assertEqual(self.post(f"/api/incidents/{iid}/contact", {})[0], 404)
        # 처음엔 안 보내고 나중에 /contact 로 붙이기(연락처 보내기가 안 되는 기기에서 누른 경우 안내)
        iid2 = self.report(903, "위치: 가람초 뒤 · 물이 차고 있어요 무릎까지", 304)
        self.sent_to(903, lambda p: "내 연락처를 보낼까요" in p["text"], "연락처 묻기")
        self.update(self.msg(903, "📞 내 연락처 보내기"), secret)
        self.sent_to(903, lambda p: "/contact" in p["text"] and "이 기기에서는" in p["text"], "기기 안내")
        self.update(self.msg(903, "/contact"), secret)
        again = self.sent_to(903, lambda p: iid2 in p["text"] and "연락처를 붙일까요" in p["text"], "/contact 묻기")
        self.assertTrue(again["reply_markup"]["keyboard"][0][0]["request_contact"])
        self.update({"update_id": 42, "message": {"chat": {"id": 903, "type": "private"}, "from": {"id": 903},
                                                  "contact": {"phone_number": "+82 10-2222-3333", "user_id": 903}}}, secret)
        self.sent_to(903, lambda p: "담당자에게만 전했어요" in p["text"] and iid2 in p["text"], "나중에 붙인 연락처")
        self.assertEqual(self.post(f"/api/incidents/{iid2}/contact", {})[1]["contacts"][0]["pretty"], "010-2222-3333")
        nobody = {"update_id": 41, "message": {"chat": {"id": 902, "type": "private"}, "from": {"id": 902},
                                               "contact": {"phone_number": "821011112222", "user_id": 902}}}
        self.update(nobody, secret)                                                              # 신고 없이 연락처만
        self.sent_to(902, lambda p: "연락처를 붙일 신고가 없어요" in p["text"], "붙일 신고 없음")

    def test_staff_room_alert_and_ack(self):
        secret, anon = self.secret(), Client(self.base)
        self.assertEqual(anon.post("/api/telegram/link-code", {"purpose": "staff"})[0], 401)   # 담당자만
        self.assertEqual(self.post("/api/telegram/link-code", {"purpose": "x"})[0], 400)
        code = self.post("/api/telegram/link-code", {"purpose": "staff"})[1]
        self.assertEqual(code["startgroup"], f"https://t.me/test_disaster_bot?startgroup=staff-{code['code']}")
        group = lambda text, chat=-1001: {"update_id": 50, "message": {"chat": {"id": chat, "type": "supergroup", "title": "상황실"},
                                                                       "from": {"id": 5, "first_name": "김담당"}, "text": text}}
        self.update(group("🆘 신고하기"), secret)                                               # 단체방에서는 신고를 받지 않는다
        self.update(group(f"/start@test_disaster_bot staff-{code['code']}"), secret)          # '단체방에 봇 추가하기' 링크
        self.sent_to(-1001, lambda p: "담당자 방으로 연결했어요" in p["text"], "담당자 방 연결")
        self.assertFalse([p for m, p in CALLS if m == "sendMessage" and p.get("chat_id") == -1001 and "무슨 일인가요" in p["text"]])
        self.update(group(f"/staff {code['code']}", -1002), secret)                            # 한 번 쓴 코드
        self.sent_to(-1002, lambda p: "연결 코드가 맞지 않거나" in p["text"], "다시 쓴 코드")
        self.assertEqual([r["chat_id"] for r in self.get("/api/telegram")[1]["rooms"]], [-1001])
        # 매우 급한 신고 → 담당자 방에 바로(확인 버튼), 보통 신고는 보내지 않는다
        _, w = anon.post("/api/incidents", {"text": "위치: 옥포동 시장 앞 · 사람이 갇혔거나 다쳤어요(구조 요청). 2명 갇힘"})
        iid = w["output"]["id"]
        self.assertEqual(w["output"]["urgency"], 1)
        alert = self.sent_to(-1001, lambda p: "매우 급한 신고" in p["text"] and iid in p["text"], "담당자 방 알림")
        self.assertEqual(alert["reply_markup"]["inline_keyboard"][0][0]["callback_data"], f"sa:{iid}")
        self.assertNotIn("H-", alert["text"])
        mid = next(1000 + i + 1 for i, (m, p) in enumerate(CALLS) if m == "sendMessage" and p is alert)
        cq = {"id": "cq-staff", "data": f"sa:{iid}", "from": {"id": 5, "first_name": "김담당"},
              "message": {"message_id": mid, "chat": {"id": -1001, "type": "supergroup"}}}
        self.update({"update_id": 51, "callback_query": cq}, secret)
        self.wait_for(lambda m, p: m == "answerCallbackQuery" and p["callback_query_id"] == "cq-staff" and "확인했다고" in p["text"], "버튼 응답")
        self.wait_for(lambda m, p: m == "editMessageText" and p.get("chat_id") == -1001 and p.get("message_id") == mid
                      and "김담당(텔레그램) 확인" in p["text"], "알림을 '확인함'으로")
        staff = next(x["output"] for x in self.get("/api/incidents")[1]["items"] if x["output"]["id"] == iid)
        self.assertEqual((staff["seen"]["by"], staff["seen"]["via"]), ("김담당(텔레그램)", "텔레그램"))
        pub = anon.get(f"/api/incidents/{iid}")[1]
        self.assertNotIn("seen", pub["result"]["output"])                                     # 담당자 이름은 공개하지 않는다
        self.assertIn("담당자가 신고 내용을 확인했어요", [s["text"] for s in pub["progress"]])   # 신고자에게는 '확인했어요'
        outsider = {**cq, "id": "cq-out", "message": {"message_id": mid, "chat": {"id": 777}}}  # 담당자 방이 아닌 곳의 버튼
        self.update({"update_id": 52, "callback_query": outsider}, secret)
        self.wait_for(lambda m, p: m == "answerCallbackQuery" and p["callback_query_id"] == "cq-out" and p["show_alert"], "남의 방 버튼")
        # 관리자 화면의 👀 확인했어요
        _, w2 = anon.post("/api/incidents", {"text": "위치: 가람초 뒤 · 사람이 다쳤어요(구조 요청). 1명 다리 다침"})
        code2, r2 = self.post(f"/api/incidents/{w2['output']['id']}/seen", {})
        self.assertEqual((code2, r2["seen"]["by"], r2["seen"]["via"]), (200, "담당자", "관리자 화면"))
        self.assertEqual(self.post(f"/api/incidents/{w2['output']['id']}/seen", {})[1].get("already"), True)
        self.update(group("/status"), secret)
        self.sent_to(-1001, lambda p: "지금 현황" in p["text"], "현황")
        self.assertEqual(self.post("/api/telegram/rooms/-1001/test", {})[0], 200)
        self.sent_to(-1001, lambda p: "시험 알림" in p["text"], "시험 알림")
        self.update(group("/unstaff"), secret)
        self.sent_to(-1001, lambda p: "연결을 끊었어요" in p["text"], "연결 끊기")
        self.assertEqual(self.get("/api/telegram")[1]["rooms"], [])


if __name__ == "__main__":
    unittest.main()
