"""관리자 화면의 현장 운영 기능: ① 위험 단계, ③ 안부 확인, ⑤ 복구·지원, ⑥ 봉사 코디, ⑦ 검증, ⑧ 물품 수급.
각 에이전트 모듈(agents/*)의 규칙을 그대로 쓰고, 여기서는 상태 저장(DB records)과 승인 대기열 연결만 한다.
모든 경로는 담당자 로그인이 필요하며(서버가 검사), 상태를 바꾸는 결정은 승인 대기열이나 담당자 동작으로만 일어난다.
"""
import base64, binascii, json, threading, time, uuid

from agents import recovery, supply, verify, volunteer, welfare
from common import docparse, places, scenario
from common.masking import mask

DAMAGE_TYPES = ("주택침수", "주택파손", "사면유실")
AREAS = ("거제시", "기타")
REQUEST_FLAGS = ("has_infant", "has_elderly", "has_mobility", "blackout", "no_water")
WHEN = ("지금", "1시간 이내", "오늘 중")


class Conflict(Exception):
    """현재 상태와 맞지 않는 요청(409)."""


def _new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"


def _text(v, name, limit, required=True):
    if v is None and not required:
        return None
    if not isinstance(v, str) or (required and not v.strip()) or len(v) > limit:
        raise ValueError(f"{name}은(는) {limit}자 이하 문자열이어야 합니다")
    return v.strip()


def _int(v, name, lo, hi):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f"{name}은(는) {lo}~{hi} 사이 정수여야 합니다")
    return v


class Features:
    def __init__(self, db, sc, travel, dispatch_cfg, guidance_cfg, llm=None, verify_llm=None, doc_parser=None):
        """travel: 현재 통제 구간을 반영한 이동 시간 표를 돌려주는 함수. doc_parser: 업스테이지 문서 파싱(없으면 텍스트 파일만)."""
        self.db, self.sc, self.travel, self.dcfg, self.gcfg = db, sc, travel, dispatch_cfg, guidance_cfg
        self.llm, self.verify_llm, self.doc_parser = llm, verify_llm, doc_parser
        with open("data/scenario/people.json", encoding="utf-8") as f:
            people = json.load(f)
        with open("data/scenario/inventory.json", encoding="utf-8") as f:
            self.inv_seed = json.load(f)
        self.households = {h["id"]: h for h in people["households"]}
        self.households.update({h["id"]: h for h in db.list_records("household")})   # 주민이 화면에서 등록한 가구
        self.volunteers = {v["id"]: v for v in people["volunteers"]}
        self.volunteers.update({v["id"]: v for v in db.list_records("volunteer")})   # 봉사자 화면에서 회원가입한 봉사자
        self.locations = {l["id"]: l for l in self.inv_seed["locations"]}
        self.catalog = supply.load_catalog()
        self.tcfg = volunteer.load_tasks()
        self.rcfg = recovery.load_rules()
        self.lock = threading.Lock()   # 상태 변경은 한 번에 하나씩(재고·투입 이중 처리 방지)
        self.notify = None             # 서버가 텔레그램 알림 함수를 넣는다: notify(kind, data)

    # ------------------------------------------------------------------ 라우팅
    def get(self, path):
        p = path.strip("/").split("/")[1:]
        if p == ["stage"]:
            return 200, self.db.get_record("stage", "current") or {"level": "정상", "confirmed": False}
        if p == ["welfare", "checks"]:
            with self.lock:
                return 200, self.checks_status()
        if p == ["verify", "docs"]:
            return 200, {"docs": [{"id": k, "text": v} for k, v in self.verify_docs().items()],
                         "model_connected": self.verify_llm is not None}
        if p == ["recovery"]:
            return 200, self.recovery_status()
        if p == ["volunteer"]:
            return 200, self.volunteer_status()
        if p == ["supply"]:
            with self.lock:
                return 200, self.supply_status()
        return None

    def post(self, path, data):
        p = path.strip("/").split("/")[1:]
        if p == ["recovery", "parse"]:   # 외부 API를 기다리는 동안 다른 기능이 멈추지 않게 잠금 밖에서
            return self.recovery_parse(data)
        with self.lock:
            # ③ 안부 확인
            if p == ["welfare", "checks", "start"]:
                return 200, self.checks_start(data)
            if len(p) == 4 and p[:2] == ["welfare", "checks"] and p[3] == "respond":
                return 200, self.checks_respond(p[2])
            # ⑦ 검증
            if p == ["verify"]:
                return 200, self.verify_draft(data)
            # ⑤ 복구·지원
            if p == ["recovery", "support"]:
                return 200, self.recovery_support(data)
            if p == ["recovery", "extract"]:
                return 200, self.recovery_extract(data)
            if p == ["recovery", "report"]:
                return 200, self.recovery_report(data)
            if p == ["recovery", "cases"]:
                return 201, self.case_create(data)
            if len(p) == 4 and p[:2] == ["recovery", "cases"] and p[3] == "advance":
                return 200, self.case_advance(p[2], data)
            if p == ["recovery", "surveys"]:
                return 201, self.survey_add(data)
            # ⑥ 봉사 코디
            if p == ["volunteer", "requests"]:
                return 200, self.volunteer_request(data)
            if len(p) == 4 and p[:2] == ["volunteer", "tasks"] and p[3] in ("recommend", "checkin", "complete"):
                return 200, getattr(self, "task_" + p[3])(p[2])
            if p == ["volunteer", "sos"]:
                return 201, self.sos_raise(data)
            if len(p) == 4 and p[:2] == ["volunteer", "sos"] and p[3] == "ack":
                return 200, self.sos_ack(p[2])
            # ⑧ 물품 수급
            if p == ["supply", "requests"]:
                return 200, self.supply_request(data)
            if len(p) == 4 and p[:2] == ["supply", "requests"]:
                action = {"review": self.request_review, "propose": self.request_propose,
                          "deliver": self.request_deliver, "receipt": self.request_receipt}.get(p[3])
                if action:
                    return 200, action(p[2], data)
            if p == ["supply", "donations"]:
                return 201, self.donation_add(data)
            if len(p) == 4 and p[:2] == ["supply", "donations"] and p[3] == "inspect":
                return 200, self.donation_inspect(p[2], data)
        return None

    # 승인 대기열 연결 ---------------------------------------------------------
    def before_approve(self, approval):
        """승인 직전 재검사. 문제가 있으면 사유 문자열."""
        if approval["kind"] == "volunteer_task":
            busy = self.busy_volunteers()
            taken = [v for v in approval["item"]["recommendation"]["team"] if v in busy]
            if taken:
                return f"봉사자 {', '.join(taken)}가 이미 다른 작업에 투입되어 있습니다. 다시 추천하세요"
        if approval["kind"] == "supply_dispatch":
            # 승인을 기록한 뒤 출고가 실패하면 '승인됐지만 출고 안 됨' 상태가 남는다. 저장하지 않는 사본으로 먼저 출고해 본다.
            inv = self.inventory()
            try:
                for row in approval["item"]["plan"]:
                    inv.issue(row["source"], row["qty"], approved=True, by="check", ts=time.time(),
                              request_id=approval["item"]["request_id"])
            except (ValueError, KeyError) as e:
                return f"출고할 수 없습니다({e}). 반려 후 다시 추천하세요"
        return None

    def after_resolve(self, approval, approved, session):
        kind, item, who = approval["kind"], approval["item"], session["username"]
        if kind == "monitor_stage" and approved:
            stage = self.db.put_record("stage", "current", {
                "level": item.get("level"), "hazards": item.get("hazards", []), "summary": item.get("summary"),
                "confirmed": True, "approval_id": approval["id"], "approved_by": who, "at": time.time()})
            if self.notify:
                self.notify("stage", stage)
        elif kind == "welfare_check" and approved:   # 신고 자동 연결: 승인되면 그 구역 가구에 안부 확인 시작
            self.checks_start({"zone": item["zone"], "timeout_s": item.get("timeout_s", self.gcfg["timeout_s"])})
        elif kind == "volunteer_task":
            task = self.db.get_record("task", item["task"]["id"])
            if task is None:
                return
            if approved:
                task.update(state="투입승인", team=item["recommendation"]["team"], approved_by=who)
            else:
                task.update(state="대기", rejected=sorted(set(task.get("rejected", [])) | set(item["recommendation"]["team"])))
            task.pop("pending_approval", None)
            self.db.put_record("task", task["id"], task)
        elif kind == "supply_dispatch":
            req = self.db.get_record("supply_request", item["request_id"])
            if req is None:
                return
            inv = self.inventory()
            if approved and req["state"] == "검토" and item.get("combined_review"):   # 신고 자동 연결: 검토 승인 겸함
                self._move(req, "승인", "staff", who, "배치 승인과 함께 검토 승인")
            if approved:   # 배치 승인 → 예약분 출고(재고 감소), 요청은 '배치'
                now = time.time()
                for row in item["plan"]:
                    inv.issue(row["source"], row["qty"], approved=True, by=who, ts=now, request_id=req["id"])
                    self.db.put_record("outflow", _new_id("OUT"), {"ts": now, "item": req["item"], "qty": row["qty"],
                                                                "source": row["source"], "request": req["id"]})
                self.save_inventory(inv)
                self._move(req, "배치", "staff", who)
                req["reservation"] = []
            else:          # 반려 → 예약 해제, 요청은 그 상태('승인' 또는 자동 연결분은 '검토')에 남아 다시 처리 가능
                req["reservation"] = []
                req.setdefault("history", []).append({"to": req["state"], "actor": "staff", "by": who,
                                                      "reason": "배치 반려(예약 해제)", "ts": time.time()})
            req.pop("pending_approval", None)
            self.db.put_record("supply_request", req["id"], req)

    # ------------------------------------------------------------------ ③ 안부 확인
    def checks_start(self, data):
        zone = data.get("zone", "all")
        if zone != "all" and zone not in {z["id"] for z in self.sc["zones"]}:
            raise ValueError("zone은 구역 ID 또는 all이어야 합니다")
        timeout = _int(data.get("timeout_s", self.gcfg["timeout_s"]), "응답 대기 시간(초)", 5, 3600)
        cfg = {**self.gcfg, "timeout_s": timeout}
        sc, now, started, skipped = welfare.SafetyChecker(cfg), time.time(), [], []
        for h in self.households.values():
            if zone != "all" and h["zone"] != zone:
                continue
            if not h["consent"]:
                skipped.append(h["id"]); continue   # 동의하지 않은 가구는 안부 확인 대상이 아니다
            sc.start(h["id"], now, vulnerable=bool(h["factors"]))
            row = sc.rows[h["id"]]
            self.db.put_record("check", h["id"], {"household": h["id"], "zone": h["zone"], "factors": h["factors"],
                                                  "vulnerable": row["vulnerable"], "deadline": row["deadline"],
                                                  "state": row["state"], "started_at": now})
            started.append(h["id"])
        self.db.log_audit("welfare_check_started", "zone", zone, {"households": started, "timeout_s": timeout})
        if self.notify and started:
            self.notify("welfare", {"households": started, "timeout_s": timeout})
        return {"started": started, "skipped_no_consent": skipped}

    def checks_respond(self, hid):
        rec = self.db.get_record("check", hid)
        if rec is None:
            raise KeyError(hid)
        if rec["state"] not in ("대기", "격상"):
            raise Conflict(f"{hid}는 이미 응답했습니다")
        sc = welfare.SafetyChecker(self.gcfg)
        sc.rows[hid] = {"deadline": rec["deadline"], "vulnerable": rec["vulnerable"], "state": rec["state"]}
        rec.update(state=sc.respond(hid, time.time()), responded_at=time.time())
        self.db.put_record("check", hid, rec)
        return rec

    def checks_status(self):
        """기한이 지난 무응답 가구를 격상(취약 가구 먼저). 출동은 하지 않고 담당자 알림만."""
        recs = {r["household"]: r for r in self.db.list_records("check")}
        sc, now = welfare.SafetyChecker(self.gcfg), time.time()
        for hid, r in recs.items():
            sc.rows[hid] = {"deadline": r["deadline"], "vulnerable": r["vulnerable"], "state": r["state"]}
        escalated = sc.tick(now)
        for e in escalated:
            r = recs[e["household"]]
            r.update(state="격상", escalated_at=now)
            self.db.put_record("check", r["household"], r)
            self.db.log_audit("welfare_escalated", "household", r["household"],
                              {"vulnerable": r["vulnerable"], "overdue_s": e["overdue_s"]})
        order = {"격상": 0, "대기": 1, "지연 응답": 2, "안전": 3}
        rows = sorted(recs.values(), key=lambda r: (order.get(r["state"], 9), not r["vulnerable"], r["household"]))
        for r in rows:
            r["seconds_left"] = round(r["deadline"] - now) if r["state"] == "대기" else None
        return {"checks": rows, "escalated_now": [e["household"] for e in escalated],
                "counts": {s: sum(r["state"] == s for r in rows) for s in order}}

    # ------------------------------------------------------------------ ⑦ 검증
    def verify_docs(self):
        docs = {f["id"]: f["text"] for f in self.gcfg["fragments"]}
        docs.update({r["id"]: f"{r['title']}: {r['content']}" for r in self.rcfg["rules"]})
        return docs

    def verify_draft(self, data):
        draft = _text(data.get("draft"), "검증할 문구", 2000)
        ids = data.get("doc_ids")
        if not isinstance(ids, list) or not ids or len(ids) > 20 or any(not isinstance(i, str) for i in ids):
            raise ValueError("근거 문서 ID를 1~20개 골라야 합니다")
        r = verify.verify(draft, ids, self.verify_docs(), client=self.verify_llm)
        self.db.log_audit("verify_checked", "draft", None, {"verdict": r.output["verdict"], "doc_ids": r.output["doc_ids"]})
        return {**r.output, "model_connected": self.verify_llm is not None, "ai_used": r.ai_used}

    def _verdict(self, draft, evidence):
        v, why = verify.verify_text(draft, evidence, client=self.verify_llm)
        return {"verdict": v, "reason": why, "displayable": v == "grounded"}

    # ------------------------------------------------------------------ ⑤ 복구·지원
    def _household_input(self, data):
        area = data.get("area", "거제시")
        if area not in AREAS:
            raise ValueError(f"area는 {AREAS} 중 하나여야 합니다")
        dmg = data.get("damage_types", [])
        if not isinstance(dmg, list) or any(d not in DAMAGE_TYPES for d in dmg):
            raise ValueError(f"피해 유형은 {DAMAGE_TYPES} 중에서 골라야 합니다")
        housing = data.get("housing", "단독주택")
        if housing not in ("단독주택", "공동주택"):
            raise ValueError("housing은 단독주택 또는 공동주택이어야 합니다")
        flags = {k: data.get(k, False) for k in ("long_term_evacuation", "lease_signed")}
        if any(not isinstance(v, bool) for v in flags.values()):
            raise ValueError("장기 대피·임대차 계약 여부는 true/false여야 합니다")
        return {"id": data.get("household") or "입력 가구", "area": area, "damage_types": dmg, "housing": housing, **flags}

    def recovery_support(self, data):
        m = recovery.match_support(self._household_input(data), self.rcfg)
        draft, evidence = recovery.support_text(m) if m.output["candidates"] else ("", "")
        check = self._verdict(draft, evidence) if draft else {"verdict": None, "reason": "해당 항목 없음", "displayable": False}
        return {**m.output, "draft": draft if check["displayable"] else None, "verification": check}

    def recovery_extract(self, data):
        r = recovery.extract_fields(_text(data.get("text"), "서류 텍스트", 3000), client=self.llm)
        return {**r.output, "ai_used": r.ai_used, "fallback_reason": r.fallback_reason}

    def recovery_parse(self, data):
        """서류 파일(PDF·사진·텍스트) → 텍스트 → 필드 추출. 원본 파일은 저장하지 않고, 화면에는 마스킹한 텍스트만 돌려준다."""
        name = _text(data.get("filename"), "파일 이름", 100)
        try:
            raw = base64.b64decode(_text(data.get("data_base64"), "파일 내용", docparse.MAX_BYTES * 4 // 3 + 8), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("파일 내용(base64)이 올바르지 않습니다") from None
        if not raw or len(raw) > docparse.MAX_BYTES:
            raise ValueError(f"파일은 1바이트~{docparse.MAX_BYTES // (1024 * 1024)}MB여야 합니다")
        kind, pages = docparse.sniff(raw), None
        if kind == "txt":   # 이미 텍스트인 서류는 외부로 보내지 않고 바로 읽는다
            text, parser = raw.decode("utf-8-sig"), "text"
        elif kind in docparse.TYPES:
            if self.doc_parser is None:
                return 503, {"error": "문서 파싱 API가 연결되지 않았습니다(LLM_MODE=solar 필요). 서류 텍스트를 직접 붙여 넣어 주세요"}
            if data.get("consent_external") is not True:   # 원본(개인정보 포함)이 외부 API로 나가므로 확인을 받는다
                raise ValueError("서류 원본이 업스테이지 API로 전송됩니다. 본인(또는 보호자) 동의를 확인하고 체크해 주세요")
            try:
                text, pages = self.doc_parser.parse(name, raw)
            except docparse.DocParseUnavailable as e:
                self.db.log_audit("document_parse_failed", "document", None, {"type": kind, "bytes": len(raw), "reason": str(e)[:200]})
                return 502, {"error": f"{e}. 서류 텍스트를 직접 붙여 넣어 주세요"}
            parser = "upstage"
        else:
            raise ValueError("PDF·PNG·JPG·텍스트(.txt) 파일만 읽을 수 있습니다")
        r = recovery.extract_fields(text, client=self.llm)
        self.db.log_audit("document_parsed", "document", None, {"type": kind, "bytes": len(raw), "pages": pages,
                                                                 "parser": parser, "fields_found": 3 - len(r.output["missing"])})
        return 200, {"text": mask(text)[:3000], "truncated": len(text) > 3000, "parser": parser, "pages": pages,
                     **r.output, "ai_used": r.ai_used, "fallback_reason": r.fallback_reason}

    def recovery_report(self, data):
        fields = data.get("fields")
        if not isinstance(fields, dict):
            raise ValueError("fields는 객체여야 합니다")
        fields = {k: _text(fields.get(k), k, 50) for k in recovery.REQUIRED}   # 수동 입력 폼: 세 필드 모두 필요
        dmg = self._household_input({"damage_types": data.get("damage_types", [])})["damage_types"]
        if not dmg:
            raise ValueError("피해 유형을 하나 이상 골라야 합니다")
        hid = data.get("household")
        if hid is not None and hid not in self.households:
            raise KeyError(hid)
        r = recovery.draft_report(hid or "입력 가구", fields, dmg, self.rcfg)
        rid = _new_id("RP")
        self.db.put_record("report", rid, {"id": rid, **r.output, "damage_types": dmg, "created_at": time.time()})
        return {"id": rid, **r.output, "notice": "모의 신고서 초안입니다. 실제 제출은 일어나지 않습니다."}

    def case_create(self, data):
        hid = data.get("household")
        if hid not in self.households:
            raise ValueError("가구 ID(H01~H20)를 골라야 합니다")
        if any(c["household"] == hid for c in self.db.list_records("case")):
            raise Conflict(f"{hid}의 복구 케이스가 이미 있습니다")
        cid = _new_id("CS")
        case = {"id": cid, "household": hid, "zone": self.households[hid]["zone"], "stage": recovery.STAGES[0],
                "timeline": [{"stage": recovery.STAGES[0], "by": None, "ts": time.time()}], "tasks": {}}
        return self.db.put_record("case", cid, case)

    def case_advance(self, cid, data):
        case = self.db.get_record("case", cid)
        if case is None:
            raise KeyError(cid)
        rc = recovery.RecoveryCase(cid, case["household"]); rc.stage_idx = recovery.STAGES.index(case["stage"])
        to = data.get("to")
        try:
            rc.advance(to, "staff", time.time())
        except ValueError as e:
            raise Conflict(str(e)) from None
        case["stage"] = rc.stage
        case["timeline"].append({"stage": rc.stage, "by": data.get("_by"), "ts": time.time()})
        return self.db.put_record("case", cid, case)

    def survey_add(self, data):
        zone, address = places.resolve(data, "피해 위치")
        dmg = data.get("damage_type")
        if dmg not in DAMAGE_TYPES:
            raise ValueError(f"피해 유형은 {DAMAGE_TYPES} 중 하나여야 합니다")
        sid = _new_id("SV")
        return self.db.put_record("survey", sid, {"id": sid, "zone": zone, "address": address, "damage_type": dmg, "ts": time.time()})

    def recovery_status(self):
        return {"cases": self.db.list_records("case"), "reports": self.db.list_records("report"),
                "aggregate": recovery.aggregate(self.db.list_records("survey")),
                "surveys": sorted(self.db.list_records("survey"), key=lambda x: -x.get("ts", 0))[:50],
                "households": list(self.households.values()), "stages": recovery.STAGES,
                "damage_types": DAMAGE_TYPES, "notice": recovery.NOTICE, "doc_parser": self.doc_parser is not None,
                "zones": [{"id": z["id"], "name": z["name"]} for z in self.sc["zones"]]}

    # ------------------------------------------------------------------ ⑥ 봉사 코디
    def busy_volunteers(self):
        return {v for t in self.db.list_records("task") if t["state"] in ("투입승인", "진행") for v in t.get("team", [])}

    def volunteer_status(self):
        busy = self.busy_volunteers()
        vols = [{**v, "busy": v["id"] in busy} for v in self.volunteers.values()]
        tasks = self.db.list_records("task")
        sos = [s for s in self.db.list_records("sos") if s["open"]]
        return {"volunteers": vols, "tasks": tasks, "sos": sos, "safety": self.tcfg["safety"],
                "task_types": list(self.tcfg["task_types"])}

    def volunteer_request(self, data):
        text = _text(data.get("text"), "작업 요청", 300)
        hid = data.get("household")
        if hid is not None and hid not in self.households:
            raise KeyError(hid)
        if hid is not None and not data.get("zone") and not data.get("address"):   # 가구를 고르면 위치는 그 가구로
            data = {**data, "zone": self.households[hid]["zone"], "address": self.households[hid].get("address")}
        zone, address = places.resolve(data, "작업 위치")
        temp = data.get("temperature_c")
        if temp is not None and (isinstance(temp, bool) or not isinstance(temp, (int, float)) or not -20 <= temp <= 60):
            raise ValueError("현장 온도는 -20~60 사이 숫자여야 합니다")
        req_id = _new_id("VR")
        tasks, ai, why = volunteer.decompose(req_id, text, zone, self.tcfg, client=self.llm)
        out = []
        for t in tasks:
            rec = {**t, "address": address, "request_id": req_id, "household": hid, "request_text": text, "state": "대기",
                   "temperature_c": temp, "created_at": time.time(), "incident_id": data.get("incident_id")}
            self.db.put_record("task", t["id"], rec)
            out.append(self._recommend_task(rec))
        return {"request_id": req_id, "tasks": out, "ai_used": ai, "fallback_reason": why,
                "note": None if tasks else "작업 유형을 찾지 못했습니다. 담당자가 직접 나눠 등록하세요"}

    def _recommend_task(self, task):
        busy = self.busy_volunteers()
        cands = [{**v, "available": v["id"] not in busy and v["id"] not in task.get("rejected", [])}
                 for v in self.volunteers.values() if v["mode"] in ("작업", "둘 다")]
        r = volunteer.assign(task, cands, self.travel(), self.dcfg, self.tcfg["safety"],
                             temperature_c=task.get("temperature_c"))
        rec = r.output
        if rec["team"]:   # 추천 인원이 있으면 투입 승인 대기열에. 부족분은 대기열 표시
            task["state"] = "추천"
            task["pending_approval"] = self.db.create_approval(
                "volunteer_task", {"task": {k: task[k] for k in ("id", "type", "zone", "skills", "equipment", "people", "hours")},
                                   "recommendation": rec, "work_order": volunteer.work_order(task, rec["team"]),
                                   "incident_id": task.get("incident_id")},
                incident_id=task.get("incident_id"))["id"]
        task["last_recommendation"] = rec
        self.db.put_record("task", task["id"], task)
        return task

    def task_recommend(self, tid):
        task = self.db.get_record("task", tid)
        if task is None:
            raise KeyError(tid)
        if task["state"] != "대기":
            raise Conflict(f"{tid}는 '{task['state']}' 상태라 다시 추천할 수 없습니다")
        return self._recommend_task(task)

    def _task_move(self, tid, to):
        task = self.db.get_record("task", tid)
        if task is None:
            raise KeyError(tid)
        ts = volunteer.TaskState(tid); ts.state = task["state"]
        try:
            ts.move(to, "volunteer")
        except (ValueError, PermissionError) as e:
            raise Conflict(str(e)) from None
        task["state"] = to
        return task

    def task_checkin(self, tid):   # 노드 E·봉사자 화면의 도착 체크인을 담당자 화면에서 모의
        task = self._task_move(tid, "진행"); task["checked_in_at"] = time.time()
        return self.db.put_record("task", tid, task)

    def task_complete(self, tid):  # 완료 보고 → 같은 가구 복구 케이스에 반영
        task = self._task_move(tid, "완료"); task["completed_at"] = time.time()
        self.db.put_record("task", tid, task)
        case = next((c for c in self.db.list_records("case") if task.get("household") and c["household"] == task["household"]), None)
        update = None
        if case:
            case["tasks"][tid] = case["tasks"].get(tid, "대기")
            for t in self.db.list_records("task"):
                if t.get("household") == case["household"] and t["id"] not in case["tasks"]:
                    case["tasks"][t["id"]] = "완료" if t["state"] == "완료" else "대기"
            update = volunteer.FieldSession().complete(case, tid)
            self.db.put_record("case", case["id"], case)
        return {**task, "case_update": update}

    def sos_raise(self, data):
        vid, tid = data.get("volunteer"), data.get("task")
        if vid not in self.volunteers:
            raise ValueError("봉사자 ID를 골라야 합니다")
        e = volunteer.FieldSession().sos(vid, tid, time.time())
        sid = _new_id("SOS")
        self.db.log_audit("sos_raised", "volunteer", vid, {"task": tid})
        return self.db.put_record("sos", sid, {"id": sid, **e, "name": self.volunteers[vid].get("name", vid), "open": True})

    def sos_ack(self, sid):
        s = self.db.get_record("sos", sid)
        if s is None:
            raise KeyError(sid)
        if not s["open"]:
            raise Conflict("이미 확인한 SOS입니다")
        s.update(open=False, acked_at=time.time())
        return self.db.put_record("sos", sid, s)

    # ------------------------------------------------------------------ ⑧ 물품 수급
    def _seed_inventory(self):
        if self.db.count_records("stock") == 0:   # 첫 사용 때 가상 재고·기부를 DB로 옮긴다
            for s in self.inv_seed["stock"]:
                loc = self.locations[s["location"]]
                self.db.put_record("stock", s["id"], {**s, "zone": loc["zone"],
                                                      "category": self.catalog["items"][s["item"]]["category"]})
            for d in self.inv_seed["donations"]:
                self.db.put_record("donation", d["id"], {**d, "category": self.catalog["items"][d["item"]]["category"]})

    def inventory(self):
        self._seed_inventory()
        inv = supply.Inventory(self.db.list_records("stock"), self.db.list_records("donation"))
        for r in self.db.list_records("supply_request"):
            if r.get("reservation"):
                inv.reservations[r["id"]] = [dict(x) for x in r["reservation"]]
        inv.outflow = [(o["ts"], o["item"], o["qty"]) for o in self.db.list_records("outflow")]
        return inv

    def save_inventory(self, inv):
        for s in inv.stock.values():
            self.db.put_record("stock", s["id"], s)
        for d in inv.donations.values():
            self.db.put_record("donation", d["id"], d)

    def _source_name(self, sid):
        s = self.db.get_record("stock", sid)
        if s:
            return self.locations[s["location"]]["name"]
        d = self.db.get_record("donation", sid)
        return f"기부 {sid}({d['donor']})" if d else sid

    def _move(self, req, to, actor, who, reason=None):
        t = supply.RequestTracker(req["id"]); t.state = req["state"]
        try:
            t.move(to, actor, reason)
        except (ValueError, PermissionError) as e:
            raise Conflict(str(e)) from None
        req["state"] = to
        req.setdefault("history", []).append({"to": to, "actor": actor, "by": who, "reason": reason, "ts": time.time()})

    def supply_request(self, data):
        hid = data.get("household")
        if hid not in self.households:
            raise ValueError("가구 ID(H01~H20)를 골라야 합니다")
        flags = {k: data.get(k, False) for k in REQUEST_FLAGS}
        if any(not isinstance(v, bool) for v in flags.values()):
            raise ValueError("요청 조건은 true/false여야 합니다")
        when = data.get("when", "오늘 중")
        if when not in WHEN:
            raise ValueError(f"희망 시각은 {WHEN} 중 하나여야 합니다")
        people = _int(data.get("people", 1), "대상 인원", 1, 50)
        ai, why, memo = False, None, None
        if data.get("item"):
            items = [{"item": _text(data["item"], "품목", 30), "qty": _int(data.get("qty"), "수량", 1, 1000)}]
        else:   # 자유 메모 해석(⑧ LLM 선택, 실패 시 카탈로그 키워드)
            memo = _text(data.get("memo"), "메모", 300)
            items, ai, why = supply.parse_memo(memo, self.catalog, client=self.llm)
            if not items:
                raise ValueError("메모에서 품목을 찾지 못했습니다. 품목을 직접 골라 주세요")
            items = [{"item": x["item"], "qty": x["qty"] or 1, "qty_unconfirmed": x["qty"] is None} for x in items]
        now, zone, results = time.time(), self.households[hid]["zone"], []
        open_reqs = [r for r in self.db.list_records("supply_request") if r["state"] in ("접수", "검토")]
        for it in items:
            prev = next((r for r in open_reqs if r["household"] == hid and r["item"] == it["item"]
                         and now - r["last_ts"] <= self.catalog["merge_window_h"] * 3600), None)
            rid = prev["id"] if prev else _new_id("SR")
            row = {"id": rid, "household": hid, "item": it["item"], "qty": it["qty"], "people": people,
                   "when": when, "zone": zone, "ts": now, **flags}
            if prev:   # 같은 가구·같은 품목의 열린 요청이 있으면 하나로 합치고 수량을 더한다(상한 검사는 합친 수량으로)
                row.update(qty=prev["qty"] + it["qty"], people=max(prev["people"], people),
                           when="지금" if "지금" in (prev["when"], when) else prev["when"],
                           **{k: prev.get(k, False) or flags[k] for k in REQUEST_FLAGS})
            p = supply.process_requests([row], self.catalog)[0]   # 분류·긴급도·상한·의약품 경로
            rec = {**(prev or {}), **row, **p, "last_ts": now, "memo": memo,
                   "qty_unconfirmed": it.get("qty_unconfirmed", False) or (prev or {}).get("qty_unconfirmed", False),
                   "merged_count": (prev or {}).get("merged_count", 0) + 1}
            if not prev:
                rec.update(state="접수", history=[], reservation=[], created_at=now)
                self._move(rec, "검토", "system", None)
            self.db.put_record("supply_request", rid, rec)
            results.append(rec)
        return {"requests": results, "ai_used": ai, "fallback_reason": why}

    def _get_request(self, rid):
        req = self.db.get_record("supply_request", rid)
        if req is None:
            raise KeyError(rid)
        return req

    def request_review(self, rid, data):
        req, decision = self._get_request(rid), data.get("decision")
        reason = _text(data.get("reason"), "사유", 300, required=decision in ("보류", "반려"))
        to = {"승인": "승인", "보류": "보류", "반려": "반려", "재검토": "검토"}.get(decision)
        if to is None:
            raise ValueError("decision은 승인·보류·반려·재검토 중 하나여야 합니다")
        if req.get("pending_approval"):   # 신고 자동 연결분은 배치 승인 한 번으로 검토까지 처리한다
            raise Conflict(f"승인 대기열의 배치 승인({req['pending_approval']})에서 처리하세요")
        self._move(req, to, "staff", data.get("_by"), reason)
        self.db.put_record("supply_request", rid, req)
        if to == "승인" and req["route"] == "matching":
            return self.request_propose(rid, {})
        return req

    def request_propose(self, rid, data):
        req = self._get_request(rid)
        if req["state"] != "승인":
            raise Conflict(f"'{req['state']}' 상태에서는 공급원을 추천할 수 없습니다(승인 상태만)")
        return self._propose(req)

    def _propose(self, req, combined=False):
        """공급원 추천·예약 후 배치 승인 대기열에 올린다. combined: 신고에서 자동으로 만든 요청이라
        검토 승인과 배치 승인을 담당자의 한 번 승인으로 처리한다(요청은 '검토' 상태로 기다림)."""
        rid = req["id"]
        if req.get("pending_approval"):
            raise Conflict(f"이미 배치 승인 대기 중입니다({req['pending_approval']})")
        if req["route"] == "manual_medicine":
            return {**req, "note": "의약품·의료 분류는 자동 매칭하지 않습니다. 담당자가 직접 처리하세요"}
        if req["route"] != "matching":
            return {**req, "note": "카탈로그에 없는 품목이라 자동 매칭하지 않습니다. 담당자가 직접 처리하세요"}
        inv = self.inventory()
        r = supply.propose(req, inv, self.travel(), self.dcfg)
        out = r.output
        req["reservation"] = inv.reservations.get(rid, [])
        req["last_proposal"] = {"shortfall": out["shortfall"], "procurement_needed": out["procurement_needed"]}
        if out["split_plan"]:   # 예약된 계획을 배치 승인 대기열에(승인 전에는 출고 없음)
            plan = [{**row, "name": self._source_name(row["source"])} for row in out["split_plan"]]
            req["pending_approval"] = self.db.create_approval("supply_dispatch", {
                "request_id": rid, "household": req["household"], "item": req["item"], "qty": req["qty"],
                "zone": req["zone"], "plan": plan, "shortfall": out["shortfall"],
                "procurement_needed": out["procurement_needed"], "reason": out["recommendation"].get("reason"),
                "incident_id": req.get("incident_id"), "combined_review": combined},
                incident_id=req.get("incident_id"))["id"]
        self.db.put_record("supply_request", rid, req)
        return {**req, "proposal": out}

    def incident_items(self, inc):
        """신고문에서 품목·수량 추출(AI 호출이 있을 수 있어 잠금 밖에서 부른다). 보완 내용까지 전부 본다."""
        return supply.parse_memo(inc["text"], self.catalog, client=self.llm, max_chars=len(inc["text"]))

    def supply_from_incident(self, inc, parsed):
        """② 접수에서 '물품 요청'으로 분류된 신고 → ⑧ 물품 요청. parsed: incident_items 결과.
        담당자 판단이 필요 없는 요청(카탈로그 품목·상한 이내·수량 확인)은 바로 공급원을 추천해
        승인 대기열에 한 건으로 올린다. 나머지는 '검토'에 두고 사유를 적는다. 품목을 못 찾으면 빈 목록."""
        items, ai, why = parsed
        now, out = time.time(), []
        for it in items:
            rid = _new_id("SR")
            row = {"id": rid, "household": None, "item": it["item"], "qty": it["qty"] or 1,
                   "people": inc.get("people") or 1, "when": "지금" if inc.get("urgency") == 1 else "오늘 중",
                   "zone": inc["zone"], "ts": now, **{k: False for k in REQUEST_FLAGS},
                   "has_elderly": bool(inc.get("vulnerable"))}
            p = supply.process_requests([row], self.catalog)[0]
            req = {**row, **p, "incident_id": inc["id"], "last_ts": now, "memo": None,
                   "qty_unconfirmed": it["qty"] is None, "merged_count": 1,
                   "state": "접수", "history": [], "reservation": [], "created_at": now}
            self._move(req, "검토", "system", None, f"신고 {inc['id']}에서 자동 생성")
            hold = list(p["flags"]) + (["수량 미확인"] if req["qty_unconfirmed"] else [])
            if hold:
                req["review_reason"] = ", ".join(hold)
                self.db.put_record("supply_request", rid, req)
            else:
                self._propose(req, combined=True)
                req = self._get_request(rid)
                if not req.get("pending_approval"):   # 재고가 하나도 없으면 대기열에 올릴 계획이 없다
                    req["review_reason"] = "재고 없음: 조달 필요"
                    self.db.put_record("supply_request", rid, req)
            out.append(self._get_request(rid))
        return {"requests": out, "ai_used": ai, "fallback_reason": why}

    def cancel_incident_supply(self, incident_id, who, reason):
        """오접수 처리: 이 신고에서 만든 물품 요청 중 아직 출고 전(검토·보류·승인)인 것을 반려·예약 해제.
        이미 배치·이동 중인 요청은 바꾸지 않고 목록으로 돌려준다(담당자가 회수 여부를 판단)."""
        cancelled, left = [], []
        for req in self.db.list_records("supply_request"):
            if req.get("incident_id") != incident_id or req["state"] in ("완료", "반려"):
                continue
            if req["state"] in ("검토", "보류", "승인"):
                req["reservation"] = []
                req.pop("pending_approval", None)
                req["state"] = "반려"   # 오접수는 어느 단계에서든 담당자 결정으로 닫는다
                req.setdefault("history", []).append({"to": "반려", "actor": "staff", "by": who,
                                                      "reason": f"오접수 처리: {reason}", "ts": time.time()})
                self.db.put_record("supply_request", req["id"], req)
                cancelled.append(req["id"])
            else:
                left.append({"id": req["id"], "state": req["state"]})
        return cancelled, left

    def request_deliver(self, rid, data):
        req = self._get_request(rid)
        self._move(req, "이동", "staff", data.get("_by"), "전달 출발(모의)")
        return self.db.put_record("supply_request", rid, req)

    def request_receipt(self, rid, data):
        req, received = self._get_request(rid), data.get("received")
        if not isinstance(received, bool):
            raise ValueError("received는 true/false여야 합니다")
        t = supply.RequestTracker(rid); t.state = req["state"]
        try:
            t.confirm_receipt(received)   # 피해자 확인(노드 D 버튼·화면 모의)
        except (ValueError, PermissionError) as e:
            raise Conflict(str(e)) from None
        req["state"] = t.state
        req.setdefault("history", []).append({"to": t.state, "actor": "victim", "by": None,
                                              "reason": None if received else "못 받음", "ts": time.time()})
        self.db.put_record("supply_request", rid, req)
        if not received:   # 미수령 → 승인 단계로 돌아가 같은 요청의 공급원을 다시 추천
            self._move(req, "승인", "system", None, "미수령 재추천")
            self.db.put_record("supply_request", rid, req)
            return self.request_propose(rid, {})
        return req

    def donation_add(self, data):
        zone, address = places.resolve(data, "전달 장소")
        d = {"donor": data.get("donor"), "item": data.get("item"), "qty": data.get("qty"), "unit": data.get("unit"),
             "condition": data.get("condition"), "days_left": data.get("days_left"), "zone": zone, "address": address}
        if d["donor"] not in self.volunteers:
            raise ValueError("기부자(봉사자 ID)를 골라야 합니다")
        _int(d["qty"], "수량", 1, 10000)
        if d["days_left"] is not None:
            _int(d["days_left"], "유통기한까지 남은 일수", -3650, 3650)
        check = supply.validate_donation(d, self.catalog)   # B8 등록 검사(의약품·파손·기한 경과·단위 불명 거부)
        if not check["ok"]:
            raise ValueError("등록할 수 없습니다: " + ", ".join(check["issues"]) + (f" ({check['action']})" if check["action"] else ""))
        self._seed_inventory()
        did = _new_id("DN")
        return self.db.put_record("donation", did, {"id": did, **d, "state": "검수 대기",
                                                    "category": self.catalog["items"][d["item"]]["category"]})

    def donation_inspect(self, did, data):
        d = self.db.get_record("donation", did)
        if d is None:
            raise KeyError(did)
        if d["state"] != "검수 대기":
            raise Conflict(f"'{d['state']}' 상태라 검수할 수 없습니다")
        accept = data.get("accept")
        if not isinstance(accept, bool):
            raise ValueError("accept는 true/false여야 합니다")
        d.update(state="접수 가능" if accept else "반려", inspected_at=time.time())
        return self.db.put_record("donation", did, d)

    def supply_status(self):
        inv, now = self.inventory(), time.time()
        fc = supply.forecast(inv, self.catalog, now)
        notice = supply.shortage_notice(fc, client=self.llm).output
        if notice["draft"]:   # 필요 물품 안내 초안은 ⑦ 검증을 통과해야 봉사자 화면에 낼 수 있다
            evidence = "\n".join(f"{r['item']} {supply.when_short(r['hours_left'])}" for r in notice["items"])
            notice["verification"] = self._verdict(notice["draft"], evidence)
        stock = [{**s, "location_name": self.locations[s["location"]]["name"],
                  "reserved": inv.reserved_qty(s["id"])} for s in inv.stock.values()]
        reqs = sorted(self.db.list_records("supply_request"),
                      key=lambda r: (r["state"] in ("완료", "반려"), r["urgency"], r.get("created_at", 0)))
        return {"stock": stock, "donations": list(inv.donations.values()), "requests": reqs, "forecast": fc,
                "notice": notice, "catalog": {k: {"category": v["category"], "units": v["units"]}
                                              for k, v in self.catalog["items"].items()},
                "households": list(self.households.values()), "volunteers": list(self.volunteers.values())}
