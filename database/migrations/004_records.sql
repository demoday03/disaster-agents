-- 물품 요청·재고·기부, 봉사 작업, 복구 케이스, 안부 확인 등 기능별 상태를 JSON 문서로 저장한다.
-- 프로토타입 단계라 기능마다 테이블을 따로 두지 않고 (종류, ID)로 구분한다.
CREATE TABLE IF NOT EXISTS records (
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (kind, id)
);

CREATE INDEX IF NOT EXISTS idx_records_kind_created ON records(kind, created_at);
