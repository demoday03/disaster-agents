"""④ 배치 추천 에이전트.
- 점수·제약은 전부 규칙. LLM은 근거 문장에만 (선택, 실패하면 템플릿).
- 하드 제약 필터 → 0~1 점수 가중합 → 1순위 + 대안 2개.
- 같은 입력이면 같은 순위(동점은 id순). 사람이 승인해야 반영(Board).
"""
import json
from common.schema import AgentResult

LABELS = {"capability": "역량 일치", "distance": "이동 거리", "equipment": "장비 보유",
          "time": "가용 시간", "load": "낮은 현재 부하", "urgency_fit": "긴급도 적합",
          "capacity": "수용량 여유", "amenity": "편의시설 일치", "avoid_control": "통제 구간 회피",
          "quantity": "수량 충족", "expiry_fit": "유통기한 적합", "remaining": "공급원 잔여"}


def load_config(path="config/dispatch.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def weighted_score(items, weights):
    assert abs(sum(weights.values()) - 1) < 1e-9, "가중치 합은 1이어야 함"
    return round(sum(w * items[k] for k, w in weights.items()), 4)


def _frac(need, have):
    need = set(need or [])
    return 1.0 if not need else len(need & set(have or [])) / len(need)


def _travel(ctx, a, b):
    return ctx.get("travel", {}).get((a, b), {"minutes": 0, "via_blocked": False})


def _dist(ctx, cfg, a, b):
    if a in (None, "미상") or b in (None, "미상"):
        return 0.5   # 직접 쓴 위치에서 구역을 추정하지 못했으면 가깝다고도 멀다고도 보지 않는다(담당자 확인 전)
    return max(0.0, 1 - _travel(ctx, a, b)["minutes"] / cfg["max_travel_min"])


def violations(kind, d, c, ctx, cfg):
    """후보 제외 사유 목록. 비어 있으면 통과."""
    v = []
    if c.get("available") is False:
        v.append("가용하지 않음")
    if _travel(ctx, c.get("zone"), d.get("zone"))["via_blocked"]:
        v.append("통제 구간 경유")
    if kind in ("volunteer", "public"):
        cap = cfg.get("max_hours_per_volunteer")
        if c.get("rest_needed"):
            v.append("휴식 필요")
        if cap is not None and c.get("hours_today", 0) + d.get("hours", 0) > cap:
            v.append("활동 시간 상한 초과")
        if d.get("must_have_skills") and _frac(d["must_have_skills"], c.get("skills")) < 1:
            v.append("필수 역량 없음")
        if d.get("must_have_equipment") and _frac(d["must_have_equipment"], c.get("equipment")) < 1:
            v.append("필수 장비 없음")
    elif kind == "shelter":
        if c["capacity_left"] <= 0:
            v.append("수용량 없음")
        elif c["capacity_left"] < d.get("people", 1):
            v.append("수용 인원 부족")
    elif kind == "goods":
        if c.get("category") == "의약품" or d.get("category") == "의약품":
            v.append("의약품은 자동 배치 금지")
        if c.get("expired"):
            v.append("유통기한 경과")
        if c.get("qty", 0) <= 0:
            v.append("재고 없음")
    return v


def features(kind, d, c, ctx, cfg):
    dist = _dist(ctx, cfg, c.get("zone"), d.get("zone"))
    if kind == "volunteer":
        need_min = d.get("hours", 1) * 60
        return {"capability": _frac(d.get("skills"), c.get("skills")), "distance": dist,
                "equipment": _frac(d.get("equipment"), c.get("equipment")),
                "time": min(1.0, c.get("available_min", 0) / need_min)}
    if kind == "public":
        return {"capability": _frac(d.get("skills"), c.get("skills")), "distance": dist,
                "load": 1 - c.get("load", 0),
                "urgency_fit": 1.0 if c.get("max_urgency", 4) <= d.get("urgency", 4) else 0.5}
    if kind == "shelter":
        margin = (c["capacity_left"] - d.get("people", 1)) / max(c["capacity"], 1)
        return {"capacity": max(0.0, min(1.0, margin)),
                "amenity": _frac(d.get("amenities"), c.get("amenities")),
                "distance": dist, "avoid_control": 1.0}  # 통제 경유는 이미 제외됨
    # goods
    need = max(d.get("qty", 1), 1)
    days = c.get("days_left")
    expiry = 1.0 if days is None else (1.0 if days <= 7 else 0.7)  # 임박분 먼저 소진
    return {"quantity": min(1.0, c["qty"] / need), "distance": dist, "expiry_fit": expiry,
            "remaining": max(0.0, min(1.0, (c["qty"] - need) / c["qty"]))}


def _reason(items, weights):
    top = sorted((k for k in items if items[k] >= 0.8),
                 key=lambda k: (-weights[k] * items[k], k))[:3]
    return (", ".join(LABELS[k] for k in top) + " 항목이 높음") if top else "뚜렷하게 높은 항목 없음"


def recommend(kind, demand, candidates, ctx, cfg, exclude=(), other_demands=(),
              explainer=None):
    weights, ranked, excluded = cfg["weights"][kind], [], []
    for c in candidates:
        if c["id"] in exclude:
            excluded.append({"id": c["id"], "reasons": ["담당자 거절로 제외"]}); continue
        why = violations(kind, demand, c, ctx, cfg)
        if why:
            excluded.append({"id": c["id"], "reasons": why}); continue
        items = features(kind, demand, c, ctx, cfg)
        ranked.append({"id": c["id"], "score": weighted_score(items, weights), "items": items})
    ranked.sort(key=lambda r: (-r["score"], r["id"]))   # 재현성: 동점은 id순

    if not ranked:  # 후보 부족: 사실과 선택지를 제시
        return AgentResult("dispatch", {
            "demand_id": demand["id"], "kind": kind, "top": None, "alternatives": [],
            "excluded": excluded, "shortage": True,
            "options": ["외부 조달 요청", "제약 완화 검토", "직접 지정"]},
            needs_approval=True, evidence=[e["id"] for e in excluded])

    top, alts = ranked[0], ranked[1:3]
    reason, ai_used, fb = _reason(top["items"], weights), False, None
    if explainer:
        try:
            reason, ai_used = explainer(demand, top, alts), True
        except Exception as e:
            fb = f"설명 생성 실패: {e}"   # 점수표만 표시해도 동작
    impact = [f"{top['id']}가 빠지면 {od['id']}의 후보가 줄어듦" for od in other_demands
              if od["id"] != demand["id"]
              and not violations(kind, od, next(c for c in candidates if c["id"] == top["id"]), ctx, cfg)]
    return AgentResult("dispatch", {
        "demand_id": demand["id"], "kind": kind, "top": top, "alternatives": alts,
        "reason": reason, "impact": impact, "excluded": excluded, "shortage": False,
        "constraint_status": "통과"},
        ai_used=ai_used, fallback_reason=fb, needs_approval=True,
        evidence=[f"{k}={v:.2f}" for k, v in top["items"].items()])


def plan(kind, demands, candidates, ctx, cfg, **kw):
    """긴급도(1이 가장 급함)순으로 탐욕 배치. 이미 1순위로 쓴 후보는 다음 수요에서 제외."""
    used, results = set(), []
    for d in sorted(demands, key=lambda d: (d.get("urgency", 4), d["id"])):
        r = recommend(kind, d, candidates, ctx, cfg, exclude=used, other_demands=demands, **kw)
        if r.output["top"]:
            used.add(r.output["top"]["id"])
        results.append(r)
    return results


class Board:
    """배치 보드: 승인·거절·수정·직접 지정과 이력(D20). 승인 전에는 아무 것도 반영되지 않는다."""
    def __init__(self, kind, candidates, ctx, cfg):
        self.kind, self.cands, self.ctx, self.cfg = kind, candidates, ctx, cfg
        self.recs, self.history, self.applied = {}, [], {}   # applied: demand_id -> candidate_id

    def submit(self, demand, result):
        self.recs[demand["id"]] = (demand, result, set())

    def _log(self, demand_id, action, by, **kw):
        self.history.append({"demand_id": demand_id, "action": action, "by": by, **kw})

    def approve(self, demand_id, by):
        demand, res, _ = self.recs[demand_id]
        top = res.output.get("top")
        if not top: raise ValueError("추천 후보가 없어 승인할 수 없음")
        candidate = next((c for c in self.cands if c["id"] == top["id"]), None)
        if candidate is None: raise ValueError("추천 후보를 후보 목록에서 찾을 수 없음")
        warnings = violations(self.kind, demand, candidate, self.ctx, self.cfg)
        if warnings: raise ValueError("추천 후보의 제약 위반으로 승인 불가: " + "; ".join(warnings))
        self.applied[demand_id] = top["id"]
        self._log(demand_id, "approve", by, chosen=self.applied[demand_id])

    def reject(self, demand_id, by, reason):
        """사유를 제약으로 반영(현재 1순위 제외)해 같은 수요를 재추천."""
        demand, res, excl = self.recs[demand_id]
        top = res.output.get("top")
        if not top: raise ValueError("제외할 추천 후보가 없음")
        excl = excl | {top["id"]}
        new = recommend(self.kind, demand, self.cands, self.ctx, self.cfg, exclude=excl)
        self.recs[demand_id] = (demand, new, excl)
        self._log(demand_id, "reject", by, reason=reason)
        return new

    def modify(self, demand_id, cand_id, by):
        demand, res, _ = self.recs[demand_id]
        top = res.output.get("top")
        if not top: raise ValueError("추천 후보가 없어 수정할 수 없음")
        pool = [top] + res.output.get("alternatives", [])
        chosen = next((r for r in pool if r["id"] == cand_id), None)
        if chosen is None: raise ValueError("수정 후보는 현재 추천 목록에 있어야 함")
        candidate = next((c for c in self.cands if c["id"] == cand_id), None)
        if candidate is None: raise ValueError("후보 목록에서 찾을 수 없음")
        warnings = violations(self.kind, demand, candidate, self.ctx, self.cfg)
        if warnings: raise ValueError("제약 위반 후보로 수정 불가: " + "; ".join(warnings))
        diff = round(top["score"] - chosen["score"], 4)
        self.applied[demand_id] = cand_id
        self._log(demand_id, "modify", by, chosen=cand_id, score_diff=diff, warnings=[])

    def direct(self, demand_id, cand_id, by, override=False, reason=None):
        """제약 위반은 명시적이고 감사 가능한 예외 승인 없이는 확정 불가."""
        demand, _, _ = self.recs[demand_id]
        candidate = next((c for c in self.cands if c["id"] == cand_id), None)
        if candidate is None: raise ValueError("후보 목록에서 찾을 수 없음")
        warnings = violations(self.kind, demand, candidate, self.ctx, self.cfg)
        if warnings and not override:
            raise ValueError("제약 위반으로 직접 지정 불가: " + "; ".join(warnings))
        if warnings and not (isinstance(reason, str) and reason.strip()):
            raise ValueError("제약 위반 예외 승인에는 사유가 필요함")
        self.applied[demand_id] = cand_id
        self._log(demand_id, "direct", by, chosen=cand_id, warnings=warnings,
                  override=bool(warnings and override), reason=reason if warnings else None)
        return warnings
