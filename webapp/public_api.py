"""시민(피해자)·봉사자 화면의 공개 기능(제안서 5.3 V1~V8, 5.5 B1~B11).

로그인 대신 가구·봉사자 코드로 구분하는 시연 방식이다. 실명·전화번호는 받지 않는다.
- 위치는 구역을 고르지 않고 글(주소·건물 이름)로 쓴다. 배치 계산에 쓰는 관리 구역은 글에서 추정하고(common/places.py),
  추정하지 못하면 '미상'으로 두어 담당자가 확인한다. 화면에는 구역 번호를 보여 주지 않는다.
- 주민 화면은 가구 코드로 로그인한다. 처음이면 '우리 집'(위치·주거 형태·함께 사는 분)을 한 번 적고 가구 코드를 받는다.
  코드는 그 기기에 기억되고, 가족 휴대폰에서도 같은 코드로 들어온다. 우선순위(취약 가구 여부)는 서버 안에서만 쓰고
  주민 화면에는 보여 주지 않는다. 급할 때는 로그인 없이 신고·대피소 보기만 할 수 있다(화면 쪽).
- 봉사자는 회원가입(별명·비밀번호·활동할 곳·할 수 있는 일·장비·가능한 시간)을 한다. 할 수 있는 일을 미리 받아 두어
  맞는 작업을 바로 보여 주고 추천한다. 봉사자 ID는 기기에만 저장하고, 다른 봉사자에게는 별명만 보인다.
- 시연용 가상 가구(H01~H20)·봉사자(V01~V12)는 그대로 둔다(담당자 화면·마이크로비트 D 노드 연동용).
코드를 아는 사람은 누구나 그 화면을 쓸 수 있으므로(시연 한계) 결정은 여전히 담당자 승인을 거친다:
안전 확인 응답·물품 요청·수령 확인·체크인·완료 보고·SOS·기부 등록은 '기록'만 하고, 배치·출고·투입은 승인 대기열에서 정한다.
이 모듈의 기능은 AI를 부르지 않는다. 부르는 쪽(server.py)이 FEATURES.lock 안에서 실행하고 접속 IP당 요청 수를 제한한다.
"""
import csv, io, re, secrets, time

from agents import supply
from common import places
from database import db as DB
from webapp import features as F

RISK_HINT = {"침수": "우리 구역은 물이 차기 쉬운 곳입니다. 비가 많이 오면 가까운 대피소와 가는 길을 미리 확인하세요",
             "사면": "우리 구역은 산사태·토사 위험이 있습니다. 비가 계속되면 산 쪽 창문에서 떨어진 방에 있고, 대피 안내가 나오면 바로 이동하세요"}
FACTOR_HINT = {"반지하": "반지하는 물이 들어오면 빠르게 차오릅니다. 비가 강해지면 먼저 높은 곳으로 옮기세요",
               "거동 불편": "혼자 이동이 어려우면 안전 확인 요청이 왔을 때 응답하고, 도움 요청으로 알려 주세요",
               "영유아": "분유·기저귀 같은 물품은 미리 요청할 수 있습니다",
               "고령": "대피할 때 함께 움직일 사람을 미리 정해 두세요",
               "임신": "대피가 늦어지지 않게 위험 단계가 오르면 먼저 이동하고, 필요한 물품을 미리 챙기세요"}
FLOOR_HINT = {("1층", "침수"): "1층은 물이 들어올 수 있습니다. 전기 제품과 귀중품을 높은 곳으로 미리 옮기세요",
              ("2층 이상", "침수"): "물이 차면 무리해서 밖으로 나가기보다 위층으로 피하고, 엘리베이터는 쓰지 마세요"}
OPEN_TASK = ("대기", "추천")
FLOORS = ("반지하", "1층", "2층 이상")
PEOPLE_FACTORS = ("영유아", "고령", "거동 불편", "임신")   # 주민이 고르는 '함께 사는 분'
MAX_REGISTERED = 2000   # 공개 화면에서 등록할 수 있는 가구·봉사자 수(무한정 쌓이지 않게)
# 봉사자 회원가입 선택지. 할 수 있는 일(역량)은 작업 유형 설정(config/tasks.json)의 역량과 같은 낱말을 쓴다
SKILL_LABEL = {"토사": "흙·토사 치우기", "청소": "청소·가재도구 세척", "운반": "짐 옮기기·정리"}
EQUIPMENT = ("삽", "고무장갑", "장화", "손수레", "마스크")
TIMES = {60: "1시간", 120: "2시간", 240: "반나절(4시간)", 480: "하루(8시간)"}
MODES = {"작업": "현장 작업", "기부": "물품 기부만", "둘 다": "현장 작업과 기부"}


class PublicAPI:
    def __init__(self, features, db, scenario_now):
        self.f, self.db, self.scenario_now = features, db, scenario_now

    # ------------------------------------------------------------------ 시민(가구)
    def households(self):
        """시연용 가상 가구만(주민이 직접 등록한 가구는 목록에 내보내지 않는다)."""
        return [{k: h[k] for k in ("id", "zone", "floor", "factors", "members")}
                for h in self.f.households.values() if not h.get("registered")]

    def _clean_home(self, data):
        zone, address = places.resolve(data, "우리 집 위치")
        floor, people = data.get("floor"), data.get("people_factors", [])
        if floor not in FLOORS:
            raise ValueError("집 형태(반지하·1층·2층 이상)를 골라 주세요")
        if not isinstance(people, list) or any(x not in PEOPLE_FACTORS for x in people):
            raise ValueError("함께 사는 분은 목록에서 골라 주세요")
        members = F._int(data.get("members", 1), "가족 수", 1, 20)
        consent = data.get("consent", True)
        if not isinstance(consent, bool):
            raise ValueError("안부 확인 동의는 예/아니오로 골라 주세요")
        # 반지하는 주거 형태이지만 우선순위(취약 요인)에도 들어간다(시연 가구 데이터와 같은 방식)
        factors = (["반지하"] if floor == "반지하" else []) + [x for x in PEOPLE_FACTORS if x in people]
        return {"zone": zone, "address": address, "floor": floor, "factors": factors, "members": members, "consent": consent}

    def register_household(self, data):
        """주민이 '우리 집'을 적으면 가구를 만든다. 돌려준 ID가 가구 코드(주민 화면 로그인)다."""
        if sum(1 for h in self.f.households.values() if h.get("registered")) >= MAX_REGISTERED:
            raise F.Conflict("등록할 수 있는 가구 수를 넘었습니다. 담당자에게 문의하세요")
        hid = "H-" + secrets.token_hex(5).upper()
        h = {"id": hid, **self._clean_home(data), "contact": "앱", "registered": True, "created_at": time.time()}
        self.f.households[hid] = h
        self.db.put_record("household", hid, h)
        return {"id": hid}

    @staticmethod
    def normalize_code(code):
        """주민이 손으로 넣은 가구 코드를 맞춘다: 소문자·띄어쓰기·'H-' 빠뜨림('3f9a1c2b7d' → 'H-3F9A1C2B7D'), 시연 가구 'h1' → 'H01'."""
        c = re.sub(r"[\s_]", "", code).upper().replace("–", "-").replace("—", "-").replace("−", "-")
        m = re.fullmatch(r"H?-?([0-9A-F]{10})", c)
        if m:
            return "H-" + m.group(1)
        m = re.fullmatch(r"H-?(\d{1,2})", c)
        return f"H{int(m.group(1)):02d}" if m else c

    def login_household(self, data):
        """주민 화면 로그인: 가구 코드가 있으면 그 가구로 들어간다(실명·비밀번호 없이 코드만, 시연 방식)."""
        code = data.get("code")
        if not isinstance(code, str) or not code.strip() or len(code) > 40:
            raise ValueError("가구 코드를 넣어 주세요")
        hid = self.normalize_code(code)
        h = self.f.households.get(hid)
        if h is None:
            raise ValueError("가구 코드를 찾을 수 없어요. 코드를 다시 확인해 주세요")
        return {"id": hid, "registered": bool(h.get("registered"))}

    def update_household(self, hid, data):
        h = self._household(hid)
        if not h.get("registered"):
            raise PermissionError("시연용 가구 정보는 바꿀 수 없습니다")
        h.update(self._clean_home(data))
        self.db.put_record("household", hid, h)
        return {"id": hid}

    def _household(self, hid):
        h = self.f.households.get(hid)
        if h is None:
            raise KeyError(hid)
        return h

    def household(self, hid):
        """V1 가구 정보 · V2 내 위험 요약 · V4 안전 확인 상태 · V7 물품 진행 · V11 복구 케이스."""
        h = self._household(hid)
        # 위치에서 관리 구역을 모르면(미상) 구역 위험은 말하지 않고 집 형태·함께 사는 분 안내만 한다
        zone = next((z for z in self.f.sc["zones"] if z["id"] == h["zone"]), {"id": h["zone"], "name": "위치 확인 중", "risk": []})
        stage = self.db.get_record("stage", "current") or {}
        risks = [RISK_HINT[r] for r in zone.get("risk", []) if r in RISK_HINT]
        risks += [FLOOR_HINT[(h["floor"], r)] for r in zone.get("risk", []) if (h["floor"], r) in FLOOR_HINT]
        risks += [FACTOR_HINT[f] for f in h["factors"] if f in FACTOR_HINT]
        if stage.get("confirmed") and set(stage.get("hazards", [])) & set(zone.get("risk", [])):
            risks.insert(0, f"지금 담당자가 확정한 '{stage['level']}' 단계의 위험({', '.join(stage['hazards'])})이 우리 구역 위험과 같습니다")
        check = self.db.get_record("check", hid)
        if check:
            check = {"state": check["state"], "seconds_left": round(check["deadline"] - time.time()) if check["state"] == "대기" else None}
        reqs = [{"id": r["id"], "item": r["item"], "qty": r["qty"], "state": r["state"], "route": r.get("route"),
                 "history": [{"to": x["to"], "ts": x["ts"], "reason": x.get("reason")} for x in r.get("history", [])]}
                for r in self.db.list_records("supply_request") if r["household"] == hid]
        case = next(({"stage": c["stage"], "timeline": [{"stage": t["stage"], "ts": t["ts"]} for t in c["timeline"]]}
                     for c in self.db.list_records("case") if c["household"] == hid), None)
        shelters = sorted(self.scenario_now()["shelters"], key=lambda s: (s["zone"] != h["zone"], s["occupied"] >= s["capacity"],
                                                                         -(s["capacity"] - s["occupied"])))
        return {"household": {**{k: h[k] for k in ("id", "zone", "floor", "factors", "members", "consent")},
                              "address": h.get("address") or places.describe(h["zone"]), "registered": bool(h.get("registered"))},
                "zone": {"id": zone["id"], "name": zone["name"], "risk": zone.get("risk", [])},
                "risks": risks or ["비가 많이 오면 가까운 대피소와 가는 길을 미리 확인하고, 담당자 안내를 확인하세요"],
                "check": check, "requests": sorted(reqs, key=lambda r: -(r["history"][-1]["ts"] if r["history"] else 0)),
                "case": case, "nearest_shelter": {"name": shelters[0]["name"], "zone": shelters[0]["zone"], "place": places.describe(shelters[0]["zone"]),
                                    "near": shelters[0]["zone"] == h["zone"]} if shelters else None,
                "catalog": [k for k, v in self.f.catalog["items"].items() if v["category"] != "의약품"],
                "when": list(F.WHEN)}

    def respond_check(self, hid):
        """V4 안전 확인: 진행 중인 안부 확인에만 응답할 수 있다(없으면 409)."""
        self._household(hid)
        if self.db.get_record("check", hid) is None:
            raise F.Conflict("지금 진행 중인 안전 확인 요청이 없습니다")
        rec = self.f.checks_respond(hid)
        return {"state": rec["state"]}

    def supply_request(self, data):
        """V6 급한 물품 요청 → 담당자 검토 대기(승인 전에는 출고 없음)."""
        hid = data.get("household")
        self._household(hid)
        body = {"household": hid, "people": data.get("people", 1), "when": data.get("when", "오늘 중")}
        for k in F.REQUEST_FLAGS:
            body[k] = data.get(k, False)
        item = data.get("item")   # 공개 화면은 품목을 고르게 한다(자유 메모 AI 해석은 담당자 화면·신고 경로에서만)
        if item not in self.f.catalog["items"] or self.f.catalog["items"][item]["category"] == "의약품":
            raise ValueError("목록에서 품목을 골라 주세요. 의약품은 담당자에게 직접 문의하세요")
        body.update(item=item, qty=data.get("qty"))
        r = self.f.supply_request(body)
        return {"requests": [{"id": x["id"], "item": x["item"], "qty": x["qty"], "state": x["state"],
                              "route": x.get("route"), "review_reason": x.get("review_reason")} for x in r["requests"]]}

    def receipt(self, rid, data):
        """V8 수령 확인. 다른 가구의 요청에는 응답할 수 없다."""
        req = self.db.get_record("supply_request", rid)
        if req is None:
            raise KeyError(rid)
        if req["household"] != data.get("household"):
            raise PermissionError("이 가구의 요청이 아닙니다")
        self.f.request_receipt(rid, {"received": data.get("received")})
        return {"id": rid, "state": self.db.get_record("supply_request", rid)["state"]}

    # ------------------------------------------------------------------ 봉사자
    def volunteers(self):
        """시연용 가상 봉사자만(회원가입한 봉사자의 ID는 내보내지 않는다)."""
        return [{**{k: v[k] for k in ("id", "zone", "skills", "equipment", "mode")}, "place": places.describe(v["zone"])}
                for v in self.f.volunteers.values() if not v.get("registered")]

    def volunteer_options(self):
        """회원가입 화면의 선택지와, 역량마다 맡을 수 있는 작업(가입할 때 미리 보여 준다)."""
        types = self.f.tcfg["task_types"]
        return {"skills": [{"id": k, "label": lab, "tasks": [t for t, m in types.items() if k in m["skills"]]}
                           for k, lab in SKILL_LABEL.items()],
                "task_equipment": {t: m["equipment"] for t, m in types.items()},
                "equipment": list(EQUIPMENT), "times": [{"min": k, "label": v} for k, v in TIMES.items()],
                "modes": [{"id": k, "label": v} for k, v in MODES.items()]}

    def _clean_volunteer(self, data):
        zone, address = places.resolve(data, "활동할 곳")
        mode = data.get("mode", "둘 다")
        if mode not in MODES:
            raise ValueError("참여 방식을 골라 주세요")
        skills, equipment = data.get("skills", []), data.get("equipment", [])
        if not isinstance(skills, list) or any(x not in SKILL_LABEL for x in skills):
            raise ValueError("할 수 있는 일은 목록에서 골라 주세요")
        if mode != "기부" and not skills:
            raise ValueError("현장 작업을 하려면 할 수 있는 일을 하나 이상 골라 주세요")
        if not isinstance(equipment, list) or any(x not in EQUIPMENT for x in equipment):
            raise ValueError("장비는 목록에서 골라 주세요")
        minutes = data.get("available_min", 120)
        if minutes not in TIMES:
            raise ValueError("가능한 시간을 골라 주세요")
        return {"zone": zone, "address": address, "skills": [x for x in SKILL_LABEL if x in skills],
                "equipment": [x for x in EQUIPMENT if x in equipment], "available_min": minutes, "mode": mode}

    @staticmethod
    def _clean_name(name):
        if not isinstance(name, str) or not 2 <= len(name.strip()) <= 20 or not re.fullmatch(r"[\w가-힣 ._-]+", name.strip()):
            raise ValueError("별명은 2~20자(한글·영문·숫자)로 적어 주세요")
        if re.fullmatch(r"[Vv]\d+", name.strip()):
            raise ValueError("그 별명은 쓸 수 없습니다")
        return name.strip()

    def register_volunteer(self, data):
        """봉사자 회원가입. 실명·전화번호는 받지 않는다. 돌려준 ID는 그 기기에만 저장한다."""
        name = self._clean_name(data.get("name"))
        password = data.get("password")
        if not isinstance(password, str) or not 6 <= len(password) <= 100:
            raise ValueError("비밀번호는 6자 이상으로 정해 주세요")
        profile = self._clean_volunteer(data)
        auths = self.db.list_records("volunteer_auth")
        if sum(1 for v in self.f.volunteers.values() if v.get("registered")) >= MAX_REGISTERED:
            raise F.Conflict("가입할 수 있는 봉사자 수를 넘었습니다. 담당자에게 문의하세요")
        if any(a["name"].casefold() == name.casefold() for a in auths):
            raise F.Conflict("이미 쓰고 있는 별명입니다. 다른 별명을 적어 주세요")
        vid = "V-" + secrets.token_hex(5).upper()
        v = {"id": vid, "name": name, **profile, "registered": True, "created_at": time.time()}
        self.db.put_record("volunteer_auth", vid, {"id": vid, "name": name, "password_hash": DB.hash_password(password)})
        self.db.put_record("volunteer", vid, v)
        self.f.volunteers[vid] = v
        return {"id": vid, "name": name}

    def login_volunteer(self, data):
        name, password = data.get("name"), data.get("password")
        if not isinstance(name, str) or not isinstance(password, str):
            raise ValueError("별명과 비밀번호를 적어 주세요")
        auth = next((a for a in self.db.list_records("volunteer_auth") if a["name"].casefold() == name.strip().casefold()), None)
        ok = DB._check_password(password, auth["password_hash"] if auth else DB._DUMMY_HASH)   # 없는 별명도 같은 시간
        if not auth or not ok or auth["id"] not in self.f.volunteers:
            raise PermissionError("별명 또는 비밀번호가 맞지 않습니다")
        return {"id": auth["id"], "name": auth["name"]}

    def update_volunteer(self, vid, data):
        v = self._volunteer(vid)
        if not v.get("registered"):
            raise PermissionError("시연용 봉사자 정보는 바꿀 수 없습니다")
        v.update(self._clean_volunteer(data))
        self.db.put_record("volunteer", vid, v)
        return {"id": vid}

    def _volunteer(self, vid):
        v = self.f.volunteers.get(vid)
        if v is None:
            raise KeyError(vid)
        return v

    def volunteer(self, vid):
        """B1 프로필 · B3 내 작업과 지시서 · 맞는 대기 작업 · B4 안전 수칙 · B9 필요 물품 · B10 내 기부."""
        v = self._volunteer(vid)
        tasks = self.db.list_records("task")
        mine = [self._task_view(t) for t in tasks if vid in t.get("team", []) and t["state"] in ("투입승인", "진행", "완료")]
        fit = []
        if v["mode"] in ("작업", "둘 다"):
            for t in tasks:
                if t["state"] in OPEN_TASK and vid not in t.get("team", []) and set(t.get("skills", [])) & set(v["skills"]):
                    pend = vid in ((t.get("last_recommendation") or {}).get("team") or [])
                    fit.append({**self._task_view(t), "recommended_for_me": pend})
        fc = supply.forecast(self.f.inventory(), self.f.catalog, time.time())
        need = [{"item": r["item"], "hours_left": r["hours_left"]} for r in fc if r.get("shortage")]
        gifts = [{"id": d["id"], "item": d["item"], "qty": d["qty"], "state": d.get("state"), "place": d.get("address") or places.describe(d.get("zone"))}
                 for d in self.db.list_records("donation") if d.get("donor") == vid]
        types = self.f.tcfg["task_types"]
        can_do = [t for t, m in types.items() if set(m["skills"]) & set(v["skills"])] if v["mode"] != "기부" else []
        return {"volunteer": {**v, "name": v.get("name", vid), "place": v.get("address") or places.describe(v["zone"]),
                              "busy": vid in self.f.busy_volunteers()}, "can_do": can_do,
                "skill_labels": SKILL_LABEL, "my_tasks": mine,
                "open_tasks": sorted(fit, key=lambda t: not t["recommended_for_me"])[:6],
                "safety": self.f.tcfg["safety"], "needed_items": need, "my_donations": gifts,
                "catalog": {k: {"units": x["units"]} for k, x in self.f.catalog["items"].items() if x["category"] != "의약품"}}

    def _task_view(self, t):
        """봉사자 화면용 작업. 팀은 별명으로만 보여 준다(회원가입한 봉사자의 ID는 다른 사람에게 보이면 안 된다)."""
        out = {k: t.get(k) for k in ("id", "type", "skills", "equipment", "people", "hours", "state", "checked_in_at", "completed_at")}
        out["place"] = t.get("address") or places.describe(t.get("zone"))
        out["team"] = [self.f.volunteers.get(x, {}).get("name", x) for x in t.get("team", [])]
        return out

    def _my_task(self, vid, tid):
        self._volunteer(vid)
        t = self.db.get_record("task", tid)
        if t is None:
            raise KeyError(tid)
        if vid not in t.get("team", []):
            raise PermissionError("담당자가 이 작업에 투입을 승인한 봉사자만 할 수 있습니다")
        return t

    def checkin(self, vid, tid):      # B5 도착 체크인
        self._my_task(vid, tid)
        return self._task_view(self.f.task_checkin(tid))

    def complete(self, vid, tid):     # B6 완료 보고 → 복구 케이스 반영
        self._my_task(vid, tid)
        return self._task_view(self.f.task_complete(tid))

    def sos(self, vid, data):         # B5 SOS → 담당자 상황판
        self._volunteer(vid)
        tid = data.get("task")
        if tid is not None:
            self._my_task(vid, tid)
        s = self.f.sos_raise({"volunteer": vid, "task": tid})
        return {"id": s["id"], "open": s["open"]}

    def donation(self, vid, data):    # B8 기부 등록 → 담당자 검수 전에는 매칭되지 않음
        self._volunteer(vid)
        d = self.f.donation_add({**{k: data.get(k) for k in ("item", "qty", "unit", "condition", "days_left", "zone", "address")}, "donor": vid})
        return {k: d.get(k) for k in ("id", "item", "qty", "state", "reason")}

    def export_csv(self, vid):        # B11 활동 기록(1365 입력용 형식을 흉내 낸 모의 파일)
        v = self._volunteer(vid)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["봉사자", "구분", "내용", "장소", "시작", "종료", "시간(분)"])
        fmt = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""
        for t in self.db.list_records("task"):
            if vid in t.get("team", []) and t["state"] == "완료":
                mins = round((t.get("completed_at", 0) - t.get("checked_in_at", 0)) / 60) if t.get("checked_in_at") else ""
                w.writerow([v.get("name", v["id"]), "작업 봉사", t["type"], t.get("address") or places.describe(t["zone"]),
                            fmt(t.get("checked_in_at")), fmt(t.get("completed_at")), mins])
        for d in self.db.list_records("donation"):
            if d.get("donor") == vid:
                w.writerow([v.get("name", v["id"]), "물품 기부", f"{d['item']} {d['qty']}", d.get("address") or places.describe(d.get("zone")), "", "", ""])
        return "﻿" + out.getvalue()   # 엑셀에서 한글이 깨지지 않게 BOM
