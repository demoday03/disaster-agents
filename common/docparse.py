"""업스테이지 문서 파싱(Document Parse) 호출. ⑤ 복구·지원에서 PDF·사진 서류를 텍스트로 바꾼다.
- 키는 환경변수(UPSTAGE_API_KEY)로만 받는다. 주소·모델명은 콘솔에서 확인해 DOCUMENT_PARSE_URL·DOCUMENT_PARSE_MODEL로 바꿀 수 있다.
- 파일 형식은 내용(매직 바이트)으로 판단한다. 확장자만 바꾼 파일은 받지 않는다.
- 원본 파일은 저장하지 않는다. 텍스트 추출 뒤의 개인정보 마스킹은 호출하는 쪽(recovery.extract_fields)이 한다.
- 실패하면 DocParseUnavailable → 화면은 '텍스트를 직접 붙여 넣기'로 돌아간다.
"""
import json, os, re, uuid, urllib.error, urllib.request

from common.llm import USER_AGENT

DEFAULT_URL = "https://api.upstage.ai/v1/document-digitization"
MAX_BYTES = 5 * 1024 * 1024
TYPES = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg"}


class DocParseUnavailable(Exception):
    pass


def sniff(data):
    """파일 앞부분으로 형식을 판단한다. 'pdf'|'png'|'jpg'|'txt'|None."""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    # 제어 문자가 섞인 바이너리는 텍스트로 보지 않는다
    return None if re.search(r"[\x00-\x08\x0e-\x1f]", text) else "txt"


def multipart(fields, file_field, filename, content, content_type):
    boundary = "----disaster" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8"))
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                 f"Content-Type: {content_type}\r\n\r\n".encode("utf-8") + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _http_transport(url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT, **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read(500).decode("utf-8", "replace").strip()
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {detail or '(본문 없음)'}") from None


def _strip_html(html):
    text = re.sub(r"<(br|/p|/h\d|/li|/tr)[^>]*>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


class DocumentParser:
    def __init__(self, api_key=None, url=None, model=None, transport=None, timeout=60, max_calls=100):
        self.api_key = api_key or os.getenv("UPSTAGE_API_KEY", "")
        self.url = url or os.getenv("DOCUMENT_PARSE_URL") or DEFAULT_URL
        self.model = model or os.getenv("DOCUMENT_PARSE_MODEL", "document-parse")
        self.transport, self.timeout = transport or _http_transport, timeout
        self.max_calls, self.calls = max_calls, 0

    def parse(self, filename, data):
        """파일 바이트 → (텍스트, 쪽수). PDF·PNG·JPEG만."""
        kind = sniff(data)
        if kind not in TYPES:
            raise ValueError("PDF·PNG·JPG 파일만 문서 파싱으로 읽을 수 있습니다")
        if len(data) > MAX_BYTES:
            raise ValueError(f"파일은 {MAX_BYTES // (1024 * 1024)}MB 이하여야 합니다")
        if self.calls >= self.max_calls:
            raise DocParseUnavailable("문서 파싱 호출 한도 초과")
        self.calls += 1
        # 파일 이름에 사람 이름이 들어 있을 수 있어(예: 홍길동_계약서.pdf) 원래 이름은 보내지 않는다
        body, ctype = multipart({"model": self.model, "ocr": "auto", "output_formats": '["text", "html"]'},
                                "document", f"document.{kind}", data, TYPES[kind])
        headers = {"Content-Type": ctype}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            res = self.transport(self.url, headers, body, self.timeout)
            content = res.get("content") or {}
            text = (content.get("text") or content.get("markdown") or _strip_html(content.get("html") or "")).strip()
            pages = (res.get("usage") or {}).get("pages")
        except Exception as e:   # 네트워크·HTTP·응답 형식 오류 모두 폴백(수동 입력)
            detail = str(e).replace(self.api_key, "[REDACTED]") if self.api_key else str(e)
            raise DocParseUnavailable(f"문서 파싱 실패: {detail}") from e
        if not text:
            raise DocParseUnavailable("문서에서 글자를 찾지 못했습니다(흐리거나 손글씨일 수 있음)")
        return text, pages if isinstance(pages, int) else None
