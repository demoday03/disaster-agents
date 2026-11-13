"""Local-only demo web server for the disaster safety agents.
Run from project root: python webapp/server.py
This is a prototype, not a production or emergency-dispatch system.
"""
from __future__ import annotations
import hashlib, hmac, json, os, secrets, ssl, sys, threading, time, traceback, uuid
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
from common.llm import SolarClient
from common.docparse import DocumentParser
from common.masking import mask
from common import scenario
from agents import crosscheck, dispatch, intake, monitor, recovery, welfare, verify
from coordinator import Coordinator
from database import db as database
from webapp import battle, features, public_api, sensors, telegram_bot, weather

# 클라우드 모드(Render 등): 플랫폼이 HTTPS를 앞에서 처리하고 PORT를 정해 준다. 이때는 사이트 접속 코드가 필수다.
CLOUD = os.getenv("WEBAPP_CLOUD") == "1" or os.getenv("RENDER") == "true"
HOST = os.getenv("WEBAPP_HOST", "0.0.0.0" if CLOUD else "127.0.0.1")
PORT = int(os.getenv("WEBAPP_PORT") or (os.getenv("PORT") if CLOUD else None) or "8765")
# 링크와 이 코드를 함께 받은 사람만 사이트를 볼 수 있다(비어 있으면 끔. 로컬 실행에서는 보통 끔)
# SITE_PUBLIC=1이면 접속 코드 없이 누구나 연다(담당자 화면은 여전히 로그인 필요). 공개 시연용으로 명시할 때만
SITE_PUBLIC = os.getenv("SITE_PUBLIC") == "1"
SITE_CODE = "" if SITE_PUBLIC else os.getenv("SITE_ACCESS_CODE", "").strip()
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
COOKIE_SECURE = os.getenv("WEBAPP_COOKIE_SECURE") == "1" or bool(TLS_CERT) or CLOUD
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
# 요청의 Host 헤더 허용 목록(DNS 리바인딩 방지). 내부망으로 열 때는 서버 주소를 WEBAPP_ALLOWED_HOSTS에 쉼표로 적는다.
ALLOWED_HOSTS = ({f"{h}:{PORT}" for h in ("127.0.0.1", "localhost", "[::1]")}
                 | ({"127.0.0.1", "localhost", "[::1]"} if PORT in (80, 443) else set())   # 기본 포트면 브라우저가 포트를 빼고 보낸다
                 | {h.strip().lower() for h in os.getenv("WEBAPP_ALLOWED_HOSTS", "").split(",") if h.strip()}
                 | ({os.getenv("RENDER_EXTERNAL_HOSTNAME", "").lower()} - {""}))   # Render가 알려 주는 배포 주소
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
PUBLIC_LIMIT = RateLimiter(int(os.getenv("WEBAPP_PUBLIC_PER_MIN", "60")), 60)   # 시민·봉사자 화면의 기록 요청
LOGIN_IP_LIMIT = RateLimiter(30, 600)   # 접속 IP당 10분에 로그인 시도 30번(계정별 5번 잠금과 별도)
HH_LOGIN_LIMIT = RateLimiter(30, 600)   # 주민 가구 코드 로그인: 접속 IP당 10분에 30번(코드 맞히기 방지)


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


SITE_TOKEN = hmac.new(SITE_CODE.encode(), b"site-access-v1", "sha256").hexdigest() if SITE_CODE else ""
GATE_FREE = {"/access", "/api/access", "/api/health", "/api/sensor/readings", "/api/sensor/downlink",
             "/api/telegram/webhook"}   # 게이트웨이·텔레그램은 자기 토큰으로 인증


def site_cookie(max_age=30 * 24 * 3600):
    return (f"site={SITE_TOKEN}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}" + ("; Secure" if COOKIE_SECURE else ""))


def session_cookie(token, max_age):
    return (f"sid={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
            + ("; Secure" if COOKIE_SECURE else ""))

AGENTS = [
    {"id": "monitor", "name": "감시", "mode": "규칙 (AI 없음)", "status": "demo-ready"},
    {"id": "intake", "name": "신고 접수", "mode": "규칙 + AI 보조", "status": "demo-ready"},
    {"id": "welfare", "name": "안부·안내", "mode": "규칙 + AI 보조", "status": "demo-ready"},
    {"id": "dispatch", "name": "배치 추천", "mode": "규칙 (AI 없음)", "status": "demo-ready"},
    {"id": "recovery", "name": "복구·지원", "mode": "규칙 + AI 보조", "status": "demo-ready"},
    {"id": "volunteer", "name": "봉사 코디", "mode": "규칙 + AI 보조", "status": "demo-ready"},
    {"id": "verify", "name": "내용 검증", "mode": "규칙 + AI 보조", "status": "demo-ready"},
    {"id": "supply", "name": "물품 수급", "mode": "규칙 + AI 보조", "status": "demo-ready"},
    {"id": "crosscheck", "name": "신고 대조", "mode": "규칙 (AI 없음)", "status": "demo-ready"},
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
        missing.append("위치: 없는 구역 번호")   # 'Z99'처럼 형식만 맞는 구역은 배치·물품을 보낼 수 없다
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
            g = generate_guidance(incident_id, level, flags, ctrl, GUIDANCE_CFG.get("hazard_by_type", {}).get(o.get("type")))
            add["guidance"] = {"level": level, "status": g["output"].get("status"), "verdict": g["output"].get("verdict"),
                               "approval_id": g.get("approval_id"), "review_id": g.get("review_id"),
                               "blocked": bool(g.get("blocked"))}
            agent_ok("welfare")
        except Exception as e:
            agent_failed("welfare", e)
            notes.append("안내문 자동 작성 실패: 담당자가 ③ 안내 화면에서 직접 만드세요")
    done = o.get("linked") or {}   # 보완 뒤 다시 불려도 같은 항목을 두 번 만들지 않는다
    if o.get("type") in STAGE_HAZARD and "monitor" not in done:
        try:
            add["monitor"] = sensor_crosscheck(o)
            agent_ok("monitor")
        except Exception as e:
            agent_failed("monitor", e)
            notes.append("센서 교차 확인 실패: 담당자가 ① 센서 화면에서 확인하세요")
    if o.get("type") in CROSSCHECK_CFG["rules"] and "crosscheck" not in done:
        try:
            add["crosscheck"] = report_crosscheck(o)
            agent_ok("crosscheck")
        except Exception as e:
            agent_failed("crosscheck", e)
            notes.append("신고 대조 실패: 담당자가 ① 센서 화면의 값과 직접 비교하세요")
    if (o.get("type") in GUIDANCE_TYPES and (o.get("urgency") or 4) <= WELFARE_CHECK_MAX_URGENCY
            and not repeat and "welfare_check" not in done):
        try:
            wid, why = queue_welfare_check(o)
            if wid:
                add["welfare_check"] = wid
            if why:
                notes.append("안부 확인: " + why)
        except Exception as e:
            agent_failed("welfare", e)
            notes.append("안부 확인 연결 실패: 담당자가 ③ 안부 화면에서 시작하세요")
    if (o.get("type") in SHELTER_TYPES and isinstance(o.get("people"), int) and o["people"] > 0
            and not repeat and "shelter" not in done):
        try:
            d = create_dispatch(incident_id, "shelter", {"people": o["people"]}, auto=True, output=o)
            if d.get("approval_id"):
                add["shelter"] = [d["approval_id"]]
            else:
                notes.append("대피소 후보가 없습니다: " + " · ".join(d["output"].get("options", [])))
            agent_ok("dispatch")
        except ValueError as e:
            notes.append(f"대피소 배정 추천을 만들지 못함({e})")
        except Exception as e:
            agent_failed("dispatch", e)
            notes.append("대피소 배정 연결 실패: 담당자가 ④ 배치 화면에서 직접 추천하세요")
    if o.get("type") in RECOVERY_DAMAGE and "recovery" not in done:
        try:
            add["recovery"] = recovery_candidates(o)
            agent_ok("recovery")
        except Exception as e:
            agent_failed("recovery", e)
            notes.append("지원제도 후보 연결 실패: 담당자가 ⑤ 복구·지원 화면에서 찾으세요")
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


def sensor_crosscheck(o):
    """① 감시 교차 확인. 신고 하나로는 위험 단계를 올리지 않는다(오탐 방지, 제안서 6장 감시 규칙).
    최근 센서 판정이 같은 위험을 가리키고 단계 확정 요청이 대기 중이면, 그 요청을 이 신고의 승인 묶음에 넣는다."""
    hazard = STAGE_HAZARD[o["type"]]
    ev = SENSORS.status().get("last_eval") or {}
    out = (ev.get("output") or {}) if time.time() - (ev.get("at") or 0) <= SENSOR_FRESH_SEC else {}
    res = {"hazard": hazard, "sensor_status": out.get("status", "최근 수신 없음"), "level": out.get("level"),
           "agrees": hazard in (out.get("hazards") or []), "approval_id": None}
    # 게이트웨이든 센서 시뮬레이터든, 같은 위험의 단계 확정 요청이 대기 중이면 센서가 이미 확정 후보로 본 것이다
    stage = next((a for a in database.list_pending_approvals()
                  if a["kind"] == "monitor_stage" and hazard in a["item"].get("hazards", [])), None)
    if stage:
        res.update(agrees=True, approval_id=stage["id"], level=stage["item"].get("level"),
                   sensor_status=stage["item"].get("status", "확정후보"))
    return res


def report_crosscheck(o):
    """신고 대조: 신고 심각도와 같은 구역 센서의 최근 최댓값을 비교한다(참고용, 긴급도·배치는 그대로)."""
    cfg = CROSSCHECK_CFG
    span = cfg["similar_window_min"] * 60
    similar = 0   # 같은 구역·유형의 최근 다른 신고(여러 사람이 같은 걸 말하면 센서 위치 밖일 가능성도 본다)
    for item in database.list_incidents(200):
        x = item.get("output", {})
        if (x.get("id") != o["id"] and x.get("state") != INVALID and x.get("type") == o["type"]
                and x.get("zone") == o["zone"] and abs((x.get("ts") or 0) - (o.get("ts") or 0)) <= span):
            similar += 1
    return crosscheck.assess(o, SENSORS.window(cfg["window_sec"]), SENSORS.thresholds(), cfg, similar=similar)


def queue_welfare_check(o):
    """③ 안부 확인 시작을 승인 대기열에(담당자가 승인해야 가구에 확인 요청이 나간다). 그 구역에 이미 진행 중이거나
    대기 중인 안부 확인이 있으면 새로 만들지 않는다. 돌려주는 값: (승인 ID 또는 None, 메모 또는 None)."""
    zone = o["zone"]
    with _welfare_lock:   # 같은 구역 신고가 동시에 들어와도 안부 확인 요청은 하나만
        for a in database.list_pending_approvals():
            if a["kind"] == "welfare_check" and a["item"].get("zone") == zone:
                return None, f"{zone} 안부 확인 시작이 이미 승인 대기 중({a['id']})"
        if any(r["zone"] == zone and r["state"] in ("대기", "격상") for r in database.list_records("check")):
            return None, f"{zone} 안부 확인이 이미 진행 중"
        hh = [h for h in FEATURES.households.values() if h["zone"] == zone and h["consent"]]
        if not hh:
            return None, f"{zone}에 안부 확인에 동의한 가구가 없음"
        item = {"incident_id": o["id"], "zone": zone, "households": [h["id"] for h in hh],
                "vulnerable": [h["id"] for h in hh if h["factors"]], "timeout_s": FEATURES.gcfg["timeout_s"]}
        return database.create_approval("welfare_check", item, incident_id=o["id"])["id"], None


def recovery_candidates(o):
    """⑤ 복구·지원: 신고 유형으로 해당 가능성이 있는 지원 항목(규칙만, AI 호출 없음). 참고용이라 승인 대상이 아니다."""
    hh = {"id": o["id"], "area": "거제시", "damage_types": RECOVERY_DAMAGE[o["type"]], "housing": "단독주택",
          "long_term_evacuation": False, "lease_signed": False}
    m = recovery.match_support(hh, FEATURES.rcfg).output
    return {"candidates": [{k: c[k] for k in ("id", "title", "status")} for c in m["candidates"]],
            "basis": f"{', '.join(hh['damage_types'])} · 거제시 · 주거 형태 미확인(단독주택 기준)", "notice": m["notice"]}


def link_pending_incidents():
    """서버를 거치지 않고 저장된 신고(예: scripts/demo_setup.py가 미리 넣은 시연 신고)는 다음 에이전트로
    넘겨진 적이 없다. 서버가 켜질 때 그런 신고(state 없음)를 찾아 연결과 후속 처리를 한 번 돌린다."""
    done = []
    for item in reversed(database.list_incidents(200)):   # 먼저 들어온 신고부터(반복 신고 묶음이 자연스럽게)
        if item.get("output", {}).get("state") is not None:
            continue
        try:
            with _incident_lock:
                row = database.update_incident(link_incident(item))
            followups(row["output"]["id"])   # 이미 백그라운드 스레드 안이라 바로 실행
            done.append(row["output"]["id"])
        except Exception:
            traceback.print_exc()
    if done:
        print(f"서버를 거치지 않고 저장된 신고 {len(done)}건을 에이전트에 연결했습니다: {', '.join(done)}")
    return done


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


PUBLIC_HIDDEN = ("notes", "invalidated_by", "invalid_reason", "invalidated_at", "triage", "owner_hash", "household")   # household = 가구 코드(로그인용)


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


def scenario_now():
    """대피소 운영(제안서 O6): 담당자가 기록한 입소·퇴소를 시나리오의 현재 인원에 더한다(0~수용량 사이)."""
    delta = {r["id"]: r["delta"] for r in database.list_records("shelter_occ")}
    return {**SCENARIO, "shelters": [{**s, "occupied": min(s["capacity"], max(0, s["occupied"] + delta.get(s["id"], 0)))}
                                     for s in SCENARIO["shelters"]]}


def daily_report():
    """일일 상황 보고서 초안(제안서 O13): 규칙으로 집계하고 문장도 규칙으로 만든다(AI 미사용, 숫자 왜곡 없음).
    사후 평가로 신고 접수부터 첫 승인까지 걸린 시간을 함께 낸다."""
    incs = [x["output"] for x in database.list_incidents(1000)]
    created = {o["id"]: o["ts"] if isinstance(o.get("ts"), (int, float)) else _epoch(o.get("ts")) for o in incs}
    live = [o for o in incs if o.get("state") != INVALID]
    by_type, by_urg = {}, {}
    for o in live:
        by_type[o.get("type", "미분류")] = by_type.get(o.get("type", "미분류"), 0) + 1
        by_urg[str(o.get("urgency") or "미정")] = by_urg.get(str(o.get("urgency") or "미정"), 0) + 1
    aps = database.list_approval_stats()
    count = lambda st: sum(a["state"] == st for a in aps)
    first = {}
    for a in aps:
        if a["state"] == "approved" and a["incident_id"] in created and a["resolved_at"]:
            t = _epoch(a["resolved_at"]) - created[a["incident_id"]]
            first[a["incident_id"]] = min(first.get(a["incident_id"], t), t)
    mins = sorted(t / 60 for t in first.values() if t >= 0)
    median = round(mins[len(mins) // 2], 1) if mins else None
    stage = database.get_record("stage", "current") or {}
    checks = database.list_records("check")
    sup = database.list_records("supply_request")
    tasks = database.list_records("task")
    stats = {"incidents": len(live), "invalid": len(incs) - len(live), "by_type": by_type, "by_urgency": by_urg,
             "waiting_info": sum(o.get("state") == "보완 필요" for o in live),
             "approved": count("approved"), "rejected": count("rejected"), "pending": count("pending"),
             "first_approval_median_min": median, "first_approval_n": len(mins),
             "stage": stage.get("level", "정상") if stage.get("confirmed") else "정상(미확정)",
             "escalated": sum(c["state"] == "격상" for c in checks),
             "supply_done": sum(r["state"] == "완료" for r in sup), "supply_open": sum(r["state"] not in ("완료", "반려") for r in sup),
             "tasks_done": sum(t["state"] == "완료" for t in tasks), "tasks_open": sum(t["state"] != "완료" for t in tasks)}
    kinds = ", ".join(f"{k} {v}건" for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])) or "없음"
    text = "\n".join([
        f"[일일 상황 보고서 초안 · {datetime.now().strftime('%Y-%m-%d %H:%M')} 기준 · 모의 데이터]",
        f"1. 위기 단계: {stats['stage']}",
        f"2. 신고: {stats['incidents']}건({kinds}). 긴급도 1은 {by_urg.get('1', 0)}건. "
        f"정보 보완 대기 {stats['waiting_info']}건, 오접수 처리 {stats['invalid']}건.",
        f"3. 결정: 승인 {stats['approved']}건, 반려 {stats['rejected']}건, 대기 {stats['pending']}건.",
        f"4. 안부 확인: 무응답 격상 {stats['escalated']}가구.",
        f"5. 물품: 완료 {stats['supply_done']}건, 진행 중 {stats['supply_open']}건. 봉사: 완료 {stats['tasks_done']}건, 진행·대기 {stats['tasks_open']}건.",
        "6. 사후 평가: 신고 접수부터 첫 승인까지 " + (f"중앙값 {median}분({len(mins)}건 기준)." if mins else "기록 없음."),
        "※ 규칙으로 집계한 초안입니다. 담당자가 확인한 뒤 사용하세요."])
    return {"stats": stats, "text": text}


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
_welfare_lock = threading.Lock()    # 구역별 안부 확인 요청 중복 방지


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
        candidates = scenario.shelter_candidates(scenario_now(), assigned)
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
# 신고 유형 → ① 감시가 보는 위험(센서 교차 확인), ④ 대피소 배정, ⑤ 지원제도 후보의 피해 유형
STAGE_HAZARD = {"인명구조": "침수", "침수": "침수", "산사태·토사": "사면"}
SHELTER_TYPES = {"인명구조", "침수", "산사태·토사"}
RECOVERY_DAMAGE = {"침수": ["주택침수"], "산사태·토사": ["사면유실"]}
WELFARE_CHECK_MAX_URGENCY = 2   # 이만큼 급한 위험 신고면 그 구역 가구 ③ 안부 확인 시작을 승인 묶음에 넣는다
SENSOR_FRESH_SEC = 120          # 이보다 오래된 센서 판정은 교차 확인 근거로 쓰지 않는다
CROSSCHECK_CFG = crosscheck.load_cfg(ROOT / "config" / "crosscheck.json")   # 신고 대조: 센서가 있는 구역·심각도 표현


def _epoch(ts):
    """DB의 UTC 시각 문자열(2026-10-28T06:51:22.000Z) → epoch 초. 읽을 수 없으면 0(아주 오래된 것으로 취급)."""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def ongoing_dispatch(output):
    """같은 구역·유형 신고에 이미 대기 중이거나 투입 중인 공공 인력 배치(같은 사건의 반복 신고로 중복 배치하지 않게).
    인명구조는 별건 누락을 막기 위해 묶지 않는다."""
    if output["type"] == "인명구조" or output.get("zone") in (None, "미상"):   # 위치를 모르면 같은 사건인지 알 수 없다
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
        shelter = next((s for s in scenario.shelter_candidates(scenario_now(), assigned) if s["id"] == top.get("id")), None)
        if shelter is None or shelter["capacity_left"] < approval["item"]["demand"]["people"]:
            return f"{top.get('id')}의 남은 수용량이 부족합니다. 다시 추천하세요"
    return None


DOC_PARSER = DocumentParser(max_calls=LLM_MAX_CALLS) if MODE == "solar" else None
FEATURES = features.Features(database, SCENARIO, travel, DISPATCH_CFG, GUIDANCE_CFG, LLM, VERIFY_LLM, DOC_PARSER)
SENSORS = sensors.SensorHub(database)
WEATHER = weather.WeatherFeed()   # 기상청 초단기실황·특보(참고 정보, 키 없으면 표시만 안 함)
PUBLIC = public_api.PublicAPI(FEATURES, database, scenario_now)   # 시민·봉사자 화면(가상 코드 선택, 로그인 없음)
TELEGRAM = telegram_bot.TelegramBot(database, PUBLIC, lambda: public_status())   # 주민 알림(TELEGRAM_BOT_TOKEN이 있을 때만)
FEATURES.notify = TELEGRAM.event   # 위기 단계 확정·안부 확인 시작을 텔레그램으로


def change_occupancy(sid, delta, by):
    """대피소 입소(+)·퇴소(−) 반영(O6). 담당자 화면과 현장 노드 C가 같이 쓴다. 대피소가 없으면 KeyError, 범위를 넘으면 Conflict."""
    shelter = next((x for x in SCENARIO["shelters"] if x["id"] == sid), None)
    if shelter is None:
        raise KeyError(sid)
    with _dispatch_lock:
        now_occ = next(x["occupied"] for x in scenario_now()["shelters"] if x["id"] == sid)
        new_occ = now_occ + delta
        if not 0 <= new_occ <= shelter["capacity"]:
            raise features.Conflict(f"현재 {now_occ}명이라 {delta:+d}명은 반영할 수 없습니다(수용량 {shelter['capacity']}명)")
        database.put_record("shelter_occ", sid, {"id": sid, "delta": new_occ - shelter["occupied"], "by": by})
    database.log_audit("shelter_occupancy", "shelter", sid, {"delta": delta, "occupied": new_occ, "by": by})
    return {"id": sid, "occupied": new_occ, "capacity": shelter["capacity"]}


def load_field_nodes(path=ROOT / "config" / "field_nodes.json"):
    """현장 노드 → 반영 대상(대피소·가구·봉사자). 없는 ID는 시작할 때 막는다."""
    with open(path, encoding="utf-8") as f:
        cfg = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    checks = {("C", "shelter"): {s["id"] for s in SCENARIO["shelters"]},
              ("D", "household"): set(FEATURES.households), ("E", "volunteer"): set(FEATURES.volunteers)}
    for (node, key), ids in checks.items():
        if cfg.get(node, {}).get(key) not in ids:
            raise RuntimeError(f"config/field_nodes.json: {node}.{key}가 없는 ID입니다({cfg.get(node, {}).get(key)})")
    return cfg


FIELD_NODES = load_field_nodes()


def apply_field_event(ev):
    """현장 버튼 신호를 기존 웹 기능에 반영한다(담당자 화면 버튼과 같은 처리, 처리자는 '마이크로비트 노드').
    반영할 기능이 없는 신호는 None, 반영하면 (True, 설명), 지금 상태로는 못 하면 (False, 사유)."""
    node, code, by = ev["node"], ev["code"], f"마이크로비트 {ev['node']}"
    target = FIELD_NODES.get(node, {})
    try:
        if node == "C" and code in ("+1", "-1"):
            r = change_occupancy(target["shelter"], 1 if code == "+1" else -1, by)
            return True, f"{r['id']} 대피소 인원 {r['occupied']}/{r['capacity']}명"
        if node == "D" and code == "SAFE":
            hid = target["household"]
            with FEATURES.lock:
                if FEATURES.db.get_record("check", hid) is None:
                    return False, f"{hid} 가구 안부 확인이 진행 중이 아닙니다"
                r = FEATURES.checks_respond(hid)
            return True, f"{hid} 가구 안부 확인 응답({r['state']})"
        if node == "E" and code in ("SOS", "IN", "DONE"):
            vid = target["volunteer"]
            with FEATURES.lock:
                tasks = [t for t in FEATURES.db.list_records("task") if vid in t.get("team", [])]
                if code == "SOS":
                    current = next((t["id"] for t in tasks if t["state"] in ("진행", "투입승인")), None)
                    r = FEATURES.sos_raise({"volunteer": vid, "task": current})
                    return True, f"봉사 탭 SOS 목록에 올림({r['id']}, 봉사자 {vid})"
                want = "투입승인" if code == "IN" else "진행"
                task = next((t for t in tasks if t["state"] == want), None)
                if task is None:
                    return False, f"봉사자 {vid}에게 '{want}' 상태 작업이 없습니다"
                (FEATURES.task_checkin if code == "IN" else FEATURES.task_complete)(task["id"])
            return True, f"작업 {task['id']} → {'진행' if code == 'IN' else '완료'}"
    except features.Conflict as e:
        return False, str(e)
    except KeyError as e:
        return False, f"대상을 찾을 수 없습니다({e})"
    return None


def ingest_sensor_batch(data):
    """게이트웨이 묶음: 측정값 판정 + 새 현장 신호를 웹 기능에 반영해 결과를 신호에 붙인다."""
    res = SENSORS.ingest(data.get("readings"), data.get("rain_stage"), data.get("events"))
    for ev in res["events"]:
        try:
            outcome = apply_field_event(ev)
        except Exception as e:   # 반영 중 예상 못 한 오류가 나도 신호 기록과 센서 판정은 그대로 둔다
            traceback.print_exc()
            outcome = (False, f"반영 중 오류({type(e).__name__}) · 담당자가 직접 처리하세요")
        if outcome is None:
            continue
        applied, note = outcome
        SENSORS.annotate(ev["id"], applied=applied, note=note,
                         # SOS를 봉사 탭으로 넘겼으면 확인은 거기서 한다(같은 SOS를 두 번 확인하지 않게)
                         **({"open": False} if applied and ev["code"] == "SOS" and ev["node"] == "E" else {}))
        database.log_audit("field_event_applied" if applied else "field_event_not_applied", "node", ev["node"],
                           {"event_id": ev["id"], "code": ev["code"], "note": note})
    return res


def generate_guidance(incident_id, level, flags, control_zones, hazard=None):
    """③ 안내문 → ⑦ 검증. grounded면 승인 대기열(welfare_guidance), notSure면 담당자 확인, notGrounded면 차단.
    AI를 쓸 수 없으면(검증기 없음 포함) 표준 원문만 만든다(fail closed). 응답에는 승인 전 AI 문구를 넣지 않는다."""
    verifier_fn = guidance_client = None
    if LLM is not None and VERIFY_LLM is not None:
        guidance_client = LLM
        verifier_fn = lambda draft, evidence: verify.verify_text(draft, evidence, client=VERIFY_LLM)[0]
    result = welfare.make_guidance(profile={"flags": flags}, level=level, control_zones=control_zones,
                                   cfg=GUIDANCE_CFG, client=guidance_client, verifier=verifier_fn, hazard=hazard)
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


REPORTER_EDITS_MAX = 3


def _owner_ok(output, token):
    h = output.get("owner_hash")
    return bool(h) and isinstance(token, str) and bool(token) and hmac.compare_digest(h, hashlib.sha256(token.encode()).hexdigest())


def withdraw_pending(output, reason):
    """신고자가 고치거나 취소할 때: 이 신고의 대기 중 승인을 반려하고, 출고 전 물품 요청을 거둔다(실행된 조치는 건드리지 않음)."""
    who = {"username": "신고자"}
    rejected = []
    for a in database.list_pending_approvals_for(output["id"]):
        database.resolve_approval(a["id"], False, "신고자", reason, None)
        with FEATURES.lock:
            FEATURES.after_resolve(a, False, who)
        rejected.append(a["id"])
    with FEATURES.lock:
        cancelled, in_transit = FEATURES.cancel_incident_supply(output["id"], "신고자", reason)
    relinked = [i for aid in rejected for i in detach_dispatch(aid, output["id"], rebuild=True)]
    return {"rejected_approvals": rejected, "cancelled_supply": cancelled, "supply_in_transit": in_transit, "relinked_incidents": relinked}


def reporter_change(iid, action, data):
    """신고자가 보낸 신고를 고치거나 취소한다. 신고할 때 그 기기에만 준 비밀 값이 있어야 한다.
    - 고치기: 담당자가 아직 아무 조치도 승인하지 않았을 때만(대기 중 추천은 반려하고 새 내용으로 다시 연결). 3번까지.
    - 취소: 언제든. 대기 중 승인은 반려하고, 이미 승인된 출동·이동 중인 물품은 담당자가 확인하도록 메모를 남긴다."""
    token, followup = data.get("token"), False
    with _incident_lock, _dispatch_lock:
        row = incident_output(iid)
        if row is None:
            return 404, {"error": "접수번호를 찾을 수 없습니다"}
        o = row["output"]
        if not _owner_ok(o, token):
            return 403, {"error": "이 기기에서 보낸 신고만 고치거나 취소할 수 있어요"}
        if o.get("state") == INVALID:
            return 409, {"error": "이미 취소됐거나 처리하지 않기로 한 신고예요"}
        if action == "cancel":
            active = [a["id"] for a in database.list_dispatch_approvals() if a["incident_id"] == iid]
            res = withdraw_pending(o, "신고자가 신고를 취소함")
            o.update(state=INVALID, invalid_reason="신고자가 취소함", invalidated_by="신고자", invalidated_at=time.time(),
                     cancelled_by_reporter=True)
            acting = bool(active or res["supply_in_transit"])
            if acting:
                o["notes"] = list(o.get("notes") or []) + [
                    f"신고자가 취소함: 이미 승인된 출동 {', '.join(active) or '없음'} · 이동 중인 물품 "
                    f"{', '.join(x['id'] for x in res['supply_in_transit']) or '없음'} → 담당자가 확인 후 해제하세요"]
            database.update_incident(row, status="rejected")
            database.log_audit("incident_cancelled_by_reporter", "incident", iid, {**res, "active_dispatch": active})
            return 200, {"id": iid, "state": INVALID, "cancelled_by_reporter": True, "already_acting": acting}
        text = data.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            return 400, {"error": "고칠 내용을 2000자 이하로 적어 주세요"}
        if o.get("edits", 0) >= REPORTER_EDITS_MAX:
            return 409, {"error": "신고는 3번까지 고칠 수 있어요. 더 알릴 내용은 119·112에 전화해 주세요"}
        moving = [r for r in database.list_records("supply_request") if r.get("incident_id") == iid and r["state"] in ("배치", "이동")]
        if database.incident_has_approved(iid) or moving:
            return 409, {"error": "담당자가 이미 출동·출고를 승인해서 내용을 고칠 수 없어요. 더 알릴 내용은 119·112에 전화하거나 새로 신고해 주세요"}
        res = withdraw_pending(o, "신고자가 신고 내용을 고침")
        new = classify_incident(iid, text.strip(), o["ts"])
        no = new["output"]
        no.update({k: o[k] for k in CARRY_FIELDS if k in o})
        no.update(owner_hash=o["owner_hash"], edits=o.get("edits", 0) + 1, supplements=o.get("supplements", 0), edited_at=time.time())
        new = database.update_incident(link_incident(new))
        database.log_audit("incident_edited_by_reporter", "incident", iid, {**res, "edits": no["edits"]})
        followup = new["output"].get("state") == "접수"
    if followup:
        start_followups(iid)
    return 200, public_row(new)


CARRY_FIELDS = ("owner_hash", "channel", "household", "edits", "supplements")   # 다시 분류해도 남겨 둘 신고 정보


def submit_report(text, rate_key, channel=None, household=None):
    """주민 신고 접수(웹 화면·텔레그램 공통). (상태 코드, 응답)을 돌려준다.
    household: 가구 코드로 로그인한 주민이 보낸 신고면 그 가구(담당자 화면에만 보이고 공개 응답에서는 뺀다)."""
    if not isinstance(text, str) or not text.strip():
        return 400, {"error": "신고 내용을 입력해 주세요"}
    if len(text) > 2000:
        return 400, {"error": "신고 내용은 2000자 이하여야 합니다"}
    if not INCIDENT_LIMIT.allow(rate_key):
        return 429, {"error": "짧은 시간에 신고가 너무 많습니다. 급한 상황이면 119·112에 바로 전화하세요"}
    duplicate = database.find_duplicate_incident(text)
    if duplicate:
        return 200, {**public_row(duplicate), "duplicate": True}
    rid = "R-" + uuid.uuid4().hex[:8].upper()
    row = classify_incident(rid, text, time.time())
    # 신고자만 고치거나 취소할 수 있게, 그 기기(텔레그램이면 그 대화방)에만 주는 비밀 값(서버에는 해시만 저장)
    token = secrets.token_urlsafe(18)
    row["output"]["owner_hash"] = hashlib.sha256(token.encode()).hexdigest()
    if channel:
        row["output"]["channel"] = channel   # 담당자 화면에 '텔레그램 신고'처럼 표시
    if isinstance(household, str) and household in FEATURES.households:
        row["output"]["household"] = household   # 로그인한 가구의 신고(로그인 없이 보낸 긴급 신고와 구분)
    # Persist only the classifier result (the intake agent applies its configured masking).
    row = database.save_incident(row)
    row = database.update_incident(link_incident(row))
    start_followups(row["output"]["id"])
    return 201, {**public_row(row), "edit_token": token}


def supplement_incident(iid, extra, rate_key):
    """신고자가 빠진 정보를 보완(접수번호로). 원문 뒤에 붙여 다시 분류한다."""
    if not isinstance(extra, str) or not extra.strip() or len(extra) > 500:
        return 400, {"error": "보완 내용은 1~500자로 입력해 주세요"}
    if not INCIDENT_LIMIT.allow(rate_key):
        return 429, {"error": "짧은 시간에 요청이 너무 많습니다. 급한 상황이면 119·112에 바로 전화하세요"}
    with _incident_lock:
        old = incident_output(iid)
        if old is None:
            return 404, {"error": "접수번호를 찾을 수 없습니다"}
        o = old["output"]
        if o.get("state") != "보완 필요":
            return 409, {"error": "보완이 필요한 신고가 아닙니다"}
        if o.get("supplements", 0) >= MAX_SUPPLEMENTS:
            return 409, {"error": "보완은 3번까지 받습니다. 담당자가 확인할 예정이며, 급한 상황이면 119·112에 바로 전화하세요"}
        # 원문 뒤에 붙이고 자르지 않는다(긴 원문이어도 보완 내용이 빠지지 않게, 최대 3번이라 길이는 제한됨)
        text = f"{o['text']}\n[보완] {extra.strip()}"
        row = classify_incident(o["id"], text, o["ts"], max_text=len(text))
        out = row["output"]
        out.update({k: o[k] for k in CARRY_FIELDS if k in o})   # 보완해도 신고자 취소·고치기 권한이 남게
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
        return 200, public_row(row)


def report_info(iid):
    """텔레그램 봇이 신고자에게 알려 줄 처리 상황(접수번호 조회와 같은 범위만)."""
    row = incident_output(iid)
    if row is None:
        return None
    o, g = row["output"], public_guidance(iid)
    return {"id": iid, "type": o.get("type"), "state": o.get("state"), "urgency": o.get("urgency"),
            "missing": o.get("missing") or [], "cancelled": bool(o.get("cancelled_by_reporter")),
            "steps": public_progress(o), "guidance": g["text"] if g else None}


# 텔레그램 봇에서 신고·보완·취소·진행 상황 조회(웹 화면과 같은 함수, 속도 제한은 대화방별)
TELEGRAM.hooks = {
    "submit": lambda text, chat: submit_report(text, f"tg:{chat}", "텔레그램"),
    "supplement": lambda iid, text, chat: supplement_incident(iid, text, f"tg:{chat}"),
    "cancel": lambda iid, token: reporter_change(iid, "cancel", {"token": token}),
    "info": report_info,
}


def public_progress(output):
    """신고자에게 보여 줄 진행 상황(도움이 오고 있는지). 담당자 이름·승인 ID는 넣지 않는다."""
    if output.get("state") == INVALID:
        return []
    steps, linked = [], output.get("linked") or {}
    if output.get("classified_by"):
        steps.append({"icon": "👤", "text": "담당자가 신고 내용을 확인했어요"})
    states = [a["state"] for a in (database.get_approval(x) for x in linked.get("dispatch", []) if isinstance(x, str)) if a]
    if "approved" in states:
        steps.append({"icon": "🚒", "text": "대응 인력 출동이 승인됐어요", "done": True})
    elif states:
        steps.append({"icon": "🚒", "text": "대응 인력 출동을 담당자가 검토하고 있어요"})
    if linked.get("supply"):
        steps.append({"icon": "📦", "text": "물품 요청을 담당자가 검토하고 있어요"})
    if linked.get("volunteer"):
        steps.append({"icon": "🙋", "text": "봉사자를 찾고 있어요"})
    return steps


def public_guidance(incident_id):
    """접수 사이트에 내보낼 안내문. 검증 통과(grounded) + 담당자 승인 건만, 표시 직전에 조건을 다시 확인한다."""
    row = database.get_approved_guidance(incident_id)
    g = (row or {}).get("item", {}).get("guidance", {})
    if not row or g.get("verdict") != "grounded" or g.get("displayable") is not True or not g.get("text"):
        return None
    return {"text": g["text"], "verdict": "grounded", "approved": True, "approved_at": row["approved_at"],
            "source": g.get("source", ""), "doc_ids": g.get("doc_ids", []), "evidence": g.get("standard", "")}

ANNOUNCE_AUDIENCE = {"all": "모두", "citizen": "주민", "volunteer": "봉사자"}
ANNOUNCE_MAX_ACTIVE = 20


def list_announcements(public=False):
    """담당자 공지(최신 순). public이면 게시 중인 것만, 작성자 이름은 빼고 돌려준다."""
    rows = sorted(database.list_records("announcement"), key=lambda a: -a["at"])
    if public:
        return [{k: a[k] for k in ("id", "text", "audience", "zone", "urgent", "at")} for a in rows if a["active"]]
    return rows[:50]


def post_announcement(data, by):
    """담당자가 쓴 공지를 바로 게시한다(사람이 직접 쓴 글이라 AI 검증·승인 대기열을 거치지 않음). 감사 기록에 남긴다."""
    text, audience, zone, urgent = data.get("text"), data.get("audience", "all"), data.get("zone", "all"), data.get("urgent", False)
    if not isinstance(text, str) or not text.strip() or len(text) > 300:
        raise ValueError("공지 내용은 1~300자로 적어 주세요")
    if audience not in ANNOUNCE_AUDIENCE:
        raise ValueError("보낼 대상을 골라 주세요(모두·주민·봉사자)")
    if zone != "all" and zone not in {z["id"] for z in SCENARIO["zones"]}:
        raise ValueError("구역은 전체 또는 구역 ID여야 합니다")
    if not isinstance(urgent, bool):
        raise ValueError("긴급 여부는 예/아니오여야 합니다")
    if sum(a["active"] for a in database.list_records("announcement")) >= ANNOUNCE_MAX_ACTIVE:
        raise features.Conflict(f"게시 중인 공지가 {ANNOUNCE_MAX_ACTIVE}개입니다. 지난 공지를 내린 뒤 올려 주세요")
    aid = "AN-" + uuid.uuid4().hex[:6].upper()
    row = {"id": aid, "text": text.strip(), "audience": audience, "zone": zone, "urgent": urgent,
           "at": time.time(), "by": by, "active": True}
    database.put_record("announcement", aid, row)
    database.log_audit("announcement_posted", "announcement", aid, {"by": by, "audience": audience, "zone": zone, "urgent": urgent})
    TELEGRAM.announcement(row)   # 주민 대상이면 텔레그램 구독자에게도
    return row


def end_announcement(aid, by):
    row = database.get_record("announcement", aid)
    if row is None:
        raise KeyError(aid)
    if row["active"]:
        row.update(active=False, ended_at=time.time(), ended_by=by)
        database.put_record("announcement", aid, row)
        database.log_audit("announcement_ended", "announcement", aid, {"by": by})
    return row


def public_status():
    """접수 사이트 첫 화면용 공개 상황(로그인 없음). 담당자 이름·승인 ID·신고 원문은 넣지 않는다."""
    stage = database.get_record("stage", "current") or {}
    notices = []
    for iid in database.list_guidance_incident_ids(20):
        inc = database.get_incident(iid)
        out = (inc or {}).get("result", {}).get("output", {})
        g = public_guidance(iid) if inc and out.get("state") != INVALID else None
        if g:
            notices.append({"zone": out.get("zone"), "type": out.get("type"), "text": g["text"],
                            "approved_at": g["approved_at"], "source": g["source"], "evidence": g["evidence"]})
        if len(notices) >= 5:
            break
    _, assigned = dispatch_state()
    shelters = [{k: s[k] for k in ("name", "type", "zone", "capacity", "capacity_left", "amenities")}
                for s in scenario.shelter_candidates(scenario_now(), assigned)]
    return {"stage": {"level": stage.get("level", "정상"), "hazards": stage.get("hazards", []),
                      "confirmed": bool(stage.get("confirmed")), "at": stage.get("at")},
            "notices": notices, "shelters": shelters, "announcements": list_announcements(public=True),
            "control_zones": [{"name": c["name"], "between": c.get("between", [])}
                              for c in current_control_zones() if c.get("active")],
            "zones": {z["id"]: z["name"] for z in SCENARIO["zones"]},
            "zone_grid": [{k: z[k] for k in ("id", "name", "risk", "row", "col")} for z in SCENARIO["zones"]],
            "weather": WEATHER.status(),
            "telegram_bot": TELEGRAM.username if TELEGRAM.enabled and TELEGRAM.mode else None}   # 주민 화면의 '텔레그램으로 알림 받기'


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

    def require_gateway(self):
        """마이크로비트 게이트웨이 토큰을 확인한다. 아니면 503/401을 보낸 뒤 False."""
        if not SENSOR_TOKEN:
            self.send_json(503, {"error": "센서 게이트웨이가 꺼져 있습니다. 서버에 SENSOR_GATEWAY_TOKEN을 설정하세요"}); return False
        given = self.headers.get("X-Gateway-Token", "").encode("utf-8")
        if not hmac.compare_digest(given, SENSOR_TOKEN.encode("utf-8")):
            self.send_json(401, {"error": "게이트웨이 토큰이 올바르지 않습니다"}); return False
        return True

    def host_allowed(self):
        """허용한 Host로 들어온 요청만 처리한다. 악성 사이트가 DNS를 바꿔 이 서버로 요청을 보내는 공격(DNS 리바인딩)을 막는다."""
        return self.headers.get("Host", "").strip().lower() in ALLOWED_HOSTS

    def client_ip(self):
        if CLOUD:   # 플랫폼 프록시 뒤에서는 모든 요청이 프록시 주소로 온다. 원래 접속 IP는 X-Forwarded-For 맨 앞
            fwd = self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            if fwd:
                return fwd
        return self.client_address[0]

    def site_ok(self):
        """사이트 접속 코드가 켜져 있으면 코드 쿠키가 있는 요청만 통과시킨다."""
        if not SITE_CODE:
            return True
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        got = cookie.get("site")
        return bool(got) and hmac.compare_digest(got.value.encode(), SITE_TOKEN.encode())

    def gate(self, path, method):
        """접속 코드가 없으면 화면은 /access로 보내고 API는 401. 처리했으면 True."""
        if path in GATE_FREE or self.site_ok():
            return False
        if method == "GET" and not path.startswith("/api/"):
            self.send_response(302)
            self.send_header("Location", "/access?next=" + quote(path))
            self.send_header("Content-Length", "0")
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
        else:
            self.send_json(401, {"error": "접속 코드가 필요합니다. 받은 링크로 다시 들어가 코드를 입력하세요", "need_access_code": True})
        return True

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
        self.send_header("Cache-Control", "no-store")   # 코드를 고친 뒤 브라우저가 예전 화면을 보여 주지 않게
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)

    def public_call(self, method, path, data):
        """시민·봉사자 공개 경로(public_api). 오류는 404/403/409/400으로 돌려준다."""
        p = path.strip("/").split("/")[2:]   # api/public/ 뒤
        routes = {
            ("GET", "households"): lambda: {"items": PUBLIC.households()},
            ("POST", "households"): lambda: PUBLIC.register_household(data),
            ("POST", "household-login"): lambda: PUBLIC.login_household(data),   # 주민 화면 로그인(가구 코드)
            ("POST", "households", "*", "update"): lambda: PUBLIC.update_household(p[1], data),
            ("GET", "households", "*"): lambda: PUBLIC.household(p[1]),
            ("POST", "households", "*", "safe"): lambda: PUBLIC.respond_check(p[1]),
            ("POST", "supply"): lambda: PUBLIC.supply_request(data),
            ("POST", "supply", "*", "receipt"): lambda: PUBLIC.receipt(p[1], data),
            ("GET", "volunteers"): lambda: {"items": PUBLIC.volunteers()},
            ("GET", "volunteer-options"): PUBLIC.volunteer_options,          # 회원가입 선택지·할 수 있는 일 미리 보기
            ("POST", "volunteers"): lambda: PUBLIC.register_volunteer(data),  # 봉사자 회원가입
            ("POST", "volunteer-login"): lambda: PUBLIC.login_volunteer(data),
            ("POST", "volunteers", "*", "update"): lambda: PUBLIC.update_volunteer(p[1], data),
            ("GET", "volunteers", "*"): lambda: PUBLIC.volunteer(p[1]),
            ("POST", "volunteers", "*", "tasks", "*", "checkin"): lambda: PUBLIC.checkin(p[1], p[3]),
            ("POST", "volunteers", "*", "tasks", "*", "complete"): lambda: PUBLIC.complete(p[1], p[3]),
            ("POST", "volunteers", "*", "sos"): lambda: PUBLIC.sos(p[1], data),
            ("POST", "volunteers", "*", "donations"): lambda: PUBLIC.donation(p[1], data),
        }
        key = (method, *[x if i % 2 == 0 else "*" for i, x in enumerate(p)])
        if method == "GET" and len(p) == 3 and p[0] == "volunteers" and p[2] == "activity.csv":
            try:
                with FEATURES.lock:
                    body = PUBLIC.export_csv(p[1]).encode("utf-8")
            except KeyError:
                return self.send_json(404, {"error": "봉사자를 찾을 수 없습니다"})
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="activity_{p[1]}.csv"')
            self.send_header("Content-Length", str(len(body)))
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers(); self.wfile.write(body)
            return
        fn = routes.get(key)
        if fn is None or any(len(x) > 40 for x in p):
            return self.send_json(404, {"error": "찾을 수 없는 경로입니다"})
        try:
            with FEATURES.lock:
                res = fn()
        except KeyError:
            return self.send_json(404, {"error": "가구·봉사자·요청을 찾을 수 없습니다"})
        except PermissionError as e:
            return self.send_json(403, {"error": str(e)})
        except features.Conflict as e:
            return self.send_json(409, {"error": str(e)})
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        return self.send_json(201 if method == "POST" else 200, res)

    def do_GET(self):
        path = urlparse(self.path).path
        if not self.host_allowed():
            return self.send_json(421, {"error": "허용되지 않은 주소로 들어온 요청입니다(WEBAPP_ALLOWED_HOSTS 확인)"})
        if self.gate(path, "GET"):
            return
        if path == "/access":
            return self.send_html("access.html")
        if path == "/":   # 첫 화면: 주민·봉사자·담당자 중 골라 들어간다
            return self.send_html("home.html")
        if path in {"/citizen", "/report"}:
            return self.send_html("report.html")
        if path == "/admin":
            return self.send_html("admin.html")
        if path == "/login":
            return self.send_html("login.html")
        if path in ("/static/admin_ops.js", "/static/ui.js"):   # 정해진 파일만 제공(경로 조작 방지)
            return self.send_html(path.rsplit("/", 1)[-1], "application/javascript; charset=utf-8")
        if path == "/api/me":
            _, s = self.session()
            if s is None:
                return self.send_json(401, {"error": "로그인이 필요합니다"})
            return self.send_json(200, {"username": s["username"], "role": s["role"], "csrf_token": s["csrf"]})
        if path == "/api/health":
            return self.send_json(200, {"ok": True, "mode": MODE, "llm_enabled": LLM is not None,
                "llm_calls": LLM.calls if LLM else 0, "llm_max_calls": LLM.max_calls if LLM else 0, "database": database.backend_label(), "telegram": TELEGRAM.status(), "database_ready": True, "prototype": True,
                "warning": "시뮬레이션/프로토타입입니다. 실제 출동·재난 경보 시스템이 아닙니다."})
        if path == "/api/agents":
            return self.send_json(200, {"items": agents_status()})
        if path == "/api/public/status":   # 접수 사이트 첫 화면: 확정 단계·승인된 안내문·대피소·통제 구간
            return self.send_json(200, public_status())
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
                                        "guidance": None if invalid else public_guidance(incident_id),
                                        "progress": public_progress(incident["result"].get("output", {}))})
        if path == "/api/approvals":
            if not self.require_staff():
                return
            return self.send_json(200, {"items": database.list_pending_approvals()})
        if path == "/api/approvals/history":   # 담당자: 최근에 처리한 승인·거절(누가·왜·언제)
            if not self.require_staff():
                return
            return self.send_json(200, {"items": database.list_resolved_approvals(30)})
        if path.startswith(FEATURE_PREFIXES):   # ①③⑤⑥⑦⑧ 현장 운영 기능(webapp/features.py)
            if not self.require_staff():
                return
            res = FEATURES.get(path)
            return self.send_json(*res) if res else self.send_json(404, {"error": "찾을 수 없는 경로입니다"})
        if path == "/api/sensor/status":   # 게이트웨이 수신 현황과 최근 판정
            if not self.require_staff():
                return
            return self.send_json(200, {**SENSORS.status(), "gateway_enabled": bool(SENSOR_TOKEN), "cloud": CLOUD})
        if path == "/api/announcements":   # 담당자: 공지 목록(내린 것 포함, 작성자 표시)
            if not self.require_staff():
                return
            return self.send_json(200, {"items": list_announcements(), "audiences": ANNOUNCE_AUDIENCE})
        if path == "/api/sensor/downlink":   # 게이트웨이: 현장 마이크로비트로 방송할 확정 단계·안부 확인 요청
            if not self.require_gateway():
                return
            return self.send_json(200, SENSORS.downlink())
        if path in ("/volunteer",):
            return self.send_html("volunteer.html")
        if path == "/battle":   # 사람 vs AI 신고 분류 대결(시연·발표용)
            return self.send_html("battle.html")
        if path == "/api/public/battle":
            return self.send_json(200, battle.questions())
        if path == "/api/public/battle/history":
            ai = database.get_record("battle_ai", battle._mode_key(LLM))
            return self.send_json(200, {"history": battle.history(database),
                                        "ai": {**battle.score({r["id"]: r["type"] for r in ai["rows"]}), "total_ms": ai["total_ms"],
                                               "mode": ai["mode"]} if ai else None})
        if path.startswith("/api/public/") and path != "/api/public/status":
            return self.public_call("GET", path, {})
        if path == "/api/weather":   # 기상청 실제 강수·특보(참고 정보)와 지금 감시 에이전트가 쓰는 강우 단계
            if not self.require_staff():
                return
            return self.send_json(200, {**WEATHER.status(), "applied_rain_stage": SENSORS.rain_stage})
        if path == "/api/report/daily":   # 일일 상황 보고서 초안과 사후 평가(O13)
            if not self.require_staff():
                return
            return self.send_json(200, daily_report())
        if path == "/api/dispatch":   # 현재 투입 현황과 자원·대피소 상태
            if not self.require_staff():
                return
            busy, assigned = dispatch_state()
            return self.send_json(200, {
                "assignments": database.list_dispatch_approvals(),
                "public": scenario.public_candidates(SCENARIO, set(busy)),
                "shelters": scenario.shelter_candidates(scenario_now(), assigned),
                "control_zones": current_control_zones(), "zones": SCENARIO["zones"]})
        return self.send_json(404, {"error": "찾을 수 없는 경로입니다"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.host_allowed():
            return self.send_json(421, {"error": "허용되지 않은 주소로 들어온 요청입니다(WEBAPP_ALLOWED_HOSTS 확인)"})
        if not self.same_origin():
            return self.send_json(403, {"error": "다른 사이트에서 보낸 요청은 받지 않습니다"})
        if self.gate(path, "POST"):
            return
        try:
            if path in UPLOAD_PATHS:   # 큰 본문은 로그인·CSRF를 먼저 확인한 뒤에만 읽는다
                if not self.require_staff(check_csrf=True):
                    return
                data = read_json_body(self, MAX_UPLOAD_BODY)
            else:
                data = read_json_body(self)
            if path == "/api/access":   # 사이트 접속 코드(링크와 함께 받은 코드)
                if not SITE_CODE:
                    return self.send_json(200, {"ok": True})
                if not LOGIN_IP_LIMIT.allow(self.client_ip()):
                    return self.send_json(429, {"error": "시도가 너무 많습니다. 10분 뒤 다시 시도하세요"})
                code = data.get("code")
                if not isinstance(code, str) or not hmac.compare_digest(code.strip().encode(), SITE_CODE.encode()):
                    return self.send_json(403, {"error": "접속 코드가 맞지 않습니다"})
                return self.send_json(200, {"ok": True}, headers={"Set-Cookie": site_cookie()})
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
            if path == "/api/telegram/webhook":   # 텔레그램 웹훅: setWebhook 때 정한 비밀 값이 헤더에 있어야 한다
                if not TELEGRAM.check_secret(self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")):
                    return self.send_json(403, {"error": "forbidden"})
                try:
                    with FEATURES.lock:
                        TELEGRAM.handle(data)
                except Exception as e:   # 이상한 업데이트 하나 때문에 텔레그램이 같은 요청을 계속 다시 보내지 않게 200
                    print("telegram update error:", type(e).__name__, e)
                return self.send_json(200, {"ok": True})
            if path == "/api/sensor/readings":   # 마이크로비트 게이트웨이(로그인 대신 게이트웨이 토큰)
                if not self.require_gateway():
                    return
                return self.send_json(200, ingest_sensor_batch(data))
            if path == "/api/announcements" or (path.startswith("/api/announcements/") and path.endswith("/end")):
                s = self.require_staff(check_csrf=True)   # 담당자: 공지 올리기·내리기
                if not s:
                    return
                try:
                    if path == "/api/announcements":
                        return self.send_json(201, post_announcement(data, s["username"]))
                    return self.send_json(200, end_announcement(path.split("/")[-2], s["username"]))
                except KeyError:
                    return self.send_json(404, {"error": "공지를 찾을 수 없습니다"})
                except features.Conflict as e:
                    return self.send_json(409, {"error": str(e)})
            if path.startswith("/api/sensor/events/") and path.endswith("/ack"):   # 담당자: 급한 현장 신호(SOS 등) 확인
                s = self.require_staff(check_csrf=True)
                if not s:
                    return
                try:
                    return self.send_json(200, SENSORS.ack_event(path.split("/")[-2], s["username"]))
                except KeyError:
                    return self.send_json(404, {"error": "현장 신호를 찾을 수 없습니다(서버를 다시 켜면 지워집니다)"})
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
            if path.startswith("/api/public/"):
                if not PUBLIC_LIMIT.allow(self.client_ip()):
                    return self.send_json(429, {"error": "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요"})
                if path == "/api/public/household-login" and not HH_LOGIN_LIMIT.allow(self.client_ip()):
                    return self.send_json(429, {"error": "로그인 시도가 너무 많아요. 10분 뒤 다시 해 주세요. 급하면 로그인 없이 신고하거나 119·112에 전화하세요"})
                if path == "/api/public/battle":   # AI 분류(처음 한 번 Solar 20회)는 FEATURES 잠금 밖에서
                    try:
                        return self.send_json(201, battle.submit(database, LLM, data))
                    except ValueError as e:
                        return self.send_json(400, {"error": str(e)})
                return self.public_call("POST", path, data)
            if path.startswith("/api/incidents/") and path.endswith("/classify"):   # 담당자: 분류 직접 수정(O3)
                s = self.require_staff(check_csrf=True)
                if not s:
                    return
                iid = path.strip("/").split("/")[2]
                typ, zone, urg = data.get("type"), data.get("zone"), data.get("urgency")
                zone_ids = {z["id"] for z in SCENARIO["zones"]}
                if typ not in intake.load_cfg()["types"] or (zone != "미상" and zone not in zone_ids) \
                        or not isinstance(urg, int) or isinstance(urg, bool) or not 1 <= urg <= 4:
                    return self.send_json(400, {"error": "유형·구역(Z1~Z10 또는 미상)·긴급도(1~4)를 확인해 주세요"})
                with _incident_lock:
                    row = incident_output(iid)
                    if row is None:
                        return self.send_json(404, {"error": "접수번호를 찾을 수 없습니다"})
                    o = row["output"]
                    if o.get("state") == INVALID:
                        return self.send_json(409, {"error": "오접수로 처리된 신고입니다"})
                    before = {k: o.get(k) for k in ("type", "zone", "urgency")}
                    after = {"type": typ, "zone": zone, "urgency": urg}
                    if before == after:
                        return self.send_json(200, public_row(row))
                    note = ("담당자 분류 수정: " + ", ".join(f"{k} {before[k]}→{after[k]}" for k in after if before[k] != after[k])
                            + (" · 이미 만든 배치·안내는 그대로이니 필요하면 해당 화면에서 다시 추천하세요" if o.get("linked") else ""))
                    o.update(after, label="담당자 수정", classified_by=s["username"])
                    # 담당자가 구역을 채워 주면 다음 에이전트로 넘긴다: 신고자 보완을 기다리던 신고, 또는 위치를 글·GPS로만
                    # 적어 구역을 몰라 아무 에이전트에도 넘어가지 못한 신고(긴급 신고의 배치 추천이 여기서 시작된다)
                    relink = zone in zone_ids and (o.get("state") == "보완 필요"
                                                   or (before["zone"] == "미상" and o.get("state") == "접수" and not o.get("linked")))
                    if relink:
                        o["missing"] = [m for m in (o.get("missing") or []) if not m.startswith("위치")]
                        row = link_incident(row)   # 메모를 새로 쓰므로 수정 메모는 그 뒤에 붙인다
                    row["output"]["notes"] = list(row["output"].get("notes") or []) + [note]
                    row = database.update_incident(row)
                database.log_audit("incident_reclassified", "incident", iid, {"before": before, "after": after, "by": s["username"]})
                if relink and row["output"].get("state") == "접수":
                    start_followups(iid)
                return self.send_json(200, public_row(row))
            if path == "/api/weather/apply-rain":   # 담당자: 실제 강수량으로 감시 에이전트의 강우 단계를 맞춘다
                s = self.require_staff(check_csrf=True)
                if not s:
                    return
                w = WEATHER.status()
                if not w.get("available") or not w.get("rain"):
                    return self.send_json(409, {"error": "실제 강수 정보를 받지 못해 적용할 수 없습니다. 센서 탭에서 강우 단계를 직접 고르세요"})
                with SENSORS.lock:
                    SENSORS.rain_stage = w["rain"]["stage"]
                database.log_audit("rain_stage_from_kma", "monitor", "rain_stage",
                                   {"stage": w["rain"]["stage"], "mm_per_h": w["rain"]["mm_per_h"], "at": w["rain"]["at"], "by": s["username"]})
                return self.send_json(200, {"applied_rain_stage": SENSORS.rain_stage, "rain": w["rain"]})
            if path.startswith("/api/shelters/") and path.endswith("/occupancy"):   # 담당자: 대피소 입소·퇴소(O6)
                s = self.require_staff(check_csrf=True)
                if not s:
                    return
                sid, delta = path.strip("/").split("/")[2], data.get("delta")
                if not any(x["id"] == sid for x in SCENARIO["shelters"]):
                    return self.send_json(404, {"error": "대피소를 찾을 수 없습니다"})
                if not isinstance(delta, int) or isinstance(delta, bool) or delta == 0 or abs(delta) > 50:
                    return self.send_json(400, {"error": "입소(+)·퇴소(−) 인원은 1~50명이어야 합니다"})
                try:
                    return self.send_json(200, change_occupancy(sid, delta, s["username"]))
                except features.Conflict as e:
                    return self.send_json(409, {"error": str(e)})
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
                             else scenario.shelter_candidates(scenario_now(), assigned))
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

                typ = (incident_output(incident_id) or {}).get("output", {}).get("type")
                row = generate_guidance(incident_id, level, flags, control_zones, GUIDANCE_CFG.get("hazard_by_type", {}).get(typ))
                row["incident_id"] = incident_id
                return self.send_json(200, row)

            if path == "/api/incidents":
                return self.send_json(*submit_report(data.get("text"), self.client_ip(), household=data.get("household")))
            if path.startswith("/api/incidents/") and path.endswith(("/edit", "/cancel")):   # 신고자가 고치기·취소(이 기기의 비밀 값으로)
                parts = path.strip("/").split("/")
                if len(parts) != 4 or not parts[2] or len(parts[2]) > 40:
                    return self.send_json(404, {"error": "찾을 수 없는 경로입니다"})
                if not INCIDENT_LIMIT.allow(self.client_ip()):
                    return self.send_json(429, {"error": "짧은 시간에 요청이 너무 많습니다. 급한 상황이면 119·112에 바로 전화하세요"})
                return self.send_json(*reporter_change(parts[2], parts[3], data))
            if path.startswith("/api/incidents/") and path.endswith("/supplement"):   # 신고자가 빠진 정보 보완(접수번호로)
                parts = path.strip("/").split("/")
                if len(parts) != 4 or not parts[2] or len(parts[2]) > 40:
                    return self.send_json(404, {"error": "찾을 수 없는 경로입니다"})
                return self.send_json(*supplement_incident(parts[2], data.get("text"), self.client_ip()))
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
                    items = database.list_pending_approvals_for(parts[2])
                    # ① 센서와 신고가 같은 위험을 가리켜 묶어 둔 단계 확정 요청(신고 ID가 없는 공용 항목)
                    mid = ((row["output"].get("linked") or {}).get("monitor") or {}).get("approval_id")
                    stage = database.get_approval(mid) if mid else None
                    if stage and stage["state"] == "pending" and stage["kind"] == "monitor_stage":
                        items.insert(0, stage)
                    for a in items:
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
                        # 연결만 해 둔 반복 신고에 알림. 백그라운드 후속 처리와 같은 신고를 동시에 쓰지 않게 잠근다
                        # (_incident_lock → _dispatch_lock 순서를 지키려고 배치 잠금을 푼 뒤에 잡는다)
                        with _incident_lock:
                            detach_dispatch(aid, current["incident_id"], rebuild=False)
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
    if env.get("WEBAPP_CLOUD") == "1" or env.get("RENDER") == "true":   # HTTPS는 플랫폼이 처리한다
        if env.get("SITE_PUBLIC") != "1" and len(env.get("SITE_ACCESS_CODE", "").strip()) < 6:
            return ("클라우드에 올릴 때는 SITE_ACCESS_CODE(6자 이상)를 정해야 합니다. 링크를 받은 사람만 들어오게 하는 코드입니다. "
                    "(접속 코드 없이 누구나 열려면 SITE_PUBLIC=1을 명시)")
        if (database.count_users() if staff_count is None else staff_count) == 0:
            return "담당자 계정이 없습니다. DEMO_SEED_ON_START=1과 DEMO_ADMIN_PASSWORD(8자 이상)를 설정하세요."
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


def seed_demo_if_empty():
    """클라우드 무료 서버는 다시 시작하면 디스크가 비므로, 비어 있을 때 시연 계정과 신고 6건을 넣는다(scripts/demo_setup.py와 같은 내용)."""
    if os.getenv("DEMO_SEED_ON_START") != "1" or database.count_users():
        return
    password = os.getenv("DEMO_ADMIN_PASSWORD", "")
    if len(password) < 8:
        raise SystemExit("DEMO_SEED_ON_START=1이면 DEMO_ADMIN_PASSWORD(8자 이상)를 정해야 합니다")
    from scripts import demo_setup
    demo_setup.seed(password)
    print(f"시연 데이터를 넣었습니다: 담당자 {demo_setup.DEMO_USER}, 신고 {len(demo_setup.REPORTS)}건")


def main():
    seed_demo_if_empty()
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
    if AUTO_FOLLOWUPS:   # 미리 넣은 시연 신고 등 아직 연결되지 않은 신고를 백그라운드로 처리(서버는 바로 응답)
        threading.Thread(target=link_pending_incidents, daemon=True).start()
    TELEGRAM.start()   # 봇 이름 확인, 웹훅(공개 주소가 있을 때) 또는 폴링 시작
    if TELEGRAM.enabled:
        print(f"텔레그램 봇: {TELEGRAM.username or '연결 실패'} ({TELEGRAM.mode or TELEGRAM.error})")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()
