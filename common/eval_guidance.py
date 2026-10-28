"""오류 안내문 세트(제안서 11.3)로 ⑦ 검증의 차단·보류율을 잰다(목표 90%, 11.2).
세트: data/eval/guidance_errors.json. 근거 문장은 config의 안내 조각·지원제도 규칙에서 ID로 모은다."""
import json
from agents import verify

TARGET = 0.9


def load(path="data/eval/guidance_errors.json", guidance="config/guidance.json",
         rules="config/support_rules.json"):
    """(세트, {근거 ID: 문장})"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    with open(guidance, encoding="utf-8") as f:
        docs = {fr["id"]: fr["text"] for fr in json.load(f)["fragments"]}
    with open(rules, encoding="utf-8") as f:
        docs.update({r["id"]: r["content"] for r in json.load(f)["rules"]})
    return data, docs


def evaluate(client, data, docs):
    """client: Groundedness 모델 SolarClient(없으면 숫자 선검사만, 나머지는 notSure)."""
    def run(items):
        return [{"id": it["id"], "category": it.get("category", "대조군"),
                 "verdict": verify.verify_text(it["draft"], "\n".join(docs[i] for i in it["evidence"]),
                                               client)[0]} for it in items]
    errors, controls = run(data["errors"]), run(data["controls"])
    stopped = sum(r["verdict"] != "grounded" for r in errors)
    passed = sum(r["verdict"] == "grounded" for r in controls)
    return {"errors": errors, "controls": controls,
            "block_rate": stopped / len(errors),           # notGrounded + notSure
            "hard_block_rate": sum(r["verdict"] == "notGrounded" for r in errors) / len(errors),
            "control_pass_rate": passed / len(controls)}
