"""오류 안내문 세트(제안서 11.3)로 ⑦ 검증의 차단·보류율을 잰다. 목표 90% 이상(11.2).

실행 예(프로젝트 루트에서):
  python scripts/eval_verify.py --rules-only      # API 없이 숫자 선검사만(나머지는 notSure)
  $env:UPSTAGE_API_KEY="..."; $env:GROUNDEDNESS_MODEL="콘솔에서_확인한_모델명"
  python scripts/eval_verify.py                   # 실제 모델 호출(문항 40개, 숫자 선검사로 막히는 7개를 빼고 33회)
  GROUNDEDNESS_MODEL이 없으면 판정 모델(VERIFY_MODEL, 기본 solar-pro3)이 판정한다.

결과는 시험 세트에 대한 측정일 뿐이며, 실제 안내의 정확성·안전성을 보증하지 않는다.
"""
import argparse
import os
import sys

# README의 안내대로 프로젝트 루트(disaster_agents)에서 실행 가능하도록 경로 보정
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from common import eval_guidance as EG  # noqa: E402
from agents import verify  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules-only", action="store_true", help="API를 호출하지 않고 숫자 선검사만 측정")
    args = ap.parse_args()

    client = None
    if not args.rules_only:
        if not (os.getenv("UPSTAGE_API_KEY") or os.getenv("SOLAR_BASE_URL")):
            print("오류: UPSTAGE_API_KEY(또는 SOLAR_BASE_URL)를 설정하거나 --rules-only로 실행하세요.",
                  file=sys.stderr)
            return 2
        client = verify.make_client(timeout=15, max_calls=100)   # 세트(오류 20 + 대조군 20)보다 넉넉하게. 한도에 걸리면 notSure로 왜곡된다

    data, docs = EG.load()
    res = EG.evaluate(client, data, docs)
    for r in res["errors"] + res["controls"]:
        print(f"{r['id']}  {r['category']:<10}  {r['verdict']}")
    print(f"\n오류 문구 차단·보류율: {res['block_rate']:.0%} (notGrounded {res['hard_block_rate']:.0%}, "
          f"목표 {EG.TARGET:.0%} 이상)")
    print(f"대조군 통과율(grounded): {res['control_pass_rate']:.0%}")
    if client is None:
        print("주의: --rules-only는 모델 판정 없이 notSure로 보류한 결과라 실제 차단 성능이 아닙니다.")
    else:
        mode = ("Groundedness" if client.verify_mode == "groundedness" else
                "채팅 모델 판정" + (", 안내문 작성 모델과 같음" if client.same_as_writer else ""))
        print(f"모델={client.model} ({mode}), 호출 {client.calls}회")
    return 0 if res["block_rate"] >= EG.TARGET else 1


if __name__ == "__main__":
    raise SystemExit(main())
