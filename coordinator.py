"""조정자: 규칙 기반(LLM 미사용). 이벤트 전달, 승인 대기열, 충돌 감지, 수동 모드 표시."""
import itertools
from common.audit import AuditLog


class Coordinator:
    def __init__(self, log: AuditLog | None = None):
        self.log = log or AuditLog()
        self.handlers, self.approvals, self.manual_agents = {}, {}, set()
        self._ids = itertools.count(1)

    # --- 이벤트 ---
    def subscribe(self, event_type, handler):
        self.handlers.setdefault(event_type, []).append(handler)

    def publish(self, event_type, payload, ts=None):
        self.log.add("coordinator", f"{event_type}", str(payload), ts=ts)
        for h in self.handlers.get(event_type, []):
            try:
                h(payload)
            except Exception as e:  # 한 핸들러의 실패가 전체를 멈추지 않게
                self.log.add("coordinator", event_type, "", result="error", error=str(e), ts=ts)

    # --- 승인 대기열: 상태 변경은 resolve(approved=True) 이후에만 ---
    def request_approval(self, kind, item):
        aid = f"AP-{next(self._ids):04d}"
        self.approvals[aid] = {"id": aid, "kind": kind, "item": item, "state": "pending"}
        return aid

    def pending(self):
        return [a for a in self.approvals.values() if a["state"] == "pending"]

    def resolve(self, aid, approved, by, reason=None):
        a = self.approvals[aid]
        if a["state"] != "pending":
            raise ValueError(f"{aid}는 이미 처리됨")
        a["state"] = "approved" if approved else "rejected"
        a["by"], a["reason"] = by, reason
        self.log.add("coordinator", a["kind"], a["state"], approver=by)
        return a

    # --- 충돌 감지 ---
    def detect_conflicts(self, assignments, capacities=None):
        """assignments: [{resource_id, demand_id, target(선택)}]
        같은 자원의 중복 배정, 대피소 정원 초과를 찾아 목록으로 돌려준다."""
        out, seen = [], {}
        for a in assignments:
            rid = a["resource_id"]
            if rid in seen and seen[rid] != a["demand_id"]:
                out.append({"type": "중복 배정", "resource_id": rid,
                            "demands": [seen[rid], a["demand_id"]]})
            seen.setdefault(rid, a["demand_id"])
        for target, (used, cap) in (capacities or {}).items():
            if used > cap:
                out.append({"type": "정원 초과", "target": target, "used": used, "capacity": cap})
        return out

    # --- 실패 대응 ---
    def mark_manual(self, agent):
        self.manual_agents.add(agent)
        self.log.add("coordinator", agent, "수동 모드 전환", result="degraded")
