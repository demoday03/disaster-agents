"""텔레그램 봇으로 주민에게 알린다(앱을 열지 않아도 위기 단계·공지·안부 확인이 휴대폰에 온다).

- 주민이 봇에 /start → 알림 구독. 주민 화면의 '텔레그램으로 알림 받기'(t.me/봇?start=가구ID)로 들어오면 우리 집과
  연결되어, 안부 확인 요청에 텔레그램 안의 '✅ 안전합니다' 버튼으로 바로 응답할 수 있다.
- 보내는 것: 주민 대상 공지(긴급 공지는 앞에 표시), 담당자가 확정한 위기 단계(주의 이상), 우리 집 안부 확인 요청.
  모두 담당자가 직접 쓰거나 승인한 내용만 보낸다(AI가 만든 문장을 승인 없이 보내지 않는다).
- 신고: 메뉴의 '🆘 신고하기'(또는 /report) → ① 무슨 일인지 버튼으로 여러 개 고르기 → ② 위치를 입력창에 적거나
  '📍 지금 위치 보내기' → ③ 더 알려 줄 것을 입력창에 적기(건너뛰기 가능) → 확인 후 보내기. 웹 화면과 같은 접수 함수를
  쓰고(접수 에이전트 분류·중복 합치기·속도 제한), 접수번호·취소 버튼을 준다. 담당자가 출동을 승인하는 등 진행이 생기면
  이 대화방으로 알린다. 그냥 글을 보내면 '이 내용으로 신고할까요?'라고 묻는다(급할 때 한 번에 보내기).
- 누가 보냈는지: 텔레그램 이름·아이디는 저장하지 않는다. 대신 ① /code(또는 메뉴 '🏠 우리 집 연결')로 가구 코드를
  연결하면 신고에 그 가구가 붙고(담당자 화면 '🏠 가구 … 로그인 신고'), ② 연결하지 않아도 대화방마다 이름 없는
  신고자 번호(T-XXXX)를 붙여 같은 사람이 보낸 신고를 묶어 본다. ③ 담당자가 '신고자에게 묻기'를 쓰면 질문이
  이 대화방으로 오고, 입력창에 적은 답이 그 신고에 붙는다.
- 📞 연락처 보내기(선택): 신고를 접수하면 '내 연락처 보내기' 버튼을 준다. 누르면 텔레그램이 본인 번호를 보내고(남의
  연락처는 받지 않는다), 그 신고에 담당자만 보는 연락처로 붙는다. 신고가 끝나거나(취소·오접수) 48시간이 지나면 지운다.
- 담당자 단체방·주민 공지 채널: 관리자 화면에서 만든 1회용 코드로 연결한다(telegram_staff.py). 담당자 방에는 급한
  신고만 보내고(많으면 묶어서), 채널을 연결하면 모두에게 보내는 공지·위기 단계는 채널에 한 번만 올린다(구독자가 몇만
  명이어도 한 번에 간다). 1:1 대화는 신고 답장·담당자 질문·우리 집 안부처럼 그 사람에게만 필요한 것에 쓴다.
- 받는 방식: 공개 주소가 있으면(Render: RENDER_EXTERNAL_URL, 또는 TELEGRAM_WEBHOOK_URL) 웹훅으로 받는다. 텔레그램이
  보내는 요청이 잠든 무료 서버도 깨운다. 공개 주소가 없는 내부망 PC는 폴링으로 받는다.
- 설정: TELEGRAM_BOT_TOKEN(필수, @BotFather에서 받음), TELEGRAM_CHANNEL_ID(선택, 관리자 화면에서 채널을 연결하지 않을 때).
  토큰이 없으면 아무것도 하지 않는다. 토큰은 저장소에 넣지 않는다.
보내기는 별도 스레드가 차례로 한다(요청 처리를 막지 않게). 신고 답장·버튼 응답이 먼저 나가고, 구독자 모두에게 보내는
공지는 그 사이사이에 뒤에서 보낸다(공지 수천 건 때문에 급한 신고 답장이 밀리지 않게). 텔레그램이 '차단됨'(403)을 주면
구독을 지우고, '너무 많이 보냄'(429)을 주면 기다렸다가 다시 보낸다.
"""
import collections, hashlib, json, os, queue, re, secrets, threading, time, urllib.error, urllib.request

from common import places
from webapp.telegram_staff import GROUP_COMMANDS, StaffDesk

LEVEL_TEXT = {"주의": "기상 정보를 자주 확인하고, 하천·계곡·지하 공간에 가까이 가지 마세요.",
              "경계": "대피를 준비하세요. 비상용품(물·약·손전등·충전기)을 챙기고 가족과 만날 곳을 정하세요.",
              "위험": "지금 대피하세요. 가까운 대피소나 높은 곳으로 바로 이동하고, 통제 중인 길은 피하세요."}
HELP = ("이 봇은 재난 안전 AI 에이전트(교육용 시연)입니다. 실제 신고·출동으로 이어지지 않습니다.\n"
        "· 🆘 신고하기 (/report) : 버튼으로 고르고 입력창에 적으면 신고가 접수돼요\n"
        "· 📍 지금 상황 (/status) : 위기 단계·공지·대피소\n"
        "· 📋 내 신고 (/my) : 보낸 신고의 처리 상황\n"
        "· 🏠 우리 집 연결 (/code 우리집번호) : 신고하면 담당자가 우리 집을 바로 알아요\n"
        "· /contact : 보낸 신고에 내 연락처 붙이기(선택, 담당자만 보고 끝나면 지워요)\n"
        "· /stop : 알림 그만 받기\n"
        "한 번에 보내려면 이렇게 적어 보내도 돼요:\n위치: 옥포동 시장 앞 · 물이 무릎까지 찼어요 · 2명 고립\n"
        "사람이 다치거나 갇혔다면 119·112에 바로 전화하세요.")

# 아래쪽 고정 메뉴(키보드 자리에 버튼으로 뜬다)
MENU = {"keyboard": [[{"text": "🆘 신고하기"}], [{"text": "📍 지금 상황"}, {"text": "📋 내 신고"}],
                     [{"text": "🏠 우리 집 연결"}, {"text": "❓ 도움말"}]],
        "resize_keyboard": True, "is_persistent": True, "input_field_placeholder": "급하면 🆘 신고하기를 누르세요"}
COMMANDS = [{"command": "report", "description": "🆘 신고하기"}, {"command": "status", "description": "지금 위기 단계·공지·대피소"},
            {"command": "my", "description": "내 신고 처리 상황"}, {"command": "code", "description": "🏠 우리 집 번호로 연결"},
            {"command": "unlink", "description": "우리 집 연결 끊기"}, {"command": "contact", "description": "📞 내 신고에 연락처 보내기(선택)"},
            {"command": "cancel", "description": "쓰던 신고 그만두기"},
            {"command": "start", "description": "알림 받기·메뉴 열기"}, {"command": "stop", "description": "알림 그만 받기"}]
REPORT_WORDS = {"🆘 신고하기", "/report", "/신고", "신고", "신고하기"}
STATUS_WORDS = {"📍 지금 상황", "/status", "/상황", "상황"}
MY_WORDS = {"📋 내 신고", "/my", "/내신고", "내 신고"}
HELP_WORDS = {"❓ 도움말", "/help", "/도움말", "도움말"}
QUIT_WORDS = {"❌ 신고 그만두기", "/cancel", "그만두기"}
CODE_WORDS = {"🏠 우리 집 연결", "/code", "/코드", "/가구"}
HOME_BTN = "🏠 우리 집"
ASK_TTL = 3 * 3600   # 담당자 질문에 답할 수 있는 시간
SKIP, NO_PLACE = "건너뛰기", "위치 모름"
# 웹 신고 화면과 같은 고르기(사람에게 더 위험한 것부터: 여러 개 고르면 신고문에도 이 순서로 적는다)
KINDS = [("life", "🚑", "사람이 다치거나 갇혔어요", "사람이 갇혔거나 다쳤어요(구조 요청)"),
         ("flood", "🌊", "물이 차요", "물이 차고 있어요(침수)"),
         ("slide", "⛰️", "흙·돌이 무너져요", "흙·돌이 무너졌어요(산사태·토사)"),
         ("power", "💡", "전기·물이 끊겼어요", "전기나 물이 끊겼어요(정전·단수)"),
         ("road", "🚧", "길이 막혔어요", "길이 막혔어요(도로 통행 불가)"),
         ("supply", "📦", "물품이 필요해요", "물품이 필요해요"),
         ("etc", "✏️", "그 밖의 일", "")]
KIND = {k[0]: k for k in KINDS}
URGENCY = {1: "매우 급함", 2: "급함", 3: "보통", 4: "확인 필요"}
STATE_TXT = {"접수": "접수되어 담당자가 확인하고 있어요", "보완 필요": "정보가 더 필요해요", "오접수": "담당자 확인 결과 처리하지 않기로 했어요"}
LIFE_WARN = "🚑 사람이 다치거나 갇혔다면 지금 바로 119에 전화하세요. 신고는 그다음에 보내도 돼요."
FLOW_TTL = 1800   # 쓰다 만 신고는 30분 뒤 잊는다
NO_CONTACT = "보내지 않을래요"
CONTACT_KB = {"keyboard": [[{"text": "📞 내 연락처 보내기", "request_contact": True}], [{"text": NO_CONTACT}]],
              "resize_keyboard": True, "one_time_keyboard": True, "input_field_placeholder": "연락처는 아래 버튼으로만 보낼 수 있어요"}
AFTER_TXT = "🚑 위험해지면 119·112에 바로 전화하세요. 아래 메뉴로 언제든 다시 신고할 수 있어요."


class TelegramBot:
    def __init__(self, db, public, status_fn, env=os.environ):
        self.token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.api = env.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
        self.channel = env.get("TELEGRAM_CHANNEL_ID", "").strip()
        base = (env.get("TELEGRAM_WEBHOOK_URL") or env.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
        self.public_base = base   # 담당자 알림의 '관리자 화면 열기' 버튼(https 주소일 때만)
        self.webhook_url = base + "/api/telegram/webhook" if base else ""
        self.db, self.public, self.status_fn = db, public, status_fn
        self.secret = secrets.token_urlsafe(24)   # 웹훅 요청이 정말 텔레그램에서 왔는지 확인(시작할 때마다 새로)
        self.username, self.mode, self.error = None, None, None
        self.q = queue.Queue(maxsize=5000)   # 먼저 보낼 것: 신고 답장·버튼 응답·질문
        self.bulk, self.block = collections.deque(), threading.Lock()   # 뒤에 보낼 것: 구독자 모두에게 보내는 공지
        self.flows, self.flock = {}, threading.Lock()   # 대화방별로 쓰고 있는 신고(메모리에만, 30분)
        self.hooks = {}   # 서버가 채운다: submit(text, chat), supplement(iid, text, chat), cancel(iid, token), info(iid)
        self.watch_s = float(env.get("TELEGRAM_WATCH_S", "15"))   # 신고 진행 상황을 확인하는 간격(초)
        self.staff = StaffDesk(self, env)   # 담당자 단체방·주민 공지 채널
        self.sweep_at = 0.0

    @property
    def enabled(self):
        return bool(self.token)

    # ------------------------------------------------------------------ 텔레그램 API
    def call(self, method, payload=None, timeout=15):
        req = urllib.request.Request(f"{self.api}/bot{self.token}/{method}", data=json.dumps(payload or {}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                return json.load(e)
            except Exception:
                return {"ok": False, "error_code": e.code}

    def start(self):
        """서버가 켜질 때 한 번. 봇 이름을 받고 웹훅 또는 폴링을 시작한다. 실패해도 서버는 계속 돈다."""
        if not self.enabled:
            return
        threading.Thread(target=self._sender, daemon=True).start()
        try:
            me = self.call("getMe")
            if not me.get("ok"):
                raise RuntimeError(f"getMe 실패: {me.get('description') or me.get('error_code')}")
            self.username = me["result"].get("username")
            self.call("setMyCommands", {"commands": COMMANDS})   # 입력창 옆 '메뉴'에 명령 목록(디스코드의 / 명령처럼)
            self.call("setMyCommands", {"commands": GROUP_COMMANDS, "scope": {"type": "all_group_chats"}})   # 단체방(담당자 방)
            threading.Thread(target=self._watch, daemon=True).start()
            if self.webhook_url:
                r = self.call("setWebhook", {"url": self.webhook_url, "secret_token": self.secret,
                                             "allowed_updates": ["message", "callback_query", "channel_post", "my_chat_member"],
                                             "drop_pending_updates": False})
                if not r.get("ok"):
                    raise RuntimeError(f"setWebhook 실패: {r.get('description') or r.get('error_code')}")
                self.mode = "webhook"
            else:
                self.call("deleteWebhook", {"drop_pending_updates": False})
                self.mode = "polling"
                threading.Thread(target=self._poll, daemon=True).start()
        except Exception as e:   # 토큰 오류·네트워크 문제. 상태에만 남긴다
            self.error = str(e)[:200]

    def status(self):
        if not self.enabled:
            return {"enabled": False}
        return {"enabled": True, "bot": self.username, "mode": self.mode, "error": self.error,
                "subscribers": len(self.db.list_records("tg_sub")), "bulk_pending": len(self.bulk),
                "staff_rooms": len(self.staff.rooms("staff")), "channel": bool(self.channel_target()[0])}

    def _poll(self):
        offset = None
        while True:
            r = self.call("getUpdates", {"timeout": 25, **({"offset": offset} if offset else {})}, timeout=35)
            if not r.get("ok"):
                time.sleep(5)
                continue
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                try:
                    self.handle(u)
                except Exception:
                    pass

    def _next_job(self):
        """먼저 보낼 것(신고 답장 등)이 있으면 그것부터, 없을 때만 공지 1:1 보내기를 하나."""
        try:
            return self.q.get_nowait()
        except queue.Empty:
            pass
        with self.block:
            if self.bulk:
                return self.bulk.popleft()
        try:
            return self.q.get(timeout=0.5)
        except queue.Empty:
            return None

    def _sender(self):
        while True:
            job = self._next_job()
            if job is None:
                continue
            method, payload = job
            r = self.call(method, payload)
            if not r.get("ok") and r.get("error_code") == 429:   # 너무 많이 보냄: 텔레그램이 알려 준 만큼 쉬고 한 번 더
                time.sleep(min(float((r.get("parameters") or {}).get("retry_after", 1)), 30))
                r = self.call(method, payload)
            cid = payload.get("chat_id")
            if not r.get("ok") and cid is not None:
                if r.get("error_code") == 403:   # 사용자가 봇을 차단함
                    self.db.delete_record("tg_sub", str(cid))
                self.staff.send_failed(cid, r)   # 연결된 담당자 방·채널이면 이유를 남긴다
            elif isinstance(cid, int) and cid < 0:   # 단체방·채널(번호가 음수): 예전 오류 표시를 지운다
                self.staff.send_ok(cid)
            time.sleep(0.05)   # 텔레그램 전송 한도(초당 약 30건) 아래로

    def _queue(self, method, payload, bulk=False):
        if bulk:
            with self.block:
                self.bulk.append((method, payload))
            return
        try:
            self.q.put_nowait((method, payload))
        except queue.Full:
            pass

    def send(self, chat_id, text, buttons=None, markup=None, bulk=False):
        """buttons: 메시지에 붙는 버튼(인라인), markup: 아래쪽 키보드 자리 버튼·입력창 안내문(ReplyKeyboard/ForceReply).
        bulk: 구독자 모두에게 보내는 공지(신고 답장보다 뒤에 보낸다)."""
        payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        elif markup:
            payload["reply_markup"] = markup
        self._queue("sendMessage", payload, bulk)

    def edit(self, chat_id, message_id, text, buttons=None):
        """버튼을 누른 메시지를 고쳐 쓴다(buttons가 없으면 버튼을 없앤다)."""
        if message_id is None:
            return self.send(chat_id, text, buttons)
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000], "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        self._queue("editMessageText", payload)

    def answer(self, cq_id, text, alert=False):
        self._queue("answerCallbackQuery", {"callback_query_id": cq_id, "text": text[:190], "show_alert": alert})

    # ------------------------------------------------------------------ 받은 메시지
    def check_secret(self, header):
        return bool(self.enabled and header) and secrets.compare_digest(header, self.secret)

    def handle(self, update):
        if "callback_query" in update:
            return self._callback(update["callback_query"])
        if "channel_post" in update:   # 주민 공지 채널 연결(/channel 코드)
            return self.staff.handle_channel(update["channel_post"])
        if "my_chat_member" in update:   # 봇이 단체방·채널에서 빠짐
            return self.staff.member_update(update["my_chat_member"])
        msg = update.get("message") or {}
        chat, text = (msg.get("chat") or {}).get("id"), (msg.get("text") or "").strip()
        if (msg.get("chat") or {}).get("type") in ("group", "supergroup"):   # 단체방: 담당자 방 연결·현황 명령만
            return self.staff.handle_group(msg)
        if chat is None or (msg.get("chat") or {}).get("type") not in (None, "private"):
            return   # 그 밖의 곳에서는 명령을 받지 않는다(신고는 1:1 대화만)
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        key = cmd if cmd.startswith("/") else text   # 명령(/report)과 메뉴 버튼 글자를 같은 뜻으로
        flow = self._flow(chat)
        if msg.get("contact"):   # '📞 내 연락처 보내기'
            return self._got_contact(chat, msg, flow)
        if flow and flow["step"] == "contact":
            self._drop(chat)   # 연락처를 묻던 중에 다른 것을 하면 묻기를 그만둔다(안 보내도 신고는 그대로)
            if text == NO_CONTACT:
                return self.send(chat, "알겠어요. 연락처 없이도 신고는 그대로 처리돼요.\n" + AFTER_TXT, markup=MENU)
            flow = None
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        ask, matched = self._pending_ask(chat, reply_to)   # 담당자 질문에 대한 답인지
        ask = ask if (matched or not flow) else None   # 질문 메시지에 답장했거나, 쓰던 신고가 없을 때만 답으로 본다
        if msg.get("location"):   # '📍 지금 위치 보내기'(또는 첨부 → 위치)
            if ask:
                loc = msg["location"]
                return self._bg(self._answer, chat, ask, f"위치: GPS {float(loc.get('latitude', 0)):.5f}, {float(loc.get('longitude', 0)):.5f}")
            return self._got_location(chat, flow, msg["location"])
        if cmd == "/start":
            self._subscribe(chat, arg.strip())
        elif cmd in ("/stop", "/그만"):
            self.db.delete_record("tg_sub", str(chat))
            self.send(chat, "알림을 그만 보냅니다. 다시 받으려면 /start 를 보내 주세요. 신고는 계속 할 수 있어요.", markup=MENU)
        elif key in QUIT_WORDS:
            self._drop(chat)
            self.send(chat, "쓰던 신고를 그만뒀어요. 필요하면 언제든 🆘 신고하기를 눌러 주세요.", markup=MENU)
        elif key in REPORT_WORDS:
            self.start_report(chat)
        elif key in CODE_WORDS:
            if arg.strip() and cmd.startswith("/"):
                self._link(chat, arg.strip())
            else:
                self._ask_code(chat)
        elif cmd in ("/unlink", "/연결끊기"):
            sub = self.db.get_record("tg_sub", str(chat))
            if sub and sub.pop("household", None):
                self.db.put_record("tg_sub", str(chat), sub)
                self.send(chat, "우리 집 연결을 끊었어요. 다시 연결하려면 🏠 우리 집 연결을 누르세요.", markup=MENU)
            else:
                self.send(chat, "연결된 우리 집이 없어요.", markup=MENU)
        elif key in STATUS_WORDS:
            self.send(chat, self.situation_text())
        elif key in MY_WORDS:
            self.my_reports(chat)
        elif cmd in ("/contact", "/연락처"):   # 신고에 연락처를 나중에 붙이기(선택)
            iid = self._latest_report(chat)
            if not iid:
                return self.send(chat, "연락처를 붙일 신고가 없어요. 🆘 신고하기로 신고한 뒤에 보내 주세요.", markup=MENU)
            with self.flock:
                self.flows[chat] = {"id": secrets.token_hex(3), "step": "contact", "iid": iid, "at": time.time()}
            self.send(chat, f"📞 {iid} 신고에 내 연락처를 붙일까요? (선택) 담당자만 보고, 신고가 끝나거나 48시간이 지나면 지워요.", markup=CONTACT_KB)
        elif key in HELP_WORDS or cmd.startswith("/"):
            self.send(chat, HELP, markup=MENU)
        elif text == "📍 지금 위치 보내기":   # 위치 보내기를 못 하는 기기(컴퓨터용 텔레그램 등)
            self.send(chat, "이 기기에서는 위치를 보낼 수 없어요. 아래 입력창에 주소나 근처 건물·가게 이름을 적어 주세요.")
        elif text == "📞 내 연락처 보내기":   # 연락처 보내기를 못 하는 기기(컴퓨터용 텔레그램 등)
            self.send(chat, "이 기기에서는 연락처를 보낼 수 없어요. 휴대폰 텔레그램에서 /contact 를 보내 주세요.", markup=MENU)
        elif text == NO_CONTACT:   # 연락처 묻기가 끝난 뒤 누른 버튼
            self.send(chat, AFTER_TXT, markup=MENU)
        elif ask and text:
            self._bg(self._answer, chat, ask, text)
        elif flow and text:
            self._flow_text(chat, flow, text)
        elif len(text) >= 4:   # 그냥 적어 보낸 글: 급할 때 한 번에 신고할 수 있게 묻는다
            self._ask_quick(chat, text)
        else:
            self.send(chat, HELP, markup=MENU)

    # ------------------------------------------------------------------ 우리 집(가구 코드) 연결
    def _home(self, chat):
        """이 대화방에 연결된 가구(없으면 None)."""
        sub = self.db.get_record("tg_sub", str(chat)) or {}
        hid = sub.get("household")
        h = self.public.f.households.get(hid) if hid else None
        return (hid, h) if h else (None, None)

    @staticmethod
    def home_place(h):
        return h.get("address") or places.describe(h["zone"])

    def _ask_code(self, chat):
        hid, h = self._home(chat)
        now = f"지금 연결된 우리 집: {self.home_place(h)} ({hid})\n다른 코드로 바꾸려면 " if h else ""
        with self.flock:
            self.flows[chat] = {"id": secrets.token_hex(3), "step": "code", "at": time.time()}
        self.send(chat, "🏠 우리 집 연결\n" + (now or "") + "주민 화면 '🏠 우리 집'에 있는 우리 집 번호(예: H-3F9A1C2B7D)를 아래 입력창에 적어 보내 주세요.\n"
                        "연결하면 신고할 때 담당자가 우리 집을 바로 알고, 안부 확인에도 버튼으로 답할 수 있어요."
                        + ("\n연결을 끊으려면 /unlink" if h else ""),
                  markup={"force_reply": True, "input_field_placeholder": "예: H-3F9A1C2B7D"})

    def _link(self, chat, code):
        if "login" not in self.hooks:
            return
        status, res = self.hooks["login"](code, chat)
        if status >= 400:
            return self.send(chat, f"⚠️ {res.get('error', '우리 집 번호를 확인하지 못했어요')}\n다시 하려면 🏠 우리 집 연결을 누르세요.", markup=MENU)
        sub = self.db.get_record("tg_sub", str(chat)) or {"chat_id": chat, "since": time.time()}   # 연결하면 알림도 받는다
        sub["household"] = res["id"]
        self.db.put_record("tg_sub", str(chat), sub)
        h = self.public.f.households.get(res["id"]) or {}
        self.send(chat, f"🏠 우리 집({self.home_place(h) if h else res['id']})과 연결했어요.\n이제 신고하면 담당자가 우리 집을 바로 알 수 있고, "
                        "담당자가 안전을 물으면 여기에서 버튼으로 답할 수 있어요.", markup=MENU)

    def pseudonym(self, chat):
        """텔레그램 신고자 번호(T-XXXX): 이름 대신, 같은 대화방이 보낸 신고끼리 묶어 보는 데만 쓴다(대화방 번호로 되돌릴 수 없게)."""
        if not getattr(self, "_salt", None):
            rec = self.db.get_record("tg_meta", "salt")
            if not rec:
                rec = {"v": secrets.token_hex(16)}
                self.db.put_record("tg_meta", "salt", rec)
            self._salt = rec["v"]
        return "T-" + hashlib.sha256(f"{self._salt}:{chat}".encode()).hexdigest()[:4].upper()

    # ------------------------------------------------------------------ 담당자 질문 → 신고자 답
    def report_chats(self, iid):
        """이 신고를 텔레그램으로 보낸(또는 같은 내용으로 합쳐진) 대화방들."""
        return sorted({r["chat_id"] for r in self.db.list_records("tg_report") if r.get("iid") == iid and not r.get("done")})

    def ask(self, chats, iid, qid, question):
        def run(chat):
            r = self.call("sendMessage", {"chat_id": chat, "disable_web_page_preview": True,
                                          "text": f"💬 담당자가 묻습니다 · {iid} 신고\n「{question}」\n\n아래 입력창에 답을 적어 보내 주세요."
                                                  "\n위치를 물으면 📎(첨부) → 위치로 보내도 돼요.",
                                          "reply_markup": {"force_reply": True, "input_field_placeholder": "답을 적어 주세요"}})
            res = r.get("result") if isinstance(r, dict) else None
            key = f"{iid}:{chat}"
            rec = self.db.get_record("tg_report", key)
            if rec is not None:
                rec["ask"] = {"qid": qid, "mid": res.get("message_id") if isinstance(res, dict) else None, "at": time.time()}
                self.db.put_record("tg_report", key, rec)
        for chat in chats:
            threading.Thread(target=run, args=(chat,), daemon=True).start()

    def _pending_ask(self, chat, reply_to):
        """이 대화방에 답을 기다리는 담당자 질문과, 그 질문 메시지에 답장한 것인지.
        질문 메시지에 답장했으면 그 질문(True), 아니면 가장 최근 질문(False). 다른 메시지에 답장했으면 없음."""
        now, best = time.time(), None
        for r in self.db.list_records("tg_report"):
            a = r.get("ask")
            if r.get("chat_id") != chat or not a or now - a.get("at", 0) > ASK_TTL:
                continue
            if reply_to and a.get("mid") == reply_to:
                return {"iid": r["iid"], **a}, True
            if best is None or a["at"] > best["at"]:
                best = {"iid": r["iid"], **a}
        return (None, False) if reply_to else (best, False)

    def _answer(self, chat, ask, text):
        status, res = self.hooks["answer"](ask["iid"], ask["qid"], text)
        if status >= 400:
            return self.send(chat, f"⚠️ 답을 전하지 못했어요: {res.get('error', '오류')}", markup=MENU)
        key = f"{ask['iid']}:{chat}"
        rec = self.db.get_record("tg_report", key)
        if rec is not None:
            rec.pop("ask", None)
            self.db.put_record("tg_report", key, rec)
        self.send(chat, f"✅ 답을 담당자에게 보냈어요 ({ask['iid']} 신고). 고맙습니다.", markup=MENU)

    # ------------------------------------------------------------------ 신고 받기(버튼 → 입력창 → 확인)
    def _flow(self, chat):
        with self.flock:
            f = self.flows.get(chat)
            if f and time.time() - f["at"] > FLOW_TTL:
                self.flows.pop(chat, None)
                return None
            return f

    def _drop(self, chat):
        with self.flock:
            self.flows.pop(chat, None)

    def _new_flow(self, chat, **kw):
        f = {"id": secrets.token_hex(3), "kinds": [], "place": None, "detail": None, "step": "kinds", "at": time.time(), **kw}
        with self.flock:
            self.flows[chat] = f
        return f

    def start_report(self, chat, **kw):
        f = self._new_flow(chat, **kw)
        self.send(chat, self._kinds_text(f), self._kinds_buttons(f))

    def _kinds_text(self, f):
        picked = [KIND[k][2] for k in f["kinds"]]
        return ("🆘 신고하기 (1/3)\n무슨 일인가요? 해당하는 것을 모두 누른 뒤 [다음 ▶]을 눌러 주세요.\n"
                + (f"\n고른 것: {', '.join(picked)}" if picked else "")
                + (f"\n\n{LIFE_WARN}" if "life" in f["kinds"] else ""))

    def _kinds_buttons(self, f):
        rows = [[{"text": ("✅ " if k in f["kinds"] else "⬜ ") + f"{e} {t}", "callback_data": f"rk:{f['id']}:{k}"}]
                for k, e, t, _ in KINDS]
        return rows + [[{"text": "다음 ▶", "callback_data": f"rn:{f['id']}"}, {"text": "❌ 그만두기", "callback_data": f"rx:{f['id']}"}]]

    def _next(self, chat, f):
        """아직 비어 있는 칸을 차례로 묻는다: 위치 → 더 알려 줄 것 → 확인."""
        etc_only = f["kinds"] == ["etc"]
        if f["place"] is None:
            f["step"] = "place"
            self.send(chat, "📍 어디인가요? (2/3)\n아래 입력창에 주소나 근처 건물·가게·다리 이름을 적어 보내 주세요.\n"
                            "주소를 모르면 [📍 지금 위치 보내기]를 누르세요." + (f"\n\n{LIFE_WARN}" if "life" in f["kinds"] else ""),
                      markup={"keyboard": ([[{"text": HOME_BTN}]] if self._home(chat)[1] else [])
                                          + [[{"text": "📍 지금 위치 보내기", "request_location": True}],
                                             [{"text": NO_PLACE}, {"text": "❌ 신고 그만두기"}]],
                              "resize_keyboard": True, "one_time_keyboard": True,
                              "input_field_placeholder": "예: 옥포동 시장 앞 / ○○아파트 101동"})
        elif f["detail"] is None or (etc_only and not f["detail"]):
            f["step"] = "detail"
            hint = ("예: 생수 10병, 담요 3장" if "supply" in f["kinds"] else "예: 2명 갇힘, 1명 다리 다침" if "life" in f["kinds"]
                    else "예: 물이 무릎까지 찼어요")
            keys = [[{"text": "❌ 신고 그만두기"}]] if etc_only else [[{"text": SKIP}, {"text": "❌ 신고 그만두기"}]]
            self.send(chat, ("✏️ 무슨 일인지 적어 주세요 (3/3)\n'그 밖의 일'을 골랐으니 아래 입력창에 무슨 일인지 적어 보내 주세요."
                             if etc_only else "✏️ 더 알려 줄 것 (3/3)\n몇 명인지, 다친 사람, 물 높이, 필요한 물품처럼 도움이 될 내용을 "
                             "아래 입력창에 적어 보내 주세요. 없으면 [건너뛰기]."),
                      markup={"keyboard": keys, "resize_keyboard": True, "one_time_keyboard": True, "input_field_placeholder": hint})
        else:
            f["step"] = "confirm"
            self.send(chat, f"이렇게 보낼까요?\n\n{self.compose(f)}" + (f"\n\n{LIFE_WARN}" if "life" in f["kinds"] else ""),
                      [[{"text": "✅ 신고 보내기", "callback_data": f"rs:{f['id']}"}],
                       [{"text": "🔁 처음부터", "callback_data": f"rr:{f['id']}"}, {"text": "❌ 그만두기", "callback_data": f"rx:{f['id']}"}]])

    @staticmethod
    def compose(f):
        """웹 신고 화면과 같은 모양의 신고문: '위치: … · 고른 일들. 더 적은 내용'."""
        parts = ([f"위치: {f['place']}"] if f.get("place") else []) + [k[3] for k in KINDS if k[0] in f["kinds"] and k[3]]
        text = " · ".join(parts)
        if f.get("detail"):
            text += (". " if text else "") + f["detail"]
        if f.get("home_floor") is not None and "home_floor" in f:
            text += f" (우리 집{' · ' + f['home_floor'] if f['home_floor'] else ''})"
        return text

    def _flow_text(self, chat, f, text):
        step = f["step"]
        if step == "code":
            self._drop(chat)
            return self._link(chat, text)
        if step == "place":
            hid, h = self._home(chat) if text == HOME_BTN else (None, None)
            if h:   # 연결한 우리 집에서 신고
                f["place"], f["home_floor"] = re.sub(r"[·\n]+", ", ", self.home_place(h))[:80], h.get("floor") or ""
            else:
                f["place"] = "" if text == NO_PLACE else re.sub(r"[·\n]+", ", ", text)[:80]
        elif step == "detail":
            if text == SKIP and f["kinds"] != ["etc"]:
                f["detail"] = ""
            elif text != SKIP:
                f["detail"] = text[:1000]
        elif step == "supp":   # 접수 뒤 빠진 정보 보완
            self._drop(chat)
            return self._bg(self._supplement, chat, f["iid"], text[:500])
        elif step == "quick":   # 묻는 사이에 글을 더 보냄 → 합쳐서 다시 묻는다
            return self._ask_quick(chat, f"{f['detail']} {text}")
        elif step == "kinds":   # 고르기 전에 글부터 쓴 경우: 내용으로 넣어 두고 고르기를 다시 보여 준다
            f["detail"] = text[:1000]
            return self.send(chat, "적어 주신 내용은 신고에 넣어 둘게요. 위에서 무슨 일인지 고르고 [다음 ▶]을 눌러 주세요.\n"
                                   "고르기 어려우면 '✏️ 그 밖의 일'을 고르세요.", self._kinds_buttons(f))
        else:   # 확인 단계에서 글을 더 보냄 → 내용에 덧붙인다
            f["detail"] = ((f["detail"] + " ") if f.get("detail") else "") + text[:500]
        f["at"] = time.time()
        self._next(chat, f)

    def _got_location(self, chat, f, loc):
        try:
            la, lo = float(loc["latitude"]), float(loc["longitude"])
        except (KeyError, TypeError, ValueError):
            return
        acc = loc.get("horizontal_accuracy")
        place = f"GPS {la:.5f}, {lo:.5f}" + (f" (오차 약 {round(acc)}m)" if isinstance(acc, (int, float)) else "")
        if f and f["step"] == "supp":
            self._drop(chat)
            return self._bg(self._supplement, chat, f["iid"], "위치: " + place)
        if not f or f["step"] == "confirm" and f.get("place"):
            f = self._new_flow(chat, place=place)   # 위치부터 보낸 경우: 이 위치로 새 신고를 시작한다
            return self.send(chat, "📍 이 위치로 신고할게요.\n\n" + self._kinds_text(f), self._kinds_buttons(f))
        f["place"], f["at"] = place, time.time()
        if f["step"] == "kinds":
            return self.send(chat, "📍 위치를 넣었어요. 위에서 무슨 일인지 고르고 [다음 ▶]을 눌러 주세요.", self._kinds_buttons(f))
        self._next(chat, f)

    def _ask_quick(self, chat, text):
        f = self._new_flow(chat, step="quick", detail=text[:1000])
        self.send(chat, f"이 내용으로 바로 신고할까요?\n\n「{text[:1000]}」\n\n위치(주소나 근처 건물)가 들어 있으면 더 빨리 찾아갈 수 있어요.",
                  [[{"text": "🆘 이대로 신고하기", "callback_data": f"rq:{f['id']}"}],
                   [{"text": "📝 버튼으로 자세히 쓰기", "callback_data": f"rf:{f['id']}"}, {"text": "아니요", "callback_data": f"rx:{f['id']}"}]])

    def _bg(self, fn, *args):
        """접수·취소는 분류(AI 호출)와 잠금이 걸려 오래 걸릴 수 있어 따로 처리한다(웹훅 응답을 막지 않게)."""
        def run():
            try:
                fn(*args)
            except Exception as e:
                print("telegram report error:", type(e).__name__, e)
                self.send(args[0], "⚠️ 처리하지 못했어요. 잠시 뒤 다시 보내거나, 급하면 119·112에 바로 전화하세요.", markup=MENU)
        threading.Thread(target=run, daemon=True).start()

    def _submit(self, chat, message_id, text):
        hid, _ = self._home(chat)
        code, res = self.hooks["submit"](text, chat, household=hid, reporter=self.pseudonym(chat))
        if code >= 400:
            return self.edit(chat, message_id, f"⚠️ 보내지 못했어요: {res.get('error', '오류')}\n급하면 119·112에 바로 전화하세요.")
        o = res.get("output", {})
        iid = o.get("id")
        key = f"{iid}:{chat}"   # 같은 신고에 합쳐져도(다른 사람이 먼저 보낸 같은 내용) 진행 알림은 대화방마다 따로
        rec = self.db.get_record("tg_report", key) or {"chat_id": chat, "iid": iid, "at": time.time()}
        if res.get("edit_token"):   # 취소는 이 대화방에서 처음 보낸 신고만(합쳐진 남의 신고는 취소 못 함)
            rec["token"] = res["edit_token"]
        info = self.hooks["info"](iid) or {}
        rec["sent"] = [s["text"] for s in info.get("steps", [])] + (["guidance"] if info.get("guidance") else [])
        rec["done"] = False
        self.db.put_record("tg_report", key, rec)
        lines = ["✅ 신고를 접수했어요" if not res.get("duplicate") else "✅ 같은 내용이 이미 접수돼 있어 그 신고에 합쳤어요",
                 f"접수번호: {iid}", f"종류: {o.get('type') or '확인 중'}" + (f" · {URGENCY[o['urgency']]}" if o.get("urgency") in URGENCY else "")]
        lines += [f"{s['icon']} {s['text']}" for s in info.get("steps", [])]
        lines.append(f"🏠 우리 집({hid})과 연결된 신고로 보냈어요." if hid else
                     "💡 🏠 우리 집 연결(/code)을 해 두면 담당자가 우리 집을 바로 알 수 있어요.")
        lines.append("담당자가 확인하거나 출동을 승인하면 이 대화방으로 알려 드릴게요. 담당자가 더 물어볼 수도 있어요.")
        buttons = [[{"text": "🔄 진행 상황", "callback_data": f"pg:{iid}"}]]
        if rec.get("token"):
            buttons[0].append({"text": "❌ 신고 취소", "callback_data": f"cx:{iid}"})
        self.edit(chat, message_id, "\n".join(lines), buttons)
        self._after_report(chat, iid, info)

    def _after_report(self, chat, iid, info):
        """빠진 정보가 있으면 입력창을 열어 바로 묻고, 없으면 연락처를 보낼지 묻는다(선택). 이미 보냈으면 메뉴로."""
        if info.get("state") == "보완 필요" and info.get("missing"):
            with self.flock:
                self.flows[chat] = {"id": secrets.token_hex(3), "step": "supp", "iid": iid, "at": time.time()}
            need = ", ".join(info["missing"])
            self.send(chat, f"⚠️ 처리에 필요한 정보가 빠졌어요: {need}\n아래 입력창에 적어 보내 주세요."
                            + ("\n위치를 모르면 📎(첨부) → 위치로 지금 위치를 보내도 돼요." if "위치" in need else ""),
                      markup={"force_reply": True, "input_field_placeholder": "예: 옥포동 ○○마트 앞이에요"})
        elif "contact" in self.hooks and not info.get("cancelled") and not (self.db.get_record("tg_report", f"{iid}:{chat}") or {}).get("contact"):
            with self.flock:
                self.flows[chat] = {"id": secrets.token_hex(3), "step": "contact", "iid": iid, "at": time.time()}
            self.send(chat, "📞 담당자가 전화로 확인할 수 있게 내 연락처를 보낼까요? (선택)\n"
                            "· 안 보내도 신고는 그대로 처리돼요\n· 담당자만 봐요(접수번호 조회·다른 주민에게는 안 보여요)\n"
                            "· 신고가 끝나거나 48시간이 지나면 지워요\n" + AFTER_TXT, markup=CONTACT_KB)
        else:
            self.send(chat, AFTER_TXT, markup=MENU)

    def _latest_report(self, chat):
        """이 대화방에서 하루 안에 보낸, 아직 끝나지 않은 가장 최근 신고 번호."""
        mine = [r for r in self.db.list_records("tg_report")
                if r.get("chat_id") == chat and not r.get("done") and time.time() - r.get("at", 0) < 86400]
        return max(mine, key=lambda r: r.get("at", 0))["iid"] if mine else None

    def _got_contact(self, chat, msg, flow):
        c, frm = msg.get("contact") or {}, msg.get("from") or {}
        if not c.get("user_id") or c.get("user_id") != frm.get("id"):   # 남의 연락처(주소록에서 고른 것)는 받지 않는다
            return self.send(chat, "📞 본인 연락처만 받을 수 있어요. 아래 [📞 내 연락처 보내기] 버튼을 눌러 주세요.", markup=CONTACT_KB)
        iid = flow["iid"] if flow and flow["step"] == "contact" else self._latest_report(chat)
        self._drop(chat)
        if not iid or "contact" not in self.hooks:
            return self.send(chat, "연락처를 붙일 신고가 없어요. 🆘 신고하기로 신고한 뒤에 보내 주세요.", markup=MENU)
        self._bg(self._contact, chat, iid, str(c.get("phone_number") or ""))

    def _contact(self, chat, iid, phone):
        status, res = self.hooks["contact"](iid, phone, self.pseudonym(chat))
        if status >= 400:
            return self.send(chat, f"⚠️ 연락처를 전하지 못했어요: {res.get('error', '오류')}", markup=MENU)
        key = f"{iid}:{chat}"
        rec = self.db.get_record("tg_report", key)
        if rec is not None:
            self.db.put_record("tg_report", key, {**rec, "contact": True})
        self.send(chat, f"📞 연락처를 담당자에게만 전했어요 ({iid} 신고).\n신고가 끝나거나 48시간이 지나면 지워요. 지금 지우려면 아래 버튼을 누르세요.",
                  [[{"text": "🗑 연락처 지우기", "callback_data": f"cd:{iid}"}]])
        self.send(chat, AFTER_TXT, markup=MENU)

    def _forget_contact(self, chat, mid, iid):
        status, res = self.hooks["forget"](iid, self.pseudonym(chat))
        key = f"{iid}:{chat}"
        rec = self.db.get_record("tg_report", key)
        if rec is not None:
            self.db.put_record("tg_report", key, {**rec, "contact": False})
        self.edit(chat, mid, f"🗑 {iid} 신고에서 연락처를 지웠어요." if status < 400 else f"⚠️ {res.get('error', '지우지 못했어요')}")

    def _supplement(self, chat, iid, text):
        code, res = self.hooks["supplement"](iid, text, chat)
        if code >= 400:
            return self.send(chat, f"⚠️ 보완하지 못했어요: {res.get('error', '오류')}", markup=MENU)
        info = self.hooks["info"](iid) or {}
        self.send(chat, f"고맙습니다. {iid} 신고에 보완 내용을 넣었어요.")
        self._after_report(chat, iid, info)

    def _cancel(self, chat, message_id, key):
        rec = self.db.get_record("tg_report", key) or {}
        code, res = self.hooks["cancel"](rec.get("iid"), rec.get("token"))
        if code >= 400:
            return self.edit(chat, message_id, f"⚠️ 취소하지 못했어요: {res.get('error', '오류')}")
        rec["done"] = True
        self.db.put_record("tg_report", key, rec)
        self.edit(chat, message_id, f"{rec['iid']} 신고를 취소했어요."
                  + ("\n이미 출동·물품이 움직이고 있어 담당자에게 확인을 요청했어요." if res.get("already_acting") else ""))

    def progress_text(self, iid):
        info = self.hooks["info"](iid) if self.hooks else None
        if not info:
            return f"{iid}: 신고를 찾을 수 없어요."
        state = "신고를 취소했어요" if info["cancelled"] else (
            "담당자가 대응 인력 출동을 승인했어요" if any(s.get("done") for s in info["steps"]) else STATE_TXT.get(info["state"], info["state"]))
        lines = [f"📨 {iid} · {info.get('type') or '확인 중'}", f"처리 상황: {state}"]
        lines += [f"{s['icon']} {s['text']}" for s in info["steps"]]
        if info.get("missing") and info["state"] == "보완 필요":
            lines.append(f"⚠️ 빠진 정보: {', '.join(info['missing'])}")
        if info.get("guidance"):
            lines.append(f"📋 담당자 안내: {info['guidance']}")
        return "\n".join(lines)

    def my_reports(self, chat):
        mine = sorted([r for r in self.db.list_records("tg_report") if r.get("chat_id") == chat], key=lambda r: -r.get("at", 0))[:5]
        if not mine:
            return self.send(chat, "이 대화방에서 보낸 신고가 없어요. 🆘 신고하기를 눌러 신고할 수 있어요.", markup=MENU)
        self.send(chat, "📋 내 신고 (최근 5건)\n\n" + "\n\n".join(self.progress_text(r["iid"]) for r in mine),
                  [[{"text": f"🔄 {r['iid']}", "callback_data": f"pg:{r['iid']}"}] for r in mine])

    def _watch(self):
        """텔레그램으로 받은 신고의 진행(출동 승인·담당자 안내 등)을 신고자에게, 급한 신고를 담당자 방에 알린다.
        10분마다 기한이 지난 신고자 연락처를 지운다."""
        while True:
            time.sleep(self.watch_s)
            for fn in (self.watch_once, self.staff.scan, self._sweep):
                try:
                    fn()
                except Exception as e:
                    print("telegram watch error:", type(e).__name__, e)

    def _sweep(self):
        if "sweep" in self.hooks and time.time() - self.sweep_at >= 600:
            self.sweep_at = time.time()
            self.hooks["sweep"]()

    def watch_once(self):
        if not self.hooks:
            return
        now = time.time()
        for rec in self.db.list_records("tg_report"):
            if rec.get("done") or now - rec.get("at", 0) > 86400:
                continue
            key, info = f"{rec['iid']}:{rec['chat_id']}", self.hooks["info"](rec["iid"])
            if info is None or info["cancelled"]:
                self.db.put_record("tg_report", key, {**rec, "done": True})
                continue
            sent, new = list(rec.get("sent") or []), []
            for s in info["steps"]:
                if s["text"] not in sent:
                    sent.append(s["text"])
                    new.append(f"{s['icon']} {s['text']}")
            if info.get("guidance") and "guidance" not in sent:
                sent.append("guidance")
                new.append(f"📋 담당자 안내: {info['guidance']}")
            done = info["state"] == "오접수"
            if done:
                new.append("담당자가 확인한 결과 이 신고는 처리하지 않기로 했어요. 상황이 다르면 다시 신고하거나 119·112에 전화하세요.")
            if new:
                self.send(rec["chat_id"], f"🔔 {rec['iid']} 신고 소식\n" + "\n".join(new), [[{"text": "🔄 진행 상황", "callback_data": f"pg:{rec['iid']}"}]])
            if new or done:
                self.db.put_record("tg_report", key, {**rec, "sent": sent, "done": done})

    def _subscribe(self, chat, payload):
        sub = self.db.get_record("tg_sub", str(chat)) or {"chat_id": chat, "since": time.time()}
        self.db.put_record("tg_sub", str(chat), sub)
        link = self.channel_target()[1]
        self.send(chat, ("🔔 재난 알림을 받습니다. 신고 답장·담당자 질문·우리 집 안부 확인을 여기로 보내 드려요."
                         if link else "🔔 재난 알림을 받습니다. 담당자가 확정한 위기 단계와 공지를 보내 드려요.") + "\n\n" + HELP, markup=MENU)
        if link:   # 모두에게 보내는 공지·위기 단계는 채널로(구독자가 많아도 한 번에 간다)
            self.send(chat, "📢 모두에게 보내는 공지와 위기 단계는 공지 채널에 올라와요. 놓치지 않게 채널에 들어와 주세요.",
                      [[{"text": "📢 공지 채널 들어가기", "url": link}]])
        if payload == "report":   # 주민 화면의 '텔레그램으로 신고' 링크(t.me/봇?start=report)
            self.start_report(chat)
        elif re.fullmatch(r"H-[0-9A-F]{6,20}|H\d{2}", payload or ""):   # 주민 화면의 '텔레그램으로 알림 받기'(가구 코드가 붙은 링크)
            self._link(chat, payload)

    def _callback(self, cq):
        m = cq.get("message") or {}
        chat, mid = (m.get("chat") or {}).get("id"), m.get("message_id")
        data, answer, alert = cq.get("data") or "", "처리할 수 없는 버튼입니다", False
        sub = self.db.get_record("tg_sub", str(chat)) if chat is not None else None
        if data.startswith("sa:"):   # 담당자 방 알림의 [👀 확인했어요]
            answer, alert = self.staff.callback(cq)
        elif data.startswith("cd:") and chat is not None:   # 보낸 연락처 지우기
            if self.db.get_record("tg_report", f"{data[3:]}:{chat}") and "forget" in self.hooks:
                answer = "연락처를 지우는 중이에요"
                self._bg(self._forget_contact, chat, mid, data[3:])
            else:
                answer = "이 대화방에서 보낸 신고만 지울 수 있어요"
        elif data.startswith("safe:") and sub and sub.get("household") == data[5:]:   # 연결된 우리 집의 안부만 답할 수 있다
            try:
                self.public.respond_check(data[5:])
                answer = "안전하다고 알렸어요. 고맙습니다."
                self.send(chat, "✅ 안전하다고 담당자에게 알렸어요.")
            except Exception as e:   # 이미 답했거나 진행 중인 확인이 없음
                answer = str(e)[:150] or "지금 진행 중인 안전 확인이 없습니다"
        elif chat is not None and data[:3] in ("rk:", "rn:", "rx:", "rs:", "rr:", "rq:", "rf:"):
            answer, alert = self._report_button(chat, mid, data)
        elif chat is not None and data.startswith("pg:"):
            answer = "처리 상황을 보냈어요"
            mine = self.db.get_record("tg_report", f"{data[3:]}:{chat}")
            self.send(chat, self.progress_text(data[3:]) if mine else "이 대화방에서 보낸 신고만 볼 수 있어요.")
        elif chat is not None and data.startswith(("cx:", "cy:")):
            key = f"{data[3:]}:{chat}"
            rec = self.db.get_record("tg_report", key)
            if not rec or not rec.get("token"):
                answer = "이 대화방에서 보낸 신고만 취소할 수 있어요"
            elif data.startswith("cx:"):   # 실수로 누르지 않게 한 번 더 묻는다
                answer = "취소할지 한 번 더 물어볼게요"
                self.send(chat, f"{data[3:]} 신고를 취소할까요?\n잘못 보냈거나 이미 해결됐을 때만 취소해 주세요. 위험하면 119·112에 바로 전화하세요.",
                          [[{"text": "예, 취소할게요", "callback_data": "cy:" + data[3:]}, {"text": "아니요", "callback_data": "cn"}]])
            else:
                answer = "취소하는 중이에요"
                self.edit(chat, mid, "취소하는 중이에요…")
                self._bg(self._cancel, chat, mid, key)
        elif chat is not None and data == "cn":
            answer = "취소하지 않았어요"
            self.edit(chat, mid, "취소하지 않았어요. 신고는 그대로 접수돼 있어요.")
        self.answer(cq.get("id"), answer, alert)

    def _report_button(self, chat, mid, data):
        kind, _, rest = data.partition(":")
        fid, _, arg = rest.partition(":")
        f = self._flow(chat)
        if not f or f["id"] != fid:
            return "이전 신고 양식이에요. 🆘 신고하기를 다시 눌러 주세요.", True
        f["at"] = time.time()
        if kind == "rk" and arg in KIND and f["step"] == "kinds":
            f["kinds"] = [k for k, *_ in KINDS if (k in f["kinds"]) != (k == arg)]   # 눌린 것만 켜고 끄기(위험한 순서 유지)
            self.edit(chat, mid, self._kinds_text(f), self._kinds_buttons(f))
            return ("골랐어요" if arg in f["kinds"] else "뺐어요"), False
        if kind == "rn" and f["step"] == "kinds":
            if not f["kinds"]:
                return "무슨 일인지 하나 이상 골라 주세요. 고르기 어려우면 '그 밖의 일'을 고르세요.", True
            self.edit(chat, mid, "🆘 신고하기\n✅ 무슨 일: " + ", ".join(KIND[k][2] for k in f["kinds"]))
            self._next(chat, f)
            return "다음으로 넘어가요", False
        if kind == "rx":
            self._drop(chat)
            self.edit(chat, mid, "신고를 그만뒀어요.")
            self.send(chat, "필요하면 언제든 🆘 신고하기를 눌러 주세요.", markup=MENU)
            return "그만뒀어요", False
        if kind == "rr":
            self.edit(chat, mid, "처음부터 다시 쓸게요.")
            self.start_report(chat)
            return "처음부터 다시 써요", False
        if kind == "rf" and f["step"] == "quick":   # 적어 보낸 글은 '더 알려 줄 것'으로 두고 버튼으로 나머지를 채운다
            f["step"] = "kinds"
            self.edit(chat, mid, f"「{f['detail']}」\n이 내용은 신고에 넣어 둘게요.")
            self.send(chat, self._kinds_text(f), self._kinds_buttons(f))
            return "버튼으로 이어서 써요", False
        if (kind == "rs" and f["step"] == "confirm") or (kind == "rq" and f["step"] == "quick"):
            text = self.compose(f) if kind == "rs" else f["detail"]
            self._drop(chat)
            self.edit(chat, mid, f"⏳ 접수하는 중이에요…\n\n{text}")
            self._bg(self._submit, chat, mid, text)
            return "신고를 보냈어요", False
        return "지금은 누를 수 없는 버튼이에요", False

    # ------------------------------------------------------------------ 보낼 알림(서버가 부른다)
    def situation_text(self):
        d = self.status_fn()
        s = d["stage"]
        lv = s["level"] if s.get("confirmed") and s.get("level") in LEVEL_TEXT else "정상"
        lines = [f"📍 지금 위기 단계: {lv}" + (f" ({', '.join(s.get('hazards') or [])})" if lv != "정상" and s.get("hazards") else "")]
        if lv in LEVEL_TEXT:
            lines.append(LEVEL_TEXT[lv])
        anns = [a for a in d.get("announcements", []) if a.get("audience") != "volunteer"]
        for a in sorted(anns, key=lambda a: (not a.get("urgent"), -a.get("at", 0)))[:3]:
            lines.append(("📢 긴급 공지: " if a.get("urgent") else "📢 공지: ") + a["text"])
        free = sorted([x for x in d.get("shelters", []) if x.get("capacity_left", 0) > 0], key=lambda x: -x["capacity_left"])[:2]
        if free:
            lines.append("🏫 자리가 남은 대피소: " + ", ".join(f"{x['name']} {x['capacity_left']}명" for x in free))
        if d.get("control_zones"):
            lines.append("⛔ 통제 중인 길: " + ", ".join(c["name"] for c in d["control_zones"]))
        link = self.channel_target()[1]
        if link:
            lines.append(f"📢 공지 채널: {link}")
        lines.append("다치거나 갇혔다면 119·112에 바로 전화하세요.")
        return "\n".join(lines)

    def channel_target(self):
        """(공지 채널 번호, 주민이 들어올 주소). 관리자 화면에서 연결한 채널이 먼저, 없으면 TELEGRAM_CHANNEL_ID."""
        room = self.staff.channel()
        if room:
            return room["chat_id"], room.get("link")
        return (self.channel or None), None

    def channel_linked(self, link):
        """채널을 연결했을 때 지금 구독자에게 한 번 알린다(뒤에서 천천히)."""
        for sub in self.db.list_records("tg_sub"):
            self.send(sub["chat_id"], "📢 이제 모두에게 보내는 공지와 위기 단계는 공지 채널에 올라와요. 놓치지 않게 채널에 들어와 주세요.\n"
                                      "신고 답장·담당자 질문·우리 집 안부 확인은 지금처럼 여기로 와요.",
                      [[{"text": "📢 공지 채널 들어가기", "url": link}]], bulk=True)

    def _broadcast(self, text, zone="all"):
        """공지·위기 단계 보내기. 채널이 있으면 채널에 한 번 올리고(바로), 1:1은 뒤에서 차례로 보낸다.
        주민이 들어올 수 있는 채널(주소가 있음)이면 모두에게 보내는 글은 1:1로 보내지 않는다(구독자가 몇만 명이어도
        채널 글 하나로 끝나고, 신고 답장이 밀리지 않게). 구역 공지는 그 구역에 연결된 집에는 1:1로도 보낸다."""
        channel, link = self.channel_target()
        if channel:
            label = places.describe(zone) if zone != "all" else ""
            self.send(channel, text if zone == "all" else f"📍 {label} 지역\n{text}")
        for sub in self.db.list_records("tg_sub"):
            hid = sub.get("household")
            if zone != "all" and hid:   # 구역 공지는 그 구역 집에만
                h = self.public.f.households.get(hid)
                if h and h.get("zone") != zone:
                    continue
            elif link:   # 채널에서 보는 글(모두에게 보내는 글, 집을 연결하지 않은 구독자의 구역 공지)
                continue
            self.send(sub["chat_id"], text, bulk=True)

    def announcement(self, row):
        if not self.enabled or row.get("audience") == "volunteer":
            return
        self._broadcast(("🚨 긴급 공지\n" if row.get("urgent") else "📢 공지\n") + row["text"], row.get("zone", "all"))

    def event(self, kind, data):
        """features.py가 부른다: stage(위기 단계 확정), welfare(안부 확인 시작)."""
        if not self.enabled:
            return
        if kind == "stage" and data.get("level") in LEVEL_TEXT:
            haz = ", ".join(data.get("hazards") or [])
            self._broadcast(f"⚠️ 위기 단계 {data['level']}" + (f" ({haz})" if haz else "") + f"\n{LEVEL_TEXT[data['level']]}\n"
                            "다치거나 갇혔다면 119·112에 바로 전화하세요.")
        elif kind == "welfare":
            started = set(data.get("households", []))
            for sub in self.db.list_records("tg_sub"):
                if sub.get("household") in started:   # 구역 전체라 많을 수 있다: 신고 답장보다 뒤에서
                    self.send(sub["chat_id"], "🙋 담당자가 우리 집이 안전한지 확인하고 있어요.\n안전하면 아래 버튼을 눌러 주세요. "
                              "도움이 필요하면 119에 전화하거나 주민 화면에서 신고해 주세요.",
                              [[{"text": "✅ 안전합니다", "callback_data": "safe:" + sub["household"]}]], bulk=True)
