"""에이전트 공통 출력 형식. 모든 에이전트는 AgentResult를 돌려준다."""
from dataclasses import dataclass, field, asdict


@dataclass
class AgentResult:
    agent: str                      # "monitor", "dispatch" ...
    output: dict                    # 에이전트별 본문
    ai_used: bool = False           # LLM을 실제로 썼는지 (폴백이면 False)
    fallback_reason: str | None = None
    needs_approval: bool = False    # 사람 승인이 필요한 결과인지
    evidence: list = field(default_factory=list)  # 센서값·규칙 ID·점수 항목 등 근거

    def to_dict(self):
        return asdict(self)
