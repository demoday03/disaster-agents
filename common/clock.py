"""한국 시각(KST, UTC+9). 클라우드 서버는 보통 UTC로 돌아서 datetime.now()를 그대로 쓰면 9시간 늦게 나온다."""
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")


def now_kst():
    """지금 한국 시각(시간대 정보 없는 값: 기상청 API처럼 '한국 시각 글자'가 필요한 곳에 그대로 쓴다)."""
    return datetime.now(KST).replace(tzinfo=None)


def kst_text(ts, fmt="%Y-%m-%d %H:%M"):
    """epoch 초 → 한국 시각 글자."""
    return datetime.fromtimestamp(ts, KST).strftime(fmt) if ts else ""
