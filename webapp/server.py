"""Local-only demo web server for the disaster safety agents.
Run from project root: python webapp/server.py
This is a prototype, not a production or emergency-dispatch system.
"""
from __future__ import annotations
import hmac, json, os, ssl, sys, threading, time, traceback, uuid
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
from common.llm import SolarClient
from common.docparse import DocumentParser
from common.masking import mask
from common import scenario
from agents import dispatch, intake, monitor, welfare, verify
from coordinator import Coordinator
from database import db as database
from webapp import features, sensors

HOST = os.getenv("WEBAPP_HOST", "127.0.0.1")
PORT = int(os.getenv("WEBAPP_PORT", "8765"))
MAX_BODY = 64 * 1024
MAX_UPLOAD_BODY = 7 * 1024 * 1024   # 서류 파일(5MB)을 base64로 담은 요청. 이 경로만 크게 받는다
UPLOAD_PATHS = {"/api/recovery/parse"}
MODE = os.getenv("LLM_MODE", "mock").strip().lower()
if MODE not in {"mock", "solar"}:
    raise RuntimeError("LLM_MODE must be 'mock' or 'solar'")
if MODE == "solar" and not os.getenv("UPSTAGE_API_KEY"):
    raise RuntimeError("LLM_MODE=solar requires UPSTAGE_API_KEY in environment")
# 서버 프로세스 전체의 호출 한도. 다 쓰면 재시작 전까지 규칙 폴백으로만 동작한다(/api/health의 llm_calls로 확인).
LLM_MAX_CALLS = int(os.getenv("SOLAR_MAX_CALLS", "1000"))
LLM = SolarClient(max_calls=LLM_MAX_CALLS) if MODE == "solar" else None
# 검증 모델: GROUNDEDNESS_MODEL이 있으면 그 모델, 없으면 판정 모델(VERIFY_MODEL, 기본 solar-pro3)이 지시문을 받아 판정한다.
VERIFY_LLM = verify.make_client(max_calls=LLM_MAX_CALLS) if MODE == "solar" else None
# 승인 카드에 표시할 검증 방식. judge는 Groundedness 모델 대신 채팅 모델이 지시문을 받아 판정했다는 뜻이고, same_as_writer면 안내문을 쓴 모델과 같은 모델이다.
VERIFIER_INFO = ({"model": VERIFY_LLM.model, "mode": VERIFY_LLM.verify_mode, "same_as_writer": VERIFY_LLM.same_as_writer}
                 if VERIFY_LLM else None)
# 마이크로비트 게이트웨이 인증 토큰. 비어 있으면 실시간 센서 입력 경로를 닫아 둔다.
SENSOR_TOKEN = os.getenv("SENSOR_GATEWAY_TOKEN", "").strip()
if SENSOR_TOKEN and len(SENSOR_TOKEN) < 16:
    raise RuntimeError("SENSOR_GATEWAY_TOKEN은 16자 이상이어야 합니다(python gateway/microbit_gateway.py --new-token)")
GUIDANCE_CFG = welfare.load_cfg()
COORDINATOR = Coordinator()
database.init_db()

STAFF_ROLES = {"operator", "admin"}   # 관리자 화면·승인·안내문 생성 권한
TLS_CERT, TLS_KEY = os.getenv("WEBAPP_TLS_CERT", "").strip(), os.getenv("WEBAPP_TLS_KEY", "").strip()
# HTTPS로 배포할 때 켠다. 인증서를 직접 지정하면 자동으로 켠다.
COOKIE_SECURE = os.getenv("WEBAPP_COOKIE_SECURE") == "1" or bool(TLS_CERT)
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
# 요청의 Host 헤더 허용 목록(DNS 리바인딩 방지). 내부망으로 열 때는 서버 주소를 WEBAPP_ALLOWED_HOSTS에 쉼표로 적는다.
ALLOWED_HOSTS = ({f"{h}:{PORT}" for h in ("127.0.0.1", "localhost", "[::1]")}
                 | ({"127.0.0.1", "localhost", "[::1]"} if PORT in (80, 443) else set())   # 기본 포트면 브라우저가 포트를 빼고 보낸다
                 | {h.strip().lower() for h in os.getenv("WEBAPP_ALLOWED_HOSTS", "").split(",") if h.strip()})
LOGIN_MAX_FAILS, LOGIN_WINDOW_S = 5, 600
_login_fails, _login_lock = {}, threading.Lock()


class RateLimiter:
    """키(접속 IP 등)별로 window_s초 동안 limit번까지만 허용한다. 공개 경로의 반복 요청(신고 폭주, LLM 한도 소진)을 막는다."""
    def __init__(self, limit, window_s):
        self.limit, self.window_s, self.hits, self.lock = limit, window_s, {}, threading.Lock()

    def allow(self, key):
        now = time.time()
        with self.lock:
            if len(self.hits) > 10000:   # 오래된 키 정리(메모리 상한)
                self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < self.window_s}
            recent = [t for t in self.hits.get(key, []) if now - t < self.window_s]
            if len(recent) >= self.limit:
                self.hits[key] = recent
                return False
            recent.append(now)
            self.hits[key] = recent
            return True


INCIDENT_LIMIT = RateLimiter(int(os.getenv("WEBAPP_INCIDENT_PER_MIN", "10")), 60)   # 접속 IP당 분당 신고 수
LOGIN_IP_LIMIT = RateLimiter(30, 600)   # 접속 IP당 10분에 로그인 시도 30번(계정별 5번 잠금과 별도)


def login_blocked(username):
    now = time.time()
    with _login_lock:
        fails = [t for t in _login_fails.get(username, []) if now - t < LOGIN_WINDOW_S]
        _login_fails[username] = fails
        return len(fails) >= LOGIN_MAX_FAILS


def record_login(username, ok):
    with _login_lock:
        if ok:
            _login_fails.pop(username, None)
        else:
            _login_fails.setdefault(username, []).append(time.time())


def session_cookie(token, max_age):
    return (f"sid={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
            + ("; Secure" if COOKIE_SECURE else ""))

AGENTS = [
    {"id": "monitor", "name": "감시", "mode": "규칙 기반", "status": "demo-ready"},
    {"id": "intake", "name": "신고 접수", "mode": "규칙 + 선택적 LLM", "status": "demo-ready"},
    {"id": "welfare", "name": "안부·안내", "mode": "규칙 기반 + 선택적 LLM", "status": "demo-ready"},
    {"id": "dispatch", "name": "배치 추천", "mode": "규칙 기반", "status": "demo-ready"},
    {"id": "recovery", "name": "복구·지원", "mode": "규칙 + 선택적 LLM", "status": "demo-ready"},
    {"id": "volunteer", "name": "봉사 코디", "mode": "규칙 + 선택적 LLM", "status": "demo-ready"},
    {"id": "verify", "name": "내용 검증", "mode": "규칙 + 선택적 LLM", "status": "demo-ready"},
    {"id": "supply", "name": "물품 수급", "mode": "규칙 + 선택적 LLM", "status": "demo-ready"},
]

FEATURE_AGENTS = {"/api/stage": "monitor", "/api/welfare/": "welfare", "/api/verify": "verify",
                  "/api/recovery": "recovery", "/api/volunteer": "volunteer", "/api/supply": "supply"}


def agent_failed(agent, err):
    """에이전트 처리 중 예상 못 한 오류 → 수동 처리 모드로 표시(조정자의 실패 대응). 다음 성공 때 자동으로 풀린다."""
    traceback.print_exc()
    database.put_record("agent_state", agent, {"id": agent, "manual": True, "since": time.time(),
                                               "reason": type(err).__name__})
    database.log_audit("agent_manual_mode", "agent", agent, {"error": type(err).__name__})


def agent_ok(agent):
    state = database.get_record("agent_state", agent)
    if state and state.get("manual"):
        database.put_record("agent_state", agent, {"id": agent, "manual": False, "since": time.time()})
        database.log_audit("agent_recovered", "agent", agent, {})


def agents_status():
    states = {r["id"]: r for r in database.list_records("agent_state")}
    return [{**a, "status": "manual", "manual_since": states[a["id"]]["since"], "manual_reason": states[a["id"]]["reason"]}
            if states.get(a["id"], {}).get("manual") else a for a in AGENTS]


INVALID = "오접수"
MAX_SUPPLEMENTS = 3


def incident_output(incident_id):
    inc = database.get_incident(incident_id)
    return None if inc is None else inc["result"]


def classify_incident(rid, text, ts, max_text=intake.MAX_TEXT):
    """② 접수. 분류기 자체가 예외로 멈춰도 신고는 버리지 않는다: 미분류로 저장하고 담당자가 직접 분류하게 한다.
    (신고자에게 보완을 요청할 일이 아니므로 missing이 아니라 triage로 담당자에게만 알린다)"""
    try:
        row = result_dict(intake.classify(rid, text, ts, intake.load_cfg(), client=LLM, max_text=max_text))
        agent_ok("intake")
        return row
    except Exception as e:
        agent_failed("intake", e)
        return {"agent": "intake", "ai_used": False, "fallback_reason": "자동 분류 실패", "needs_approval": False,
                "evidence": [], "output": {"id": rid, "text": mask(text)[:max_text], "ts": ts, "type": "미분류",
                                           "zone": "미상", "people": None, "vulnerable": False, "urgency": None,
                                           "route": "inbox", "notify_now": True, "label": "자동 분류 실패",
                                           "missing": [], "triage": "자동 분류 실패: 담당자가 직접 분류하세요"}}


def auto_dispatch(out, linked, notes):
    """급한 신고(긴급도 ≤ AUTO_DISPATCH_MAX_URGENCY) → ④ 공공 인력 배치 추천을 승인 대기열에. 같은 사건의 배치가
    진행 중이면 연결만 한다. 신고 내용 때문에 못 만들면 메모만, 예상 못 한 오류면 배치 에이전트를 수동 처리로."""
    if not (out.get("type") in SCENARIO["type_skills"] and out["type"] not in AUTO_DISPATCH_EXCLUDE
            and isinstance(out.get("urgency"), int) and out["urgency"] <= AUTO_DISPATCH_MAX_URGENCY):
        return
    try:
        same = ongoing_dispatch(out)
        if same:
            linked["dispatch"] = [same]
            notes.append(f"같은 구역·유형 배치가 이미 진행 중이라 연결만 함({same})")
        else:
            d = create_dispatch(out["id"], "public", {}, auto=True, output=out)
            if d.get("approval_id"):
                linked["dispatch"] = [d["approval_id"]]
            else:
                notes.append("배치 후보가 없습니다: " + " · ".join(d["output"].get("options", [])))
        agent_ok("dispatch")
    except ValueError as e:
        notes.append(f"배치 추천을 만들지 못함({e})")
    except Exception as e:
        agent_failed("dispatch", e)
        notes.append("배치 추천 연결 실패: 담당자가 ④ 배치 화면에서 직접 추천하세요")


def detach_dispatch(aid, host_id, rebuild):
    """다른 신고(host_id)의 배치 aid에 '연결만' 해 둔 신고들을 정리한다. 그 배치가 반려되거나 원래 신고가
    오접수 처리되면 연결된 신고에는 배치가 없어지므로, rebuild면 배치 추천을 새로 만들고 아니면 담당자 확인 메모를 남긴다."""
    touched = []
    for item in database.list_incidents(200):
        o = item.get("output", {})
        if o.get("id") == host_id or o.get("state") == INVALID or aid not in (o.get("linked") or {}).get("dispatch", []):
            continue
        linked, notes = {k: v for k, v in o["linked"].items() if k != "dispatch"}, list(o.get("notes") or [])
        if rebuild:
            notes.append(f"연결됐던 배치 {aid}의 신고가 오접수 처리되어 배치 추천을 새로 만듦")
            auto_dispatch(o, linked, notes)
        else:
            notes.append(f"연결됐던 배치 {aid}가 반려됨: 이 신고의 배치를 담당자가 다시 판단하세요")
        o.update(linked=linked, notes=notes)
        database.update_incident(item)
        touched.append(o["id"])
    return touched


def link_incident(row):
    """신고를 다음 에이전트로 넘긴다. 급한 신고 → ④ 배치 추천, 물품 요청 → ⑧ 물품 수급(추천까지 만들어 승인 대기열에).
    필수 정보가 빠졌으면 넘기지 않고 '보완 필요'로 두어 신고자에게 추가 정보를 받는다.
    신고 내용 때문에 못 넘기는 경우(없는 구역 등)는 메모만 남기고, 예상 못 한 오류일 때만 그 에이전트를 수동 처리로 표시한다."""
    out = row["output"]
    missing, linked, notes = list(out.get("missing") or []), {}, []
    if out.get("triage"):
        notes.append(out["triage"])
    if out.get("zone") not in (None, "미상") and out["zone"] not in {z["id"] for z in SCENARIO["zones"]}:
        missing.append("위치(구역): 없는 구역 번호")   # 'Z99'처럼 형식만 맞는 구역은 배치·물품을 보낼 수 없다
    if not missing:
        auto_dispatch(out, linked, notes)
    if out.get("route") == "supply" and not missing:
        try:
            parsed = FEATURES.incident_items(out)   # AI 호출은 잠금 밖에서(담당자 기능이 그동안 멈추지 않게)
            with FEATURES.lock:
                res = FEATURES.supply_from_incident(out, parsed)
            agent_ok("supply")
            if res["requests"]:
                linked["supply"] = [r["id"] for r in res["requests"]]
                waiting = [r for r in res["requests"] if r.get("review_reason")]
                if waiting:
                    notes.append("물품 담당자 검토 필요: " + "; ".join(f"{r['item']}({r['review_reason']})" for r in waiting))
            else:
                missing.append("필요한 물품")
        except (ValueError, features.Conflict) as e:
            notes.append(f"물품 요청을 만들지 못함({e}): 담당자가 ⑧ 물품 화면에서 직접 등록하세요")
        except Exception as e:
            agent_failed("supply", e)
            notes.append("물품 수급 연결 실패: 담당자가 ⑧ 물품 화면에서 직접 등록하세요")
    out.update(missing=missing, linked=linked, notes=notes, state="보완 필요" if missing else "접수")
    return row


def followups(incident_id):
    """접수·보완 직후 별도 스레드에서 실행: 신고자를 기다리게 하지 않고 다음 에이전트로 이어서 넘긴다.
    - 위험 유형 신고 → ③ 안내문(현재 확정 단계, 없으면 긴급도로 주의/경계) → ⑦ 검증 → 통과분만 승인 대기열
    - 대민지원 중 일손 요청 → ⑥ 작업 분해·봉사자 추천 → 투입 승인 대기열
    같은 사건의 반복 신고(다른 신고의 배치에 연결만 된 경우)는 안내문을 다시 만들지 않는다."""
    row = incident_output(incident_id)
    o = (row or {}).get("output", {})
    if not row or o.get("state") != "접수" or o.get("zone") in (None, "미상"):
        return
    repeat = any("이미 진행 중" in n for n in o.get("notes", []))
    add, notes = {}, []
    if o.get("type") in GUIDANCE_TYPES and not repeat:
        try:
            stage = database.get_record("stage", "current") or {}
            level = stage["level"] if stage.get("confirmed") and stage.get("level") in ("주의", "경계", "위험") \
                else ("경계" if (o.get("urgency") or 4) <= 2 else "주의")
            flags = sorted(f for f, ws in GUIDANCE_FLAG_WORDS.items() if any(w in o["text"] for w in ws))
            ctrl = [c["name"] for c in current_control_zones() if c.get("active")]
            g = generate_guidance(incident_id, level, flags, ctrl)
            add["guidance"] = {"level": level, "status": g["output"].get("status"), "verdict": g["output"].get("verdict"),
                               "approval_id": g.get("approval_id"), "review_id": g.get("review_id"),
                               "blocked": bool(g.get("blocked"))}
            agent_ok("welfare")
        except Exception as e:
            agent_failed("welfare", e)
            notes.append("안내문 자동 작성 실패: 담당자가 ③ 안내 화면에서 직접 만드세요")
    if o.get("type") == "대민지원" and any(w in o["text"] for w in LABOR_WORDS):
        try:
            with FEATURES.lock:
                v = FEATURES.volunteer_request({"text": o["text"][:300], "zone": o["zone"], "incident_id": incident_id})
            add["volunteer"] = [t["id"] for t in v["tasks"]]
            if not v["tasks"]:
                notes.append("일손 요청으로 보이지만 작업을 나누지 못함: 담당자가 ⑥ 봉사 화면에서 등록하세요")
            agent_ok("volunteer")
        except ValueError as e:
            notes.append(f"봉사 요청을 만들지 못함({e})")
        except Exception as e:
            agent_failed("volunteer", e)
            notes.append("봉사자 추천 연결 실패: 담당자가 ⑥ 봉사 화면에서 직접 등록하세요")
    if not add and not notes:
        return
    with _incident_lock:   # 그사이 보완·오접수 처리가 있었을 수 있어 최신 상태를 다시 읽고 합친다
        cur = incident_output(incident_id)
        co = cur["output"]
        if co.get("state") == INVALID:
            return
        co["linked"] = {**(co.get("linked") or {}), **add}
        co["notes"] = list(co.get("notes") or []) + notes
        database.update_incident(cur)


def start_followups(incident_id):
    if AUTO_FOLLOWUPS:
        threading.Thread(target=followups, args=(incident_id,), daemon=True).start()


def approve_one(current, s, reason):
    """승인 직전 재검사 → 승인 기록 → 결과 반영. 문제가 있으면 승인하지 않고 사유 문자열을 돌려준다.
    호출하는 쪽에서 _dispatch_lock을 잡는다."""
    problem = recheck_dispatch(current) if current["kind"].startswith("dispatch_") else FEATURES.before_approve(current)
    if problem:
        return problem
    database.resolve_approval(current["id"], True, s["username"], reason, s["user_id"])
    with FEATURES.lock:
        FEATURES.after_resolve(current, True, s)
    return None


BULK_SKIP_KINDS = {"welfare_guidance_review"}   # 검증이 불확실한 안내문은 하나씩 보고 판단한다


PUBLIC_HIDDEN = ("notes", "invalidated_by", "invalid_reason", "invalidated_at", "triage")


def public_row(row):
    """접수번호만 알면 누구나 보는 응답에서 담당자 이름·처리 사유·내부 메모를 뺀다."""
    out = {k: v for k, v in row.get("output", {}).items() if k not in PUBLIC_HIDDEN}
    return {**row, "output": out}


def json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")

def result_dict(result):
    return result.to_dict() if hasattr(result, "to_dict") else result

SCENARIO = scenario.load()
DISPATCH_CFG = dispatch.load_config()


def current_control_zones():
    """통제 구간(제안서 O5): 시나리오 기본값에 담당자가 등록·해제한 내용을 덮어쓴다."""
    saved = {r["id"]: r for r in database.list_records("control")}
    zones = [{**c, **saved.pop(c["id"], {})} for c in SCENARIO["control_zones"]]
    return zones + sorted(saved.values(), key=lambda c: c["id"])


def travel():
    """현재 통제 상태로 이동 시간·경로 차단을 매번 다시 계산한다(구간 변경이 다음 추천에 바로 반영)."""
    return scenario.travel_ctx({**SCENARIO, "control_zones": current_control_zones()})
FEATURE_PREFIXES = ("/api/stage", "/api/welfare/", "/api/verify", "/api/recovery", "/api/volunteer", "/api/supply")
_incident_lock = threading.Lock()   # 신고 보완·오접수 처리가 같은 신고를 동시에 고치지 않게
_dispatch_lock = threading.Lock()   # 배치 승인 직전 재검사와 승인 기록을 한 번에(같은 자원 이중 투입 방지)


def dispatch_state():
    """승인되고 해제되지 않은 배치로 현재 투입 중인 공공 인력과 대피소별 배정 인원을 계산한다."""
    busy, assigned = {}, {}
    for a in database.list_dispatch_approvals():
        top = (a["item"].get("recommendation") or {}).get("top") or {}
        if a["kind"] == "dispatch_public" and top.get("id"):
            busy[top["id"]] = a
        elif a["kind"] == "dispatch_shelter" and top.get("id"):
            assigned[top["id"]] = assigned.get(top["id"], 0) + a["item"]["demand"]["people"]
    return busy, assigned


def recommend_dispatch(incident_id, kind, data, output=None):
    """output: 저장 전(보완 직후 등) 신고 내용을 직접 줄 때. 없으면 DB에서 읽는다."""
    output = output or database.get_incident(incident_id)["result"]["output"]
    busy, assigned = dispatch_state()
    if kind == "public":
        demand = scenario.public_demand(output, SCENARIO)
        candidates = scenario.public_candidates(SCENARIO, set(busy))
        dkind = "public"
    else:
        demand = scenario.shelter_demand(output, SCENARIO, data.get("people"), data.get("amenities", []))
        candidates = scenario.shelter_candidates(SCENARIO, assigned)
        dkind = "shelter"
    # 같은 신고·같은 배치 종류에서 담당자가 거절한 1순위는 제외하고 다시 추천(제안서 5.7 거절 → 재추천)
    rejected = {((a["item"].get("recommendation") or {}).get("top") or {}).get("id")
                for a in database.list_dispatch_approvals(states=("rejected",), include_released=True)
                if a["incident_id"] == incident_id and a["kind"] == "dispatch_" + kind}
    result = dispatch.recommend(dkind, demand, candidates, travel(), DISPATCH_CFG, exclude=rejected - {None})
    return demand, result_dict(result)


def create_dispatch(incident_id, kind, data, auto=False, output=None):
    """배치 추천을 만들고, 후보가 있으면 승인 대기열에 올린다. 없으면 부족 사실과 선택지만 돌려준다."""
    demand, row = recommend_dispatch(incident_id, kind, data, output)
    out = row["output"]
    names = scenario.names(SCENARIO)
    for r in [out.get("top")] + out.get("alternatives", []):
        if r:
            r["name"] = names.get(r["id"], r["id"])
    if out.get("top"):
        row["approval_id"] = database.create_approval(
            "dispatch_" + kind, {"incident_id": incident_id, "kind": kind, "demand": demand,
                                 "recommendation": out, "auto": auto}, incident_id=incident_id)["id"]
    row["incident_id"] = incident_id
    return row


# 긴급도가 이 값 이하인 신고는 접수되면 공공 인력 배치 추천을 자동으로 승인 대기열에 올린다(0이면 끔).
AUTO_DISPATCH_MAX_URGENCY = int(os.getenv("AUTO_DISPATCH_MAX_URGENCY", DISPATCH_CFG.get("auto_public_max_urgency", 4)))
# 문의·생활 지원(대민지원)은 출동 대상이 아니라 자동 배치에서 뺀다(일손 요청은 ⑥ 봉사로 간다).
AUTO_DISPATCH_EXCLUDE = set(DISPATCH_CFG.get("auto_public_exclude", ["대민지원"]))
# 접수 뒤 백그라운드로 이어서 처리(③ 안내문 → ⑦ 검증, 일손 요청 → ⑥ 봉사 추천). 0이면 끔.
AUTO_FOLLOWUPS = os.getenv("AUTO_FOLLOWUPS", "1") != "0"
GUIDANCE_TYPES = {"인명구조", "침수", "산사태·토사", "단전·단수", "도로·시설 장애"}
GUIDANCE_FLAG_WORDS = {"반지하": ["반지하"], "거동불편": ["거동", "휠체어", "누워 계"], "영유아": ["아기", "영유아", "분유", "기저귀"]}
LABOR_WORDS = ("일손", "치울", "치워", "정리", "도와줄", "도와주실", "옮길", "옮겨", "청소", "퍼낼", "세척")


def _epoch(ts):
    """DB의 UTC 시각 문자열(2026-10-28T06:51:22.000Z) → epoch 초. 읽을 수 없으면 0(아주 오래된 것으로 취급)."""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def ongoing_dispatch(output):
    """같은 구역·유형 신고에 이미 대기 중이거나 투입 중인 공공 인력 배치(같은 사건의 반복 신고로 중복 배치하지 않게).
    인명구조는 별건 누락을 막기 위해 묶지 않는다."""
    if output["type"] == "인명구조":
        return None
    pending = [a for a in database.list_pending_approvals() if a["kind"] == "dispatch_public"]
    active = [a for a in database.list_dispatch_approvals() if a["kind"] == "dispatch_public"]
    window = intake.load_cfg().get("follow_up_window_min", 180) * 60
    for a in pending + active:
        if time.time() - _epoch(a.get("created_at")) > window:   # 오래된 배치에 새 사건을 묻지 않게
            continue
        other = incident_output(a["item"].get("incident_id", ""))
        o = (other or {}).get("output", {})
        if (o.get("zone"), o.get("type")) == (output["zone"], output["type"]) and o.get("state") != INVALID:
            return a["id"]
    return None


def affected_assignments():
    """현재 투입 중인 배치 중 통제 구간 때문에 경로가 막히게 된 것(재추천이 필요한 것)."""
    zones = {x["id"]: x["zone"] for x in SCENARIO["public"] + SCENARIO["shelters"]}
    t = travel()["travel"]
    out = []
    for a in database.list_dispatch_approvals():
        top = (a["item"].get("recommendation") or {}).get("top") or {}
        src, dst = zones.get(top.get("id")), a["item"]["demand"]["zone"]
        if src and t.get((src, dst), {}).get("via_blocked"):
            out.append({"approval_id": a["id"], "incident_id": a["incident_id"], "resource": top.get("id")})
    return out


def recheck_dispatch(approval):
    """승인 직전 재검사. 그사이 같은 자원이 다른 배치에 투입됐거나 수용량이 찼으면 사유를 돌려준다."""
    top = (approval["item"].get("recommendation") or {}).get("top") or {}
    busy, assigned = dispatch_state()
    if approval["kind"] == "dispatch_public":
        if top.get("id") in busy:
            return f"{top['id']}는 이미 다른 배치({busy[top['id']]['incident_id']})에 투입되어 있습니다. 다시 추천하세요"
    elif approval["kind"] == "dispatch_shelter":
        shelter = next((s for s in scenario.shelter_candidates(SCENARIO, assigned) if s["id"] == top.get("id")), None)
        if shelter is None or shelter["capacity_left"] < approval["item"]["demand"]["people"]:
            return f"{top.get('id')}의 남은 수용량이 부족합니다. 다시 추천하세요"
    return None


DOC_PARSER = DocumentParser(max_calls=LLM_MAX_CALLS) if MODE == "solar" else None
FEATURES = features.Features(database, SCENARIO, travel, DISPATCH_CFG, GUIDANCE_CFG, LLM, VERIFY_LLM, DOC_PARSER)
SENSORS = sensors.SensorHub(database)


def generate_guidance(incident_id, level, flags, control_zones):
    """③ 안내문 → ⑦ 검증. grounded면 승인 대기열(welfare_guidance), notSure면 담당자 확인, notGrounded면 차단.
    AI를 쓸 수 없으면(검증기 없음 포함) 표준 원문만 만든다(fail closed). 응답에는 승인 전 AI 문구를 넣지 않는다."""
    verifier_fn = guidance_client = None
    if LLM is not None and VERIFY_LLM is not None:
        guidance_client = LLM
        verifier_fn = lambda draft, evidence: verify.verify_text(draft, evidence, client=VERIFY_LLM)[0]
    result = welfare.make_guidance(profile={"flags": flags}, level=level, control_zones=control_zones,
                                   cfg=GUIDANCE_CFG, client=guidance_client, verifier=verifier_fn)
    row = result_dict(result)
    output = row.get("output", {})
    if output.get("status") == "승인 대기" and output.get("verdict") == "grounded" and output.get("displayable") is True:
        row["approval_id"] = database.create_approval(
            "welfare_guidance", {"incident_id": incident_id, "guidance": output, "verifier": VERIFIER_INFO},
            incident_id=incident_id)["id"]
    elif row.get("ai_used") and output.get("verdict") == "notSure":
        # 제안서 6.9: notSure는 담당자 확인 요청. 표준 원문을 보여 주되 초안을 검토 대기열에 올린다.
        row["review_id"] = database.create_approval(
            "welfare_guidance_review", {"incident_id": incident_id, "guidance": output, "verifier": VERIFIER_INFO},
            incident_id=incident_id)["id"]
    elif row.get("ai_used") and output.get("verdict") == "notGrounded":
        # 제안서 6.9: notGrounded는 차단하고 기록만 남긴다(대기열에 넣지 않음).
        database.log_audit("guidance_blocked", "incident", incident_id,
                           {"verdict": "notGrounded", "doc_ids": output.get("doc_ids", [])})
        row["blocked"] = True
    # 사람 승인 전에는 AI 문구를 응답으로 내보내지 않는다(제안서 9.2-4: 승인 후 표시).
    if row.get("ai_used"):
        public = dict(output)
        public.pop("draft", None)
        public["text"] = public.get("standard", "")
        public["displayable"] = True
        public["ai_text_withheld"] = True
        row["output"] = public
    row["incident_id"] = incident_id
    return row


def public_guidance(incident_id):
    """접수 사이트에 내보낼 안내문. 검증 통과(grounded) + 담당자 승인 건만, 표시 직전에 조건을 다시 확인한다."""
    row = database.get_approved_guidance(incident_id)
    g = (row or {}).get("item", {}).get("guidance", {})
    if not row or g.get("verdict") != "grounded" or g.get("displayable") is not True or not g.get("text"):
        return None
    return {"text": g["text"], "verdict": "grounded", "approved": True, "approved_at": row["approved_at"],
            "source": g.get("source", ""), "doc_ids": g.get("doc_ids", []), "evidence": g.get("standard", "")}

def read_json_body(handler, limit=MAX_BODY):
    raw_len = handler.headers.get("Content-Length", "0")
    try:
        length = int(raw_len)
    except ValueError:
        raise ValueError("잘못된 Content-Length")
    if length < 0 or length > limit:
        raise ValueError(f"요청 본문은 {limit}바이트 이하여야 합니다")
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("JSON 본문이 올바르지 않습니다")
    if not isinstance(data, dict):
        raise ValueError("JSON 최상위 값은 객체여야 합니다")
    return data

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "DisasterAgentDemo/0.1"
    timeout = 30   # 요청을 아주 느리게 보내 연결을 붙잡는 클라이언트를 끊는다
    def log_message(self, fmt, *args):
        # Do not log request bodies, reports, or API credentials.
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def session(self):
        """(세션 토큰, 세션 정보 또는 None)"""
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None, None
        token = cookie["sid"].value if "sid" in cookie else None
        return token, database.get_session(token)

    def require_staff(self, check_csrf=False):
        """담당자 세션이면 반환하고, 아니면 401/403을 보낸 뒤 None."""
        _, s = self.session()
        if s is None:
            self.send_json(401, {"error": "로그인이 필요합니다"}); return None
        if s["role"] not in STAFF_ROLES:
            self.send_json(403, {"error": "담당자 권한이 필요합니다"}); return None
        if check_csrf and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), s["csrf"]):
            self.send_json(403, {"error": "보안 토큰(CSRF)이 올바르지 않습니다. 페이지를 새로 고쳐 주세요"}); return None
        return s

    def host_allowed(self):
        """허용한 Host로 들어온 요청만 처리한다. 악성 사이트가 DNS를 바꿔 이 서버로 요청을 보내는 공격(DNS 리바인딩)을 막는다."""
        return self.headers.get("Host", "").strip().lower() in ALLOWED_HOSTS

    def client_ip(self):
        return self.client_address[0]

    def same_origin(self):
        """브라우저가 다른 사이트에서 보낸 POST를 막는다(Origin이 없는 비브라우저 요청은 허용)."""
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        origin = self.headers.get("Origin")
        return origin is None or urlparse(origin).netloc == self.headers.get("Host", "")

    def send_json(self, status, data, headers=None):
        body = json_bytes(data)
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)

    def send_html(self, filename="report.html", content_type="text/html; charset=utf-8"):
        path = ROOT / "webapp" / filename
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if not self.host_allowed():
            return self.send_json(421, {"error": "허용되지 않은 주소로 들어온 요청입니다(WEBAPP_ALLOWED_HOSTS 확인)"})
        if path in {"/", "/report"}:
            return self.send_html("report.html")
        if path == "/admin":
            return self.send_html("admin.html")
        if path == "/login":
            return self.send_html("login.html")
        if path == "/static/admin_ops.js":   # 정해진 파일만 제공(경로 조작 방지)
            return self.send_html("admin_ops.js", "application/javascript; charset=utf-8")
        if path == "/api/me":
            _, s = self.session()
            if s is None:
                return self.send_json(401, {"error": "로그인이 필요합니다"})
            return self.send_json(200, {"username": s["username"], "role": s["role"], "csrf_token": s["csrf"]})
        if path == "/api/health":
            return self.send_json(200, {"ok": True, "mode": MODE, "llm_enabled": LLM is not None,
                "llm_calls": LLM.calls if LLM else 0, "llm_max_calls": LLM.max_calls if LLM else 0, "database": "SQLite", "database_ready": True, "prototype": True,
                "warning": "시뮬레이션/프로토타입입니다. 실제 출동·재난 경보 시스템이 아닙니다."})
        if path == "/api/agents":
            return self.send_json(200, {"items": agents_status()})
        if path == "/api/incidents":   # 전체 목록은 담당자만. 접수번호 단건 조회는 공개
            if not self.require_staff():
                return
            return self.send_json(200, {"items": database.list_incidents(100)})
        if path.startswith("/api/incidents/"):
            incident_id = path.rsplit("/", 1)[-1]
            if not incident_id or len(incident_id) > 40:
                return self.send_json(400, {"error": "접수번호 형식이 올바르지 않습니다"})
            incident = database.get_incident(incident_id)
            if incident is None:
                return self.send_json(404, {"error": "접수번호를 찾을 수 없습니다"})
            invalid = incident["result"].get("output", {}).get("state") == INVALID
            return self.send_json(200, {"id": incident_id, **incident, "result": public_row(incident["result"]),
                                        "guidance": None if invalid else public_guidance(incident_id)})
        if path == "/api/approvals":
            if not self.require_staff():
                return
            return self.send_json(200, {"items": database.list_pending_approvals()})
        if path.startswith(FEATURE_PREFIXES):   # ①③⑤⑥⑦⑧ 현장 운영 기능(webapp/features.py)
            if not self.require_staff():
                return
            res = FEATURES.get(path)
            return self.send_json(*res) if res else self.send_json(404, {"error": "찾을 수 없는 경로입니다"})
        if path == "/api/sensor/status":   # 게이트웨이 수신 현황과 최근 판정
            if not self.require_staff():
                return
            return self.send_json(200, {**SENSORS.status(), "gateway_enabled": bool(SENSOR_TOKEN)})
        if path == "/api/dispatch":   # 현재 투입 현황과 자원·대피소 상태
            if not self.require_staff():
                return
            busy, assigned = dispatch_state()
            return self.send_json(200, {
                "assignments": database.list_dispatch_approvals(),
                "public": scenario.public_candidates(SCENARIO, set(busy)),
                "shelters": scenario.shelter_candidates(SCENARIO, assigned),
                "control_zones": current_control_zones(), "zones": SCENARIO["zones"]})
        return self.send_json(404, {"error": "찾을 수 없는 경로입니다"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.host_allowed():
            return self.send_json(421, {"error": "허용되지 않은 주소로 들어온 요청입니다(WEBAPP_ALLOWED_HOSTS 확인)"})
        if not self.same_origin():
            return self.send_json(403, {"error": "다른 사이트에서 보낸 요청은 받지 않습니다"})
        try:
            if path in UPLOAD_PATHS:   # 큰 본문은 로그인·CSRF를 먼저 확인한 뒤에만 읽는다
                if not self.require_staff(check_csrf=True):
                    return
                data = read_json_body(self, MAX_UPLOAD_BODY)
            else:
                data = read_json_body(self)
            if path == "/api/login":
                username, password = data.get("username"), data.get("password")
                if not isinstance(username, str) or not isinstance(password, str) or len(username) > 50 or len(password) > 200:
                    return self.send_json(400, {"error": "사용자 이름과 비밀번호를 입력해 주세요"})
                key = username.strip().casefold()
                if not LOGIN_IP_LIMIT.allow(self.client_ip()):
                    return self.send_json(429, {"error": "이 기기에서 로그인 시도가 너무 많습니다. 10분 뒤 다시 시도해 주세요"})
                if login_blocked(key):
                    return self.send_json(429, {"error": "로그인 실패가 많아 잠시 막혔습니다. 10분 뒤 다시 시도해 주세요"})
                user = database.authenticate(username, password)
                record_login(key, user is not None)
                if user is None:
                    return self.send_json(401, {"error": "사용자 이름 또는 비밀번호가 올바르지 않습니다"})
                token, csrf = database.create_session(user["id"])
                database.log_audit("login", "user", user["username"], {"role": user["role"]})
                return self.send_json(200, {"username": user["username"], "role": user["role"], "csrf_token": csrf},
                                      headers={"Set-Cookie": session_cookie(token, database.SESSION_TTL_S)})
            if path == "/api/sensor/readings":   # 마이크로비트 게이트웨이(로그인 대신 게이트웨이 토큰)
                if not SENSOR_TOKEN:
                    return self.send_json(503, {"error": "센서 게이트웨이가 꺼져 있습니다. 서버에 SENSOR_GATEWAY_TOKEN을 설정하세요"})
                given = self.headers.get("X-Gateway-Token", "").encode("utf-8")
                if not hmac.compare_digest(given, SENSOR_TOKEN.encode("utf-8")):
                    return self.send_json(401, {"error": "게이트웨이 토큰이 올바르지 않습니다"})
                return self.send_json(200, SENSORS.ingest(data.get("readings"), data.get("rain_stage")))
            if path == "/api/logout":
                token, s = self.session()
                if s is not None:
                    if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), s["csrf"]):
                        return self.send_json(403, {"error": "보안 토큰(CSRF)이 올바르지 않습니다"})
                    database.delete_session(token)
                return self.send_json(200, {"ok": True}, headers={"Set-Cookie": session_cookie("", 0)})
            if path.startswith(FEATURE_PREFIXES):
                s = self.require_staff(check_csrf=True)
                if not s:
                    return
                agent = next(a for pre, a in FEATURE_AGENTS.items() if path.startswith(pre))
                try:
                    res = FEATURES.post(path, {**data, "_by": s["username"]})   # 처리자는 로그인 계정
                except KeyError:
                    return self.send_json(404, {"error": "대상을 찾을 수 없습니다"})
                except features.Conflict as e:
                    return self.send_json(409, {"error": str(e)})
                except ValueError:
                    raise
                except Exception as e:   # 예상 못 한 오류: 이 에이전트만 수동 처리로 돌리고 나머지는 계속 동작
                    agent_failed(agent, e)
                    return self.send_json(500, {"error": "이 기능에서 오류가 나 수동 처리로 전환했습니다. 담당자가 직접 처리하세요",
                                                "manual_mode": agent})
                if res:
                    agent_ok(agent)
                return self.send_json(*res) if res else self.send_json(404, {"error": "찾을 수 없는 경로입니다"})
            if path.startswith("/api/incidents/") and path.endswith("/dispatch"):
                if not self.require_staff(check_csrf=True):
                    return
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    return self.send_json(404, {"error": "찾을 수 없는 경로입니다"})
                incident_id, kind = parts[2], data.get("kind")
                if not incident_id or len(incident_id) > 40:
                    return self.send_json(400, {"error": "접수번호 형식이 올바르지 않습니다"})
                if kind not in scenario.KINDS:
                    return self.send_json(400, {"error": f"kind는 {list(scenario.KINDS)} 중 하나여야 합니다"})
                if database.get_incident(incident_id) is None:
                    return self.send_json(404, {"error": "접수번호를 찾을 수 없습니다"})
                if incident_output(incident_id)["output"].get("state") == INVALID:
                    return self.send_json(409, {"error": "오접수로 처리된 신고입니다"})
                waiting = next((a["id"] for a in database.list_pending_approvals()
                                if a["kind"] == "dispatch_" + kind and a["item"].get("incident_id") == incident_id), None)
                if waiting:   # 같은 신고에 같은 종류의 추천이 이미 대기 중이면 중복으로 올리지 않는다
                    return self.send_json(409, {"error": f"이 신고에는 승인 대기 중인 배치 추천({waiting})이 있습니다. 먼저 승인하거나 거절하세요",
                                                "approval_id": waiting})
                return self.send_json(200, create_dispatch(incident_id, kind, data))   # 입력 오류는 ValueError → 400
            if path == "/api/control-zones" or (path.startswith("/api/control-zones/") and path.endswith("/toggle")):
                s = self.require_staff(check_csrf=True)
                if not s:
                    return
                zone_ids = {z["id"] for z in SCENARIO["zones"]}
                with _dispatch_lock:
                    if path == "/api/control-zones":   # 통제 구간 등록(O5)
                        name, between = data.get("name"), data.get("between")
                        if not isinstance(name, str) or not name.strip() or len(name) > 50:
                            return self.send_json(400, {"error": "구간 이름은 1~50자여야 합니다"})
                        if (not isinstance(between, list) or len(between) != 2 or len(set(between)) != 2
                                or any(z not in zone_ids for z in between)):
                            return self.send_json(400, {"error": "서로 다른 구역 두 개를 골라야 합니다"})
                        if any(frozenset(c["between"]) == frozenset(between) for c in current_control_zones()):
                            return self.send_json(409, {"error": "같은 두 구역 사이의 구간이 이미 있습니다. 목록에서 통제를 켜세요"})
                        rec = {"id": "C-" + uuid.uuid4().hex[:6].upper(), "name": name.strip(), "between": between,
                               "active": True, "changed_by": s["username"]}
                        database.put_record("control", rec["id"], rec)
                        database.log_audit("control_zone_added", "control_zone", rec["id"], rec)
                        return self.send_json(201, {**rec, "affected": affected_assignments()})
                    cid, active = path.split("/")[-2], data.get("active")
                    if not isinstance(active, bool):
                        return self.send_json(400, {"error": "active는 true/false여야 합니다"})
                    zone = next((c for c in current_control_zones() if c["id"] == cid), None)
                    if zone is None:
                        return self.send_json(404, {"error": "통제 구간을 찾을 수 없습니다"})
                    rec = {**zone, "active": active, "changed_by": s["username"]}
                    database.put_record("control", cid, rec)
                    database.log_audit("control_zone_changed", "control_zone", cid, {"active": active})
                    return self.send_json(200, {**rec, "affected": affected_assignments()})
            if path.startswith("/api/dispatch/") and (path.endswith("/modify") or path.endswith("/direct")):
                s = self.require_staff(check_csrf=True)
                if not s:
                    return
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    return self.send_json(404, {"error": "찾을 수 없는 경로입니다"})
                aid, action, cand_id, reason = parts[2], parts[3], data.get("candidate"), data.get("reason")
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
                    return self.send_json(400, {"error": "승인 사유를 입력해야 합니다(500자 이하)"})
                with _dispatch_lock:
                    ap = database.get_approval(aid)
                    if ap is None or not ap["kind"].startswith("dispatch_"):
                        return self.send_json(404, {"error": "배치 승인 항목을 찾을 수 없습니다"})
                    if ap["state"] != "pending":
                        return self.send_json(409, {"error": f"{aid}는 이미 처리됐습니다"})
                    item, kind = ap["item"], ap["item"]["kind"]
                    rec = item["recommendation"]
                    busy, assigned = dispatch_state()
                    cands = (scenario.public_candidates(SCENARIO, set(busy)) if kind == "public"
                             else scenario.shelter_candidates(SCENARIO, assigned))
                    cand = next((c for c in cands if c["id"] == cand_id), None)
                    if cand is None:
                        return self.send_json(400, {"error": "후보 목록에 없는 자원입니다"})
                    warnings = dispatch.violations(kind, item["demand"], cand, travel(), DISPATCH_CFG)
                    pool = [rec["top"]] + rec.get("alternatives", [])
                    chosen = next((r for r in pool if r["id"] == cand_id), None)
                    if action == "modify":   # 추천 목록(1순위·대안) 안에서 고른다. 제약 위반이면 불가
                        if chosen is None:
                            return self.send_json(400, {"error": "수정은 1순위·대안 후보 중에서만 고를 수 있습니다. 그 밖은 직접 지정을 쓰세요"})
                        if warnings:
                            return self.send_json(409, {"error": "제약 위반으로 수정할 수 없습니다: " + ", ".join(warnings), "warnings": warnings})
                        extra = {"decision": "modify", "original_top": rec["top"]["id"],
                                 "score_diff": round(rec["top"]["score"] - chosen["score"], 4)}
                    else:   # 직접 지정: 제약 위반은 예외 승인(override)과 사유가 있어야 확정
                        if warnings and data.get("override") is not True:
                            return self.send_json(409, {"error": "제약 위반으로 직접 지정할 수 없습니다: " + ", ".join(warnings)
                                                        + ". 예외 승인하려면 사유와 함께 예외 승인을 선택하세요", "warnings": warnings})
                        chosen = chosen or {"id": cand_id, "score": None, "items": {}}
                        extra = {"decision": "direct", "original_top": rec["top"]["id"], "warnings": warnings,
                                 "override": bool(warnings)}
                    chosen = {**chosen, "name": scenario.names(SCENARIO).get(cand_id, cand_id)}
                    database.update_pending_approval_item(aid, {**item, "recommendation": {**rec, "top": chosen}, **extra})
                    result = database.resolve_approval(aid, True, s["username"], reason.strip(), s["user_id"])
                    database.log_audit("dispatch_" + action, "approval", aid, {**extra, "chosen": cand_id})
                return self.send_json(200, {**result, **extra})
            if path.startswith("/api/dispatch/") and path.endswith("/release"):
                s = self.require_staff(check_csrf=True)
                if not s:
                    return
                try:
                    database.release_dispatch(path.split("/")[-2], s["username"], s["user_id"])
                except KeyError:
                    return self.send_json(404, {"error": "배치 항목을 찾을 수 없습니다"})
                except ValueError as e:
                    return self.send_json(409, {"error": str(e)})
                return self.send_json(200, {"ok": True})
            if path.startswith("/api/incidents/") and path.endswith("/guidance"):
                if not self.require_staff(check_csrf=True):
                    return
                parts = path.strip("/").split("/")
                if len(parts) != 4 or parts[0] != "api" or parts[1] != "incidents":
                    return self.send_json(404, {"error": "찾을 수 없는 경로입니다"})

                incident_id = parts[2]
                if not incident_id or len(incident_id) > 40:
                    return self.send_json(400, {"error": "접수번호 형식이 올바르지 않습니다"})
                if database.get_incident(incident_id) is None:
                    return self.send_json(404, {"error": "접수번호를 찾을 수 없습니다"})
                if incident_output(incident_id)["output"].get("state") == INVALID:
                    return self.send_json(409, {"error": "오접수로 처리된 신고입니다"})

                level = data.get("level")
                flags = data.get("flags", [])
                control_zones = data.get("control_zones", [])

                allowed_levels = {
                    name
                    for fragment in GUIDANCE_CFG.get("fragments", [])
                    for name in fragment.get("levels", [])
                }
                if not isinstance(level, str) or level not in allowed_levels:
                    return self.send_json(400, {
                        "error": f"level은 다음 중 하나여야 합니다: {sorted(allowed_levels)}"
                    })
                if (
                    not isinstance(flags, list)
                    or len(flags) > 20
                    or any(not isinstance(item, str) or len(item) > 50 for item in flags)
                ):
                    return self.send_json(400, {
                        "error": "flags는 최대 20개의 문자열 배열이어야 합니다"
                    })
                if (
                    not isinstance(control_zones, list)
                    or len(control_zones) > 20
                    or any(
                        not isinstance(item, str) or not item.strip() or len(item) > 100
                        for item in control_zones
                    )
                ):
                    return self.send_json(400, {
                        "error": "control_zones는 최대 20개의 문자열 배열이어야 합니다"
                    })

                row = generate_guidance(incident_id, level, flags, control_zones)
                row["incident_id"] = incident_id
                return self.send_json(200, row)

            if path == "/api/incidents":
                text = data.get("text")
                if not isinstance(text, str) or not text.strip():
                    return self.send_json(400, {"error": "신고 내용을 입력해 주세요"})
                if len(text) > 2000:
                    return self.send_json(400, {"error": "신고 내용은 2000자 이하여야 합니다"})
                if not INCIDENT_LIMIT.allow(self.client_ip()):
                    return self.send_json(429, {"error": "짧은 시간에 신고가 너무 많습니다. 급한 상황이면 119·112에 바로 전화하세요"})
                duplicate = database.find_duplicate_incident(text)
                if duplicate:
                    return self.send_json(200, {**public_row(duplicate), "duplicate": True})
                rid = "R-" + uuid.uuid4().hex[:8].upper()
                row = classify_incident(rid, text, time.time())
                # Persist only the classifier result (the intake agent applies its configured masking).
                row = database.save_incident(row)
                row = database.update_incident(link_incident(row))
                start_followups(row["output"]["id"])
                return self.send_json(201, public_row(row))
            if path.startswith("/api/incidents/") and path.endswith("/supplement"):   # 신고자가 빠진 정보 보완(접수번호로)
                parts = path.strip("/").split("/")
                if len(parts) != 4 or not parts[2] or len(parts[2]) > 40:
                    return self.send_json(404, {"error": "찾을 수 없는 경로입니다"})
                extra = data.get("text")
                if not isinstance(extra, str) or not extra.strip() or len(extra) > 500:
                    return self.send_json(400, {"error": "보완 내용은 1~500자로 입력해 주세요"})
                if not INCIDENT_LIMIT.allow(self.client_ip()):
                    return self.send_json(429, {"error": "짧은 시간에 요청이 너무 많습니다. 급한 상황이면 119·112에 바로 전화하세요"})
                with _incident_lock:
                    old = incident_output(parts[2])
                    if old is None:
                        return self.send_json(404, {"error": "접수번호를 찾을 수 없습니다"})
                    o = old["output"]
                    if o.get("state") != "보완 필요":
                        return self.send_json(409, {"error": "보완이 필요한 신고가 아닙니다"})
                    if o.get("supplements", 0) >= MAX_SUPPLEMENTS:
                        return self.send_json(409, {"error": "보완은 3번까지 받습니다. 담당자가 확인할 예정이며, 급한 상황이면 119·112에 바로 전화하세요"})
                    # 원문 뒤에 붙이고 자르지 않는다(긴 원문이어도 보완 내용이 빠지지 않게, 최대 3번이라 길이는 제한됨)
                    text = f"{o['text']}\n[보완] {extra.strip()}"
                    row = classify_incident(o["id"], text, o["ts"], max_text=len(text))
                    out = row["output"]
                    zone_given = intake._zones(extra)
                    if len(zone_given) == 1:   # 보완에 적은 구역이 원문의 모호한 구역('2구역인지 3구역인지')보다 우선
                        out["zone"] = zone_given.pop()
                        out["missing"] = intake.missing_fields(out)
                    out["supplements"] = o.get("supplements", 0) + 1
                    row = link_incident(row)
                    database.log_audit("incident_supplemented", "incident", o["id"],
                                       {"still_missing": row["output"]["missing"]})
                    row = database.update_incident(row)
                    start_followups(o["id"])
                    return self.send_json(200, public_row(row))
            if path.startswith("/api/incidents/") and path.endswith("/approve-all"):   # 담당자: 이 신고의 대응안 한 번에 승인
                s = self.require_staff(check_csrf=True)
                if not s:
                    return
                parts = path.strip("/").split("/")
                if len(parts) != 4 or not parts[2] or len(parts[2]) > 40:
                    return self.send_json(404, {"error": "찾을 수 없는 경로입니다"})
                reason = data.get("reason")
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
                    return self.send_json(400, {"error": "승인 사유를 500자 이하로 입력해 주세요"})
                row = incident_output(parts[2])
                if row is None:
                    return self.send_json(404, {"error": "접수번호를 찾을 수 없습니다"})
                if row["output"].get("state") == INVALID:
                    return self.send_json(409, {"error": "오접수로 처리된 신고입니다"})
                approved, skipped = [], []
                with _dispatch_lock:   # 항목마다 승인 직전 재검사(중복 투입·재고 부족이면 그 항목만 건너뜀)
                    for a in database.list_pending_approvals_for(parts[2]):
                        if a["kind"] in BULK_SKIP_KINDS:
                            skipped.append({"id": a["id"], "kind": a["kind"], "reason": "검증이 불확실한 안내문은 따로 확인하세요"})
                            continue
                        problem = approve_one(a, s, reason.strip())
                        if problem:
                            skipped.append({"id": a["id"], "kind": a["kind"], "reason": problem})
                        else:
                            approved.append({"id": a["id"], "kind": a["kind"]})
                database.log_audit("incident_bulk_approved", "incident", parts[2],
                                   {"approved": [x["id"] for x in approved], "skipped": [x["id"] for x in skipped],
                                    "by": s["username"]})
                return self.send_json(200, {"incident_id": parts[2], "approved": approved, "skipped": skipped})
            if path.startswith("/api/incidents/") and path.endswith("/invalidate"):   # 담당자: 허위·오접수 처리
                s = self.require_staff(check_csrf=True)
                if not s:
                    return
                parts = path.strip("/").split("/")
                if len(parts) != 4 or not parts[2] or len(parts[2]) > 40:
                    return self.send_json(404, {"error": "찾을 수 없는 경로입니다"})
                reason = data.get("reason")
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 300:
                    return self.send_json(400, {"error": "오접수 사유를 300자 이하로 입력해 주세요"})
                reason = reason.strip()
                with _incident_lock, _dispatch_lock:
                    row = incident_output(parts[2])
                    if row is None:
                        return self.send_json(404, {"error": "접수번호를 찾을 수 없습니다"})
                    o = row["output"]
                    if o.get("state") == INVALID:
                        return self.send_json(409, {"error": "이미 오접수로 처리된 신고입니다"})
                    rejected = []
                    for a in database.list_pending_approvals_for(o["id"]):   # 배치·물품·안내문 대기 건을 함께 반려
                        database.resolve_approval(a["id"], False, s["username"], f"오접수 처리: {reason}", s["user_id"])
                        with FEATURES.lock:
                            FEATURES.after_resolve(a, False, s)
                        rejected.append(a["id"])
                    released = []
                    for a in database.list_dispatch_approvals():   # 이미 투입된 배치는 해제해 다른 신고에 쓸 수 있게
                        if a["incident_id"] == o["id"]:
                            database.release_dispatch(a["id"], s["username"], s["user_id"])
                            released.append(a["id"])
                    with FEATURES.lock:
                        cancelled, in_transit = FEATURES.cancel_incident_supply(o["id"], s["username"], reason)
                    relinked = [i for aid in rejected + released for i in detach_dispatch(aid, o["id"], rebuild=True)]
                    o.update(state=INVALID, invalid_reason=reason, invalidated_by=s["username"], invalidated_at=time.time())
                    database.update_incident(row, status="rejected")
                    summary = {"rejected_approvals": rejected, "released_dispatches": released,
                               "cancelled_supply": cancelled, "supply_in_transit": in_transit,
                               "relinked_incidents": relinked}
                    database.log_audit("incident_invalidated", "incident", o["id"], {"reason": reason, **summary})
                    return self.send_json(200, {**row, **summary})
            if path == "/api/monitor/evaluate":
                if not self.require_staff(check_csrf=True):
                    return
                readings = data.get("readings", [])
                if not isinstance(readings, list) or len(readings) > 500:
                    return self.send_json(400, {"error": "readings는 최대 500개 항목의 배열이어야 합니다"})
                now = data.get("now", time.time())
                stage = data.get("rain_stage", 0)
                if isinstance(now, bool) or not isinstance(now, (int, float)):
                    return self.send_json(400, {"error": "now는 숫자여야 합니다"})
                if isinstance(stage, bool) or not isinstance(stage, int) or not 0 <= stage <= 4:
                    return self.send_json(400, {"error": "rain_stage는 0~4 정수여야 합니다"})
                result = monitor.Monitor(monitor.load_config()).evaluate(readings, float(now), stage)
                row = result_dict(result)
                if row.get("needs_approval"):
                    row["approval_id"] = database.create_approval("monitor_stage", row["output"])["id"]
                return self.send_json(200, row)
            if path.startswith("/api/approvals/") and path.endswith("/resolve"):
                s = self.require_staff(check_csrf=True)
                if not s:
                    return
                aid = path.split("/")[-2]
                approved, reason = data.get("approved"), data.get("reason")
                if not isinstance(approved, bool):
                    return self.send_json(400, {"error": "approved는 true/false여야 합니다"})
                if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
                    return self.send_json(400, {"error": "사유는 500자 이하 문자열이어야 합니다"})
                if approved and (not isinstance(reason, str) or not reason.strip()):
                    return self.send_json(400, {"error": "승인 시 사유를 입력해야 합니다"})
                try:   # 처리자는 요청 본문이 아니라 로그인한 계정으로 기록한다
                    with _dispatch_lock:
                        current = database.get_approval(aid)
                        if approved and current and current["state"] == "pending":
                            problem = (recheck_dispatch(current) if current["kind"].startswith("dispatch_")
                                       else FEATURES.before_approve(current))
                            if problem:
                                return self.send_json(409, {"error": problem})
                        result = database.resolve_approval(aid, approved, s["username"], (reason or "").strip(),
                                                           s["user_id"])
                        with FEATURES.lock:   # 승인 결과 반영(단계 확정, 봉사 투입, 물품 출고·예약 해제)
                            FEATURES.after_resolve(current, approved, s)
                        if not approved and current and current["kind"] == "dispatch_public":
                            detach_dispatch(aid, current["incident_id"], rebuild=False)   # 연결만 해 둔 반복 신고에 알림
                except KeyError:
                    return self.send_json(404, {"error": "승인 요청을 찾을 수 없습니다"})
                except ValueError as e:
                    return self.send_json(409, {"error": str(e)})
                return self.send_json(200, result)
            return self.send_json(404, {"error": "찾을 수 없는 경로입니다"})
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        except Exception:
            # Avoid returning stack traces or secrets to the browser; keep them in the local console only.
            traceback.print_exc()
            return self.send_json(500, {"error": "서버 내부 오류입니다. 콘솔 로그를 확인해 주세요"})

def check_bind_policy(host=HOST, env=os.environ, staff_count=None):
    """내부망 공개 조건을 검사한다. 문제가 있으면 이유 문자열, 괜찮으면 None (docs/내부망_배포_점검표.md).
    - 기본은 이 PC(localhost)만. 내부망은 WEBAPP_ALLOW_LAN=1로 명시해야 열린다.
    - 내부망에서는 HTTPS 인증서가 있어야 한다. 시연처럼 잠깐 HTTP로 열려면 WEBAPP_ALLOW_HTTP_LAN=1.
    - 담당자 계정이 있고, 접속 주소가 WEBAPP_ALLOWED_HOSTS에 있어야 한다."""
    if host in LOCAL_HOSTS:
        return None
    if env.get("WEBAPP_ALLOW_LAN") != "1":
        return ("안전상 기본은 localhost만 허용합니다. 학교 내부망으로 열려면 docs/내부망_배포_점검표.md를 확인하고 "
                "WEBAPP_ALLOW_LAN=1을 설정하세요.")
    if host in {"0.0.0.0", "::", ""}:
        return "모든 네트워크(0.0.0.0)에 열지 말고 이 PC의 내부망 IP(예: 192.168.0.10)를 WEBAPP_HOST로 지정하세요."
    has_tls = bool(env.get("WEBAPP_TLS_CERT", "").strip() and env.get("WEBAPP_TLS_KEY", "").strip())
    if not has_tls and env.get("WEBAPP_ALLOW_HTTP_LAN") != "1":
        return ("내부망에서는 로그인 정보가 암호화되도록 HTTPS가 필요합니다. WEBAPP_TLS_CERT·WEBAPP_TLS_KEY를 지정하세요. "
                "(시연용으로 잠깐 HTTP를 쓰려면 WEBAPP_ALLOW_HTTP_LAN=1)")
    if not any(h.strip() for h in env.get("WEBAPP_ALLOWED_HOSTS", "").split(",")):
        return f"WEBAPP_ALLOWED_HOSTS에 접속 주소를 적으세요(예: {host}:{PORT})."
    if (database.count_users() if staff_count is None else staff_count) == 0:
        return "담당자 계정이 없습니다. 먼저 python scripts/create_user.py 이름 --role operator 로 만드세요."
    return None


def main():
    problem = check_bind_policy()
    if problem:
        raise SystemExit(problem)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    scheme = "http"
    if TLS_CERT and TLS_KEY:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(TLS_CERT, TLS_KEY)
        # 핸드셰이크를 accept()가 아닌 요청 처리 스레드에서 하게 한다(연결만 하고 가만히 있는 클라이언트가 전체를 막지 않게).
        server.socket = ctx.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        scheme = "https"
    elif HOST not in LOCAL_HOSTS:
        print("경고: 내부망에 HTTP(암호화 없음)로 열었습니다. 같은 네트워크에서 로그인 정보가 노출될 수 있으니 시연에만 쓰세요.")
    print(f"재난 안전 AI 에이전트 프로토타입: {scheme}://{HOST}:{PORT}")
    print(f"LLM_MODE={MODE}; {'Solar API 사용 설정됨' if LLM else 'LLM은 사용하지 않고 규칙 기반 데모로 실행'}")
    if database.count_users() == 0:
        print("담당자 계정이 없습니다. 관리자 화면을 쓰려면 먼저 실행하세요: python scripts/create_user.py 이름 --role operator")
    print("Ctrl+C로 종료하세요. 실재난 경보/출동 용도로 사용하지 마세요.")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()
