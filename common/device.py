"""접속 환경 감지: 스마트폰·태블릿·노트북/PC, 운영체제, 브라우저, 앱 안 브라우저(카카오톡 등).

- 서버가 받은 User-Agent와 브라우저가 알려 주는 힌트(Sec-CH-UA-Mobile·Sec-CH-UA-Platform)로 판단하고, 화면이 보낸
  값(터치 지점 수·화면 크기)으로 보정한다. 아이패드는 요즘 '맥(Macintosh)'이라고 자신을 소개하므로 터치가 되면 태블릿으로 본다.
- 저장하는 것은 이 결과(종류·운영체제·브라우저)뿐이다. User-Agent 원문과 IP는 저장하지 않는다.
- 어디까지나 추정이다(브라우저가 거짓으로 소개할 수 있다). 화면 안내와 담당자 참고용으로만 쓴다.
"""
import re

PHONE, TABLET, PC, UNKNOWN = "스마트폰", "태블릿", "노트북·PC", "알 수 없음"
ICON = {PHONE: "📱", TABLET: "📲", PC: "💻", UNKNOWN: "❔"}
KEY = {PHONE: "phone", TABLET: "tablet", PC: "pc", UNKNOWN: "unknown"}

# 앱 안 브라우저(링크를 앱 안에서 열었을 때). 위치 보내기·저장이 막히는 경우가 있어 주민 화면에서 안내한다.
INAPP = [("KAKAOTALK", "카카오톡"), ("NAVER(inapp", "네이버 앱"), ("Instagram", "인스타그램"), ("FBAN", "페이스북"),
         ("FBAV", "페이스북"), ("Line/", "라인"), ("DaumApps", "다음 앱"), ("everytimeApp", "에브리타임"), ("Telegram", "텔레그램")]
BROWSERS = [("SamsungBrowser", "삼성 인터넷"), ("Whale/", "네이버 웨일"), ("Edg", "엣지"), ("OPR/", "오페라"),
            ("FxiOS", "파이어폭스"), ("Firefox/", "파이어폭스"), ("CriOS", "크롬"), ("Chrome/", "크롬"), ("Safari/", "사파리")]
BOT = re.compile(r"bot|crawl|spider|curl|wget|python|headless|monitor|uptime|preview", re.I)


def _os(ua, platform):
    p = (platform or "").strip('" ').lower()
    if "iphone" in ua.lower() or "ipod" in ua.lower():
        return "iOS"
    if "ipad" in ua.lower():
        return "iPadOS"
    if "android" in ua.lower() or p == "android":
        return "Android"
    if "cros" in ua.lower() or p == "chrome os":
        return "ChromeOS"
    if "windows" in ua.lower() or p == "windows":
        return "Windows"
    if "macintosh" in ua.lower() or "mac os x" in ua.lower() or p == "macos":
        return "macOS"
    if "linux" in ua.lower() or p == "linux":
        return "Linux"
    return "알 수 없음"


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def detect(ua, mobile_hint=None, platform_hint=None, client=None):
    """ua: User-Agent, mobile_hint: Sec-CH-UA-Mobile('?1'/'?0'), platform_hint: Sec-CH-UA-Platform,
    client: 화면이 보낸 {touch: 터치 지점 수, w: 화면 너비, h: 화면 높이}(없어도 된다)."""
    ua = (ua or "")[:512]
    client = client if isinstance(client, dict) else {}
    touch, w, h = _int(client.get("touch")), _int(client.get("w")), _int(client.get("h"))
    os_ = _os(ua, platform_hint)
    inapp = next((name for key, name in INAPP if key in ua), None)
    browser = inapp or next((name for key, name in BROWSERS if key in ua), "알 수 없음")
    low = ua.lower()
    if not ua or BOT.search(ua):
        kind = UNKNOWN
    elif "ipad" in low or "tablet" in low or (os_ == "Android" and "mobile" not in low and mobile_hint != "?1"):
        kind = TABLET
    elif os_ == "macOS" and touch and touch > 1:   # 아이패드(데스크톱 사이트 모드)는 맥이라고 소개한다
        kind, os_ = TABLET, "iPadOS"
    elif mobile_hint == "?1" or "mobi" in low or os_ in ("iOS", "Android"):
        kind = PHONE
    elif os_ in ("Windows", "macOS", "Linux", "ChromeOS"):
        kind = PC
    else:
        kind = UNKNOWN
    if kind == PHONE and w and h and min(w, h) >= 700:   # 큰 화면(접는 폰을 펼친 것·태블릿)
        kind = TABLET
    label = f"{ICON[kind]} {kind}" + (f" · {os_}" if os_ != "알 수 없음" else "") + (f" · {browser}" if browser != "알 수 없음" else "")
    return {"kind": kind, "key": KEY[kind], "os": os_, "browser": browser, "inapp": bool(inapp), "label": label,
            "touch": bool(touch and touch > 0) if touch is not None else kind in (PHONE, TABLET)}


def stored(d):
    """신고에 남길 만큼만(라벨을 다시 만들 수 있게)."""
    return {k: d[k] for k in ("kind", "os", "browser", "inapp")}
