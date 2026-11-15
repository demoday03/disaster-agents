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


# ---------------------------------------------------------------- 같은 건물(아파트) 알아보기
# 이웃이 같은 아파트에서 같은 일을 신고하면 한 건으로 합치려고, 위치 글에서 아파트 단지 이름과 동을 뽑는다.
# 단지 이름만 같아도 같은 아파트로 보고, 둘 다 동이 적혀 있는데 다르면(101동·105동) 다른 건물로 본다.
_GENERIC = "아파트|빌라|맨션|오피스텔|연립|주공|단지|기숙사|빌리지|하이츠|타운"
_BRAND = "e편한세상|이편한세상|힐스테이트|푸르지오|래미안|자이|아이파크|더샵|롯데캐슬|캐슬|스위첸|데시앙|휴먼시아|센트레빌|리슈빌|해링턴"
_TAIL = r"(?:\s?(\d{1,2})\s?(차|단지))?(?:\s*(\d{1,4}|[A-Za-z]|[가-하])\s?동)?"
_LOOSE = re.compile(rf"(?:([가-힣A-Za-z0-9]{{1,12}})\s?)?({_BRAND}|{_GENERIC}){_TAIL}", re.I)    # '위치: …' 칸: 띄어 써도
_STRICT = re.compile(rf"(?:([가-힣A-Za-z0-9]{{2,12}}))?({_BRAND}|{_GENERIC}){_TAIL}", re.I)        # 그 밖의 글: 붙여 쓴 이름만
_NOT_NAME = {"우리", "저희", "옆", "이", "그", "저", "앞", "뒤", "근처", "건너편", "같은", "이웃", "옆집", "주변", "동네", "위치"}
_GPS = re.compile(r"GPS\s*(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)(?:\s*\(오차 약\s*(\d+)\s*m\))?")


def _find(text, pattern):
    for m in pattern.finditer(text):
        name, kind, num, unit, dong = m.group(1) or "", m.group(2), m.group(3), m.group(4), m.group(5) or ""
        is_brand = re.fullmatch(_BRAND, kind, re.I) is not None
        if name in _NOT_NAME or name.endswith(("동네", "근처")):
            name = ""
        if not name and not is_brand:   # '아파트' 한 낱말만으로는 어느 아파트인지 모른다
            continue
        sub = f"{num}{unit}" if num else ""
        full = re.sub(r"\s+", "", f"{name}{kind}{sub}").lower()
        label = f"{name}{kind}{' ' + sub if sub else ''}{' ' + dong.upper() + '동' if dong else ''}".strip()
        return {"name": full, "dong": dong.upper(), "label": label}
    return None


def building(text):
    """위치 글 → {'name': 단지 이름, 'dong': 동(없으면 ''), 'label': '한빛아파트 101동'} 또는 None.
    '위치: …' 칸이 있으면 그 칸에서 먼저 찾고(띄어 쓴 '한빛 아파트'도), 없으면 글 전체에서 붙여 쓴 이름만 찾는다
    ('물이 넘쳐서 아파트'를 '넘쳐서아파트'로 읽지 않게)."""
    text = text or ""
    m = re.search(r"위치\s*:\s*([^·\n]+)", text)
    return (_find(m.group(1), _LOOSE) if m else None) or _find(text, _STRICT)


def same_building(a, b):
    if not a or not b or a["name"] != b["name"]:
        return False
    return not (a["dong"] and b["dong"] and a["dong"] != b["dong"])


def gps(text):
    """글 속 'GPS 위도, 경도 (오차 약 Nm)' → (위도, 경도, 오차 m 또는 None) 또는 None."""
    m = _GPS.search(text or "")
    return (float(m.group(1)), float(m.group(2)), int(m.group(3)) if m.group(3) else None) if m else None


def near(a, b, meters=40, max_error=50):
    """두 GPS 위치가 같은 건물이라고 볼 만큼 가까운지(둘 다 오차를 알고 max_error 이하일 때만)."""
    if not a or not b or a[2] is None or b[2] is None or a[2] > max_error or b[2] > max_error:
        return False
    import math
    dy = (a[0] - b[0]) * 111_320
    dx = (a[1] - b[1]) * 111_320 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy) <= meters
