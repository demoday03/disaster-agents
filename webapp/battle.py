"""사람 vs AI 신고 분류 대결(심사 피드백: 기대효과를 숫자로).

1차 규칙 조정이 끝난 뒤 새로 쓴 별도 신고 세트(data/eval/intake_holdout.json, 2차 보강 때 일부 참고)의 앞 20건을, 사람은 화면에서 유형을 하나씩 골라 분류하고
AI는 실제 서비스와 같은 ② 접수 에이전트로 분류한다. 정답과 비교해 정확도·걸린 시간·인명구조를 놓쳤는지를 나란히 보여 준다.
- AI 결과는 모드(Solar 모델 또는 규칙)마다 한 번만 계산해 DB에 둔다(같은 신고로 Solar를 반복 호출하지 않게).
- 사람 기록은 별명과 함께 DB에 남겨 팀원 여러 명의 평균을 볼 수 있다. 개인정보는 받지 않는다.
- 가상 신고에 대한 시연용 측정이다. 실제 재난 대응 성능을 보증하지 않는다.
"""
import json, re, threading, time

from agents import intake

DATA = "data/eval/intake_holdout.json"
COUNT = 20
_lock = threading.Lock()   # AI 계산은 한 번에 하나만(동시에 눌러도 Solar를 두 번 부르지 않게)


def items():
    with open(DATA, encoding="utf-8") as f:
        return json.load(f)["reports"][:COUNT]


def questions():
    cfg = intake.load_cfg()
    return {"items": [{"id": r["id"], "text": r["text"]} for r in items()], "types": cfg["types"],
            "guide": cfg.get("type_guide", {}),
            "note": "가상 신고 20건(1차 조정 뒤 새로 쓴 별도 세트, 2차 보강 때 일부 참고). 가상 데이터로 잰 시연용 측정입니다."}


def _mode_key(llm):
    return f"solar:{llm.model}" if llm is not None else "rules"


def ai_result(db, llm):
    """같은 20건을 ② 접수 에이전트로 분류한 결과(처음 한 번만 계산)."""
    key = _mode_key(llm)
    cached = db.get_record("battle_ai", key)
    if cached:
        return cached
    with _lock:
        cached = db.get_record("battle_ai", key)
        if cached:
            return cached
        cfg, rows, total = intake.load_cfg(), [], 0.0
        for r in items():
            t0 = time.perf_counter()
            out = intake.classify("B-" + r["id"], r["text"], time.time(), cfg, client=llm)
            ms = (time.perf_counter() - t0) * 1000
            total += ms
            rows.append({"id": r["id"], "type": out.output["type"], "ms": round(ms, 1), "ai_used": out.ai_used})
        res = {"mode": "AI(Solar " + llm.model + ")" if llm is not None else "규칙(AI 미사용)", "rows": rows,
               "total_ms": round(total, 1), "at": time.time()}
        return db.put_record("battle_ai", key, res)


def score(answers):
    gold = {r["id"]: r for r in items()}
    correct = sum(1 for i, r in gold.items() if answers.get(i) == r["type"])
    rescue = [i for i, r in gold.items() if r["type"] == "인명구조"]
    missed = [i for i in rescue if answers.get(i) != "인명구조"]
    return {"correct": correct, "total": len(gold), "accuracy": round(correct / len(gold), 3),
            "rescue_total": len(rescue), "rescue_missed": missed}


def submit(db, llm, data):
    """사람 답안을 채점하고 같은 20건의 AI 결과와 비교한다."""
    gold = {r["id"]: r for r in items()}
    types = set(intake.load_cfg()["types"])
    answers = data.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(gold) or any(v not in types for v in answers.values()):
        raise ValueError("20건 모두 유형을 골라 주세요")
    elapsed = data.get("elapsed_ms")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not 1000 <= elapsed <= 3_600_000:
        raise ValueError("걸린 시간이 올바르지 않습니다")
    name = data.get("name") or "익명"
    if not isinstance(name, str) or not re.fullmatch(r"[\w가-힣 ._-]{1,20}", name.strip()):
        raise ValueError("별명은 20자 이하로 적어 주세요")
    human = {**score(answers), "total_ms": round(elapsed), "name": name.strip(), "at": time.time()}
    rid = f"HB-{int(time.time() * 1000)}"
    db.put_record("battle_human", rid, human)
    ai = ai_result(db, llm)
    ai_answers = {r["id"]: r["type"] for r in ai["rows"]}
    return {"human": human, "ai": {**score(ai_answers), "total_ms": ai["total_ms"], "mode": ai["mode"]},
            "rows": [{"id": i, "text": r["text"], "gold": r["type"], "human": answers[i], "ai": ai_answers.get(i)}
                     for i, r in gold.items()],
            "history": history(db)}


def history(db):
    runs = sorted(db.list_records("battle_human"), key=lambda x: -x.get("at", 0))
    keep = ("name", "correct", "total", "total_ms", "rescue_missed", "at")
    out = {"runs": [{k: x.get(k) for k in keep} for x in runs[:30]], "count": len(runs)}
    if runs:
        out["avg_accuracy"] = round(sum(x["correct"] / x["total"] for x in runs) / len(runs), 3)
        out["avg_seconds"] = round(sum(x["total_ms"] for x in runs) / len(runs) / 1000, 1)
    return out
