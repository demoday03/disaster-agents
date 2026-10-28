"""SQLite persistence and simple SQL migration runner for the local demo."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("DISASTER_DB_PATH", str(ROOT / "data" / "disaster.db"))).expanduser().resolve()
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"



@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        with conn:
            yield conn
    finally:
        conn.close()

def init_db():
    """Apply each numbered SQL migration once, in order."""
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )""")
        applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
            if path.name in applied:
                continue
            # Each migration is versioned and recorded only after its SQL succeeds.
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (path.name,))


def save_incident(result: dict) -> dict:
    output = result.get("output") if isinstance(result, dict) else {}
    output = output if isinstance(output, dict) else {}
    incident_id = str(output.get("id") or ("R-" + uuid.uuid4().hex[:8].upper()))
    result = dict(result)
    result.setdefault("output", {})
    result["output"] = dict(result["output"])
    result["output"]["id"] = incident_id
    with connect() as conn:
        conn.execute("""INSERT INTO incidents
            (id, incident_type, zone, severity, status, result_json)
            VALUES (?, ?, ?, ?, 'submitted', ?)
            ON CONFLICT(id) DO UPDATE SET result_json=excluded.result_json,
              incident_type=excluded.incident_type, zone=excluded.zone,
              severity=excluded.severity, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
            (incident_id, str(output.get("type", "미분류")), str(output.get("zone", "미상")),
             output.get("urgency") if isinstance(output.get("urgency"), int) else None,
             json.dumps(result, ensure_ascii=False)))
        conn.execute("INSERT INTO ai_analyses(incident_id, agent_name, result_json, model_name) VALUES (?, ?, ?, ?)",
                     (incident_id, str(result.get("agent", "intake")), json.dumps(result, ensure_ascii=False),
                      "Solar" if result.get("ai_used") else "rules/mock"))
        conn.execute("INSERT INTO agent_logs(incident_id, agent_name, status, message) VALUES (?, ?, 'success', ?)",
                     (incident_id, str(result.get("agent", "intake")), "분류 결과 저장 완료"))
    return result


def update_incident(result: dict, status: str | None = None) -> dict:
    """저장된 신고의 결과(JSON)를 고친다. status는 incidents.status 허용값 중 하나(없으면 그대로)."""
    output = result["output"]
    with connect() as conn:
        cur = conn.execute("""UPDATE incidents SET result_json=?, incident_type=?, zone=?, severity=?,
              status=COALESCE(?, status), updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?""",
            (json.dumps(result, ensure_ascii=False), str(output.get("type", "미분류")), str(output.get("zone", "미상")),
             output.get("urgency") if isinstance(output.get("urgency"), int) else None, status, output["id"]))
        if cur.rowcount != 1:
            raise KeyError(output["id"])
    return result


def list_pending_approvals_for(incident_id: str) -> list[dict]:
    """한 신고에 걸린 대기 중 승인 항목(오접수 처리 때 함께 반려)."""
    with connect() as conn:
        rows = conn.execute("SELECT id FROM approvals WHERE state='pending' AND incident_id=? ORDER BY created_at",
                            (incident_id,)).fetchall()
    return [get_approval(r["id"]) for r in rows]


def list_incidents(limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT result_json FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [json.loads(r["result_json"]) for r in rows]



def find_duplicate_incident(text: str) -> dict | None:
    """Find an identical recent report (case/whitespace-insensitive) to avoid repeat submissions."""
    import re
    from common.masking import mask
    # 저장된 신고문은 마스킹돼 있으므로 같은 방식으로 마스킹한 뒤 비교한다(전화번호가 있으면 중복을 못 찾던 문제)
    normalized = re.sub(r"\s+", " ", mask(text)).strip().casefold()
    if not normalized:
        return None
    # Recent list is deliberately bounded for this local demo.
    import time
    now = time.time()
    for item in list_incidents(200):
        output = item.get("output", {}) if isinstance(item, dict) else {}
        existing = re.sub(r"\s+", " ", str(output.get("text", ""))).strip().casefold()
        try:
            submitted_at = float(output.get("ts", 0))
        except (TypeError, ValueError):
            submitted_at = 0
        if existing == normalized and output.get("id") and 0 <= now - submitted_at <= 600:
            return item
    return None

def create_approval(kind: str, item: dict, incident_id: str | None = None) -> dict:
    aid = "AP-" + uuid.uuid4().hex[:8].upper()
    with connect() as conn:
        conn.execute("INSERT INTO approvals(id, incident_id, kind, item_json) VALUES (?, ?, ?, ?)",
                     (aid, incident_id, kind, json.dumps(item, ensure_ascii=False, default=str)))
        conn.execute("INSERT INTO audit_logs(action, target_type, target_id, details_json) VALUES (?, ?, ?, ?)",
                     ("approval_requested", "approval", aid, json.dumps({"kind": kind}, ensure_ascii=False)))
    return {"id": aid, "kind": kind, "item": item, "state": "pending"}


def log_audit(action: str, target_type: str, target_id: str | None, details: dict) -> None:
    """승인과 무관한 기록(예: 안내문 차단)을 audit_logs에 남긴다."""
    with connect() as conn:
        conn.execute("INSERT INTO audit_logs(action, target_type, target_id, details_json) VALUES (?, ?, ?, ?)",
                     (action, target_type, target_id, json.dumps(details, ensure_ascii=False, default=str)))


def list_pending_approvals() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT id, kind, item_json, state, created_at FROM approvals WHERE state='pending' ORDER BY created_at").fetchall()
    return [{"id": r["id"], "kind": r["kind"], "item": json.loads(r["item_json"]), "state": r["state"],
             "created_at": r["created_at"]} for r in rows]


def get_approval(aid: str) -> dict | None:
    with connect() as conn:
        r = conn.execute("SELECT id, incident_id, kind, item_json, state FROM approvals WHERE id=?", (aid,)).fetchone()
    return None if r is None else {"id": r["id"], "incident_id": r["incident_id"], "kind": r["kind"],
                                   "item": json.loads(r["item_json"]), "state": r["state"]}


def update_pending_approval_item(aid: str, item: dict) -> None:
    """대기 중인 승인 항목의 내용만 바꾼다(배치 수정·직접 지정). 이미 처리됐으면 ValueError."""
    with connect() as conn:
        cur = conn.execute("UPDATE approvals SET item_json=? WHERE id=? AND state='pending'",
                           (json.dumps(item, ensure_ascii=False, default=str), aid))
        if cur.rowcount != 1:
            raise ValueError(f"{aid}는 대기 중인 승인 항목이 아님")


def list_dispatch_approvals(states=("approved",), include_released=False) -> list[dict]:
    """배치(dispatch_*) 승인 항목. 기본은 승인되고 아직 해제되지 않은 것(현재 투입 중)."""
    with connect() as conn:
        rows = conn.execute(f"""SELECT a.id, a.incident_id, a.kind, a.item_json, a.state, a.approver_name,
                a.created_at, a.resolved_at, r.released_at
            FROM approvals a LEFT JOIN dispatch_releases r ON r.approval_id = a.id
            WHERE a.kind LIKE 'dispatch\\_%' ESCAPE '\\' AND a.state IN ({','.join('?' * len(states))})
            ORDER BY a.resolved_at""", states).fetchall()
    return [{"id": r["id"], "incident_id": r["incident_id"], "kind": r["kind"], "item": json.loads(r["item_json"]),
             "state": r["state"], "approver": r["approver_name"], "created_at": r["created_at"], "resolved_at": r["resolved_at"],
             "released_at": r["released_at"]}
            for r in rows if include_released or r["released_at"] is None]


def release_dispatch(aid: str, by: str, user_id: int | None = None) -> None:
    """승인된 배치를 해제한다. 없는 항목은 KeyError, 승인 전이거나 이미 해제됐으면 ValueError."""
    with connect() as conn:
        row = conn.execute("SELECT kind, state FROM approvals WHERE id=?", (aid,)).fetchone()
        if row is None or not row["kind"].startswith("dispatch_"):
            raise KeyError(aid)
        if row["state"] != "approved":
            raise ValueError(f"{aid}는 승인된 배치가 아님")
        cur = conn.execute("INSERT OR IGNORE INTO dispatch_releases(approval_id, released_by, user_id) VALUES (?, ?, ?)",
                           (aid, by, user_id))
        if cur.rowcount != 1:
            raise ValueError(f"{aid}는 이미 해제됨")
        conn.execute("INSERT INTO audit_logs(user_id, action, target_type, target_id, details_json) "
                     "VALUES (?, 'dispatch_released', 'approval', ?, ?)",
                     (user_id, aid, json.dumps({"by": by}, ensure_ascii=False)))


def resolve_approval(aid: str, approved: bool, by: str, reason: str, user_id: int | None = None) -> dict:
    state = "approved" if approved else "rejected"
    with connect() as conn:
        # 조회 후 갱신하면 동시 요청이 모두 '대기'를 보고 성공으로 응답한다.
        # 조건부 UPDATE 한 문장으로 처리하고, 실제로 바뀐 행이 있을 때만 성공으로 본다.
        cur = conn.execute("""UPDATE approvals SET state=?, approver_name=?, reason=?,
            resolved_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND state='pending'""",
            (state, by, reason, aid))
        row = conn.execute("SELECT id, kind, item_json, state FROM approvals WHERE id=?", (aid,)).fetchone()
        if row is None:
            raise KeyError(aid)
        if cur.rowcount != 1:
            raise ValueError(f"{aid}는 이미 처리됨")
        conn.execute("INSERT INTO audit_logs(user_id, action, target_type, target_id, details_json) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (user_id, "approval_resolved", "approval", aid,
                      json.dumps({"decision": state, "by": by, "reason": reason}, ensure_ascii=False)))
    return {"id": aid, "kind": row["kind"], "item": json.loads(row["item_json"]),
            "state": state, "by": by, "reason": reason}


# ---- 기능별 상태 문서(records) ---------------------------------------------------
def put_record(kind: str, rid: str, data: dict) -> dict:
    with connect() as conn:
        conn.execute("""INSERT INTO records(kind, id, data_json) VALUES (?, ?, ?)
            ON CONFLICT(kind, id) DO UPDATE SET data_json=excluded.data_json,
              updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
            (kind, rid, json.dumps(data, ensure_ascii=False, default=str)))
    return data


def get_record(kind: str, rid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT data_json FROM records WHERE kind=? AND id=?", (kind, rid)).fetchone()
    return None if row is None else json.loads(row["data_json"])


def list_records(kind: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT data_json FROM records WHERE kind=? ORDER BY created_at, id", (kind,)).fetchall()
    return [json.loads(r["data_json"]) for r in rows]


def count_records(kind: str) -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM records WHERE kind=?", (kind,)).fetchone()[0]


# ---- 사용자·세션 -------------------------------------------------------------
ROLES = ("citizen", "operator", "admin")
PBKDF2_ITERATIONS = 200_000
SESSION_TTL_S = 8 * 3600


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _check_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(calc.hex(), digest)
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = hash_password(secrets.token_hex(8))   # 없는 계정도 같은 시간만큼 계산(계정 존재 여부 노출 방지)


def create_user(username: str, password: str, role: str) -> int:
    username = (username or "").strip()
    if not username or len(username) > 50:
        raise ValueError("사용자 이름은 1~50자여야 합니다")
    if role not in ROLES:
        raise ValueError(f"역할은 {ROLES} 중 하나여야 합니다")
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("비밀번호는 8자 이상이어야 합니다")
    try:
        with connect() as conn:
            cur = conn.execute("INSERT INTO users(username, password_hash, role) VALUES (?, ?, ?)",
                               (username, hash_password(password), role))
            conn.execute("INSERT INTO audit_logs(user_id, action, target_type, target_id, details_json) "
                         "VALUES (?, 'user_created', 'user', ?, ?)",
                         (cur.lastrowid, username, json.dumps({"role": role})))
            return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError("이미 있는 사용자 이름입니다") from None


def count_users(roles=("operator", "admin")) -> int:
    with connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM users WHERE role IN ({','.join('?' * len(roles))})",
                            roles).fetchone()[0]


def authenticate(username: str, password: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT id, username, password_hash, role FROM users WHERE username=?",
                           ((username or "").strip(),)).fetchone()
    ok = _check_password(password or "", row["password_hash"] if row else _DUMMY_HASH)
    return {"id": row["id"], "username": row["username"], "role": row["role"]} if row and ok else None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: int, ttl_s: int = SESSION_TTL_S) -> tuple[str, str]:
    """(세션 토큰, CSRF 토큰). 토큰 원문은 DB에 남기지 않는다."""
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = time.time()
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        conn.execute("INSERT INTO sessions(token_hash, user_id, csrf_token, expires_at) VALUES (?, ?, ?, ?)",
                     (_token_hash(token), user_id, csrf, now + ttl_s))
    return token, csrf


def get_session(token: str | None) -> dict | None:
    if not token:
        return None
    with connect() as conn:
        row = conn.execute("""SELECT s.csrf_token, s.expires_at, u.id, u.username, u.role
            FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=?""",
            (_token_hash(token),)).fetchone()
    if row is None or row["expires_at"] < time.time():
        return None
    return {"user_id": row["id"], "username": row["username"], "role": row["role"], "csrf": row["csrf_token"]}


def delete_session(token: str | None) -> None:
    if token:
        with connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def get_approved_guidance(incident_id: str) -> dict | None:
    """검증 통과(welfare_guidance) 후 담당자가 승인한 가장 최근 안내문. 담당자 확인(review) 건은 제외."""
    with connect() as conn:
        row = conn.execute(
            "SELECT item_json, resolved_at FROM approvals WHERE incident_id=? AND kind='welfare_guidance' "
            "AND state='approved' ORDER BY resolved_at DESC LIMIT 1",
            (incident_id,),
        ).fetchone()
    if row is None:
        return None
    return {"item": json.loads(row["item_json"]), "approved_at": row["resolved_at"]}


def get_incident(incident_id: str) -> dict | None:
    """Return one persisted incident by its public receipt ID."""
    with connect() as conn:
        row = conn.execute(
            "SELECT result_json, status, created_at, updated_at FROM incidents WHERE id = ?",
            (incident_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "result": json.loads(row["result_json"]),
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
