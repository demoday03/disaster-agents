"""시연용 DB 준비. 실제 운영 DB(data/disaster.db)와 따로 data/demo.db를 만들고 담당자 계정과 신고 6건을 넣는다.

실행(프로젝트 루트에서):
  python scripts/demo_setup.py            # data/demo.db가 없을 때 새로 만든다
  python scripts/demo_setup.py --reset    # 시연을 다시 할 때: data/demo.db를 지우고 처음 상태로
출력되는 명령을 그대로 따라 서버·게이트웨이를 켠다. 순서는 docs/시연_시나리오.md.
"""
import argparse, os, secrets, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
from agents import intake  # noqa: E402
from database import db  # noqa: E402

DEMO_USER = "시연담당"
# (신고문, 기대 유형) — 키워드 규칙만으로 분류되는 문장. 시연 중 AI 연결이 끊겨도 같은 결과가 나온다.
REPORTS = [
    ("Z3 반지하에 할머니가 갇혀 있어요 물이 허리까지 찼어요", "인명구조"),
    ("1구역 하천 옆 도로가 침수됐어요 차가 못 지나가요", "침수"),
    ("Z5 뒷산에서 토사가 흘러내려요 집 앞까지 왔어요", "산사태·토사"),
    ("Z2 상가 쪽 정전이에요 엘리베이터가 멈췄어요", "단전·단수"),
    ("Z6 마을회관에 생수랑 담요가 필요해요 어르신 10명", "물품 요청"),
    ("Z4 학교 앞 맨홀 뚜껑이 열려 있어요", "도로·시설 장애"),
]


def setup(db_path, reset=False, password=None, now=None):
    """시연 DB를 만들고 (비밀번호, 접수 결과 목록)을 돌려준다."""
    db_path = Path(db_path).resolve()
    if db_path.exists():
        if not reset:
            raise SystemExit(f"{db_path}가 이미 있습니다. 처음부터 다시 하려면 --reset을 붙이세요.")
        if not db_path.name.startswith("demo"):   # 운영 DB를 실수로 지우지 않게 이름으로 한 번 더 막는다
            raise SystemExit("안전을 위해 이름이 demo로 시작하는 DB만 지웁니다.")
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
    old = db.DB_PATH
    db.DB_PATH = db_path
    try:
        db.init_db()
        password = password or secrets.token_urlsafe(9)
        db.create_user(DEMO_USER, password, "operator")
        now = time.time() if now is None else now
        results = []
        for i, (text, _) in enumerate(REPORTS):   # 몇 분 간격으로 들어온 것처럼 시각을 나눈다
            rid = f"R-DEMO{i + 1:04d}"
            results.append(db.save_incident(intake.classify(rid, text, now - (len(REPORTS) - i) * 180,
                                                            intake.load_cfg()).to_dict()))
        return password, results
    finally:
        db.DB_PATH = old


def main(argv=None):
    ap = argparse.ArgumentParser(description="시연용 DB 준비")
    ap.add_argument("--db", default=str(ROOT / "data" / "demo.db"))
    ap.add_argument("--reset", action="store_true", help="기존 시연 DB를 지우고 다시 만든다")
    args = ap.parse_args(argv)
    password, results = setup(args.db, args.reset)
    token = secrets.token_urlsafe(24)
    print(f"시연 DB를 만들었습니다: {Path(args.db).resolve()}")
    print(f"담당자 계정: {DEMO_USER} / 비밀번호: {password}   (이 창에만 표시됩니다. 메모해 두세요)\n")
    print("미리 넣은 신고:")
    for r in results:
        o = r["output"]
        print(f"  {o['id']}  {o['type']:<8} {o['zone']:<4} 긴급도 {o['urgency']}  {o['text'][:30]}")
    db_env = Path(args.db).resolve()
    print("\n[PowerShell 창 1: 서버]")
    print(f'  $env:DISASTER_DB_PATH="{db_env}"')
    print(f'  $env:SENSOR_GATEWAY_TOKEN="{token}"')
    print("  python webapp/server.py")
    print("\n[PowerShell 창 2: 센서 게이트웨이(장면 3에서)]")
    print(f'  $env:SENSOR_GATEWAY_TOKEN="{token}"')
    print("  python gateway/microbit_gateway.py --simulate flood --ticks 8      # 실제 기기는 --port COM3")
    print("\n브라우저: http://127.0.0.1:8765/admin  ·  순서는 docs/시연_시나리오.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
