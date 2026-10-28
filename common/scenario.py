"""④ 배치 추천에 쓰는 가상 시나리오(제안서 D1~D5)를 읽고, 신고를 배치 수요로, 자원을 후보로 바꾼다.
현재 투입 중인 자원·대피소 입소 인원은 DB의 승인 기록에서 계산해 넘겨받는다(여기서는 DB를 읽지 않는다)."""
import json

KINDS = {"public": "공공 인력", "shelter": "대피소"}
SHELTER_AMENITIES = ("엘리베이터", "경사로", "의료 공간")


def load(zones_path="data/scenario/zones.json", resources_path="data/scenario/resources.json"):
    with open(zones_path, encoding="utf-8") as f:
        z = json.load(f)
    with open(resources_path, encoding="utf-8") as f:
        r = json.load(f)
    return {"zones": z["zones"], "travel_rule": z["travel_rule"], "control_zones": z["control_zones"],
            "type_skills": r["type_skills"], "public": r["public"], "shelters": r["shelters"]}


def travel_ctx(sc):
    """dispatch.recommend가 쓰는 {'travel': {(출발, 도착): {'minutes', 'via_blocked'}}}."""
    rule, pos = sc["travel_rule"], {z["id"]: (z["row"], z["col"]) for z in sc["zones"]}
    blocked = {frozenset(c["between"]) for c in sc["control_zones"] if c.get("active")}
    travel = {}
    for a, (ra, ca) in pos.items():
        for b, (rb, cb) in pos.items():
            minutes = rule["same_zone_min"] if a == b else rule["base_min"] + rule["per_cell_min"] * (abs(ra - rb) + abs(ca - cb))
            travel[(a, b)] = {"minutes": minutes, "via_blocked": frozenset((a, b)) in blocked}
    return {"travel": travel}


def _incident_zone(output, sc):
    zone = output.get("zone")
    if zone not in {z["id"] for z in sc["zones"]}:
        raise ValueError("구역이 확인되지 않은 신고입니다. 구역을 확인한 뒤 배치를 추천하세요")
    return zone


def public_demand(output, sc):
    typ = output.get("type")
    if typ == "물품 요청":
        raise ValueError("물품 요청은 물품 수급(⑧)에서 처리합니다")
    if typ not in sc["type_skills"]:
        raise ValueError(f"배치 대상이 아닌 유형입니다: {typ}")
    return {"id": output["id"], "zone": _incident_zone(output, sc), "skills": sc["type_skills"][typ],
            "urgency": output.get("urgency", 4)}


def shelter_demand(output, sc, people, amenities):
    if isinstance(people, bool) or not isinstance(people, int) or not 1 <= people <= 200:
        raise ValueError("대피 인원은 1~200 사이 정수여야 합니다")
    if not isinstance(amenities, list) or any(a not in SHELTER_AMENITIES for a in amenities):
        raise ValueError(f"편의시설은 {list(SHELTER_AMENITIES)} 중에서 골라야 합니다")
    return {"id": output["id"], "zone": _incident_zone(output, sc), "people": people,
            "amenities": sorted(set(amenities))}


def public_candidates(sc, busy_ids):
    return [{**p, "available": p["id"] not in busy_ids} for p in sc["public"]]


def shelter_candidates(sc, assigned_people):
    """assigned_people: {대피소 ID: 승인된 배치 인원 합}."""
    return [{**s, "capacity_left": s["capacity"] - s["occupied"] - assigned_people.get(s["id"], 0)}
            for s in sc["shelters"]]


def names(sc):
    return {x["id"]: x["name"] for x in sc["public"] + sc["shelters"]}
