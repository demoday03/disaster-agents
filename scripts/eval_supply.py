"""물품 요청 평가 세트(제안서 D18, 40건)로 ⑧ 물품 수급의 메모 해석(품목·수량) 정확도를 잰다(11.2).

실행 예(프로젝트 루트에서):
  python scripts/eval_supply.py            # 카탈로그 키워드 규칙(LLM 실패 시 폴백)만 측정, API 호출 없음
  $env:UPSTAGE_API_KEY="..."
  python scripts/eval_supply.py --solar    # Solar 해석(최대 40회 호출)

지표: 품목 일치율(정답 품목 집합과 정확히 같은 요청 비율), 수량 일치율(맞힌 품목 중 수량까지 같은 비율).
목표(제안 11.2): 품목·수량 90% 이상. 긴급도는 메모가 아니라 요청 화면의 조건(영유아·정전 등)으로 규칙 계산하므로 여기서 재지 않는다.
목표 미달이면 종료 코드 1. 가상 세트에 대한 측정일 뿐이다.
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from agents import supply  # noqa: E402
from common.llm import SolarClient  # noqa: E402

TARGET = 0.9


def evaluate(data, catalog, client=None):
    item_ok, qty_ok, qty_n, misses = 0, 0, 0, []
    for r in data["requests"]:
        got, _, _ = supply.parse_memo(r["memo"], catalog, client=client)
        got = {x["item"]: x["qty"] for x in got}
        want = r["items"]
        if set(got) == set(want):
            item_ok += 1
        else:
            misses.append((r["id"], r["memo"], want, got))
        for k, q in want.items():
            if k in got:
                qty_n += 1
                qty_ok += got[k] == q
    n = len(data["requests"])
    return {"n": n, "item_accuracy": item_ok / n, "qty_accuracy": qty_ok / qty_n if qty_n else 0.0, "misses": misses}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--solar", action="store_true", help="Solar API로 메모 해석(실제 호출)")
    ap.add_argument("-v", "--verbose", action="store_true", help="틀린 건을 모두 출력")
    ap.add_argument("--data", default="data/eval/supply_requests.json")
    args = ap.parse_args()
    client = None
    if args.solar:
        if not (os.getenv("UPSTAGE_API_KEY") or os.getenv("SOLAR_BASE_URL")):
            print("오류: UPSTAGE_API_KEY(또는 SOLAR_BASE_URL)를 설정하세요.", file=sys.stderr)
            return 2
        client = SolarClient(timeout=15, max_calls=60)
    with open(args.data, encoding="utf-8") as f:
        data = json.load(f)
    res = evaluate(data, supply.load_catalog(), client)
    print(f"모드: {'Solar' if client else '규칙'} · {res['n']}건")
    print(f"품목 일치율: {res['item_accuracy']:.1%}  (목표 {TARGET:.0%} 이상)")
    print(f"수량 일치율: {res['qty_accuracy']:.1%}  (맞힌 품목 기준, 목표 {TARGET:.0%} 이상)")
    for m in res["misses"][: None if args.verbose else 5]:
        print(f"  틀림 {m[0]} '{m[1]}' 정답 {m[2]} / 결과 {m[3]}")
    return 0 if res["item_accuracy"] >= TARGET and res["qty_accuracy"] >= TARGET else 1


if __name__ == "__main__":
    sys.exit(main())
