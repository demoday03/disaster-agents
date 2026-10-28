-- 승인된 배치를 작업 완료 등으로 해제한 기록. 해제된 배치의 자원은 다시 추천 후보가 된다.
CREATE TABLE IF NOT EXISTS dispatch_releases (
    approval_id TEXT PRIMARY KEY REFERENCES approvals(id),
    released_by TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    released_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
