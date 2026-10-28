# 재난 안전 AI 에이전트 (구현 1차 — 8개 에이전트 + 조정자)

실행: `python -m unittest discover -s tests -t . -v`  (2026-10-28 기준 134개 통과; Solar·문서 파싱 단위 테스트는 가짜 응답 사용)

| 에이전트 | 파일 | LLM 용도 | 설정 |
|---|---|---|---|
| 조정자 | coordinator.py | 없음(규칙) | |
| ① 감시 | agents/monitor.py | 근거 요약(선택) | config/monitor.json |
| ② 접수 | agents/intake.py | 유형·구역·인원 추출 | config/intake.json |
| ③ 안부·안내 | agents/welfare.py | 안내문 문장 조정 | config/guidance.json |
| ④ 배치 추천 | agents/dispatch.py | 근거 문장(선택) | config/dispatch.json |
| ⑤ 복구·지원 | agents/recovery.py | 서류 필드 추출 | config/support_rules.json |
| ⑥ 봉사 코디 | agents/volunteer.py | 작업 분해 | config/tasks.json |
| ⑦ 검증 | agents/verify.py | Groundedness Check | config/verify.json |
| ⑧ 물품 수급 | agents/supply.py | 메모 해석, 안내 초안 | config/catalog.json |

공통: common/llm.py(Solar 호출·재시도·캐시·한도), schema.py(AgentResult), masking.py, audit.py

## 배포 구조 (2026-10-28 결정)
제안서의 정적 웹앱(GitHub Pages) + 프록시 대신 **로컬 Python 서버 + SQLite**를 유지한다. 서버가 승인·감사 기록을 강제하고 API 키를 환경변수로 보관한다. 접속 범위는 ① 실행 PC(현재) → ② 학교 내부망(로그인·권한·CSRF 구현 후) → ③ HTTPS 서버 순으로 넓힌다. 이유와 제안서 절별 수정 문안은 `docs/제안서_수정안_배포구조.md`에 있다.

## 공통 원칙
- 점수·제약·상태 전이는 규칙. LLM이 실패하면 규칙 폴백(ai_used=False, fallback_reason 기록).
- 입력 글은 <태그>로 감싸 데이터로만 전달. 긴급도·승인 같은 판단은 LLM이 바꿀 수 없게 규칙으로 고정.
- 배치·투입·물품 승인·단계 확정·제출은 사람(needs_approval). 승인 전에는 아무것도 반영되지 않음.
- AI가 만든 안내문은 ⑦ 검증을 통과해야 표시. 불통과·불확실이면 표준 원문.

## 지금 비어 있거나 확정이 필요한 것
- 감시 임계값(calibrated=false): 설치 점검에서 측정해 교체.
- 안전 수치(연속 활동 시간·고온·체크인 지연)·활동 시간 상한: 공식 지침 확인 전이라 null → 규칙 미적용, '기준 미확정' 표시.
- Groundedness 모델명: 콘솔 확인 후 SolarClient(model=...)로 주입. 선택 근거 검색(임베딩)은 미구현(문서 ID 매핑만).
- 안내 문구(guidance.json)·물품 카탈로그·지원제도 규칙의 '확인 필요' 항목·as_of(기준일): 공식 자료로 교체·확인.
- 업스테이지 문서 파싱: 연동 코드는 있으나 실제 API로는 아직 호출해 보지 않음. 주소·모델명(`DOCUMENT_PARSE_URL`·`DOCUMENT_PARSE_MODEL`)을 콘솔에서 확인해야 함.
- 수요 1건에 여러 명이 필요한 인력 배치(④ 자체)는 ⑥·⑧ 쪽에서 반복 추천으로 처리.
- 웹앱/API 서버는 `webapp/`에 포함되어 있으며 SQLite 영속 저장을 사용합니다. 마이크로비트 게이트웨이는 가상 센서 모드로만 시험했고, 실제 기기 배선·임계값은 설치 점검에서 확정해야 합니다.
- ② 접수: 인명구조와 대민지원은 자동 병합하지 않음. 키워드 규칙은 폴백용이며 평가 세트(80건)로 1차 보강함. 별도 세트로 재측정 필요.


## 회귀 수정 사항 (안전 강화)
- 출고·예약 수량은 양의 정수만 허용한다. 재고 예약은 다른 요청의 추천 가능 수량에서 제외한다.
- `propose()`가 생성한 임시 예약은 거절/취소 시 `inventory.release_reservation(request_id)`로 해제해야 한다.
- 예약 재고 출고 시 `issue(..., request_id=request_id)`로 해당 요청의 예약량만 소비한다.
- 센서 판단은 미래 시각/오래된 측정값을 무시하고, 필수 센서 데이터가 오래되면 보류한다.
- 배치 직접 지정에서 제약 위반은 기본 차단한다. 예외는 `direct(..., override=True, reason="...")`처럼 사유가 있어야 한다.
- 봉사자 `assign(..., temperature_c=...)`에 현장 온도를 전달하면 고온 경고가 배정에 반영된다.


## 실제 Solar API 연결 확인(스모크 테스트)

1. Upstage 콘솔에서 사용 가능한 모델명을 확인한다. 기본 `SOLAR_MODEL` 값은 예시이므로 계정에서 지원하지 않으면 변경한다.
2. 실제 API 키를 코드나 저장소에 쓰지 말고 OS 환경변수 또는 비밀관리 시스템에 설정한다.
   - Windows CMD: `set UPSTAGE_API_KEY=실제키`
   - PowerShell: `$env:UPSTAGE_API_KEY="실제키"`
   - 선택: `set SOLAR_MODEL=콘솔에서_확인한_모델명`
3. 프로젝트 루트(`disaster_agents`)에서 `python scripts/smoke_solar.py`를 실행한다.
4. 프록시를 사용하는 경우 `SOLAR_BASE_URL`에 OpenAI 호환 API base URL을 지정하고, 프록시 인증 방식이 `Authorization: Bearer`와 맞는지 확인한다.

### 오류 안내문 세트로 검증 차단율 측정 (제안서 11.2·11.3)

`data/eval/guidance_errors.json`에 오류 안내문 20종(근거와 다른 숫자 7, 한글로 쓴 숫자 2, 없는 제도 5, 위험한 행동 6)과 근거에 맞는 대조군 5종이 있다. 목표는 오류 문구의 90% 이상을 차단(`notGrounded`) 또는 보류(`notSure`)하는 것이다.

- `python scripts/eval_verify.py --rules-only`: API 없이 실행. 숫자 선검사로 막히는 것은 7/20(35%)뿐이고 나머지는 검증 모델이 없어 `notSure`로 보류된다. 실제 차단 성능이 아니다.
- `GROUNDEDNESS_MODEL`과 `UPSTAGE_API_KEY`를 설정한 뒤 `python scripts/eval_verify.py`: 실제 Groundedness 모델을 25회 호출해 차단·보류율과 대조군 통과율을 출력한다. 목표 미달이면 종료 코드 1.
- `tests/test_guidance_errors.py`는 세트 형식과, 모델이 전부 `grounded`라고 답하는 최악의 경우에도 숫자 오류가 규칙으로 차단되는지를 오프라인으로 확인한다.

### 신고 데이터셋으로 접수 성능 측정 (제안서 D8·11.2)

`data/eval/intake_reports.json`에 가상 신고 80건과 정답(유형·구역·인원·취약 여부·같은 사건 묶음)이 있다. 인명구조 6, 침수 12, 그 밖 62건이며 어려운 사례(혼합 유형, 모호한 구역, 상반 보고, 오탈자, 병합 함정)가 17건(21%)이다.

- `python scripts/eval_intake.py`: API 없이 키워드 규칙만 측정. `-v`로 틀린 건 목록.
- `python scripts/eval_intake.py --solar`: Solar 추출로 측정(실제 호출, 최대 80회).

2026-10-28 키워드 규칙 측정 결과:

| 지표 | 보강 전 | 보강 후 | 목표 |
|---|---|---|---|
| 분류 정확도 | 73.8% (59/80) | 87.5% (70/80) | 90% |
| 인명구조 긴급도 1 부여율 | 83.3% (5/6) | 100% (6/6) | 100% |
| 병합 정확도(사건 단위) | 55.9% (38/68) | 89.7% (61/68) | 90% |
| 구역 정확도 | 97.5% | 97.5% | — |

보강 내용:
- 대민지원은 자동 병합하지 않는다(`agents/intake.py` `NO_MERGE_TYPES`). 보강 전에는 놓친 인명구조 신고(R05 "차가 물에 떠내려가요 사람이 타고 있어요")가 같은 구역의 다른 문의 2건과 묶여 긴급도 4로 묻혔다.
- 키워드(`config/intake.json`): 인명구조에 생명 위험 표현(떠내려·휩쓸림·물에 빠짐·못 나옴·부상), 물품 요청은 `config/catalog.json` 품목명·별칭으로 바꾸고 노동 요청까지 잡던 '필요해요' 삭제, 취약 키워드에 할머니·할아버지·장애인 등 추가. 오탈자 변형은 넣지 않았다.

**주의:** 이 세트의 오류를 보고 규칙을 고쳤으므로 보강 후 수치는 낙관적이다. 실제 성능은 이 세트와 별도로, 다른 사람이 작성한 신고 세트로 다시 재야 한다. 남은 오류(오탈자, 모호한 구역, 상반·해제 보고, 지하차도·교량처럼 유형이 겹치는 신고)는 키워드보다 LLM 추출로 다룰 문제이며, 정답은 1차 작성본이라 사람 검수가 필요하다.

`scripts/smoke_solar.py`는 최소한의 API 연결과 JSON 응답만 검사한다. 실제 API 키가 없는 환경에서는 실행되지 않으며, 이 작업 과정에서 실제 Upstage API 호출은 수행하지 않았다. 웹앱/API 프로토타입은 포함되어 있으나, 실제 마이크로비트 게이트웨이와 운영용 보안 기능은 별도 구현이 필요하다.


## 로컬 웹 대시보드 / API 프록시 프로토타입

이 폴더의 `webapp/`에는 기존 Python 에이전트를 호출하는 로컬 웹 대시보드와 API 서버가 포함되어 있습니다. Python 표준 라이브러리만 사용합니다.

```bash
python webapp/server.py
```

브라우저에서 `http://127.0.0.1:8765`를 엽니다. 기본 `LLM_MODE=mock`은 규칙 기반 데모이며 실제 AI API를 호출하지 않습니다. 실제 Solar 호출을 시험하려면 **본인 PC의 환경변수**에서 `LLM_MODE=solar`, `UPSTAGE_API_KEY`, 그리고 계정에서 지원하는 `SOLAR_MODEL`을 설정한 뒤 서버를 실행하세요. API 키를 HTML, JavaScript, ZIP, 소스 코드 또는 채팅에 넣지 마세요.

Windows PowerShell 예시:

```powershell
$env:LLM_MODE="solar"
$env:UPSTAGE_API_KEY="여기에_로컬에서_설정"
$env:SOLAR_MODEL="계정에서_지원하는_모델명"
python webapp/server.py
```

API: `GET /api/health`, `GET /api/agents`, `GET /api/incidents`, `GET /api/approvals`, `POST /api/incidents`, `POST /api/monitor/evaluate`, `POST /api/approvals/{id}/resolve`, `POST /api/incidents/{id}/dispatch`, `GET /api/dispatch`, `POST /api/dispatch/{id}/release`, `POST /api/dispatch/{id}/modify`, `POST /api/dispatch/{id}/direct`, `POST /api/control-zones`, `POST /api/control-zones/{id}/toggle`. 현장 운영 기능 API는 `webapp/features.py` 맨 위 라우팅을 보세요.

테스트:

```bash
python -m unittest discover -s tests -t . -v      # 단위 테스트 134개
python -m unittest webapp.test_server -v           # mock 모드 서버 API 33개(로그인·권한·CSRF, 배치, 내부망 보안 4개, 센서 게이트웨이 3개 포함)
python -m unittest webapp.test_features -v         # 현장 운영 기능 API 11개(①③⑤⑥⑦⑧, 서류 파일 읽기 포함)
python -m unittest webapp.test_server_solar -v     # solar 경로 9개(가짜 로컬 서버, 실제 API 호출 없음. 문서 파싱 포함)
```

`test_server_solar`는 127.0.0.1에 가짜 OpenAI 호환 서버를 띄워 `LLM_MODE=solar` 경로(안내문 생성 → Groundedness 판정 → 승인 대기열)를 검사합니다. 가짜 키 문자열만 쓰며, **통과해도 실제 Solar 연결이나 안내 정확성이 확인된 것은 아닙니다.** 포트 18765~18768을 사용합니다.

### 안내문 승인 흐름 (현재 구현)
- `GROUNDEDNESS_MODEL`이 없거나 mock 모드면 AI 안내문을 만들지 않고 표준 원문만 반환합니다.
- 검증 판정은 응답이 정확히 `grounded`/`notGrounded`/`notSure`일 때만 인정합니다. 그 밖의 문장(예: "not grounded")은 `notSure`로 처리합니다.
- `grounded`만 승인 대기열(`welfare_guidance`)에 들어갑니다. API 응답에는 승인 전 AI 문구를 넣지 않고 표준 원문만 담으며(`ai_text_withheld: true`), 초안은 승인 대기열 항목에서만 볼 수 있습니다.
- `notSure`(모호한 판정, 검증 호출 실패, 근거 길이 초과 포함)는 표준 원문을 반환하고, 초안을 `welfare_guidance_review`(담당자 확인) 항목으로 승인 대기열에 올립니다. 응답에는 `review_id`가 붙습니다.
- `notGrounded`는 대기열에 넣지 않고 차단합니다. 응답에 `blocked: true`가 붙고 `audit_logs`에 `guidance_blocked` 기록이 남습니다(초안 문구는 기록하지 않음).
- 관리자 화면(`/admin`)은 대기열 종류를 한국어로 표시합니다: 위험 단계 확정 / 안내문 승인(검증 통과) / 안내문 담당자 확인(검증 불확실).
- 승인 이후 표시(제안서 9.2-4, V13): 검증 통과(`welfare_guidance`) 항목을 담당자가 **승인한 경우에만** `GET /api/incidents/{접수번호}` 응답의 `guidance`에 AI 문구가 담기고, 접수 사이트의 상태 조회에 출처·근거 문서 ID·근거 원문과 함께 표시됩니다. 표시 직전에 `verdict=grounded`를 다시 확인하며, 승인자 이름은 공개하지 않습니다. 반려된 건, 대기 중인 건, 담당자 확인(`welfare_guidance_review`) 건은 승인해도 표시하지 않고 `guidance: null`을 돌려줍니다.
- 관리자 화면(`/admin`)의 **안내문 생성** 카드에서 신고·위험 단계·가구 특성(반지하/거동 불편/영유아)·통제 구간을 골라 `POST /api/incidents/{접수번호}/guidance`를 호출합니다. 결과에 처리 경로(승인 대기열 / 담당자 확인 / 차단 / AI 미사용)가 표시되고 승인 대기열이 새로 고쳐집니다. mock 모드에서는 표준 원문만 만들어지고 대기열에 올라가지 않습니다. 단계·특성 선택지는 현재 `config/guidance.json`에 맞춰 화면에 고정되어 있습니다.

**보안·범위 한계:** 서버는 안전을 위해 기본적으로 `127.0.0.1`에만 바인딩합니다. 로그인·역할·CSRF 방어는 아래 "담당자 로그인" 절대로 들어 있지만, 운영용 감사 정책, TLS/리버스 프록시, 실제 마이크로비트 게이트웨이 연결은 아직 없습니다. 접수·승인 데이터는 SQLite에 저장됩니다. 공개망에 노출하거나 실제 재난 대응에 사용하지 마세요. 8개 에이전트가 모두 관리자 화면의 탭으로 연결되어 있습니다(아래 "관리자 화면 탭" 절). 센서 예제는 빈 입력으로 시작하며 현장 보정 전 경보 근거가 아닙니다.


## SQLite 데이터베이스 연결 (현재 프로토타입)

웹 API는 이제 `database/db.py`를 통해 SQLite에 접수 결과와 승인 대기/처리 결과를 저장합니다. 기본 DB 파일은 `data/disaster.db`이며 서버를 재시작해도 데이터가 유지됩니다. 첫 실행 시 `database/migrations/001_initial.sql`이 자동 적용되고, 적용된 버전은 `schema_migrations`에 기록됩니다.

- 접수 기록: `incidents`, `ai_analyses`, `agent_logs`
- 승인 대기 및 결과: `approvals`
- 감사 이력: `audit_logs`
- 향후 기능을 위한 초기 테이블: `users`, `resources`, `resource_transactions`

테스트나 별도 실행에서 DB 파일을 바꾸려면 환경변수 `DISASTER_DB_PATH`를 지정합니다. 예: PowerShell `$env:DISASTER_DB_PATH="$PWD\data\test.db"`. 기존 `data/disaster.db`를 삭제하면 저장된 데모 데이터가 사라지므로, 초기화가 필요할 때만 백업 후 삭제하세요.

**아직 미완성:** 봉사자 역할, 승인된 신고의 업무 상태 자동 전이, 자원 배분 API, 실서비스용 보안은 구현되지 않았습니다. 이 버전은 로컬 시연용이며 실제 재난 대응에 사용하지 마세요.


## 분리된 접수 사이트와 관리자 사이트

- 접수 사이트: `http://127.0.0.1:8765/report` (신고 접수, 접수번호로 상태 조회)
- 관리자 시연 화면: `http://127.0.0.1:8765/admin` (신고 목록, 에이전트 상태, 시연용 승인 대기열)
- `/`는 접수 사이트로 이동하는 기본 화면입니다. 두 화면은 같은 API 서버와 SQLite DB를 사용합니다.
- `GET /api/incidents/{접수번호}`로 접수 상태를 조회할 수 있습니다.

## 관리자 화면 탭 — 8개 에이전트 연결 (2026-10-28)

| 탭 | 에이전트 | 할 수 있는 것 | 사람 승인·확인 지점 |
|---|---|---|---|
| 상황판 | 조정자 | 확정된 위험 단계, 격상 가구, SOS, 물품 부족·검토 대기, 진행 중 작업 요약 | SOS 확인 |
| ① 센서·⑦ 검증 | ① 감시 | 센서 시뮬레이터(수위 2개·기울기·진동·강우 단계) → 교차 확인 판정 | 단계 확정은 승인 대기열 |
| | ⑦ 검증 | 문구와 근거 문서(안내 조각·지원제도 규칙)를 골라 grounded/notGrounded/notSure 판정 | notSure → 담당자 확인 |
| ② 신고·③ 안내 | ②·③ | 신고 목록, 맞춤 안내문 생성 | 검증 통과분만 승인 후 접수 사이트 표시 |
| ④ 배치 | ④ 배치 추천 | 공공 인력·대피소 추천, 투입 현황, 해제 | 배치 승인(재검사) |
| ⑧ 물품 | ⑧ 물품 수급 | 요청(품목 선택 또는 메모 해석)·병합·긴급도, 검토, 공급원 추천(재고 예약), 전달·수령 확인, 미수령 재추천, 기부 등록·검수, 부족 예측과 안내 초안 | 요청 검토(승인·보류·반려), 배치 승인 뒤에만 출고. 의약품은 자동 매칭 안 함 |
| ⑥ 봉사 | ⑥ 봉사 코디 | 요청문 → 작업 분해 → 봉사자 추천, 도착 체크인·완료 보고(복구 케이스 반영), SOS | 투입 승인(이중 투입 재검사) |
| ⑤ 복구·지원 | ⑤ 복구·지원 | 지원제도 매칭(출처·확인 상태 표시), 서류 필드 추출 → 수동 입력 → 신고서 초안·체크리스트, 복구 케이스 단계, 피해조사 집계 | 제출은 모의, 단계는 담당자가 한 단계씩 |
| ③ 안부 | ③ 안부·안내 | 구역별 안전 확인 시작, 무응답 시 취약 가구부터 격상, 응답 모의 | 격상은 알림만(자동 출동 없음) |
| 승인 대기열 | 공통 | 모든 승인 항목(단계·안내·배치·투입·출고)과 요약 | 승인 시 사유 필수, 처리자는 로그인 계정 |

상태는 `records` 테이블(마이그레이션 004)에 JSON으로 저장합니다. 가상 데이터는 `data/scenario/people.json`(가구 20·봉사자 12), `data/scenario/inventory.json`(재고 6곳·기부 10건)이며 물품 재고는 첫 사용 때 DB로 옮긴 뒤 승인된 출고로만 줄어듭니다. 서버 코드는 `webapp/features.py`, 화면 코드는 `webapp/admin_ops.js`입니다.

**모의 범위:** 피해자·봉사자 화면은 따로 없고, 그 사람들이 하는 동작(물품 요청, 수령 확인, 안전 응답, 체크인, SOS, 기부 등록)을 관리자 화면에서 대신 눌러 시연합니다. 실제 문자·출동·배송·제출은 일어나지 않습니다. mock 모드에서는 메모·서류 해석과 근거 문장이 규칙 폴백으로 동작하고, 검증 모델이 없으면 notSure로 처리합니다.

## 배치 보드 (제안서 O4·5.7, 2026-10-28)

관리자 화면의 **배치 추천** 카드에서 신고를 골라 ④ 배치 에이전트의 추천을 받습니다.

- **공공 인력 → 신고 현장**: 신고 유형으로 필요한 역량(인명구조→구조, 침수→배수 등)을 정하고, 역량·이동 거리·현재 부하·긴급도 적합을 가중합해 1순위와 대안 2개를 보여 줍니다. 물품 요청은 ⑧ 몫이라 받지 않고, 구역 미상 신고는 구역 확인 전까지 추천하지 않습니다.
- **주민 → 대피소**: 대피 인원과 필요한 편의시설(엘리베이터·경사로·의료 공간)로 수용량 여유·편의시설·거리를 점수화합니다.
- 제약(투입 중, 통제 구간 경유, 수용량 부족)을 어기는 후보는 점수 전에 빼고 사유를 표시합니다.
- 1순위는 승인 대기열(`dispatch_public` / `dispatch_shelter`)에 올라가며, **승인해야 투입으로 반영**됩니다. 승인 직전에 그 자원이 다른 배치에 이미 투입됐거나 대피소가 찼는지 다시 확인해 409로 막습니다(같은 자원 이중 투입 방지).
- 거절하면 같은 신고에서 그 후보를 빼고 다시 추천합니다. 같은 신고·같은 종류의 추천이 이미 대기 중이면 새로 만들지 않습니다(409).
- **현재 투입 현황**에서 투입 중인 배치, 대기 중인 공공 인력 수, 대피소 남은 수용량, 통제 구간을 보고, 작업이 끝나면 **배치 해제**로 자원을 다시 후보로 돌립니다(해제 기록은 `dispatch_releases`, 마이그레이션 003).

가상 데이터: `data/scenario/zones.json`(구역 10개, 이동 시간 규칙, 통제 구간), `data/scenario/resources.json`(공공 자원 10팀, 대피소 8곳). 실제 기관·시설과 관계없습니다.

- **다른 후보로 수정**(1순위·대안 중에서, 제약 위반이면 불가, 1순위와의 점수 차이 기록)과 **직접 지정**(목록의 어느 자원이든, 제약 위반이면 '예외 승인'과 사유가 있어야 확정, 위반 내용을 감사 로그에 기록)을 승인 대기열의 배치 카드에서 할 수 있습니다.
- **통제 구간 관리(O5)**: 배치 탭에서 통제 구간을 등록하거나 통제를 켜고 끕니다. 바뀐 상태는 다음 배치·봉사·물품 추천의 경로 제약에 바로 반영되고, 이미 투입 중인 배치 중 경로가 막힌 것을 알려 줍니다(자동으로 바꾸지는 않음). 안내문 생성 폼의 '통제 중인 구간 불러오기'로 안내문에도 넣을 수 있습니다.

**아직 없는 것:** 여러 신고 일괄 배치(`plan`), 근거 문장 LLM 생성, 통제 구간 변경 시 자동 재추천.

## 담당자 로그인 (2026-10-28)

관리자 화면과 담당자 API는 로그인한 `operator`(담당자) 또는 `admin`(관리자) 계정만 쓸 수 있습니다. 신고 접수(`POST /api/incidents`)와 접수번호 조회(`GET /api/incidents/{접수번호}`)는 로그인 없이 가능합니다.

1. 계정 만들기(서버를 실행하는 PC에서, 비밀번호는 화면에 보이지 않게 두 번 입력):
   `python scripts/create_user.py 담당자1 --role operator`
2. `http://127.0.0.1:8765/admin`에 들어가면 `/login`으로 이동하고, 로그인 후 관리자 화면이 열립니다.

| 항목 | 동작 |
|---|---|
| 비밀번호 | PBKDF2-SHA256(20만 회, 계정별 salt) 해시만 저장. 8자 이상 |
| 세션 | 무작위 토큰을 `sid` 쿠키(HttpOnly, SameSite=Strict)로 발급, DB에는 토큰 해시만 저장, 8시간 유효. 로그아웃 시 서버 세션 삭제 |
| 역할 | 목록·승인 대기열 조회, 승인·반려, 안내문 생성, 센서 판정은 operator/admin만(로그인 안 함 401, citizen 403) |
| CSRF | 담당자 POST는 `X-CSRF-Token` 헤더가 세션 토큰과 같아야 함(403). 모든 POST는 다른 사이트의 `Origin`·`Sec-Fetch-Site: cross-site`를 거부 |
| 로그인 시도 제한 | 같은 사용자 이름으로 10분 안에 5번 실패하면 10분 동안 429 |
| 처리자 기록 | 승인·반려 처리자는 요청 본문이 아니라 로그인 계정 이름으로 기록되고, 감사 로그에 user_id가 남음 |

새 API: `GET /login`, `POST /api/login`, `POST /api/logout`, `GET /api/me`. 마이그레이션 `002_sessions.sql`이 첫 실행 때 자동 적용됩니다.

**아직 남은 한계:** 실행 PC 밖(학교 내부망)으로 열려면 서버 바인딩 주소를 바꿔야 하는데, 지금은 HTTP라 내부망에서 비밀번호가 암호화되지 않은 채 오갑니다. 내부망에 열기 전에 HTTPS(또는 리버스 프록시)를 붙이고 `WEBAPP_COOKIE_SECURE=1`로 쿠키에 Secure를 켜야 합니다. 봉사자 역할과 계정 관리 화면은 없습니다(계정은 스크립트로만 생성).


## 2026-10-28 추가: 게이트웨이 · 문서 파싱 · 내부망 보안 · 시연 준비

| 작업 | 파일 | 안내 문서 |
|---|---|---|
| 마이크로비트 게이트웨이(① 실시간 센서 입력) | `microbit/*.py`, `gateway/microbit_gateway.py`, `webapp/sensors.py` | `docs/마이크로비트_게이트웨이.md` |
| 업스테이지 문서 파싱(⑤ PDF·사진 서류 읽기) | `common/docparse.py`, `POST /api/recovery/parse` | 아래 |
| 학교 내부망 배포 준비(보안 강화) | `webapp/server.py` | `docs/내부망_배포_점검표.md` |
| 시연 준비와 순서 | `scripts/demo_setup.py`, `data/demo/` | `docs/시연_시나리오.md` |

**센서 게이트웨이**: 서버와 게이트웨이에 같은 `SENSOR_GATEWAY_TOKEN`(16자 이상, `python gateway/microbit_gateway.py --new-token`)을 설정한다. 기기 없이 `--simulate flood`로 시험할 수 있다. 확정 후보는 승인 대기열에 한 번만 올라가고 단계는 담당자가 확정한다. 새 API: `POST /api/sensor/readings`(게이트웨이 토큰), `GET /api/sensor/status`(담당자).

**문서 파싱**: `LLM_MODE=solar`일 때 ⑤ 복구·지원 탭에서 PDF·PNG·JPG를 올리면 업스테이지 문서 파싱으로 텍스트를 뽑고 필드를 추출한다. 원본은 저장하지 않고, 외부 전송 전에 동의 체크가 필요하며, 원래 파일 이름도 보내지 않는다. 텍스트(.txt) 파일은 mock 모드에서도 외부로 보내지 않고 바로 읽는다. AI가 없을 때는 '구역: Z3', '세대주: …' 같은 서식을 규칙으로 읽는다.

**코드 리뷰로 고친 것**
- ② 접수: 신고문을 300자에서 잘라 뒤쪽의 인명구조 문장이 분류·저장에서 빠지던 문제 → 2000자(서버 입력 상한)까지 사용.
- ② 접수: AI 출력을 그대로 펼쳐 신고문 속 지시로 접수번호(id)·내용(text)을 바꿀 수 있던 문제 → 필요한 네 키만 사용. 인원이 true·음수면 AI 출력 버림.
- 중복 신고 검사가 마스킹 전 문장과 마스킹 후 저장 문장을 비교해, 전화번호가 있는 신고는 중복을 못 찾던 문제.
- ⑧ 물품 배치 승인: 승인을 기록한 뒤 출고가 실패하면 '승인됐지만 출고 안 됨'이 남던 문제 → 승인 전에 저장하지 않는 사본으로 출고를 먼저 시험.
- LLM 응답 캐시가 서버 실행 중 끝없이 늘던 문제(500개 상한). 서버 호출 한도를 `SOLAR_MAX_CALLS`로 조정 가능.
- 남은 설계 한계(고치지 않음): 통제 구간은 '두 구역 사이 직접 이동'만 막는다. Z1↔Z6이 통제여도 Z2를 거쳐 가는 경로는 계산하지 않는다(격자 이동 시간 규칙의 단순화).
