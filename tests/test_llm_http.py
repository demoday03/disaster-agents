"""실제 HTTP 경로(_http_transport)를 로컬 가짜 서버로 검사한다. 외부 API는 호출하지 않는다."""
import json, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from common.llm import SolarClient, LLMUnavailable, USER_AGENT

SEEN = {}


class Forbidden(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        SEEN["ua"] = self.headers.get("User-Agent")
        out = json.dumps({"error": {"message": "Your API key has no access to this model",
                                    "code": "forbidden"}}).encode()
        self.send_response(403); self.send_header("Content-Length", str(len(out))); self.end_headers()
        self.wfile.write(out)


class TestHttpErrors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Forbidden)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def test_http_error_shows_body_and_hides_key(self):
        c = SolarClient(base_url=self.base, api_key="fake-secret-key", model="m")
        with self.assertRaises(LLMUnavailable) as cm:
            c.chat([{"role": "user", "content": "hi"}])
        msg = str(cm.exception)
        self.assertIn("HTTP 403", msg)
        self.assertIn("no access to this model", msg)   # 원인을 알 수 있게 본문을 보여 줌
        self.assertNotIn("fake-secret-key", msg)
        self.assertEqual(SEEN["ua"], USER_AGENT)


if __name__ == "__main__":
    unittest.main()
