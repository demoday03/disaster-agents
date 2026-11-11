"""텔레그램 봇으로 주민에게 알린다(앱을 열지 않아도 위기 단계·공지·안부 확인이 휴대폰에 온다).

- 주민이 봇에 /start → 알림 구독. 주민 화면의 '텔레그램으로 알림 받기'(t.me/봇?start=가구ID)로 들어오면 우리 집과
  연결되어, 안부 확인 요청에 텔레그램 안의 '✅ 안전합니다' 버튼으로 바로 응답할 수 있다.
- 보내는 것: 주민 대상 공지(긴급 공지는 앞에 표시), 담당자가 확정한 위기 단계(주의 이상), 우리 집 안부 확인 요청.
  모두 담당자가 직접 쓰거나 승인한 내용만 보낸다(AI가 만든 문장을 승인 없이 보내지 않는다).
- 받는 방식: 공개 주소가 있으면(Render: RENDER_EXTERNAL_URL, 또는 TELEGRAM_WEBHOOK_URL) 웹훅으로 받는다. 텔레그램이
  보내는 요청이 잠든 무료 서버도 깨운다. 공개 주소가 없는 내부망 PC는 폴링으로 받는다.
- 설정: TELEGRAM_BOT_TOKEN(필수, @BotFather에서 받음), TELEGRAM_CHANNEL_ID(선택, 공지·단계를 채널에도 올림).
  토큰이 없으면 아무것도 하지 않는다. 토큰은 저장소에 넣지 않는다.
보내기는 별도 스레드가 차례로 한다(요청 처리를 막지 않게). 텔레그램이 '차단됨'(403)을 주면 구독을 지운다.
"""
import json, os, queue, re, secrets, threading, time, urllib.error, urllib.request

LEVEL_TEXT = {"주의": "기상 정보를 자주 확인하고, 하천·계곡·지하 공간에 가까이 가지 마세요.",
              "경계": "대피를 준비하세요. 비상용품(물·약·손전등·충전기)을 챙기고 가족과 만날 곳을 정하세요.",
              "위험": "지금 대피하세요. 가까운 대피소나 높은 곳으로 바로 이동하고, 통제 중인 길은 피하세요."}
HELP = ("이 봇은 재난 안전 AI 에이전트(교육용 시연)의 주민 알림입니다. 실제 신고·출동으로 이어지지 않습니다.\n"
        "· /상황 또는 /status : 지금 위기 단계와 공지\n· /stop : 알림 그만 받기\n"
        "사람이 다치거나 갇혔다면 119·112에 바로 전화하세요.")


class TelegramBot:
    def __init__(self, db, public, status_fn, env=os.environ):
        self.token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.api = env.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
        self.channel = env.get("TELEGRAM_CHANNEL_ID", "").strip()
        base = (env.get("TELEGRAM_WEBHOOK_URL") or env.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
        self.webhook_url = base + "/api/telegram/webhook" if base else ""
        self.db, self.public, self.status_fn = db, public, status_fn
        self.secret = secrets.token_urlsafe(24)   # 웹훅 요청이 정말 텔레그램에서 왔는지 확인(시작할 때마다 새로)
        self.username, self.mode, self.error = None, None, None
        self.q = queue.Queue(maxsize=5000)

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
            if self.webhook_url:
                r = self.call("setWebhook", {"url": self.webhook_url, "secret_token": self.secret,
                                             "allowed_updates": ["message", "callback_query"], "drop_pending_updates": False})
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
                "subscribers": len(self.db.list_records("tg_sub"))}

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

    def _sender(self):
        while True:
            method, payload = self.q.get()
            r = self.call(method, payload)
            if not r.get("ok") and r.get("error_code") == 403 and payload.get("chat_id"):   # 사용자가 봇을 차단함
                self.db.delete_record("tg_sub", str(payload["chat_id"]))
            time.sleep(0.05)   # 텔레그램 전송 한도(초당 약 30건) 아래로

    def send(self, chat_id, text, buttons=None):
        payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        try:
            self.q.put_nowait(("sendMessage", payload))
        except queue.Full:
            pass

    # ------------------------------------------------------------------ 받은 메시지
    def check_secret(self, header):
        return bool(self.enabled and header) and secrets.compare_digest(header, self.secret)

    def handle(self, update):
        if "callback_query" in update:
            return self._callback(update["callback_query"])
        msg = update.get("message") or {}
        chat, text = (msg.get("chat") or {}).get("id"), (msg.get("text") or "").strip()
        if chat is None or (msg.get("chat") or {}).get("type") not in (None, "private"):
            return   # 1:1 대화만(단체방에서 명령을 받지 않는다)
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd == "/start":
            self._subscribe(chat, arg.strip())
        elif cmd in ("/stop", "/그만"):
            self.db.delete_record("tg_sub", str(chat))
            self.send(chat, "알림을 그만 보냅니다. 다시 받으려면 /start 를 보내 주세요.")
        elif cmd in ("/status", "/상황", "상황"):
            self.send(chat, self.situation_text())
        else:
            self.send(chat, HELP)

    def _subscribe(self, chat, payload):
        sub = self.db.get_record("tg_sub", str(chat)) or {"chat_id": chat, "since": time.time()}
        linked = ""
        if re.fullmatch(r"H-[0-9A-F]{6,20}", payload or ""):   # 주민 화면의 '텔레그램으로 알림 받기'에서 온 경우
            try:
                h = self.public.f.households[payload]
                if h.get("registered"):
                    sub["household"] = payload
                    linked = f"\n\n🏠 우리 집({h.get('address') or '등록한 집'})과 연결했어요. 담당자가 안전을 물으면 여기에서 버튼으로 답할 수 있어요."
            except KeyError:
                pass
        self.db.put_record("tg_sub", str(chat), sub)
        self.send(chat, "🔔 재난 알림을 받습니다. 담당자가 확정한 위기 단계와 공지를 보내 드려요." + linked + "\n\n" + HELP)

    def _callback(self, cq):
        chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
        data, answer = cq.get("data") or "", "처리할 수 없는 버튼입니다"
        sub = self.db.get_record("tg_sub", str(chat)) if chat is not None else None
        if data.startswith("safe:") and sub and sub.get("household") == data[5:]:   # 연결된 우리 집의 안부만 답할 수 있다
            try:
                self.public.respond_check(data[5:])
                answer = "안전하다고 알렸어요. 고맙습니다."
                self.send(chat, "✅ 안전하다고 담당자에게 알렸어요.")
            except Exception as e:   # 이미 답했거나 진행 중인 확인이 없음
                answer = str(e)[:150] or "지금 진행 중인 안전 확인이 없습니다"
        try:
            self.q.put_nowait(("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": answer}))
        except queue.Full:
            pass

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
        lines.append("다치거나 갇혔다면 119·112에 바로 전화하세요.")
        return "\n".join(lines)

    def _broadcast(self, text, zone="all"):
        for sub in self.db.list_records("tg_sub"):
            hid = sub.get("household")
            if zone != "all" and hid:   # 구역 공지는 그 구역 집에만(집을 연결하지 않은 구독자는 받는다)
                h = self.public.f.households.get(hid)
                if h and h.get("zone") != zone:
                    continue
            self.send(sub["chat_id"], text)
        if self.channel:
            self.send(self.channel, text)

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
                if sub.get("household") in started:
                    self.send(sub["chat_id"], "🙋 담당자가 우리 집이 안전한지 확인하고 있어요.\n안전하면 아래 버튼을 눌러 주세요. "
                              "도움이 필요하면 119에 전화하거나 주민 화면에서 신고해 주세요.",
                              [[{"text": "✅ 안전합니다", "callback_data": "safe:" + sub["household"]}]])
