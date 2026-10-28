"""⑦ 검증 에이전트. 안내문·지원제도 안내·필요 물품 안내가 근거 문서와 맞는지 확인한다.
1) 규칙 선검사: 근거에 없는 숫자는 바로 차단(오프라인에서도 동작)
2) 업스테이지 Groundedness Check: grounded / notGrounded / notSure
실패·불확실은 모두 안전한 쪽(담당자 확인 또는 차단)으로 처리한다. 근거 문서 자체의 오류는 판단하지 못한다."""
import re
from common.schema import AgentResult

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_VERDICTS = {"grounded", "notGrounded", "notSure"}


def _nums(t):
    return {n.replace(",", "") for n in _NUM.findall(t)}


def verify_text(draft, evidence, client=None, max_chars=2000):
    """(verdict, reason). client는 Groundedness 모델로 만든 SolarClient."""
    bad = sorted(_nums(draft) - _nums(evidence))
    if bad:
        return "notGrounded", f"근거에 없는 숫자: {', '.join(bad)}"
    if client is None:
        return "notSure", "근거 검증 클라이언트 없음"
    try:   # 근거 발췌를 사용자 메시지로, 검증할 초안을 어시스턴트 메시지로
        raw = client.chat([{"role": "user", "content": evidence[:max_chars]},
                           {"role": "assistant", "content": draft}]).strip()
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
