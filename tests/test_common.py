import unittest
from common.masking import mask
from common.llm import SolarClient, LLMUnavailable, ask_or_fallback
from coordinator import Coordinator


def fake(responses):
    it = iter(responses)
    def transport(url, headers, body, timeout):
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return {"choices": [{"message": {"content": r}}]}
    return transport


class TestCommon(unittest.TestCase):
    def test_mask(self):
        s = mask("010-1234-5678 / 900101-1234567 / a@b.com")
        self.assertNotIn("1234", s)
        self.assertIn("[전화]", s); self.assertIn("[주민번호]", s); self.assertIn("[이메일]", s)

    def test_retry_then_ok(self):
        c = SolarClient(transport=fake(["not json", '```json\n{"a":1}\n```']))
        self.assertEqual(c.chat_json([{"role": "user", "content": "x"}], ["a"]), {"a": 1})

    def test_two_failures_unavailable(self):
        c = SolarClient(transport=fake(["bad", "bad"]))
        with self.assertRaises(LLMUnavailable):
            c.chat_json([{"role": "user", "content": "x"}], ["a"])

    def test_fallback(self):
        c = SolarClient(transport=fake([RuntimeError("net")]))
        out, ai, why = ask_or_fallback(c, [{"role": "user", "content": "x"}], ["a"], lambda: {"rule": 1})
        self.assertEqual((out, ai), ({"rule": 1}, False)); self.assertIn("호출 실패", why)
        out, ai, _ = ask_or_fallback(None, [], [], lambda: {"rule": 2})
        self.assertFalse(ai)

    def test_call_limit_and_cache(self):
        c = SolarClient(transport=fake(['{"a":1}']), max_calls=1)
        m = [{"role": "user", "content": "x"}]
        c.chat(m); c.chat(m)                      # 두 번째는 캐시
        with self.assertRaises(LLMUnavailable):
            c.chat([{"role": "user", "content": "y"}])

    def test_solar_request_uses_configured_endpoint_model_and_auth(self):
        seen = {}
        def transport(url, headers, body, timeout):
            seen.update(url=url, headers=headers, body=body, timeout=timeout)
            return {"choices": [{"message": {"content": '{"ok":true}'}}]}
        c = SolarClient(base_url="https://proxy.example/v1/", api_key="test-secret",
                        model="test-model", transport=transport, timeout=7)
        self.assertEqual(c.chat_json([{"role": "user", "content": "x"}], ["ok"]), {"ok": True})
        self.assertEqual(seen["url"], "https://proxy.example/v1/chat/completions")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer test-secret")
        self.assertEqual(seen["body"]["model"], "test-model")
        self.assertEqual(seen["timeout"], 7)

    def test_cache_key_includes_temperature(self):
        calls = []
        def transport(url, headers, body, timeout):
            calls.append(body)
            return {"choices": [{"message": {"content": '{"ok":true}'}}]}
        c = SolarClient(transport=transport)
        m = [{"role": "user", "content": "x"}]
        c.chat(m, temperature=0.0)
        c.chat(m, temperature=0.8)
        self.assertEqual(len(calls), 2)

    def test_invalid_client_limits_rejected(self):
        with self.assertRaises(ValueError):
            SolarClient(timeout=0)
        with self.assertRaises(ValueError):
            SolarClient(max_calls=0)

    def test_malformed_provider_response_becomes_unavailable(self):
        c = SolarClient(transport=lambda *args: {"choices": []})
        with self.assertRaises(LLMUnavailable):
            c.chat([{"role": "user", "content": "x"}])

    def test_approval_and_conflicts(self):
        co = Coordinator()
        aid = co.request_approval("배치", {"x": 1})
        self.assertEqual(len(co.pending()), 1)
        co.resolve(aid, True, "담당자A")
        self.assertEqual(co.pending(), [])
        with self.assertRaises(ValueError):
            co.resolve(aid, True, "담당자A")
        cf = co.detect_conflicts([{"resource_id": "B-02", "demand_id": "T1"},
                                  {"resource_id": "B-02", "demand_id": "T2"}],
                                 {"S1": (51, 50)})
        self.assertEqual({c["type"] for c in cf}, {"중복 배정", "정원 초과"})
