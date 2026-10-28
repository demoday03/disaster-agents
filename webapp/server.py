"""Local-only demo web server for the disaster safety agents.
Run from project root: python webapp/server.py
This is a prototype, not a production or emergency-dispatch system.
"""
from __future__ import annotations
import hmac, json, os, sys, threading, time, uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
from common.llm import SolarClient
from agents import intake, monitor, welfare, verify
from coordinator import Coordinator
from database import db as database

HOST = os.getenv("WEBAPP_HOST", "127.0.0.1")
PORT = int(os.getenv("WEBAPP_PORT", "8765"))
MAX_BODY = 64 * 1024
MODE = os.getenv("LLM_MODE", "mock").strip().lower()
if MODE not in {"mock", "solar"}:
    raise RuntimeError("LLM_MODE must be 'mock' or 'solar'")
if MODE == "solar" and not os.getenv("UPSTAGE_API_KEY"):
    raise RuntimeError("LLM_MODE=solar requires UPSTAGE_API_KEY in environment")
LLM = SolarClient() if MODE == "solar" else None
VERIFY_MODEL = os.getenv("GROUNDEDNESS_MODEL", "").strip()
VERIFY_LLM = (
    SolarClient(model=VERIFY_MODEL)
    if MODE == "solar" and VERIFY_MODEL
    else None
)
GUIDANCE_CFG = welfare.load_cfg()
COORDINATOR = Coordinator()
database.init_db()

STAFF_ROLES = {"operator", "admin"}   # 관리자 화면·승인·안내문 생성 권한
COOKIE_SECURE = os.getenv("WEBAPP_COOKIE_SECURE") == "1"   # HTTPS로 배포할 때 켠다
LOGIN_MAX_FAILS, LOGIN_WINDOW_S = 5, 600
_login_fails, _login_lock = {}, threading.Lock()


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
    {"id": "dispatch", "name": "배치 추천", "mode": "규칙 기반", "status": "not-wired"},
    {"id": "recovery", "name": "복구·지원", "mode": "규칙 + 선택적 LLM", "status": "not-wired"},
    {"id": "volunteer", "name": "봉사 코디", "mode": "규칙 + 선택적 LLM", "status": "not-wired"},
    {"id": "verify", "name": "내용 검증", "mode": "규칙 + 선택적 LLM", "status": "not-wired"},
    {"id": "supply", "name": "물품 수급", "mode": "규칙 + 선택적 LLM", "status": "not-wired"},
]

def json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")

def result_dict(result):
    return result.to_dict() if hasattr(result, "to_dict") else result

def public_guidance(incident_id):
    """접수 사이트에 내보낼 안내문. 검증 통과(grounded) + 담당자 승인 건만, 표시 직전에 조건을 다시 확인한다."""
    row = database.get_approved_guidance(incident_id)
    g = (row or {}).get("item", {}).get("guidance", {})
    if not row or g.get("verdict") != "grounded" or g.get("displayable") is not True or not g.get("text"):
        return None
    return {"text": g["text"], "verdict": "grounded", "approved": True, "approved_at": row["approved_at"],
            "source": g.get("source", ""), "doc_ids": g.get("doc_ids", []), "evidence": g.get("standard", "")}

def read_json_body(handler):
    raw_len = handler.headers.get("Content-Length", "0")
    try:
        length = int(raw_len)
    except ValueError:
        raise ValueError("잘못된 Content-Length")
    if length < 0 or length > MAX_BODY:
        raise ValueError(f"요청 본문은 {MAX_BODY}바이트 이하여야 합니다")
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

class Handler(BaseHTTPRequestHandler):
    server_version = "DisasterAgentDemo/0.1"
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
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        self.end_headers(); self.wfile.write(body)

    def send_html(self, filename="report.html"):
        path = ROOT / "webapp" / filename
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in {"/", "/report"}:
            return self.send_html("report.html")
        if path == "/admin":
            return self.send_html("admin.html")
        if path == "/login":
            return self.send_html("login.html")
        if path == "/api/me":
            _, s = self.session()
            if s is None:
                return self.send_json(401, {"error": "로그인이 필요합니다"})
            return self.send_json(200, {"username": s["username"], "role": s["role"], "csrf_token": s["csrf"]})
        if path == "/api/health":
            return self.send_json(200, {"ok": True, "mode": MODE, "llm_enabled": LLM is not None,
                "llm_calls": LLM.calls if LLM else 0, "database": "SQLite", "database_ready": True, "prototype": True,
                "warning": "시뮬레이션/프로토타입입니다. 실제 출동·재난 경보 시스템이 아닙니다."})
        if path == "/api/agents":
            return self.send_json(200, {"items": AGENTS})
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
            return self.send_json(200, {"id": incident_id, **incident, "guidance": public_guidance(incident_id)})
        if path == "/api/approvals":
            if not self.require_staff():
                return
            return self.send_json(200, {"items": database.list_pending_approvals()})
        return self.send_json(404, {"error": "찾을 수 없는 경로입니다"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.same_origin():
            return self.send_json(403, {"error": "다른 사이트에서 보낸 요청은 받지 않습니다"})
        try:
            data = read_json_body(self)
            if path == "/api/login":
                username, password = data.get("username"), data.get("password")
                if not isinstance(username, str) or not isinstance(password, str) or len(username) > 50 or len(password) > 200:
                    return self.send_json(400, {"error": "사용자 이름과 비밀번호를 입력해 주세요"})
                key = username.strip().casefold()
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
            if path == "/api/logout":
                token, s = self.session()
                if s is not None:
                    if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), s["csrf"]):
                        return self.send_json(403, {"error": "보안 토큰(CSRF)이 올바르지 않습니다"})
                    database.delete_session(token)
                return self.send_json(200, {"ok": True}, headers={"Set-Cookie": session_cookie("", 0)})
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

                # Fail closed: do not generate AI guidance unless a verifier is configured.
                verifier_fn = None
                guidance_client = None
                if LLM is not None and VERIFY_LLM is not None:
                    guidance_client = LLM
                    verifier_fn = lambda draft, evidence: verify.verify_text(
                        draft, evidence, client=VERIFY_LLM
                    )[0]

                result = welfare.make_guidance(
                    profile={"flags": flags},
                    level=level,
                    control_zones=control_zones,
                    cfg=GUIDANCE_CFG,
                    client=guidance_client,
                    verifier=verifier_fn,
                )
                row = result_dict(result)
                output = row.get("output", {})
                if (
                    output.get("status") == "승인 대기"
                    and output.get("verdict") == "grounded"
                    and output.get("displayable") is True
                ):
                    approval = database.create_approval(
                        "welfare_guidance",
                        {"incident_id": incident_id, "guidance": output},
                        incident_id=incident_id,
                    )
                    row["approval_id"] = approval["id"]
                elif row.get("ai_used") and output.get("verdict") == "notSure":
                    # 제안서 6.9: notSure는 담당자 확인 요청. 표준 원문을 보여 주되 초안을 검토 대기열에 올린다.
                    review = database.create_approval(
                        "welfare_guidance_review",
                        {"incident_id": incident_id, "guidance": output},
                        incident_id=incident_id,
                    )
                    row["review_id"] = review["id"]
                elif row.get("ai_used") and output.get("verdict") == "notGrounded":
                    # 제안서 6.9: notGrounded는 차단하고 기록만 남긴다(대기열에 넣지 않음).
                    database.log_audit("guidance_blocked", "incident", incident_id,
                                       {"verdict": "notGrounded", "doc_ids": output.get("doc_ids", [])})
                    row["blocked"] = True

                # 사람 승인 전에는 AI 문구를 응답으로 내보내지 않는다(제안서 9.2-4: 승인 후 표시).
                # 초안 원문은 승인 대기열 항목(DB)에만 남고, 응답에는 표준 원문만 담는다.
                if row.get("ai_used"):
                    public = dict(output)
                    public.pop("draft", None)
                    public["text"] = public.get("standard", "")
                    public["displayable"] = True
                    public["ai_text_withheld"] = True
                    row["output"] = public

                row["incident_id"] = incident_id
                return self.send_json(200, row)

            if path == "/api/incidents":
                text = data.get("text")
                if not isinstance(text, str) or not text.strip():
                    return self.send_json(400, {"error": "신고 내용을 입력해 주세요"})
                if len(text) > 2000:
                    return self.send_json(400, {"error": "신고 내용은 2000자 이하여야 합니다"})
                duplicate = database.find_duplicate_incident(text)
                if duplicate:
                    return self.send_json(200, {**duplicate, "duplicate": True})
                rid = "R-" + uuid.uuid4().hex[:8].upper()
                result = intake.classify(rid, text, time.time(), intake.load_cfg(), client=LLM)
                row = result_dict(result)
                # Persist only the classifier result (the intake agent applies its configured masking).
                row = database.save_incident(row)
                return self.send_json(201, row)
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
                    result = database.resolve_approval(aid, approved, s["username"], (reason or "").strip(),
                                                       s["user_id"])
                except KeyError:
                    return self.send_json(404, {"error": "승인 요청을 찾을 수 없습니다"})
                except ValueError as e:
                    return self.send_json(409, {"error": str(e)})
                return self.send_json(200, result)
            return self.send_json(404, {"error": "찾을 수 없는 경로입니다"})
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        except Exception:
            # Avoid returning stack traces or secrets to the browser.
            return self.send_json(500, {"error": "서버 내부 오류입니다. 콘솔 로그를 확인해 주세요"})

def main():
    if HOST not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("안전상 기본은 localhost만 허용합니다. 공개망 배포 전 인증/HTTPS/CSRF/운영 보안 설계가 필요합니다.")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"재난 안전 AI 에이전트 프로토타입: http://{HOST}:{PORT}")
    print(f"LLM_MODE={MODE}; {'Solar API 사용 설정됨' if LLM else 'LLM은 사용하지 않고 규칙 기반 데모로 실행'}")
    if database.count_users() == 0:
        print("담당자 계정이 없습니다. 관리자 화면을 쓰려면 먼저 실행하세요: python scripts/create_user.py 이름 --role operator")
    print("Ctrl+C로 종료하세요. 실재난 경보/출동 용도로 사용하지 마세요.")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()
