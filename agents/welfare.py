"""③ 안부·안내 에이전트. 안전 확인 타이머·격상은 규칙, 안내문 문장 조정만 LLM.
AI가 만든 안내문은 ⑦ 검증(verifier)을 통과해야 표시하고, 실패하면 표준 행동요령 원문을 보여 준다."""
import json
from common.llm import ask_or_fallback
from common.schema import AgentResult


def load_cfg(path="config/guidance.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class SafetyChecker:
    def __init__(self, cfg):
        self.cfg, self.rows = cfg, {}

    def start(self, hid, ts, vulnerable=False):
        t = self.cfg["timeout_s"] * (self.cfg["vulnerable_factor"] if vulnerable else 1)  # 취약 프로필은 짧게
        self.rows[hid] = {"deadline": ts + t, "vulnerable": vulnerable, "state": "대기"}

    def respond(self, hid, ts):
        r = self.rows[hid]
        r["state"] = "지연 응답" if r["state"] == "격상" else "안전"
        return r["state"]

    def tick(self, now):
        """기한이 지난 무응답 가구를 격상. 취약 가구 먼저. 자동 출동은 하지 않고 담당자 알림만."""
        due = [(h, r) for h, r in self.rows.items() if r["state"] == "대기" and now >= r["deadline"]]
        due.sort(key=lambda x: (not x[1]["vulnerable"], x[1]["deadline"], x[0]))
        out = []
        for h, r in due:
            r["state"] = "격상"
            out.append({"type": "격상", "household": h, "vulnerable": r["vulnerable"],
                        "overdue_s": round(now - r["deadline"], 1),
                        "notify": "staff", "dispatch_requires_approval": True})
        return out


def make_guidance(profile, level, control_zones, cfg, client=None, verifier=None):
    """profile: {'flags': [...]}. verifier(draft, evidence) -> 'grounded'|'notGrounded'|'notSure'."""
    flags = set(profile.get("flags", []))
    frs = [f for f in cfg["fragments"] if level in f["levels"] and (not f["flags"] or flags & set(f["flags"]))]
    std = [f["text"] for f in frs]
    if control_zones:
        std.append(f"통제 구간({', '.join(control_zones)})은 피해서 이동하세요.")
    doc_ids = [f["id"] for f in frs] + (["RULE-CTRL"] if control_zones else [])
    standard = " ".join(std)
    base = {"doc_ids": doc_ids, "source": cfg["source"], "standard": standard}
    if client is None:   # AI 미사용: 표준 원문이 곧 근거
        return AgentResult("welfare", {**base, "text": standard, "status": "표준 원문", "displayable": True},
                           fallback_reason="LLM 클라이언트 없음", evidence=doc_ids)
    msgs = [{"role": "system", "content": "근거 문장만 사용해 가구 상황에 맞게 문장을 다듬는다. "
             "근거에 없는 수치·제도·행동을 추가하지 않는다. JSON만: {\"text\": str}"},
            {"role": "user", "content": json.dumps({"profile": sorted(flags), "level": level,
                                                    "근거": standard}, ensure_ascii=False)}]
    out, ai, why = ask_or_fallback(client, msgs, ["text"], lambda: {"text": standard})
    if not ai:
        return AgentResult("welfare", {**base, "text": standard, "status": "표준 원문", "displayable": True},
                           fallback_reason=why, evidence=doc_ids)
    if verifier is None:   # 검증 미연결이면 AI 문구는 표시 금지
        return AgentResult("welfare", {**base, "text": standard, "draft": out["text"],
                           "status": "검증 대기", "displayable": False}, ai_used=True,
                           needs_approval=True, evidence=doc_ids)
    verdict = verifier(out["text"], standard)
    ok = verdict == "grounded"
    return AgentResult("welfare", {**base, "text": out["text"] if ok else standard, "draft": out["text"],
                       "verdict": verdict, "displayable": True,
                       "status": {"grounded": "승인 대기", "notGrounded": "차단(표준 원문 표시)"}
                                 .get(verdict, "담당자 확인 요청(표준 원문 표시)")},
                       ai_used=True, needs_approval=True, evidence=doc_ids)
