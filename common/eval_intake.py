"""신고 데이터셋(제안서 D8)으로 ② 접수의 분류·긴급도·병합을 잰다(목표는 11.2).
세트: data/eval/intake_reports.json. 긴급도 정답은 정답 필드에 intake.urgency 규칙을 적용한 값."""
import json
from collections import Counter, defaultdict
from agents import intake

TARGETS = {"type_accuracy": 0.9, "rescue_urgency1_rate": 1.0, "merge_accuracy": 0.9}


def load(path="data/eval/intake_reports.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def evaluate(data, cfg, client=None):
    """client: SolarClient(없으면 키워드 규칙 폴백만)."""
    gold = data["reports"]
    pred = [intake.classify(g["id"], g["text"], g["min"] * 60, cfg, client).output for g in gold]
    rows = []
    for g, p in zip(gold, pred):
        gu = intake.urgency(g["type"], g["vulnerable"], g["people"])
        rows.append({"id": g["id"], "gold_type": g["type"], "type": p["type"], "gold_zone": g["zone"],
                     "zone": p["zone"], "gold_urgency": gu, "urgency": p["urgency"], "hard": g.get("hard", []),
                     "ai": p["label"] is None})
    n = len(rows)
    rescue = [r for r in rows if r["gold_type"] == "인명구조"]

    # 병합: 정답 사건(event, 없으면 단독)과 접수함 묶음이 정확히 같은 구성인 비율
    events = defaultdict(set)
    for g in gold:
        events[g.get("event", g["id"])].add(g["id"])
    groups = [set(gr["members"]) for gr in intake.build_inbox(pred, cfg)]
    matched = sum(ev in groups for ev in events.values())
    multi = [ev for ev in events.values() if len(ev) > 1]

    confusion = Counter((r["gold_type"], r["type"]) for r in rows if r["gold_type"] != r["type"])
    hard = [r for r in rows if r["hard"]]
    return {
        "rows": rows,
        "type_accuracy": sum(r["type"] == r["gold_type"] for r in rows) / n,
        "zone_accuracy": sum(r["zone"] == r["gold_zone"] for r in rows) / n,
        "urgency_accuracy": sum(r["urgency"] == r["gold_urgency"] for r in rows) / n,
        "rescue_urgency1_rate": sum(r["urgency"] == 1 for r in rescue) / len(rescue),
        "merge_accuracy": matched / len(events),
        "multi_event_merged": sum(ev in groups for ev in multi) / len(multi),
        "hard_type_accuracy": sum(r["type"] == r["gold_type"] for r in hard) / len(hard),
        "under_urgent": [r["id"] for r in rows if r["urgency"] > r["gold_urgency"]],   # 덜 급하게 매긴 건
        "confusion": confusion,
        "ai_used": sum(r["ai"] for r in rows),
    }
