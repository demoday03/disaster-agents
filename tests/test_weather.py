"""기상청 날씨 연결(webapp/weather.py) 시험. 실제 API는 부르지 않고 가짜 응답을 쓴다."""
import unittest
from datetime import datetime
from webapp import weather as W


def resp(items, code="00"):
    return {"response": {"header": {"resultCode": code, "resultMsg": "x"}, "body": {"items": {"item": items}}}}


def fake(rn1="12.5", t6="o 호우경보 : 경상남도(거제, 통영)\r\no 풍랑주의보 : 남해동부먼바다", fail=None):
    calls = []
    def fetch(url):
        calls.append(url)
        if fail and fail in url:
            raise OSError("network down")
        if "getUltraSrtNcst" in url:
            return resp([{"category": "RN1", "obsrValue": rn1}, {"category": "T1H", "obsrValue": "21.0"}])
        return resp([{"t6": t6, "tmFc": "202608170400"}])
    return fetch, calls


class TestWeather(unittest.TestCase):
    def test_geoje_grid(self):
        self.assertEqual(W.grid(*W.GEOJE), (90, 69))   # 기상청 격자(실제 호출로 확인한 값)

    def test_rain_stage_follows_kma_intensity(self):
        self.assertEqual([W.rain_stage(x)[0] for x in (0, 0.5, 3, 14.9, 15, 29.9, 30, 80)], [0, 1, 2, 2, 3, 3, 4, 4])
        self.assertEqual(W._rn1("강수없음"), 0.0)
        self.assertEqual(W._rn1("1mm 미만"), 0.5)
        self.assertEqual(W._rn1("30.0~50.0mm"), 30.0)

    def test_status_parses_rain_and_local_warnings(self):
        fetch, calls = fake()
        w = W.WeatherFeed(key="k", fetch=fetch, now=lambda: datetime(2026, 8, 17, 4, 50)).status()
        self.assertTrue(w["available"])
        self.assertEqual((w["rain"]["mm_per_h"], w["rain"]["stage"], w["rain"]["at"]), (12.5, 2, "2026-08-17 04:00"))
        self.assertEqual(w["warnings"], ["호우경보 : 경상남도(거제, 통영)"])   # 거제와 상관없는 먼바다 특보는 뺀다
        self.assertIn("nx=90", calls[0])

    def test_no_key_and_cache_and_partial_failure(self):
        self.assertFalse(W.WeatherFeed(key="").status()["available"])
        fetch, calls = fake()
        t = [0]
        feed = W.WeatherFeed(key="k", fetch=fetch, clock=lambda: t[0])
        feed.status(); feed.status()
        self.assertEqual(len(calls), 2)                        # 10분 안에는 다시 부르지 않음
        t[0] = W.TTL_S + 1; feed.status()
        self.assertEqual(len(calls), 4)
        fetch, _ = fake(fail="getPwnStatus")
        w = W.WeatherFeed(key="k", fetch=fetch).status()
        self.assertTrue(w["available"]); self.assertIsNone(w["warnings"]); self.assertIn("특보", w["warnings_error"])
        fetch, _ = fake(fail="apis.data.go.kr")
        self.assertFalse(W.WeatherFeed(key="k", fetch=fetch).status()["available"])   # 둘 다 실패하면 확인 불가


if __name__ == "__main__":
    unittest.main()
