"""접속 환경 감지(스마트폰·태블릿·노트북/PC) 시험. 실행: python -m unittest tests.test_device -v"""
import unittest

from common.device import detect

IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
GALAXY_SAMSUNG = ("Mozilla/5.0 (Linux; Android 14; SM-S918N) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/25.0 "
                  "Chrome/121.0.0.0 Mobile Safari/537.36")
ANDROID_CHROME = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
GALAXY_TAB = "Mozilla/5.0 (Linux; Android 13; SM-X710N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
IPAD_DESKTOP = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"
WIN_CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
WIN_EDGE = WIN_CHROME + " Edg/124.0.2478.67"
WIN_WHALE = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Whale/3.25.232.19 Safari/537.36"
CHROMEBOOK = "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
KAKAO = ("Mozilla/5.0 (Linux; Android 14; SM-S918N Build/UP1A; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
         "Chrome/124.0.0.0 Mobile Safari/537.36;KAKAOTALK 2410530")
NAVER = IPHONE + " NAVER(inapp; search; 2000; 12.5.3)"


class TestDevice(unittest.TestCase):
    def check(self, ua, kind, os_, browser, **kw):
        d = detect(ua, **kw)
        self.assertEqual((d["kind"], d["os"], d["browser"]), (kind, os_, browser), ua)
        return d

    def test_phones(self):
        self.check(IPHONE, "스마트폰", "iOS", "사파리")
        self.check(GALAXY_SAMSUNG, "스마트폰", "Android", "삼성 인터넷")
        self.check(ANDROID_CHROME, "스마트폰", "Android", "크롬")
        self.check(WIN_CHROME, "스마트폰", "Windows", "크롬", mobile_hint="?1")   # 브라우저가 '모바일'이라고 알려 주면 따른다

    def test_tablets(self):
        self.check(GALAXY_TAB, "태블릿", "Android", "크롬")
        self.check(IPAD_DESKTOP, "노트북·PC", "macOS", "사파리")                         # 터치 정보가 없으면 맥으로 본다
        self.check(IPAD_DESKTOP, "태블릿", "iPadOS", "사파리", client={"touch": 5})        # 터치가 되는 '맥' = 아이패드
        self.check(ANDROID_CHROME, "태블릿", "Android", "크롬", client={"touch": 5, "w": 800, "h": 1280})   # 큰 화면

    def test_computers(self):
        self.check(WIN_CHROME, "노트북·PC", "Windows", "크롬")
        self.check(WIN_EDGE, "노트북·PC", "Windows", "엣지")
        self.check(WIN_WHALE, "노트북·PC", "Windows", "네이버 웨일")
        self.check(CHROMEBOOK, "노트북·PC", "ChromeOS", "크롬")
        self.check(IPAD_DESKTOP, "노트북·PC", "macOS", "사파리", client={"touch": 0})

    def test_in_app_and_unknown(self):
        d = self.check(KAKAO, "스마트폰", "Android", "카카오톡")
        self.assertTrue(d["inapp"])
        self.assertTrue(self.check(NAVER, "스마트폰", "iOS", "네이버 앱")["inapp"])
        self.assertFalse(detect(IPHONE)["inapp"])
        for ua in ("", "curl/8.5.0", "python-urllib/3.12", "Googlebot/2.1"):
            self.assertEqual(detect(ua)["kind"], "알 수 없음")
        self.assertEqual(detect(IPHONE)["label"], "📱 스마트폰 · iOS · 사파리")
        self.assertEqual(detect(IPHONE, client={"touch": "x", "w": None})["kind"], "스마트폰")   # 이상한 값은 무시


if __name__ == "__main__":
    unittest.main()
