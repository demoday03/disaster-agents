"""실제 Upstage Solar 연결 확인용 최소 스모크 테스트.

실행 예:
  set UPSTAGE_API_KEY=...        (Windows CMD)
  set SOLAR_MODEL=solar-pro4     (콘솔에서 확인한 모델명)
  python scripts/smoke_solar.py

프록시 사용 시 SOLAR_BASE_URL을 프록시의 OpenAI 호환 API base URL로 설정한다.
이 스크립트는 짧은 테스트 프롬프트만 전송하며 실제 재난 판단에는 사용하지 않는다.
"""
import os
import sys

# README의 안내대로 프로젝트 루트(disaster_agents)에서 실행 가능하도록 경로 보정
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.llm import SolarClient, LLMUnavailable  # noqa: E402


def main():
    if not os.getenv("UPSTAGE_API_KEY") and not os.getenv("SOLAR_BASE_URL"):
        print("오류: UPSTAGE_API_KEY 또는 인증을 처리하는 SOLAR_BASE_URL을 설정하세요.", file=sys.stderr)
        return 2

    client = SolarClient(timeout=15, max_calls=1)
    try:
        result = client.chat_json(
            [{"role": "system", "content": "Return only valid JSON."},
             {"role": "user", "content": 'Return exactly this JSON object: {"ok": true, "service": "solar"}'}],
            required_keys=("ok", "service"), retries=0,
        )
    except LLMUnavailable as exc:
        # Do not print API key or request headers.
        print(f"Solar 연결 실패: {exc}", file=sys.stderr)
        return 1

    if result.get("ok") is not True or result.get("service") != "solar":
        print(f"연결은 되었지만 응답 검증 실패: {result}", file=sys.stderr)
        return 1
    print(f"Solar 연결 성공 (model={client.model}, base_url={client.base_url})")
    print("주의: 연결 스모크 테스트 통과는 재난 안내의 정확성/안전성 검증을 의미하지 않습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
