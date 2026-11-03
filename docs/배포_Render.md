# Render로 팀 공유용 시연 서버 올리기

시연 사이트를 만든다. 무료 요금제 기준이다.
지금은 `SITE_PUBLIC=1`(render.yaml)이라 **접속 코드 없이 링크만으로** 누구나 들어온다(2026-11-03부터). 담당자 화면은 여전히 로그인해야 한다.
다시 팀원만 들어오게 하려면 `SITE_PUBLIC`을 `0`으로 바꾸고 `SITE_ACCESS_CODE`를 정한다(아래는 코드를 쓰는 경우의 설명).
모든 데이터는 가상이고, 실제 출동·문자 발송은 일어나지 않는다. 학교 프로젝트라면 올리기 전에 지도 선생님께 알린다.

## 1. 미리 정할 값 (메모장에 적어 두기)

| 이름 | 무엇 | 예 |
|---|---|---|
| `SITE_ACCESS_CODE` | 팀원에게 링크와 함께 줄 접속 코드(6자 이상) | `geoje-team-1029` 같은 문구 |
| `DEMO_ADMIN_PASSWORD` | 담당자 화면 `시연담당` 계정 비밀번호(8자 이상). 접속 코드와 다르게 | |
| `UPSTAGE_API_KEY` | Solar 키(이 PC에 저장한 것과 같은 값) | |
| `KMA_API_KEY` | 공공데이터포털 인증키(Decoding). 없으면 비워도 됨(날씨만 '확인 불가') | |

키 값은 Render 화면에만 입력한다. 저장소·채팅·문서에 적지 않는다.

## 2. Render에서 할 일

1. https://render.com 에 들어가 **GitHub로 로그인**한다(`demoday03` 계정). 계정 만들기와 로그인은 직접 한다.
2. 위쪽 **New +** → **Blueprint**를 고른다.
3. 저장소 `demoday03/disaster-agents`를 고른다. 처음이면 GitHub에서 Render에 저장소 접근을 허락하는 화면이 나온다.
4. 브랜치가 `claude/solar-connection-verify-byjenq`인지 확인한다(아직 main에 합치지 않았으므로).
5. Render가 `render.yaml`을 읽어 서비스 `disaster-agents`를 보여 준다. 비밀 값 4개 입력 칸에 1번에서 정한 값을 넣는다.
6. **Apply**(또는 Deploy)를 누른다. 처음 배포는 2~5분 걸린다.
7. 서비스 화면 위쪽의 주소(예: `https://disaster-agents-xxxx.onrender.com`)가 팀원에게 줄 **링크**다.

## 3. 확인

- 링크를 열면 **접속 코드** 화면이 나온다 → 코드 입력 → 시민 화면.
- `링크/admin` → 접속 코드(이미 넣었으면 생략) → 담당자 로그인(`시연담당` / `DEMO_ADMIN_PASSWORD`).
- `링크/volunteer` → 봉사자 화면.
- 문제가 있으면 Render 서비스 화면의 **Logs**를 본다. 서버가 안 뜨는 흔한 이유:
  - `SITE_ACCESS_CODE가 필요` → 접속 코드가 비었거나 6자 미만
  - `DEMO_ADMIN_PASSWORD(8자 이상)` → 비밀번호가 짧음
  - `LLM_MODE=solar requires UPSTAGE_API_KEY` → Solar 키를 안 넣음

## 4. 팀원에게 보낼 내용

```
재난 안전 AI 시연 사이트: https://(Render 주소)
접속 코드: (SITE_ACCESS_CODE)
담당자 화면은 링크 뒤에 /admin, 봉사자 화면은 /volunteer
처음 들어가면 30초~1분 걸릴 수 있어요(무료 서버가 깨어나는 중).
모든 데이터는 가상이고 실제 개인정보는 넣지 마세요.
```
담당자 비밀번호는 담당자 역할을 할 팀원에게만 따로 준다.

## 5. 알아 둘 점(무료 요금제)

- 15분쯤 아무도 안 들어오면 서버가 잠든다. 다음 첫 접속이 30초~1분 걸린다.
- 서버가 다시 시작되거나 새로 배포되면 **데이터가 처음 상태로 돌아간다**(신고 6건이 자동으로 다시 들어감). 시연 연습에는 오히려 편하다.
- Solar 호출은 서버가 켜져 있는 동안 300회(`SOLAR_MAX_CALLS`)까지만 하고, 넘으면 규칙으로만 동작한다. 공개 화면 요청은 접속 IP당 분당 30건으로 제한한다.
- 이 브랜치에 push하면 자동으로 다시 배포된다.
- 그만 공개하려면 Render 서비스 화면에서 **Suspend**(일시 중지) 또는 **Delete**. 접속 코드를 바꾸면(Environment에서 수정 → 저장) 예전 코드로 들어온 사람은 다시 코드를 넣어야 한다.
- 센서 게이트웨이(마이크로비트)는 이 배포에 연결하지 않는다(`SENSOR_GATEWAY_TOKEN` 없음 → 센서 입력 경로 닫힘). 센서 시연은 PC 로컬 서버로 한다.

## 6. 코드 쪽 동작(참고)

- `RENDER=true`(Render가 자동 설정) 또는 `WEBAPP_CLOUD=1`이면 클라우드 모드: `0.0.0.0:$PORT`로 열고, Render 주소를 허용 Host에 넣고, 쿠키에 Secure를 붙이고, 접속 IP는 `X-Forwarded-For`로 본다.
- 클라우드 모드는 접속 코드(6자 이상)와 담당자 계정이 없으면 시작하지 않는다(`check_bind_policy`). 단 `SITE_PUBLIC=1`을 명시하면 접속 코드 없이 시작하고, 설정된 접속 코드도 무시한다.
- 공개(`SITE_PUBLIC=1`)일 때 보호 장치: 담당자 기능은 로그인·CSRF 필요, 신고는 IP당 분당 제한, Solar 호출은 300회 상한.
- 접속 코드는 `/access`에서 받고, 맞으면 30일짜리 HttpOnly 쿠키를 준다. `/api/health`(Render 상태 확인)와 게이트웨이 경로만 코드 없이 열린다.
- `DEMO_SEED_ON_START=1`이고 계정이 없으면 시작할 때 `scripts/demo_setup.py`와 같은 시연 데이터를 넣는다.
