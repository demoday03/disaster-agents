"""기상청 공공데이터(초단기실황·기상특보)를 상황판 참고 정보로 읽는다.

- 키: 환경변수 KMA_API_KEY(공공데이터포털 일반 인증키, Decoding). 없거나 호출이 실패하면 available=False로 돌려주고
  나머지 기능은 그대로 동작한다(인터넷이 끊긴 시연장에서도 멈추지 않게).
- 10분 동안은 같은 결과를 다시 쓴다(공공데이터 호출 한도 보호, 화면이 15초마다 새로 고쳐도 호출은 10분에 한 번).
- 이 값은 참고 정보다. 위기 단계는 바꾸지 않고, 강우 단계도 담당자가 '적용'을 눌러야 감시 에이전트에 들어간다.
"""
import json, math, os, threading, time, urllib.parse, urllib.request
from datetime import datetime, timedelta

BASE = "http://apis.data.go.kr/1360000"
GEOJE = (34.8806, 128.6211)   # 거제시청(격자 변환용)
AREA_WORDS = ("거제", "경상남도", "경남")
TTL_S = 600
# 기상청 비 강도 표현(약한 비 3mm/h 미만, 보통 비 3~15, 강한 비 15~30, 매우 강한 비 30 이상) → 감시 에이전트 강우 단계 0~4
RAIN_STAGES = [(30, 4, "매우 강한 비"), (15, 3, "강한 비"), (3, 2, "보통 비"), (0.1, 1, "약한 비"), (0, 0, "비 없음")]


def grid(lat, lon):
    """위경도 → 기상청 동네예보 격자(nx, ny). 기상청 LCC 투영 공식."""
    re_, grid_, s1, s2, olon, olat, xo, yo = 6371.00877, 5.0, 30.0, 60.0, 126.0, 38.0, 43, 136
    d = math.pi / 180.0
    re_ /= grid_
    s1, s2, olon, olat = s1 * d, s2 * d, olon * d, olat * d
    sn = math.log(math.cos(s1) / math.cos(s2)) / math.log(math.tan(math.pi / 4 + s2 / 2) / math.tan(math.pi / 4 + s1 / 2))
    sf = math.tan(math.pi / 4 + s1 / 2) ** sn * math.cos(s1) / sn
    ro = re_ * sf / math.tan(math.pi / 4 + olat / 2) ** sn
    ra = re_ * sf / math.tan(math.pi / 4 + lat * d / 2) ** sn
    th = lon * d - olon
    th = th - 2 * math.pi if th > math.pi else th + 2 * math.pi if th < -math.pi else th
    th *= sn
    return int(ra * math.sin(th) + xo + .5), int(ro - ra * math.cos(th) + yo + .5)


def rain_stage(mm):
    for limit, stage, label in RAIN_STAGES:
        if mm >= limit:
            return stage, label
    return 0, "비 없음"


def _rn1(v):
    """RN1(1시간 강수량)은 '강수없음'·'1mm 미만'·'30.0~50.0mm' 같은 글자로 올 수 있다."""
    s = str(v).replace("mm", "").strip()
    if s in ("", "-", "강수없음", "0"):
        return 0.0
    if "미만" in s:
        return 0.5
    try:
        return float(s.split("~")[0])
    except ValueError:
        return 0.0


def _get(op, params, key, fetch):
    q = urllib.parse.urlencode({"serviceKey": key, "pageNo": 1, "dataType": "JSON", **params})
    data = fetch(f"{BASE}/{op}?{q}")
    head = data.get("response", {}).get("header", {})
    if head.get("resultCode") not in ("00", "03"):   # 03: 데이터 없음
        raise ValueError(f"기상청 응답 {head.get('resultCode')} {head.get('resultMsg', '')}")
    items = data.get("response", {}).get("body", {}).get("items", {})
    return items.get("item", []) if isinstance(items, dict) else (items or [])


def _fetch(url):
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


class WeatherFeed:
    def __init__(self, key=None, fetch=_fetch, clock=time.time, now=datetime.now):
        self.key = os.getenv("KMA_API_KEY", "") if key is None else key
        self.fetch, self.clock, self.now = fetch, clock, now
        self.lock, self.cache = threading.Lock(), None

    def status(self):
        with self.lock:
            if self.cache and self.clock() - self.cache["fetched"] < TTL_S:
                return self.cache["data"]
            data = self._load()
            self.cache = {"fetched": self.clock(), "data": data}
            return data

    def _load(self):
        if not self.key:
            return {"available": False, "reason": "기상청 인증키(KMA_API_KEY)가 없어 실제 날씨를 표시하지 않습니다"}
        out = {"available": True, "source": "기상청 초단기실황·기상특보(공공데이터포털)", "area": "거제시"}
        try:   # 초단기실황: 매시 정각 자료가 40분쯤 뒤에 나오므로 45분 전 기준 시각으로 조회
            base = self.now() - timedelta(minutes=45)
            nx, ny = grid(*GEOJE)
            items = _get("VilageFcstInfoService_2.0/getUltraSrtNcst", {
                "numOfRows": 20, "base_date": base.strftime("%Y%m%d"), "base_time": base.strftime("%H00"), "nx": nx, "ny": ny},
                self.key, self.fetch)
            obs = {i["category"]: i["obsrValue"] for i in items}
            mm = _rn1(obs.get("RN1", 0))
            stage, label = rain_stage(mm)
            out["rain"] = {"mm_per_h": mm, "stage": stage, "label": label, "temp_c": obs.get("T1H"),
                           "at": base.strftime("%Y-%m-%d %H:00")}
        except Exception as e:
            out["rain"] = None
            out["rain_error"] = f"강수 정보를 받지 못함({type(e).__name__})"
        try:   # 특보 현황: 지금 발효 중인 특보 글에서 거제·경남이 들어간 줄만 뽑는다
            items = _get("WthrWrnInfoService/getPwnStatus", {"numOfRows": 5}, self.key, self.fetch)
            cur = items[0] if items else {}
            lines = [ln.strip(" o\r") for ln in str(cur.get("t6", "")).split("\n")]
            out["warnings"] = [ln for ln in lines if ln and any(w in ln for w in AREA_WORDS)]
            out["warnings_at"] = cur.get("tmFc")
        except Exception as e:
            out["warnings"] = None
            out["warnings_error"] = f"특보 정보를 받지 못함({type(e).__name__})"
        if out["rain"] is None and out["warnings"] is None:
            return {"available": False, "reason": out["rain_error"]}
        return out
