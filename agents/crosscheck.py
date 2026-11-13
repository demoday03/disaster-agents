"""신고 대조 에이전트. 신고에 적힌 심각도를 같은 구역 센서 값과 비교해, 과장(또는 축소)됐을 수 있는 신고를 담당자에게 알린다.
- 규칙만 쓴다(AI 없음). 결과는 참고용: 긴급도·배치 추천·승인은 바꾸지 않는다.
- 센서는 설치된 한 지점만 본다. 센서가 없는 구역, 끊긴 센서, 센서로 볼 수 없는 위험은 '확인 불가'로 둔다
  (센서가 조용하다고 위험이 없다는 뜻이 아니다).
- 인명·취약 가구가 걸린 신고는 결과와 관계없이 현장 확인을 먼저 권한다.
"""
import json

CONSISTENT, PARTIAL, NO_EVIDENCE, EXAGGERATED, UNDERSTATED, UNKNOWN = (
    "일치", "일부 일치", "센서 근거 없음", "과장 가능", "축소 가능", "확인 불가")


def load_cfg(path="config/crosscheck.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def claimed_level(typ, text, cfg):
    """신고 글의 심각도(1 가벼움, 2 심각)와 근거 표현. 심각도 표현이 없으면 1."""
    words = cfg["claim_words"].get(typ, {})
    for level in ("2", "1"):
        hit = [w for w in words.get(level, []) if w in text]
        if hit:
            return int(level), hit
    return 1, []


def sensor_level(rule, window, thresholds, cautions=None):
    """센서 쪽 심각도(0~2). 센서가 여러 개면 기준을 넘은 센서 수, 수심 센서처럼 하나에 주의 기준(cautions)이 있으면
    주의 기준 넘음 1 · 위험 기준 넘음 2. 값이 없는 센서가 있으면 None."""
    cautions = cautions or {}
    if len(rule["keys"]) == 1 and rule["keys"][0] in cautions:
        key = rule["keys"][0]
        w = window.get(key)
        if w is None:
            return None, [f"{key} 최근 값 없음"]
        t, c = thresholds[key], cautions[key]
        level = 2 if w["max"] > t else 1 if w["max"] > c else 0
        return level, [f"{key} 최대 {w['max']:g}(주의 {c:g} · 위험 {t:g} → {['기준 아래', '주의 기준 넘음', '위험 기준 넘음'][level]})"]
    over, detail = 0, []
    for key in rule["keys"]:
        w = window.get(key)
        if w is None:
            return None, [f"{key} 최근 값 없음"]
        hit = w["max"] > thresholds[key]
        over += hit
        detail.append(f"{key} 최대 {w['max']:g}(기준 {thresholds[key]:g}{' 넘음' if hit else ' 아래'})")
    return over, detail


def assess(report, window, thresholds, cfg, similar=0, window_sec=None, cautions=None):
    """report: 접수 결과(type·zone·text·urgency·people·vulnerable). window: {센서키: {'max','age_s'}} 최근 window_sec초.
    similar: 같은 구역·유형의 최근 다른 신고 수. 대조할 수 없는 유형이면 None."""
    typ, zone = report.get("type"), report.get("zone")
    rule = cfg["rules"].get(typ)
    if rule is None:
        return None
    window_sec = window_sec or cfg["window_sec"]
    claim, words = claimed_level(typ, report.get("text", ""), cfg)
    res = {"type": typ, "zone": zone, "claimed": claim, "claim_words": words, "sensor_level": None,
           "verdict": UNKNOWN, "reasons": [], "advice": "", "similar_reports": similar, "changes_urgency": False}
    if zone not in rule["zones"]:
        res["reasons"].append(f"{zone}에는 {typ} 센서가 없습니다(설치 구역: {', '.join(rule['zones'])})")
    else:
        level, detail = sensor_level(rule, window, thresholds, cautions)
        res["reasons"] += detail
        if level is None:
            res["reasons"].append(f"노드 {rule['node']}가 최근 {window_sec}초 동안 값을 보내지 않았습니다")
        else:
            res["sensor_level"] = level
            res["verdict"] = (CONSISTENT if level == claim else UNDERSTATED if level > claim
                              else PARTIAL if level > 0 else EXAGGERATED if claim == 2 else NO_EVIDENCE)
    res["reasons"].insert(0, f"신고 심각도 {'심각' if claim == 2 else '가벼움'}"
                             + (f"({', '.join(words)})" if words else "(심각도 표현 없음)"))
    if similar:
        res["reasons"].append(f"같은 구역·유형 신고가 최근 {similar}건 더 있습니다")
    res["advice"] = {
        EXAGGERATED: "센서는 신고만큼 심각하지 않습니다. 출동 전 신고자에게 물 높이·위치를 다시 확인하세요"
                     + ("(다른 신고도 있어 센서 위치와 다른 곳일 수 있음)" if similar else ""),
        NO_EVIDENCE: "센서에 반응이 없습니다. 센서 위치와 떨어진 곳일 수 있으니 위치를 확인하세요",
        PARTIAL: "센서는 초기 단계만 보입니다. 상황이 빠르게 바뀔 수 있어 계속 지켜보세요",
        UNDERSTATED: "센서가 신고보다 심각합니다. 신고자가 상황을 작게 말했을 수 있으니 우선 확인하세요",
        CONSISTENT: "센서와 신고가 맞습니다",
        UNKNOWN: "센서로 확인할 수 없습니다. 신고 내용과 현장 확인으로 판단하세요",
    }[res["verdict"]]
    if report.get("vulnerable") or (report.get("people") or 0) > 0:
        res["advice"] += " · 사람·취약 가구가 걸린 신고라 결과와 관계없이 현장 확인을 먼저 하세요"
    return res
