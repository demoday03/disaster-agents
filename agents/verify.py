"""⑦ 검증 에이전트. 안내문·지원제도 안내·필요 물품 안내가 근거 문서와 맞는지 확인한다.
1) 규칙 선검사: 근거에 없는 숫자는 바로 차단(오프라인에서도 동작)
2) 업스테이지 Groundedness Check: grounded / notGrounded / notSure
   Groundedness 모델(GROUNDEDNESS_MODEL)이 없으면 안내문을 쓰는 모델과 다른 판정 모델(기본 solar-pro3)이 같은 세 판정으로 판단한다.
실패·불확실은 모두 안전한 쪽(담당자 확인 또는 차단)으로 처리한다. 근거 문서 자체의 오류는 판단하지 못한다."""
import json, os, re
from common.llm import SolarClient
from common.schema import AgentResult

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_VERDICTS = {"grounded", "notGrounded", "notSure"}
# Groundedness 모델 대신 채팅 모델로 판정할 때의 지시문. 응답 형식은 Groundedness Check와 같게 맞춘다.
JUDGE_PROMPT = ("너는 재난 안내문 검증기다. [근거]에 있는 내용만 사실로 본다.\n"
                "[안내문]의 모든 주장(숫자·대상·기한·장소·행동 지시)이 [근거]로 뒷받침되면 grounded,\n"
                "근거와 어긋나거나 근거에 없는 내용이 하나라도 있으면 notGrounded,\n"
                "판단하기 어려우면 notSure.\n"
                "다른 말 없이 grounded, notGrounded, notSure 중 한 단어만 출력하라.")


def load_cfg(path="config/verify.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def make_client(**kw):
    """검증용 SolarClient.
    GROUNDEDNESS_MODEL(진짜 Groundedness 모델)이 있으면 그 모델로, 없으면 판정 모델(VERIFY_MODEL, 기본은
    config/verify.json의 judge_model=solar-pro3)이 지시문을 받아 판정한다. 안내문을 쓰는 모델과 다른 모델을 써서
    같은 모델이 자기 초안을 통과시키는 일을 줄인다. client.same_as_writer로 같은 모델인지 알려 준다."""
    grounded = os.getenv("GROUNDEDNESS_MODEL", "").strip()
    model = grounded or os.getenv("VERIFY_MODEL", "").strip() or load_cfg().get("judge_model")
    client = SolarClient(model=model or None, **kw)
    client.verify_mode = "groundedness" if grounded else "judge"
    client.same_as_writer = client.model == os.getenv("SOLAR_MODEL", "solar-pro4")
    return client


def _messages(client, draft, evidence):
    if getattr(client, "verify_mode", "groundedness") == "judge":
        return [{"role": "system", "content": JUDGE_PROMPT},
                {"role": "user", "content": f"[근거]\n{evidence}\n\n[안내문]\n{draft}"}]
    # Groundedness Check: 근거 발췌를 사용자 메시지로, 검증할 초안을 어시스턴트 메시지로
    return [{"role": "user", "content": evidence}, {"role": "assistant", "content": draft}]


def _nums(t):
    return {n.replace(",", "") for n in _NUM.findall(t)}


def verify_text(draft, evidence, client=None, max_chars=2000):
    """(verdict, reason). client는 make_client()로 만든 SolarClient(Groundedness 모델 또는 채팅 모델 판정)."""
    bad = sorted(_nums(draft) - _nums(evidence))
    if bad:
        return "notGrounded", f"근거에 없는 숫자: {', '.join(bad)}"
    if client is None:
        return "notSure", "근거 검증 클라이언트 없음"
    try:
        raw = client.chat(_messages(client, draft, evidence[:max_chars])).strip()
    except Exception as e:
        return "notSure", f"검증 호출 실패: {e}"
    # 부분 문자열 검사는 "not grounded"·"ungrounded"를 grounded로 오판한다(fail-open).
    # 응답 전체가 정확히 세 판정 중 하나일 때만 인정하고, 나머지는 모두 notSure.
    token = raw.strip().strip("\"'`.").strip()
    if token not in _VERDICTS:
        return "notSure", "판정 해석 불가"
    v = token
    if v == "grounded" and len(evidence) > max_chars:
        return "notSure", "근거 발췌가 길이 제한을 넘어 일부만 확인됨"
    return v, "근거 검증 결과"


def as_verifier(client, max_chars=2000):
    """③·⑧ 등이 쓰는 verifier(draft, evidence) -> verdict 어댑터."""
    return lambda draft, evidence: verify_text(draft, evidence, client, max_chars)[0]


def verify(draft, doc_ids, docs, client=None, max_chars=2000):
    """문서 ID로 근거를 모아 검증. grounded만 승인 대기열로 보낸다."""
    found = [i for i in doc_ids if i in docs]
    if not found:
        verdict, reason = "notSure", "근거 문서 ID를 찾지 못함"
    else:
        verdict, reason = verify_text(draft, "\n".join(docs[i] for i in found), client, max_chars)
    route = {"grounded": "승인 대기열", "notGrounded": "차단", "notSure": "담당자 확인 요청"}[verdict]
    return AgentResult("verify", {"verdict": verdict, "reason": reason, "route": route,
                                  "doc_ids": found, "blocked": verdict == "notGrounded"},
                       ai_used=client is not None, needs_approval=(verdict == "grounded"), evidence=found)
