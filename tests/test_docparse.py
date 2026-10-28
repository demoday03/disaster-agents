"""업스테이지 문서 파싱 호출부(common/docparse.py). 가짜 전송 함수만 쓰고 실제 API는 호출하지 않는다."""
import unittest
from common import docparse as D

PDF = b"%PDF-1.4\n...fake..."
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


def fake(res=None, exc=None, seen=None):
    def transport(url, headers, body, timeout):
        if seen is not None:
            seen.update(url=url, headers=headers, body=body)
        if exc:
            raise exc
        return res
    return transport


class TestDocParse(unittest.TestCase):
    def test_sniff(self):
        self.assertEqual([D.sniff(x) for x in (PDF, PNG, b"\xff\xd8\xff\xe0", "구역 Z3".encode(), b"\x00\x01bin", b"\xff\xfe")],
                         ["pdf", "png", "jpg", "txt", None, None])

    def test_parse_sends_multipart_with_key_and_reads_text(self):
        seen = {}
        p = D.DocumentParser(api_key="k-123", url="http://fake/parse", transport=fake(
            {"content": {"text": "구역 Z3 세대주 가구 H05 주택침수", "html": "<p>x</p>"}, "usage": {"pages": 1}}, seen=seen))
        text, pages = p.parse("임대차 계약서.pdf", PDF)
        self.assertEqual((text, pages), ("구역 Z3 세대주 가구 H05 주택침수", 1))
        self.assertEqual(seen["headers"]["Authorization"], "Bearer k-123")
        self.assertIn("multipart/form-data; boundary=", seen["headers"]["Content-Type"])
        self.assertIn(b'name="model"\r\n\r\ndocument-parse', seen["body"])
        self.assertIn(b'filename="document.pdf"', seen["body"])        # 원래 파일 이름(사람 이름 포함 가능)은 보내지 않음
        self.assertNotIn("임대차".encode(), seen["body"])
        self.assertIn(b"Content-Type: application/pdf", seen["body"])
        self.assertIn(PDF, seen["body"])

    def test_html_fallback(self):
        p = D.DocumentParser(api_key="k", transport=fake({"content": {"html": "<h1>피해 신고</h1><p>구역 Z2</p>"}}))
        self.assertEqual(p.parse("a.png", PNG)[0].split(), ["피해", "신고", "구역", "Z2"])

    def test_failures_become_unavailable_and_hide_key(self):
        for transport in (fake(exc=RuntimeError("HTTP 401 key=secret-key")), fake({"content": {}}), fake({"oops": 1})):
            with self.assertRaises(D.DocParseUnavailable) as cm:
                D.DocumentParser(api_key="secret-key", transport=transport).parse("a.pdf", PDF)
            self.assertNotIn("secret-key", str(cm.exception))

    def test_rejects_unsupported_and_large_files_and_call_limit(self):
        p = D.DocumentParser(api_key="k", transport=fake({"content": {"text": "x"}}), max_calls=1)
        with self.assertRaises(ValueError):
            p.parse("a.exe", b"MZ\x90\x00\x03")
        with self.assertRaises(ValueError):
            p.parse("big.pdf", PDF + b"0" * D.MAX_BYTES)
        p.parse("a.pdf", PDF)
        with self.assertRaises(D.DocParseUnavailable):
            p.parse("a.pdf", PDF)


if __name__ == "__main__":
    unittest.main()
