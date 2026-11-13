"""텔레그램 담당자 단체방·주민 공지 채널 연결과 담당자 알림(telegram_bot.py가 쓴다).

- 연결: 관리자 화면 '📨 텔레그램'에서 1회용 연결 코드(6자리, 10분)를 만든다. 환경 변수를 고칠 필요가 없다.
  · 담당자 단체방: [단체방에 봇 추가하기] 링크(t.me/봇?startgroup=staff-코드)로 방을 고르거나, 봇을 초대한 뒤
    /staff@봇이름 코드 를 보낸다.
  · 주민 공지 채널: 채널 관리자에 봇을 넣고 채널에 /channel 코드 를 올린다(봇이 그 글을 지우고 연결한다).
  코드는 한 번 쓰면 없어진다. 틀린 코드가 대화방마다 5번이면 그 방은 10분 동안 무시하고, 전체에서 30번이면
  만들어 둔 코드를 모두 없앤다(코드 맞히기를 해도 소용없게. 담당자는 새 코드를 만들면 된다).
- 담당자 알림: 신고가 수천 건이 되어도 단체방이 넘치지 않게 '사람이 바로 봐야 하는 것'만 보낸다.
  · 매우 급함(긴급도 1) 신고 → 바로. 한 번에 TELEGRAM_STAFF_BATCH(3)건이 넘게 들어오면 한 메시지로 묶는다.
  · 긴급도 1~2인데 TELEGRAM_STAFF_REMIND_S(300초) 동안 아무도 확인하지 않은 신고 → 한 번 더(역시 묶어서).
  · 나머지는 TELEGRAM_STAFF_DIGEST_S(1800초)마다 숫자 요약만(그동안 새 신고가 있었을 때만).
  · [👀 확인했어요]를 누르면 그 신고에 '담당자 확인'이 남고(신고자에게도 '담당자가 확인했어요'), 다른 방에 보낸
    같은 알림도 '확인함'으로 바뀐다. 관리자 화면에서 확인·분류 수정·승인해도 같다.
  · 알림에는 신고자 연락처·가구 코드를 넣지 않는다(종류·위치·짧은 내용만).
- 단체방에서는 연결 명령과 /status(지금 현황)·/unstaff(연결 끊기)만 듣는다. 주민 신고는 봇과 1:1 대화에서만 받는다.
"""
import re, secrets, threading, time

CODE_TTL = 600
PURPOSES = {"staff": "담당자 단체방", "channel": "주민 공지 채널"}
MAX_ROOMS = 5
URGENCY = {1: "매우 급함", 2: "급함", 3: "보통", 4: "확인 필요"}
# /start@봇 staff-123456 (단체방에 봇 추가하기 링크), /staff 123456, /channel 123456, /link 123456
LINK_RE = re.compile(r"^/(start|staff|channel|link)(?:@([A-Za-z0-9_]+))?(?:\s+(?:staff-|ch-)?(\d{6}))?\s*$", re.I)
GROUP_COMMANDS = [{"command": "status", "description": "지금 신고 현황"}, {"command": "unstaff", "description": "담당자 방 연결 끊기"},
                  {"command": "staff", "description": "담당자 방으로 연결(관리자 화면의 코드)"}, {"command": "help", "description": "도움말"}]
RULES = ("· 매우 급한 신고(긴급도 1)는 바로 알려요\n"
         "· 급한 신고(긴급도 1~2)를 5분 동안 아무도 확인하지 않으면 한 번 더 알려요\n"
         "· 나머지는 30분마다 숫자로만 요약해요 · 한꺼번에 많이 들어오면 한 메시지로 묶어요\n"
         "· [👀 확인했어요]를 누르면 신고자에게도 '담당자가 확인했어요'가 가요")


def hm(ts):
    """한국 시각 시:분(서버가 UTC로 돌아도)."""
    return time.strftime("%H:%M", time.gmtime((ts or 0) + 9 * 3600))


class StaffDesk:
    def __init__(self, bot, env):
        self.bot, self.db = bot, bot.db
        self.batch = int(env.get("TELEGRAM_STAFF_BATCH", "3"))
        self.remind_s = float(env.get("TELEGRAM_STAFF_REMIND_S", "300"))
        self.digest_s = float(env.get("TELEGRAM_STAFF_DIGEST_S", "1800"))
        self.lock = threading.Lock()
        self.fails = {}   # 틀린 연결 코드 시도: {"c:대화방"|"all": [시각…]}

    # ------------------------------------------------------------------ 연결 코드(관리자 화면)
    def new_code(self, purpose, by):
        if purpose not in PURPOSES:
            raise ValueError("연결할 곳은 담당자 단체방(staff) 또는 주민 공지 채널(channel)이에요")
        now = time.time()
        with self.lock:
            live = {}
            for r in self.db.list_records("tg_link"):   # 같은 용도의 예전 코드·지난 코드는 없앤다(용도마다 하나만)
                if r.get("purpose") == purpose or r.get("exp", 0) < now:
                    self.db.delete_record("tg_link", r["code"])
                else:
                    live[r["code"]] = r
            code = f"{secrets.randbelow(10 ** 6):06d}"
            while code in live:
                code = f"{secrets.randbelow(10 ** 6):06d}"
            self.db.put_record("tg_link", code, {"code": code, "purpose": purpose, "by": by, "at": now, "exp": now + CODE_TTL})
        self.db.log_audit("telegram_link_code", "telegram", purpose, {"by": by})
        name = self.bot.username
        out = {"code": code, "purpose": purpose, "expires_in": CODE_TTL,
               "command": f"/{purpose}{'@' + name if name and purpose == 'staff' else ''} {code}"}
        if purpose == "staff" and name:
            out["startgroup"] = f"https://t.me/{name}?startgroup=staff-{code}"
        return out

    def _blocked(self, chat):
        now = time.time()
        with self.lock:
            hits = [t for t in self.fails.get(f"c:{chat}", []) if now - t < CODE_TTL]
            self.fails[f"c:{chat}"] = hits
            return len(hits) >= 5

    def _failed(self, chat):
        now = time.time()
        with self.lock:
            for key in (f"c:{chat}", "all"):
                self.fails[key] = [t for t in self.fails.get(key, []) if now - t < CODE_TTL] + [now]
            burn = len(self.fails["all"]) >= 30
            if burn:
                self.fails["all"] = []
                for r in self.db.list_records("tg_link"):
                    self.db.delete_record("tg_link", r["code"])
        if burn:
            self.db.log_audit("telegram_link_codes_burned", "telegram", None, {"reason": "틀린 연결 코드 시도가 많음"})

    def _take_code(self, code, purpose):
        """맞는 코드면 지우고(한 번만 쓰게) 돌려준다. (코드 기록, 틀렸을 때 안내문)"""
        with self.lock:
            rec = self.db.get_record("tg_link", code)
            if not rec or rec.get("exp", 0) < time.time():
                return None, "⚠️ 연결 코드가 맞지 않거나 10분이 지났어요. 관리자 화면 → 📨 텔레그램에서 새 코드를 만들어 주세요."
            if rec.get("purpose") != purpose:   # 용도가 다른 코드는 쓰지 않고 남겨 둔다
                return None, f"⚠️ 이 코드는 {PURPOSES[rec['purpose']]}용이에요. {PURPOSES[purpose]}용 코드를 새로 만들어 주세요."
            self.db.delete_record("tg_link", code)
            return rec, None

    # ------------------------------------------------------------------ 연결된 방
    def rooms(self, kind=None):
        rows = [r for r in self.db.list_records("tg_room") if kind is None or r.get("kind") == kind]
        return sorted(rows, key=lambda r: r.get("linked_at", 0))

    def channel(self):
        rows = self.rooms("channel")
        return rows[-1] if rows else None

    def unlink(self, chat_id, by):
        room = self.db.get_record("tg_room", str(chat_id))
        if not room:
            raise KeyError(chat_id)
        self.db.delete_record("tg_room", str(chat_id))
        self.db.log_audit("telegram_room_unlinked", "telegram", str(chat_id), {"kind": room.get("kind"), "by": by})
        return room

    def send_failed(self, chat_id, r):
        """연결된 방으로 못 보냈을 때: 일반 단체방이 슈퍼그룹으로 바뀌었으면 새 번호로 옮기고, 아니면 이유를 남긴다."""
        room = self.db.get_record("tg_room", str(chat_id))
        if not room:
            return
        new = (r.get("parameters") or {}).get("migrate_to_chat_id")
        if new:
            self.db.delete_record("tg_room", str(chat_id))
            self.db.put_record("tg_room", str(new), {**room, "chat_id": new})
            return
        room["error"] = ("봇이 방에서 빠졌거나 글을 쓸 권한이 없어요" if r.get("error_code") == 403
                         else (r.get("description") or f"오류 {r.get('error_code')}")[:120])
        room["error_at"] = time.time()
        self.db.put_record("tg_room", str(chat_id), room)

    def send_ok(self, chat_id):
        room = self.db.get_record("tg_room", str(chat_id))
        if room and room.get("error"):
            room.pop("error", None)
            room.pop("error_at", None)
            self.db.put_record("tg_room", str(chat_id), room)

    def public(self, room):
        """관리자 화면에 보여 줄 방 정보."""
        return {k: room.get(k) for k in ("chat_id", "kind", "title", "username", "link", "linked_at", "by", "error", "error_at")}

    # ------------------------------------------------------------------ 받은 메시지(단체방·채널)
    def _cmd(self, text):
        """(명령, 다른 봇에게 보낸 것인지). 명령이 아니면 ('', False)."""
        if not text.startswith("/"):
            return "", False
        head = text.split()[0]
        cmd, _, at = head.partition("@")
        other = bool(at) and bool(self.bot.username) and at.lower() != self.bot.username.lower()
        return cmd.lower(), other

    def handle_group(self, msg):
        chat = msg.get("chat") or {}
        cid, text = chat.get("id"), (msg.get("text") or "").strip()
        if cid is None or (msg.get("from") or {}).get("is_bot"):
            return
        if msg.get("migrate_to_chat_id"):   # 일반 단체방 → 슈퍼그룹(번호가 바뀐다)
            return self.send_failed(cid, {"parameters": {"migrate_to_chat_id": msg["migrate_to_chat_id"]}})
        cmd, other = self._cmd(text)
        if not cmd or other:
            return   # 그 밖의 대화는 읽지 않는다
        room = self.db.get_record("tg_room", str(cid))
        m = LINK_RE.match(text)
        if m and m.group(3) and m.group(1).lower() in ("start", "staff", "link"):
            who = (msg.get("from") or {}).get("first_name") or ""
            return self._link(cid, chat, "staff", m.group(3), who=who[:20])
        if cmd in ("/status", "/현황") and room and room.get("kind") == "staff":
            return self._run(self._send_status, cid)
        if cmd == "/unstaff" and room:
            self.unlink(cid, "텔레그램: " + ((msg.get("from") or {}).get("first_name") or "")[:20])
            return self.bot.send(cid, "연결을 끊었어요. 이 방으로는 더 이상 알림을 보내지 않아요.")
        if cmd in ("/start", "/staff", "/link", "/help"):
            name = f"@{self.bot.username}" if self.bot.username else ""
            if room and room.get("kind") == "staff":
                return self.bot.send(cid, "👮 이 방은 담당자 방이에요.\n" + RULES + "\n\n/status 지금 현황 · /unstaff 연결 끊기")
            return self.bot.send(cid, "이 방을 담당자 방으로 쓰려면 관리자 화면 → 📨 텔레그램 → [연결 코드 만들기]를 누른 뒤 "
                                      f"여기에 /staff{name} 코드 를 보내 주세요.\n주민 신고는 봇과 1:1 대화에서만 받아요.")

    def handle_channel(self, post):
        chat = post.get("chat") or {}
        m = LINK_RE.match((post.get("text") or "").strip())
        if chat.get("id") is not None and m and m.group(3) and m.group(1).lower() in ("channel", "link", "start"):
            self._link(chat["id"], chat, "channel", m.group(3), post_id=post.get("message_id"))

    def member_update(self, upd):
        """봇이 연결된 방·채널에서 빠지면(내보내짐) 연결을 지운다."""
        chat = upd.get("chat") or {}
        status = (upd.get("new_chat_member") or {}).get("status")
        if status in ("left", "kicked") and self.db.get_record("tg_room", str(chat.get("id"))):
            self.unlink(chat["id"], "텔레그램: 봇이 방에서 빠짐")

    def _link(self, cid, chat, purpose, code, post_id=None, who=""):
        if self._blocked(cid):
            return
        rec, err = self._take_code(code, purpose)
        if not rec:
            self._failed(cid)
            if purpose == "staff":   # 채널에는 틀렸다는 글을 올리지 않는다(구독자에게 보이므로)
                self.bot.send(cid, err)
            return
        if purpose == "staff" and not self.db.get_record("tg_room", str(cid)) and len(self.rooms("staff")) >= MAX_ROOMS:
            return self.bot.send(cid, f"⚠️ 담당자 방은 {MAX_ROOMS}개까지 연결할 수 있어요. 관리자 화면에서 쓰지 않는 방의 연결을 끊어 주세요.")
        if purpose == "channel":   # 공지 채널은 하나만
            for r in self.rooms("channel"):
                if r["chat_id"] != cid:
                    self.db.delete_record("tg_room", str(r["chat_id"]))
        room = {"chat_id": cid, "kind": purpose, "title": (chat.get("title") or "")[:60], "username": chat.get("username"),
                "link": f"https://t.me/{chat['username']}" if chat.get("username") else None,
                "linked_at": time.time(), "by": rec.get("by"), "tg_by": who or None}
        self.db.put_record("tg_room", str(cid), room)
        self.db.log_audit("telegram_room_linked", "telegram", str(cid), {"kind": purpose, "title": room["title"], "code_by": rec.get("by")})
        self._run(self._after_link, room, post_id)

    def _after_link(self, room, post_id):
        cid = room["chat_id"]
        if room["kind"] == "staff":
            return self.bot.call("sendMessage", {"chat_id": cid, "text": "✅ 이 방을 담당자 방으로 연결했어요.\n" + RULES
                                                 + "\n\n/status 지금 현황 · /unstaff 연결 끊기"})
        if post_id:   # 코드가 적힌 글은 지운다(구독자에게 보이지 않게)
            self.bot.call("deleteMessage", {"chat_id": cid, "message_id": post_id})
        if not room.get("link"):   # 비공개 채널: 초대 링크를 만들어 둔다(주민에게 알려 줄 주소)
            r = self.bot.call("exportChatInviteLink", {"chat_id": cid})
            if r.get("ok") and isinstance(r.get("result"), str):
                room["link"] = r["result"]
                self.db.put_record("tg_room", str(cid), room)
        self.bot.call("sendMessage", {"chat_id": cid, "text": "📢 이 채널은 재난 안전 공지 채널이에요. 담당자가 올리는 공지와 위기 단계가 "
                                                              "여기에 올라와요.\n신고는 봇과 1:1 대화(🆘 신고하기)에서 해 주세요. "
                                                              "다치거나 갇혔다면 119·112에 바로 전화하세요."})
        if room.get("link"):
            self.bot.channel_linked(room["link"])

    def _run(self, fn, *args):
        def run():
            try:
                fn(*args)
            except Exception as e:
                print("telegram staff error:", type(e).__name__, e)
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------ [👀 확인했어요]
    def callback(self, cq):
        m = cq.get("message") or {}
        chat, mid, data = (m.get("chat") or {}).get("id"), m.get("message_id"), cq.get("data") or ""
        room = self.db.get_record("tg_room", str(chat)) if chat is not None else None
        if not room or room.get("kind") != "staff":
            return "담당자 방에서만 누를 수 있어요", True
        frm = cq.get("from") or {}
        who = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)[:20] or "담당자"
        self._run(self._ack, chat, mid, data[3:], who)
        return "👀 확인했다고 남겼어요", False

    def _ack(self, chat, mid, iid, who):
        status, res = self.bot.hooks["seen"](iid, f"{who}(텔레그램)", "텔레그램")
        if status >= 400:
            self.bot.call("sendMessage", {"chat_id": chat, "text": f"⚠️ {iid}: {res.get('error', '확인을 남기지 못했어요')}"})
        self.close_alerts(iid, (res.get("seen") or {}) if status < 400 else {"by": res.get("error", "끝난 신고")})

    def close_alerts(self, iid, seen):
        """이 신고로 보낸 알림(모든 방)에 '확인함'을 적고 버튼을 없앤다. 한 번만."""
        with self.lock:
            rec = self.db.get_record("tg_alert", iid)
            if not rec or rec.get("closed"):
                return
            rec["closed"] = time.time()
            self.db.put_record("tg_alert", iid, rec)
        note = f"\n\n✅ {seen.get('by') or '담당자'} 확인" + (f" · {hm(seen['at'])}" if seen.get("at") else "")
        for chat, mid in rec.get("msgs", []):
            self.bot.call("editMessageText", {"chat_id": chat, "message_id": mid, "text": (rec.get("text", "") + note)[:4000],
                                              "disable_web_page_preview": True})

    # ------------------------------------------------------------------ 알림 보내기(봇의 감시 스레드가 부른다)
    def _admin_button(self):
        base = self.bot.public_base
        return [{"text": "🖥️ 관리자 화면 열기", "url": base + "/admin"}] if base.startswith("https://") else []

    @staticmethod
    def _line(x):
        text = re.sub(r"\s+", " ", x.get("text") or "")
        return f"{x['id']} · {x.get('type') or '미분류'} · 📍 {x.get('place') or '위치 확인 필요'}\n「{text[:90]}{'…' if len(text) > 90 else ''}」"

    def _post(self, rooms, text, buttons=None):
        """방마다 보내고 (방, 메시지 번호) 목록을 돌려준다."""
        sent = []
        for room in rooms:
            payload = {"chat_id": room["chat_id"], "text": text[:4000], "disable_web_page_preview": True}
            if buttons:
                payload["reply_markup"] = {"inline_keyboard": buttons}
            r = self.bot.call("sendMessage", payload)
            if r.get("ok") and isinstance(r.get("result"), dict):
                sent.append([room["chat_id"], r["result"].get("message_id")])
                if room.get("error"):
                    self.send_ok(room["chat_id"])
            else:
                self.send_failed(room["chat_id"], r)
            time.sleep(0.05)
        return sent

    def _alert(self, rooms, items, kind, now):
        """kind: new(새 매우 급한 신고) / remind(아무도 확인하지 않은 급한 신고). 많으면 한 메시지로 묶는다."""
        head = {"new": "🚨 매우 급한 신고", "remind": f"⏰ {round(self.remind_s / 60)}분 넘게 아무도 확인하지 않은 급한 신고"}[kind]
        for x in items:
            with self.lock:   # 보내는 도중 서버가 멈춰도 같은 알림을 두 번 보내지 않게 먼저 표시
                rec = self.db.get_record("tg_alert", x["id"]) or {"iid": x["id"], "msgs": []}
                rec[kind] = now
                self.db.put_record("tg_alert", x["id"], rec)
        if len(items) > self.batch:
            text = (f"{head} {len(items)}건\n\n" + "\n".join("· " + self._line(x) for x in items[:6])
                    + (f"\n… 외 {len(items) - 6}건" if len(items) > 6 else "")
                    + "\n\n한꺼번에 들어와 하나씩 보내지 않고 묶었어요. 관리자 화면에서 긴급도 높은 순으로 확인해 주세요.")
            btn = self._admin_button()
            return self._post(rooms, text, [btn] if btn else None)
        for x in items:
            text = (f"{head} · {URGENCY.get(x.get('urgency'), '긴급도 미정')}\n{self._line(x)}\n"
                    f"📨 {x.get('channel') or '웹'} 신고 · {hm(x.get('ts'))} 접수" + (f" · 같은 신고 {x['repeats']}번 더" if x.get("repeats") else ""))
            fresh = [r for r in rooms if x.get("ts", 0) >= r.get("linked_at", 0) - 5]   # 방을 연결하기 전 신고는 /status로
            btn = self._admin_button()
            msgs = self._post(fresh, text, [[{"text": "👀 확인했어요", "callback_data": f"sa:{x['id']}"}]] + ([btn] if btn else []))
            with self.lock:
                rec = self.db.get_record("tg_alert", x["id"]) or {"iid": x["id"], "msgs": []}
                rec["msgs"] = (rec.get("msgs") or []) + msgs
                rec["text"] = text
                self.db.put_record("tg_alert", x["id"], rec)

    def scan(self, now=None):
        rooms = self.rooms("staff")
        if not rooms or "staff_view" not in self.bot.hooks:
            return
        now = now or time.time()
        since = min(r.get("linked_at", now) for r in rooms) - 5
        items = self.bot.hooks["staff_view"]()
        alerts = {r["iid"]: r for r in self.db.list_records("tg_alert")}
        live = [x for x in items if not x.get("invalid") and x.get("ts", 0) >= max(since, now - 86400)]
        # 확인된 신고의 알림은 '확인함'으로 바꾼다(관리자 화면에서 확인·승인한 경우도)
        for x in items:
            a = alerts.get(x["id"])
            if a and a.get("msgs") and not a.get("closed") and (x.get("attended") or x.get("invalid")):
                self.close_alerts(x["id"], x.get("seen") or {"by": "관리자 화면에서 처리" if not x.get("invalid") else "처리하지 않기로 함"})
        new = [x for x in live if x.get("urgency") == 1 and not x.get("attended") and not alerts.get(x["id"], {}).get("new")]
        new.sort(key=lambda x: x.get("ts", 0))
        if new:
            self._alert(rooms, new, "new", now)
        just = {x["id"] for x in new}
        late = [x for x in live if x.get("urgency") in (1, 2) and not x.get("attended") and now - x.get("ts", now) >= self.remind_s
                and not alerts.get(x["id"], {}).get("remind") and x["id"] not in just]
        late.sort(key=lambda x: (x.get("urgency"), x.get("ts", 0)))
        if late:
            self._alert(rooms, late, "remind", now)
        meta = self.db.get_record("tg_meta", "staff") or {}
        if not meta.get("digest_at"):
            self.db.put_record("tg_meta", "staff", {**meta, "digest_at": now})
        elif self.digest_s > 0 and now - meta["digest_at"] >= self.digest_s:
            self.db.put_record("tg_meta", "staff", {**meta, "digest_at": now})
            fresh = [x for x in items if x.get("ts", 0) > meta["digest_at"]]
            if fresh:
                self._post(rooms, self.summary(items, fresh, f"📊 지난 {round(self.digest_s / 60)}분 신고 요약 "
                                                             f"({hm(meta['digest_at'])}~{hm(now)})"))

    def summary(self, items, fresh, title):
        lines = [title]
        if fresh is not None:
            by = [sum(1 for x in fresh if x.get("urgency") == u) for u in (1, 2, 3, 4)]
            tg = sum(1 for x in fresh if x.get("channel") == "텔레그램")
            lines.append(f"새 신고 {len(fresh)}건: " + " · ".join(f"{URGENCY[u]} {n}" for u, n in zip((1, 2, 3, 4), by) if n)
                         + f"\n텔레그램 {tg}건 · 웹 {len(fresh) - tg}건")
        now = time.time()
        waiting = [x for x in items if not x.get("invalid") and x.get("urgency") in (1, 2) and not x.get("attended")
                   and now - x.get("ts", 0) < 86400]
        if waiting:
            waiting.sort(key=lambda x: (x.get("urgency"), x.get("ts", 0)))
            lines.append(f"⏰ 아직 아무도 확인하지 않은 급한 신고 {len(waiting)}건: " + ", ".join(x["id"] for x in waiting[:8])
                         + (" …" if len(waiting) > 8 else ""))
        else:
            lines.append("✅ 급한 신고는 모두 담당자가 확인했어요")
        try:
            st = self.bot.status_fn()["stage"]
            lines.append(f"위기 단계: {st['level'] if st.get('confirmed') else '정상(확정된 단계 없음)'}"
                         + (f" ({', '.join(st.get('hazards') or [])})" if st.get("confirmed") and st.get("hazards") else ""))
        except Exception:
            pass
        return "\n".join(lines)

    def _send_status(self, cid):
        items = self.bot.hooks["staff_view"]() if "staff_view" in self.bot.hooks else []
        open_ = [x for x in items if not x.get("invalid") and time.time() - x.get("ts", 0) < 86400]
        by = [sum(1 for x in open_ if x.get("urgency") == u) for u in (1, 2, 3, 4)]
        head = (f"📊 지금 현황 ({hm(time.time())})\n최근 24시간 신고 {len(open_)}건"
                + (": " + " · ".join(f"{URGENCY[u]} {n}" for u, n in zip((1, 2, 3, 4), by) if n) if open_ else ""))
        btn = self._admin_button()
        self.bot.call("sendMessage", {"chat_id": cid, "text": self.summary(items, None, head), "disable_web_page_preview": True,
                                      **({"reply_markup": {"inline_keyboard": [btn]}} if btn else {})})
