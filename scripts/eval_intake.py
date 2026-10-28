"""신고 데이터셋(제안서 D8, 80건)으로 ② 접수의 분류 정확도·인명구조 긴급도·병합 정확도를 잰다(11.2).

실행 예(프로젝트 루트에서):
  python scripts/eval_intake.py            # 키워드 규칙(LLM 실패 시 폴백)만 측정, API 호출 없음
  $env:UPSTAGE_API_KEY="..."; $env:SOLAR_MODEL="콘솔에서_확인한_모델명"
  python scripts/eval_intake.py --solar    # Solar 추출 + 규칙 긴급도(최대 80회 호출)

목표 미달 항목이 있으면 종료 코드 1. 결과는 가상 시험 세트에 대한 측정일 뿐이다.
"""
import argparse
import os
import sys

# README의 안내대로 프로젝트 루트(disaster_agents)에서 실행 가능하도록 경로 보정
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from agents import intake  # noqa: E402
from common import eval_intake as EI  # noqa: E402
from common.llm import SolarClient  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--solar", action="store_true", help="Solar API로 유형·구역·인원 추출(실제 호출)")
    ap.add_argument("-v", "--verbose", action="store_true", help="틀린 건을 모두 출력")
    args = ap.parse_args()

    client = None
    if args.solar:
        if not (os.getenv("UPSTAGE_API_KEY") or os.getenv("SOLAR_BASE_URL")):
            print("오류: UPSTAGE_API_KEY(또는 SOLAR_BASE_URL)를 설정하세요.", file=sys.stderr)
            return 2
        client = SolarClient(timeout=15, max_calls=100)

    res = EI.evaluate(EI.load(), intake.load_cfg(), client)
    print(f"분류 정확도(유형):        {res['type_accuracy']:.1%}  (목표 {EI.TARGETS['type_accuracy']:.0%} 이상)")
    print(f"인명구조 긴급도 1 부여율: {res['rescue_urgency1_rate']:.1%}  (목표 100%)")
    print(f"병합 정확도(사건 단위):   {res['merge_accuracy']:.1%}  (목표 {EI.TARGETS['merge_accuracy']:.0%} 이상)")
    print(f"  여러 건 사건 병합 성공: {res['multi_event_merged']:.1%}")
    print(f"구역 정확도:              {res['zone_accuracy']:.1%}")
    print(f"긴급도 일치:              {res['urgency_accuracy']:.1%}")
    print(f"어려운 사례 유형 정확도:  {res['hard_type_accuracy']:.1%}")
    print(f"덜 급하게 매긴 건:        {', '.join(res['under_urgent']) or '없음'}")
    print("주요 오분류(정답 -> 결과):")
    for (g, p), c in res["confusion"].most_common():
        print(f"  {g} -> {p}: {c}건")
    if args.verbose:
        for r in res["rows"]:
            if (r["type"], r["zone"], r["urgency"]) != (r["gold_type"], r["gold_zone"], r["gold_urgency"]):
                print(f"  {r['id']}: 유형 {r['gold_type']}/{r['type']}, 구역 {r['gold_zone']}/{r['zone']}, "
                      f"긴급도 {r['gold_urgency']}/{r['urgency']}")
    print(f"AI 추출 사용: {res['ai_used']}건" + (f", 호출 {client.calls}회" if client else " (규칙만)"))
    missed = [k for k, t in EI.TARGETS.items() if res[k] < t]
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
