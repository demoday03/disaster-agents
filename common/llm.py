"""Solar(업스테이지, OpenAI 호환) 호출 공통부.
- 키는 직접 쓰지 않고 프록시(SOLAR_BASE_URL)나 환경변수로만 받는다.
- JSON 형식이 어긋나면 1회 재시도, 그래도 실패하면 LLMUnavailable → 규칙 폴백.
- 같은 입력 캐시, 세션당 호출 한도.
"""
import hashlib, json, os, re, urllib.error, urllib.request

USER_AGENT = "disaster-agents-prototype/0.1"   # 기본 'Python-urllib'는 일부 API 방화벽에서 403으로 막힌다


class LLMUnavailable(Exception):
    pass


def _http_transport(url, headers, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"User-Agent": USER_AGENT, **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # 상태 코드만으로는 원인(키 오류·권한·크레딧 등)을 알 수 없어 응답 본문 앞부분을 붙인다.
        detail = e.read(500).decode("utf-8", "replace").strip()
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {detail or '(본문 없음)'}") from None


class SolarClient:
    CACHE_MAX = 500
    def __init__(self, base_url=None, api_key=None, model=None,
                 transport=None, timeout=20, max_calls=200):
        self.base_url = (base_url or os.getenv("SOLAR_BASE_URL")
                         or "https://api.upstage.ai/v1").rstrip("/")
        self.api_key = api_key or os.getenv("UPSTAGE_API_KEY", "")
        self.model = model or os.getenv("SOLAR_MODEL", "solar-pro4")
        self.transport, self.timeout = transport or _http_transport, timeout
        if timeout <= 0:
            raise ValueError("timeout은 0보다 커야 합니다")
        if max_calls < 1:
            raise ValueError("max_calls는 1 이상이어야 합니다")
        self.max_calls, self.calls, self._cache = max_calls, 0, {}

    def chat(self, messages, temperature=0.0):
        key = hashlib.sha256(json.dumps([self.base_url, self.model, messages, temperature],
                                        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if key in self._cache:
            return self._cache[key]
        if self.calls >= self.max_calls:
            raise LLMUnavailable("세션 호출 한도 초과")
        self.calls += 1
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {"model": self.model, "messages": messages, "temperature": temperature}
        try:
            data = self.transport(f"{self.base_url}/chat/completions", headers, body, self.timeout)
            text = data["choices"][0]["message"]["content"]
            if not isinstance(text, str):
                raise TypeError("응답 content가 문자열이 아닙니다")
        except Exception as e:  # 네트워크·HTTP·응답 형식 오류는 폴백 대상
            # 일부 예외 메시지에 URL/요청 세부정보가 포함될 수 있어 키는 출력하지 않는다.
            detail = str(e).replace(self.api_key, "[REDACTED]") if self.api_key else str(e)
            raise LLMUnavailable(f"호출 실패: {detail}") from e
        if len(self._cache) >= self.CACHE_MAX:   # 서버처럼 오래 도는 프로세스에서 메모리가 끝없이 늘지 않게
            self._cache.clear()
        self._cache[key] = text
        return text

    def chat_json(self, messages, required_keys=(), retries=1):
        last = None
        for _ in range(retries + 1):
            text = self.chat(messages)
            text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
            try:
                obj = json.loads(text)
                if isinstance(obj, dict) and all(k in obj for k in required_keys):
                    return obj
                last = f"필수 키 누락: {list(required_keys)}"
            except json.JSONDecodeError as e:
                last = f"JSON 오류: {e}"
            self._cache.clear()  # 같은 응답을 캐시에서 다시 받지 않도록
        raise LLMUnavailable(last)


def ask_or_fallback(client, messages, required_keys, fallback_fn):
    """(출력, ai_used, 폴백 사유). client가 None이면 바로 규칙 폴백."""
    if client is None:
        return fallback_fn(), False, "LLM 클라이언트 없음"
    try:
        return client.chat_json(messages, required_keys), True, None
    except LLMUnavailable as e:
        return fallback_fn(), False, str(e)
