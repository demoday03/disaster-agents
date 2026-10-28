"""담당자 계정 만들기. 비밀번호는 화면에 보이지 않게 두 번 입력받고, DB에는 해시만 저장한다.

실행 예(프로젝트 루트에서):
  python scripts/create_user.py 담당자1 --role operator
  python scripts/create_user.py 관리자 --role admin
DB 위치는 서버와 같이 DISASTER_DB_PATH(없으면 data/disaster.db)를 따른다.
"""
import argparse
import getpass
import os
import sys

# README의 안내대로 프로젝트 루트(disaster_agents)에서 실행 가능하도록 경로 보정
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import db  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("username")
    ap.add_argument("--role", choices=db.ROLES, default="operator",
                    help="operator=담당자, admin=관리자, citizen=일반(관리자 화면 사용 불가)")
    ap.add_argument("--password-stdin", action="store_true", help="표준 입력 첫 줄을 비밀번호로 사용(자동화용)")
    args = ap.parse_args()

    if args.password_stdin:
        # PowerShell 파이프는 맨 앞에 BOM(﻿)을 붙일 수 있어 지운다.
        password = sys.stdin.readline().lstrip("﻿").rstrip("\r\n")
    else:
        password = getpass.getpass("비밀번호(8자 이상, 화면에 보이지 않음): ")
        if password != getpass.getpass("비밀번호 확인: "):
            print("오류: 두 비밀번호가 다릅니다.", file=sys.stderr)
            return 1
    db.init_db()
    try:
        db.create_user(args.username, password, args.role)
    except ValueError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    print(f"계정을 만들었습니다: {args.username.strip()} ({args.role}) · DB: {db.DB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
