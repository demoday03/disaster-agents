"""사람이 직접 쓴 위치(주소·건물 이름)를 다루는 도우미.

화면에서는 구역을 고르지 않고 위치를 글로 쓴다. 배치·물품·봉사 추천 규칙은 여전히 시연용 관리 구역(zones.json)으로
거리를 계산하므로, 글에서 관리 구역을 추정해 함께 저장한다.
- 'Z3'·'3구역'처럼 구역이 적혀 있거나, 가상 장소 이름(zones.json의 places)이 하나의 구역만 가리키면 그 구역.
- 그 밖에는 '미상'. 실제 지명(동·도로명)은 가상 구역에 연결하지 않는다(가상 위험 정보를 실제 동네에 붙이지 않게).
  미상인 위치는 담당자가 확인해 분류를 고친다.
"""
import json, re

from common.masking import mask

UNKNOWN = "미상"
MAX_ADDRESS = 100
_ZONES = None


def zones(path="data/scenario/zones.json"):
    global _ZONES
    if _ZONES is None:
        with open(path, encoding="utf-8") as f:
            _ZONES = json.load(f)["zones"]
    return _ZONES


def explicit_zones(text):
    """글에 적힌 구역 표기('Z3', '3구역')."""
    text = text or ""
    return {f"Z{int(n)}" for n in re.findall(r"Z\s?(\d+)", text) + re.findall(r"(\d+)\s*구역", text)}


def infer_zone(text):
    """위치 글 → 관리 구역 ID 또는 '미상'. 서로 다른 구역을 가리키는 단서가 둘 이상이면 추측하지 않는다."""
    text = text or ""
    ids = {z["id"] for z in zones()}
    found = explicit_zones(text) & ids
    if len(found) == 1:
        return found.pop()
    if found:
        return UNKNOWN
    hits = {z["id"] for z in zones() if any(p in text for p in z.get("places", []))}
    return hits.pop() if len(hits) == 1 else UNKNOWN


def describe(zone_id):
    """관리 구역의 장소 설명('하천 하류 주거지'). 미상이면 '위치 확인 필요'."""
    z = next((z for z in zones() if z["id"] == zone_id), None)
    if z is None:
        return "위치 확인 필요"
    m = re.search(r"\(([^)]+)\)", z["name"])
    return m.group(1) if m else z["name"]


def clean_address(value, label="위치", required=True):
    """직접 쓴 위치를 검사해 돌려준다. 전화번호 같은 개인정보는 가린다."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"{label}를 적어 주세요")
        return ""
    if not isinstance(value, str) or len(value) > MAX_ADDRESS:
        raise ValueError(f"{label}는 {MAX_ADDRESS}자 이하 글로 적어 주세요")
    return mask(" ".join(value.split()))


def resolve(data, label="위치", required=True):
    """요청 본문의 zone(관리 구역 ID, 담당자·시연용)과 address(직접 쓴 위치)로 (구역, 주소)를 정한다.
    구역 ID가 오면 그것을 쓰고, 없으면 주소에서 추정한다. 둘 다 없으면 오류."""
    ids = {z["id"] for z in zones()}
    zone, address = data.get("zone"), clean_address(data.get("address"), label, required=False)
    if zone not in (None, ""):
        if zone not in ids:
            raise ValueError(f"{label}를 다시 확인해 주세요")
        return zone, address
    if not address:
        if required:
            raise ValueError(f"{label}를 적어 주세요")
        return UNKNOWN, ""
    return infer_zone(address), address
