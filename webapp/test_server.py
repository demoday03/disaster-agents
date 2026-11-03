import json, os, secrets, subprocess, sys, tempfile, time, unittest, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from database import db


def make_users(db_path, users):
    """서버를 띄우기 전에 임시 DB에 시험용 계정을 만든다. users: {이름: 역할}. 비밀번호는 매번 새로 생성."""
    old = db.DB_PATH
    db.DB_PATH = Path(db_path)
    try:
        db.init_db()
        pw = {}
        for name, role in users.items():
            pw[name] = secrets.token_urlsafe(12)
            db.create_user(name, pw[name], role)
        return pw
    finally:
        db.DB_PATH = old


class Client:
    """쿠키(sid)와 CSRF 토큰을 들고 다니는 작은 HTTP 클라이언트."""
    def __init__(self, base, timeout=3):
        self.base, self.cookie, self.csrf, self.timeout = base, None, "", timeout

    def request(self, method, path, obj=None, headers=None, csrf=True):
        h = {"Content-Type": "application/json"}
        if self.cookie: h["Cookie"] = self.cookie
        if method == "POST" and csrf and self.csrf: h["X-CSRF-Token"] = self.csrf
        h.update(headers or {})
        data = json.dumps(obj).encode() if obj is not None else (b"{}" if method == "POST" else None)
        req = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read()), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), e.headers

    def get(self, path): return self.request("GET", path)[:2]
    def post(self, path, obj, **kw): return self.request("POST", path, obj, **kw)[:2]

    def login(self, username, password):
        status, d, headers = self.request("POST", "/api/login", {"username": username, "password": password})
        if status == 200:
            sid = headers["Set-Cookie"].split(";")[0]
            keep = [c for c in (self.cookie or "").split("; ") if c and not c.startswith("sid=")]   # 사이트 접속 코드 쿠키는 유지
            self.cookie = "; ".join(keep + [sid])
            self.csrf = d["csrf_token"]
        return status, d


class ServerBase(unittest.TestCase):
    """임시 DB와 시험 계정으로 서버를 띄우고 담당자로 로그인해 둔다."""
    port = None
    extra_env = {"WEBAPP_INCIDENT_PER_MIN": "1000"}   # 시험은 한 IP에서 신고를 많이 보낸다

    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory(prefix="disaster-db-test-")
        db_path = Path(cls.tempdir.name) / "test.db"
        cls.pw = make_users(db_path, {"담당자": "operator", "주민": "citizen", "잠금시험": "operator"})
        env = os.environ.copy(); env["WEBAPP_PORT"] = str(cls.port); env["LLM_MODE"] = "mock"
        env["DISASTER_DB_PATH"] = str(db_path)
        env.update(cls.extra_env)
        cls.proc = subprocess.Popen([sys.executable, "webapp/server.py"], cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.base = f"http://127.0.0.1:{cls.port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(cls.base + "/api/health", timeout=.2).read(); break
            except Exception: time.sleep(.1)
        else: raise RuntimeError("dashboard server did not start")
        cls.staff = Client(cls.base)
        if cls.extra_env.get("SITE_ACCESS_CODE") and cls.extra_env.get("SITE_PUBLIC") != "1":   # 접속 코드가 켜진 서버는 코드부터
            status, _, h = cls.staff.request("POST", "/api/access", {"code": cls.extra_env["SITE_ACCESS_CODE"]})
            assert status == 200
            cls.staff.cookie = h["Set-Cookie"].split(";")[0]
        assert cls.staff.login("담당자", cls.pw["담당자"])[0] == 200
    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try: cls.proc.wait(timeout=3)
        except subprocess.TimeoutExpired: cls.proc.kill()
        cls.tempdir.cleanup()
    def get(self, path): return self.staff.get(path)
    def post(self, path, obj): return self.staff.post(path, obj)

    def _flood_readings(self, now):
        rows = []
        for i in range(3):
            t = now - 2 + i
            rows += [{'node': 'A', 'sensor': 'water_low', 'ts': t, 'value': 600},
                     {'node': 'A', 'sensor': 'water_high', 'ts': t, 'value': 600},
                     {'node': 'B', 'sensor': 'tilt', 'ts': t, 'value': 0},
                     {'node': 'B', 'sensor': 'vibration', 'ts': t, 'value': 10}]
        return rows

    def _new_approval(self):
        now = time.time()
        status, d = self.post('/api/monitor/evaluate',
                              {'readings': self._flood_readings(now), 'now': now, 'rain_stage': 0})
        self.assertEqual(status, 200)
        self.assertTrue(d['needs_approval'])
        return d['approval_id']


class TestDashboardServer(ServerBase):
    port = 18765

    def test_health_and_dashboard(self):
        status, d=self.get('/api/health'); self.assertEqual(status,200); self.assertTrue(d['ok']); self.assertFalse(d['llm_enabled']); self.assertEqual(d['database'], 'SQLite'); self.assertTrue(d['database_ready'])
        with urllib.request.urlopen(self.base + '/', timeout=2) as r:
            self.assertEqual(r.status, 200); self.assertIn('재난 안전 AI 에이전트'.encode(), r.read())
    def test_incident_validation_and_classification(self):
        self.assertEqual(self.post('/api/incidents', {'text':' '})[0],400)
        self.assertEqual(self.post('/api/incidents', {'text':'x'*2001})[0],400)
        status,d=self.post('/api/incidents', {'text':'Z2 구역 침수, 어르신 2명 구조 요청'})
        self.assertEqual(status,201); self.assertEqual(d['output']['type'],'인명구조'); self.assertEqual(d['output']['urgency'],1)
        # 다른 테스트가 먼저 접수했을 수 있으므로 개수 대신 ID 포함 여부로 확인(실행 순서 무관)
        status, listing = self.get('/api/incidents'); self.assertEqual(status, 200)
        self.assertIn(d['output']['id'], [i['output']['id'] for i in listing['items']])
    def test_separate_report_and_admin_pages(self):
        with urllib.request.urlopen(self.base + '/report', timeout=2) as r:
            self.assertEqual(r.status, 200)
            self.assertIn('신고 접수'.encode(), r.read())
        with urllib.request.urlopen(self.base + '/admin', timeout=2) as r:
            self.assertEqual(r.status, 200)
            page = r.read()
            self.assertIn('관리자 화면'.encode(), page)
            self.assertIn(b'id="guidanceForm"', page)   # 안내문 생성 폼
        with urllib.request.urlopen(self.base + '/login', timeout=2) as r:
            self.assertIn(b'id="loginForm"', r.read())
        with urllib.request.urlopen(self.base + '/static/admin_ops.js', timeout=2) as r:
            self.assertIn('javascript', r.headers['Content-Type']); self.assertIn(b'opsInit', r.read())
        self.assertEqual(self.get('/static/../server.py')[0], 404)   # 정해진 파일만 제공

    def test_receipt_status_lookup(self):
        anon = Client(self.base)   # 신고 접수와 접수번호 조회는 로그인 없이 가능
        status, created = anon.post('/api/incidents', {'text':'Z2 구역 침수 신고'})
        self.assertIn(status, (200, 201))  # 중복 접수(200)도 허용: 실행 순서 무관
        incident_id = created['output']['id']
        status, found = anon.get('/api/incidents/' + incident_id)
        self.assertEqual(status, 200)
        self.assertEqual(found['id'], incident_id)
        self.assertEqual(found['status'], 'submitted')
        self.assertIsNone(found['guidance'])   # mock 모드에는 승인된 AI 안내문이 없다
        self.assertEqual(anon.get('/api/incidents/R-NOT-FOUND')[0], 404)

    def test_welfare_guidance_standard_fallback(self):
        status, created = self.post(
            '/api/incidents',
            {'text': 'Z2 구역 침수 신고'}
        )
        # 같은 문구가 앞선 테스트에서 이미 접수됐으면 중복(200), 아니면 신규(201).
        # 테스트 실행 순서에 의존하지 않도록 둘 다 허용한다.
        self.assertIn(status, (200, 201))
        incident_id = created['output']['id']

        status, result = self.post(
            f'/api/incidents/{incident_id}/guidance',
            {
                'level': '경계',
                'flags': ['침수'],
                'control_zones': ['Z2']
            }
        )

        self.assertEqual(status, 200)
        self.assertEqual(result['agent'], 'welfare')
        self.assertFalse(result['ai_used'])
        self.assertTrue(result['output']['displayable'])
        self.assertTrue(result['output']['text'])
        self.assertNotIn('approval_id', result)

    def test_welfare_guidance_rejects_invalid_inputs(self):
        status, _ = self.post(
            '/api/incidents/R-NOT-FOUND/guidance',
            {'level': '경계'}
        )
        self.assertEqual(status, 404)

        status, created = self.post(
            '/api/incidents',
            {'text': 'Z2 구역 침수 신고'}
        )
        # 같은 문구가 앞선 테스트에서 이미 접수됐으면 중복(200), 아니면 신규(201).
        # 테스트 실행 순서에 의존하지 않도록 둘 다 허용한다.
        self.assertIn(status, (200, 201))
        incident_id = created['output']['id']

        status, _ = self.post(
            f'/api/incidents/{incident_id}/guidance',
            {
                'level': '잘못된단계',
                'flags': [],
                'control_zones': []
            }
        )
        self.assertEqual(status, 400)
    def test_monitor_rejects_bad_inputs(self):
        self.assertEqual(self.post('/api/monitor/evaluate', {'readings':[], 'rain_stage':99})[0],400)
        self.assertEqual(self.post('/api/monitor/evaluate', {'readings':[], 'rain_stage':0})[0],200)
    def test_approval_requires_reason(self):
        # 존재하지 않는 승인 요청은 404
        self.assertEqual(self.post('/api/approvals/AP-NOPE/resolve', {'approved': True, 'reason': '확인'})[0], 404)
        # 승인 사유가 없으면 400
        self.assertEqual(self.post('/api/approvals/AP-NOPE/resolve', {'approved': True, 'reason': ''})[0], 400)
        self.assertEqual(self.post('/api/approvals/AP-NOPE/resolve', {'approved': True, 'reason': 'x' * 501})[0], 400)
        # 반려는 사유 없이도 입력 검증을 통과하며, 존재하지 않는 승인 ID이므로 최종 결과는 404
        self.assertEqual(self.post('/api/approvals/AP-NOPE/resolve', {'approved': False, 'reason': ''})[0], 404)

    def test_approval_lifecycle_and_double_resolve(self):
        aid = self._new_approval()
        _, pending = self.get('/api/approvals')
        self.assertIn(aid, [a['id'] for a in pending['items']])
        # approved가 bool이 아니면 400 (문자열 "true" 거부)
        self.assertEqual(self.post(f'/api/approvals/{aid}/resolve', {'approved': 'true', 'reason': '확인'})[0], 400)
        # 승인 사유 공백만 있으면 400
        self.assertEqual(self.post(f'/api/approvals/{aid}/resolve', {'approved': True, 'reason': '   '})[0], 400)
        # 본문에 다른 이름을 넣어도 처리자는 로그인한 계정으로 기록된다
        status, r = self.post(f'/api/approvals/{aid}/resolve',
                              {'approved': True, 'by': '다른 사람', 'reason': '센서 2개 동시 확인'})
        self.assertEqual(status, 200); self.assertEqual((r['state'], r['by']), ('approved', '담당자'))
        # 중복 승인·처리 후 반려 시도는 409
        self.assertEqual(self.post(f'/api/approvals/{aid}/resolve', {'approved': True, 'reason': '다시'})[0], 409)
        self.assertEqual(self.post(f'/api/approvals/{aid}/resolve', {'approved': False, 'reason': ''})[0], 409)
        _, pending = self.get('/api/approvals')
        self.assertNotIn(aid, [a['id'] for a in pending['items']])

    def test_mock_guidance_never_creates_approval(self):
        status, created = self.post('/api/incidents', {'text': 'Z5 구역 산사태 징후 신고 (mock 안내 테스트)'})
        self.assertIn(status, (200, 201))
        iid = created['output']['id']
        _, before = self.get('/api/approvals')
        status, g = self.post(f'/api/incidents/{iid}/guidance', {'level': '경계', 'flags': [], 'control_zones': []})
        self.assertEqual(status, 200)
        self.assertEqual(g['output']['status'], '표준 원문')
        self.assertNotIn('draft', g['output'])
        _, after = self.get('/api/approvals')
        self.assertEqual(len(before['items']), len(after['items']))

    def test_guidance_bad_types(self):
        status, created = self.post('/api/incidents', {'text': 'Z3 구역 도로 침수 (타입 테스트)'})
        iid = created['output']['id']
        for body in ({'level': 1}, {'level': '경계', 'flags': '침수'},
                     {'level': '경계', 'flags': [1]}, {'level': '경계', 'control_zones': ['']},
                     {'level': '경계', 'control_zones': ['Z'] * 21}):
            self.assertEqual(self.post(f'/api/incidents/{iid}/guidance', body)[0], 400, body)
        self.assertEqual(self.post('/api/incidents/' + 'X' * 41 + '/guidance', {'level': '경계'})[0], 400)
        self.assertEqual(self.post('/api/incidents', {'text': 123})[0], 400)
    def test_body_must_be_json_object(self):
        req=urllib.request.Request(self.base+'/api/incidents', data=b'[]', headers={'Content-Type':'application/json'}, method='POST')
        try: urllib.request.urlopen(req,timeout=2); self.fail('expected HTTP 400')
        except urllib.error.HTTPError as e: self.assertEqual(e.code,400)


class TestAuth(ServerBase):
    """로그인·역할·CSRF·출처 검사(별도 서버·DB)."""
    port = 18769

    def test_staff_endpoints_need_login(self):
        anon = Client(self.base)
        self.assertEqual(anon.get('/api/approvals')[0], 401)
        self.assertEqual(anon.get('/api/incidents')[0], 401)
        self.assertEqual(anon.get('/api/me')[0], 401)
        self.assertEqual(anon.post('/api/monitor/evaluate', {'readings': [], 'rain_stage': 0})[0], 401)
        self.assertEqual(anon.post('/api/approvals/AP-NOPE/resolve', {'approved': False})[0], 401)
        self.assertEqual(anon.post('/api/incidents/R-X/guidance', {'level': '경계'})[0], 401)

    def test_citizen_role_is_forbidden(self):
        c = Client(self.base)
        self.assertEqual(c.login('주민', self.pw['주민'])[0], 200)
        self.assertEqual(c.get('/api/approvals')[0], 403)
        self.assertEqual(c.post('/api/monitor/evaluate', {'readings': [], 'rain_stage': 0})[0], 403)

    def test_wrong_password_and_lockout(self):
        c = Client(self.base)
        self.assertEqual(c.login('없는계정', 'whatever-password')[0], 401)
        for _ in range(5):
            self.assertEqual(c.login('잠금시험', 'wrong-password')[0], 401)
        self.assertEqual(c.login('잠금시험', self.pw['잠금시험'])[0], 429)   # 맞는 비밀번호여도 잠김
        self.assertEqual(c.login('담당자', 'wrong-password')[0], 401)   # 다른 계정은 영향 없음

    def test_csrf_token_required(self):
        aid = self._new_approval()
        self.assertEqual(self.staff.post(f'/api/approvals/{aid}/resolve', {'approved': False}, csrf=False)[0], 403)
        self.assertEqual(self.staff.post(f'/api/approvals/{aid}/resolve', {'approved': False},
                                         headers={'X-CSRF-Token': 'wrong'})[0], 403)
        self.assertEqual(self.staff.post(f'/api/approvals/{aid}/resolve', {'approved': False})[0], 200)

    def test_cross_site_post_rejected(self):
        anon = Client(self.base)
        self.assertEqual(anon.post('/api/incidents', {'text': 'Z1 교차 출처 시험'},
                                   headers={'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(anon.post('/api/incidents', {'text': 'Z1 교차 출처 시험'},
                                   headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertIn(anon.post('/api/incidents', {'text': 'Z1 같은 출처 시험'},
                                headers={'Origin': self.base})[0], (200, 201))

    def test_cookie_flags_and_logout(self):
        c = Client(self.base)
        status, d, headers = c.request("POST", "/api/login", {"username": "담당자", "password": self.pw["담당자"]})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"]
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, cookie)
        c.cookie, c.csrf = cookie.split(";")[0], d["csrf_token"]
        self.assertEqual(c.get('/api/me')[1]['username'], '담당자')
        self.assertEqual(c.post('/api/logout', {})[0], 200)
        self.assertEqual(c.get('/api/me')[0], 401)   # 서버 쪽 세션도 삭제됨


class TestDispatch(ServerBase):
    """배치 추천(④) API: 추천 → 승인 대기열 → 승인(재검사) → 투입 현황 → 해제."""
    port = 18770
    extra_env = {**ServerBase.extra_env, "AUTO_DISPATCH_MAX_URGENCY": "0"}   # 담당자가 직접 요청하는 경로를 시험(자동 연결은 test_pipeline)

    def setUp(self):   # 테스트끼리 같은 서버를 쓰므로 앞 테스트의 투입을 모두 해제하고 시작
        for a in self.get('/api/dispatch')[1]['assignments']:
            self.post(f"/api/dispatch/{a['id']}/release", {})

    def incident(self, text):
        status, d = self.post('/api/incidents', {'text': text})
        self.assertEqual(status, 201, d)
        return d['output']['id']

    def dispatch(self, iid, **body):
        return self.post(f'/api/incidents/{iid}/dispatch', body)

    def approve(self, aid):
        return self.post(f'/api/approvals/{aid}/resolve', {'approved': True, 'reason': '배치 시험'})

    def test_public_flow_and_busy_resource_excluded(self):
        a = self.incident('Z1 지하에 사람이 갇혀 있어요 (배치 시험 1)')
        status, r = self.dispatch(a, kind='public')
        self.assertEqual(status, 200)
        self.assertEqual(r['output']['top']['id'], 'P01')          # 같은 구역·구조 역량
        self.assertEqual(len(r['output']['alternatives']), 2)
        self.assertTrue(r['output']['top']['name'])
        self.assertEqual(self.get('/api/dispatch')[1]['assignments'], [])   # 승인 전에는 반영 없음
        self.assertEqual(self.approve(r['approval_id'])[0], 200)
        _, st = self.get('/api/dispatch')
        self.assertEqual([x['id'] for x in st['assignments']], [r['approval_id']])
        self.assertFalse(next(p for p in st['public'] if p['id'] == 'P01')['available'])
        b = self.incident('Z1 사람이 갇혀 있어요 다른 건물 (배치 시험 2)')
        _, r2 = self.dispatch(b, kind='public')
        self.assertNotEqual(r2['output']['top']['id'], 'P01')       # 투입 중인 자원은 후보에서 제외
        self.assertIn('P01', [e['id'] for e in r2['output']['excluded']])
        self.assertEqual(self.post(f"/api/dispatch/{r['approval_id']}/release", {})[0], 200)
        self.assertEqual(self.post(f"/api/dispatch/{r['approval_id']}/release", {})[0], 409)   # 이미 해제
        self.assertTrue(next(p for p in self.get('/api/dispatch')[1]['public'] if p['id'] == 'P01')['available'])

    def test_double_booking_blocked_at_approval(self):
        a = self.incident('Z2 상가에 사람이 갇혀 있어요 (이중 투입 시험 1)')
        b = self.incident('Z2 상가 2층에 사람이 갇혀 있어요 (이중 투입 시험 2)')
        _, ra = self.dispatch(a, kind='public'); _, rb = self.dispatch(b, kind='public')
        self.assertEqual(ra['output']['top']['id'], rb['output']['top']['id'])   # 둘 다 같은 1순위
        self.assertEqual(self.approve(ra['approval_id'])[0], 200)
        status, err = self.approve(rb['approval_id'])
        self.assertEqual(status, 409); self.assertIn('이미 다른 배치', err['error'])
        self.assertIn(rb['approval_id'], [x['id'] for x in self.get('/api/approvals')[1]['items']])   # 대기 상태 유지

    def test_reject_then_recommend_excludes_rejected_top(self):
        iid = self.incident('Z3 반지하 침수됐어요 (재추천 시험)')
        _, r = self.dispatch(iid, kind='public')
        first = r['output']['top']['id']
        self.assertEqual(self.post(f"/api/approvals/{r['approval_id']}/resolve",
                                   {'approved': False, 'reason': '다른 현장 우선'})[0], 200)
        _, r2 = self.dispatch(iid, kind='public')
        self.assertNotEqual(r2['output']['top']['id'], first)
        self.assertIn('담당자 거절로 제외', {x['id']: x['reasons'] for x in r2['output']['excluded']}[first])

    def test_control_zone_route_excluded(self):
        iid = self.incident('Z6 논두렁에 사람이 고립됐어요 (통제 구간 시험)')
        _, r = self.dispatch(iid, kind='public')
        reasons = {x['id']: x['reasons'] for x in r['output']['excluded']}
        self.assertIn('통제 구간 경유', reasons.get('P01', []))     # Z1↔Z6 하천변 통제

    def test_shelter_capacity_updates_after_approval(self):
        iid = self.incident('Z4 학교 앞 주택 침수 대피 필요 (대피소 시험)')
        _, r = self.dispatch(iid, kind='shelter', people=10, amenities=['엘리베이터'])
        self.assertEqual(r['output']['top']['id'], 'S4')
        before = next(s for s in self.get('/api/dispatch')[1]['shelters'] if s['id'] == 'S4')['capacity_left']
        self.assertEqual(self.approve(r['approval_id'])[0], 200)
        after = next(s for s in self.get('/api/dispatch')[1]['shelters'] if s['id'] == 'S4')['capacity_left']
        self.assertEqual(before - after, 10)

    def test_control_zone_toggle_changes_recommendation(self):
        iid = self.incident('Z5 뒷산 토사가 마당까지 흘러내려요 (통제 구간 전환 시험)')
        _, r = self.dispatch(iid, kind='public')
        self.assertNotIn('P10', {x['id'] for x in r['output']['excluded']})
        self.post(f"/api/approvals/{r['approval_id']}/resolve", {'approved': False, 'reason': '정리'})
        status, c = self.post('/api/control-zones/C3/toggle', {'active': True})   # Z5↔Z10 통제 시작
        self.assertEqual(status, 200, c); self.assertTrue(c['active'])
        _, r2 = self.dispatch(iid, kind='public')
        self.assertIn('통제 구간 경유', {x['id']: x['reasons'] for x in r2['output']['excluded']}['P10'])
        self.post(f"/api/approvals/{r2['approval_id']}/resolve", {'approved': False, 'reason': '정리'})
        self.assertEqual(self.post('/api/control-zones/C3/toggle', {'active': False})[0], 200)
        status, new = self.post('/api/control-zones', {'name': '시험 구간', 'between': ['Z2', 'Z7']})
        self.assertEqual((status, new['active']), (201, True))
        self.assertIn(new['id'], [z['id'] for z in self.get('/api/dispatch')[1]['control_zones']])
        self.assertEqual(self.post('/api/control-zones', {'name': '중복', 'between': ['Z7', 'Z2']})[0], 409)
        self.assertEqual(self.post('/api/control-zones', {'name': '잘못', 'between': ['Z2', 'Z2']})[0], 400)
        self.assertEqual(self.post('/api/control-zones/C-NOPE/toggle', {'active': True})[0], 404)
        self.assertEqual(self.post(f"/api/control-zones/{new['id']}/toggle", {'active': False})[0], 200)

    def test_modify_and_direct_assignment(self):
        a = self.incident('Z3 지하에 사람이 갇혀 있어요 (수정 시험)')
        _, r = self.dispatch(a, kind='public')
        alt = r['output']['alternatives'][0]['id']
        self.assertEqual(self.post(f"/api/dispatch/{r['approval_id']}/modify", {'candidate': alt, 'reason': ''})[0], 400)
        self.assertEqual(self.post(f"/api/dispatch/{r['approval_id']}/modify", {'candidate': 'P10', 'reason': '목록 밖'})[0], 400)
        status, m = self.post(f"/api/dispatch/{r['approval_id']}/modify", {'candidate': alt, 'reason': '현장 판단'})
        self.assertEqual((status, m['decision'], m['state']), (200, 'modify', 'approved'))
        self.assertIsNotNone(m['score_diff'])
        st = self.get('/api/dispatch')[1]
        self.assertEqual(st['assignments'][0]['item']['recommendation']['top']['id'], alt)   # 고른 후보로 투입
        self.assertEqual(self.post(f"/api/dispatch/{r['approval_id']}/modify", {'candidate': alt, 'reason': 'x'})[0], 409)
        b = self.incident('Z3 반지하에 사람이 갇혀 있어요 (직접 지정 시험)')
        _, r2 = self.dispatch(b, kind='public')
        status, err = self.post(f"/api/dispatch/{r2['approval_id']}/direct", {'candidate': alt, 'reason': '같은 팀'})
        self.assertEqual(status, 409); self.assertIn('가용하지 않음', err['warnings'])   # 이미 투입 중
        status, d = self.post(f"/api/dispatch/{r2['approval_id']}/direct",
                              {'candidate': alt, 'reason': '현장 책임자 예외 승인', 'override': True})
        self.assertEqual((status, d['override']), (200, True)); self.assertIn('가용하지 않음', d['warnings'])
        status, d2 = self.post(f"/api/dispatch/{self.dispatch(self.incident('Z9 사람이 고립됐어요 (직접 지정 2)'), kind='public')[1]['approval_id']}/direct",
                               {'candidate': 'P10', 'reason': '가까운 지원반'})
        self.assertEqual((status, d2['override']), (200, False))   # 제약 위반 없으면 예외 승인 없이 확정

    def test_validation_and_auth(self):
        iid = self.incident('Z5 뒷산 토사 유출 (검증 시험)')
        self.assertEqual(self.dispatch(iid, kind='helicopter')[0], 400)
        _, first = self.dispatch(iid, kind='public')
        status, dup = self.dispatch(iid, kind='public')   # 대기 중인 추천이 있으면 중복 생성 안 함
        self.assertEqual((status, dup['approval_id']), (409, first['approval_id']))
        self.assertEqual(self.dispatch(iid, kind='shelter', people=0)[0], 400)
        self.assertEqual(self.dispatch(iid, kind='shelter', people=3, amenities=['수영장'])[0], 400)
        self.assertEqual(self.dispatch('R-NOPE', kind='public')[0], 404)
        supply = self.incident('Z1 생수랑 담요 보내주세요 (물품 시험)')
        self.assertEqual(self.dispatch(supply, kind='public')[0], 400)   # 물품 요청은 ⑧ 몫
        nozone = self.incident('어딘지 모르겠는데 사람이 갇혀 있어요 (구역 미상 시험)')
        self.assertEqual(self.dispatch(nozone, kind='public')[0], 400)
        self.assertEqual(self.post('/api/dispatch/AP-NOPE/release', {})[0], 404)
        anon = Client(self.base)
        self.assertEqual(anon.get('/api/dispatch')[0], 401)
        self.assertEqual(anon.post(f'/api/incidents/{iid}/dispatch', {'kind': 'public'})[0], 401)
        self.assertEqual(self.staff.post(f'/api/incidents/{iid}/dispatch', {'kind': 'public'}, csrf=False)[0], 403)


if __name__=='__main__': unittest.main()


class TestNetworkSecurity(ServerBase):
    """내부망 배포 대비: Host 허용 목록, 공개 신고 횟수 제한, 보안 헤더, 공개 조건 검사."""
    port = 18772
    extra_env = {"WEBAPP_INCIDENT_PER_MIN": "3"}

    def test_unknown_host_rejected(self):
        # DNS 리바인딩: 악성 사이트 주소(Host)로 들어온 요청은 처리하지 않는다
        for method in ("GET", "POST"):
            status, d, _ = self.staff.request(method, "/api/health" if method == "GET" else "/api/incidents",
                                              {"text": "Z1 침수"} if method == "POST" else None,
                                              headers={"Host": "evil.example:%d" % self.port})
            self.assertEqual(status, 421, (method, d))

    def test_incident_rate_limit_per_ip(self):
        codes = [Client(self.base).post("/api/incidents", {"text": f"Z2 정전 시험 {i}"})[0] for i in range(4)]
        self.assertEqual(codes, [201, 201, 201, 429])

    def test_security_headers(self):
        with urllib.request.urlopen(self.base + "/", timeout=2) as r:
            self.assertEqual(r.headers["X-Frame-Options"], "DENY")
            self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
            self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")

    def test_bind_policy(self):
        # server 모듈을 불러오면 기본 DB를 건드리므로 별도 프로세스에서 검사한다
        code = (
            "import json,sys;from webapp import server as s;"
            "cases={'local':s.check_bind_policy('127.0.0.1',{}),"
            "'lan_no_optin':s.check_bind_policy('192.168.0.10',{}),"
            "'all_ifaces':s.check_bind_policy('0.0.0.0',{'WEBAPP_ALLOW_LAN':'1'}),"
            "'no_tls':s.check_bind_policy('192.168.0.10',{'WEBAPP_ALLOW_LAN':'1','WEBAPP_ALLOWED_HOSTS':'x'},1),"
            "'http_ok':s.check_bind_policy('192.168.0.10',{'WEBAPP_ALLOW_LAN':'1','WEBAPP_ALLOW_HTTP_LAN':'1','WEBAPP_ALLOWED_HOSTS':'192.168.0.10:8765'},1),"
            "'no_staff':s.check_bind_policy('192.168.0.10',{'WEBAPP_ALLOW_LAN':'1','WEBAPP_ALLOW_HTTP_LAN':'1','WEBAPP_ALLOWED_HOSTS':'192.168.0.10:8765'},0)};"
            "print(json.dumps({k:v is None for k,v in cases.items()}))")
        env = os.environ.copy(); env["DISASTER_DB_PATH"] = str(Path(self.tempdir.name) / "policy.db")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1]),
                         {"local": True, "lan_no_optin": False, "all_ifaces": False, "no_tls": False,
                          "http_ok": True, "no_staff": False})


GATEWAY_TOKEN = "test-gateway-token-0123456789"


class TestSensorGateway(ServerBase):
    """마이크로비트 게이트웨이(가상 센서 모드) → 서버 → 감시 판정 → 승인 대기열."""
    port = 18773
    extra_env = {"WEBAPP_INCIDENT_PER_MIN": "1000", "SENSOR_GATEWAY_TOKEN": GATEWAY_TOKEN}

    def gateway(self, *args, token=GATEWAY_TOKEN):
        env = os.environ.copy(); env["SENSOR_GATEWAY_TOKEN"] = token
        return subprocess.run([sys.executable, "gateway/microbit_gateway.py", "--server", self.base, *args],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)

    def test_simulated_flood_reaches_approval_queue_once(self):
        out = self.gateway("--simulate", "flood", "--ticks", "6", "--interval", "0.05")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("승인 대기열에 올림", out.stdout)
        pending = [a for a in self.get("/api/approvals")[1]["items"] if a["kind"] == "monitor_stage"]
        self.assertEqual(len(pending), 1)
        self.assertEqual((pending[0]["item"]["source"], pending[0]["item"]["hazards"]), ("gateway", ["침수"]))
        status, st = self.get("/api/sensor/status")
        self.assertEqual(status, 200)
        self.assertTrue(st["gateway_enabled"])
        self.assertTrue(next(s for s in st["sensors"] if s["key"] == "A.water_low")["over"])
        self.assertEqual(Client(self.base).get("/api/sensor/status")[0], 401)   # 현황은 담당자만

    def send_events(self, *events):
        body = {"readings": [], "events": [{"node": n, "code": c, "seq": s} for n, c, s in events]}
        status, d, _ = Client(self.base).request("POST", "/api/sensor/readings", body, headers={"X-Gateway-Token": GATEWAY_TOKEN})
        self.assertEqual(status, 200, d)
        return d["events"]

    def test_simulated_sos_goes_to_volunteer_tab(self):
        out = self.gateway("--simulate", "sos", "--ticks", "2", "--interval", "0.05")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("현장 신호 E SOS 요청(봉사 탭 SOS 목록에 올림", out.stdout)
        st = self.get("/api/sensor/status")[1]
        sos = [e for e in st["events"] if e["code"] == "SOS" and e["node"] == "E"]
        self.assertEqual((len(sos), sos[0]["applied"], sos[0]["open"]), (1, True, False))   # 두 번 와도 한 건, 확인은 봉사 탭에서
        self.assertEqual([s["volunteer"] for s in self.get("/api/volunteer")[1]["sos"]], ["V01"])

    def test_field_buttons_update_shelter_and_welfare(self):
        occ = lambda: next(s["occupied"] for s in self.get("/api/dispatch")[1]["shelters"] if s["id"] == "S1")
        before = occ()
        ev = self.send_events(("C", "+1", 51), ("C", "+1", 52), ("C", "-1", 53), ("C", "GOT", 54))
        self.assertEqual([e.get("applied") for e in ev], [True, True, True, None])   # 물품 도착은 표시만
        self.assertEqual(occ(), before + 1)
        self.assertIn("S1 대피소 인원", ev[0]["note"])
        no_check = self.send_events(("D", "SAFE", 61))[0]                           # 안부 확인 전에는 반영 안 됨
        self.assertEqual((no_check["applied"], no_check["open"]), (False, False))
        self.assertIn("진행 중이 아닙니다", no_check["note"])
        self.assertEqual(self.post("/api/welfare/checks/start", {"zone": "Z1"})[0], 200)
        self.assertTrue(self.send_events(("D", "SAFE", 62))[0]["applied"])
        h01 = next(c for c in self.get("/api/welfare/checks")[1]["checks"] if c["household"] == "H01")
        self.assertNotIn(h01["state"], ("대기", "격상"))
        again = self.send_events(("D", "SAFE", 63))[0]
        self.assertEqual(again["applied"], False)                                   # 이미 응답한 가구
        no_task = self.send_events(("E", "IN", 71))[0]
        self.assertEqual(no_task["applied"], False)                                 # 투입 승인된 작업이 없으면 바꾸지 않음
        # 급한 신호 중 웹 기능이 없는 것(D SOS)은 센서 탭에서 담당자가 확인
        d_sos = self.send_events(("D", "SOS", 64))[0]
        self.assertEqual((d_sos["open"], d_sos.get("applied")), (True, None))
        self.assertEqual(Client(self.base).request("POST", f"/api/sensor/events/{d_sos['id']}/ack", {})[0], 401)
        status, d = self.post(f"/api/sensor/events/{d_sos['id']}/ack", {})
        self.assertEqual((status, d["open"]), (200, False))
        self.assertEqual(self.post("/api/sensor/events/EV999/ack", {})[0], 404)
        import sqlite3   # 처리자 '마이크로비트 C'로 남는지 감사 기록에서 확인
        conn = sqlite3.connect(Path(self.tempdir.name) / "test.db")
        try:
            audit = conn.execute("SELECT action, details_json FROM audit_logs").fetchall()
        finally:
            conn.close()   # 열어 두면 Windows에서 임시 폴더를 지우지 못한다
        actions = {a for a, _ in audit}
        self.assertTrue({"field_event_applied", "field_event_not_applied"} <= actions)
        self.assertTrue(any(a == "shelter_occupancy" and '"by": "마이크로비트 C"' in d for a, d in audit))

    def test_downlink_needs_gateway_token(self):
        anon = Client(self.base)
        self.assertEqual(anon.get("/api/sensor/downlink")[0], 401)
        self.assertEqual(self.get("/api/sensor/downlink")[0], 401)                # 담당자 쿠키로도 안 됨
        status, d, _ = anon.request("GET", "/api/sensor/downlink", headers={"X-Gateway-Token": GATEWAY_TOKEN})
        self.assertEqual((status, set(d)), (200, {"level", "level_name", "safety_check"}))

    def test_wrong_token_and_session_cannot_post(self):
        out = self.gateway("--simulate", "normal", "--ticks", "1", token="wrong-token-wrong-token")
        self.assertIn("HTTP 401", out.stdout)
        # 담당자 로그인 쿠키로도 게이트웨이 경로는 쓸 수 없다(토큰 필수)
        self.assertEqual(self.post("/api/sensor/readings", {"readings": []})[0], 401)


class TestReportCrosscheck(ServerBase):
    """신고 대조: 같은 구역 센서가 마른 상태인데 '허리까지 잠겼다'면 과장 가능, 센서가 젖어 있으면 일치. 긴급도는 그대로."""
    port = 18781
    extra_env = {"WEBAPP_INCIDENT_PER_MIN": "1000", "SENSOR_GATEWAY_TOKEN": GATEWAY_TOKEN}

    def water(self, low, high):
        now = time.time()
        body = {"readings": [{"node": "A", "sensor": "water_low", "value": low, "ts": now},
                             {"node": "A", "sensor": "water_high", "value": high, "ts": now}]}
        status, d, _ = Client(self.base).request("POST", "/api/sensor/readings", body, headers={"X-Gateway-Token": GATEWAY_TOKEN})
        self.assertEqual(status, 200, d)

    def report_crosscheck(self, text):
        code, d = Client(self.base).post("/api/incidents", {"text": text})
        self.assertEqual(code, 201, d)
        end = time.time() + 15
        while time.time() < end:   # 대조는 접수 뒤 백그라운드 후속 처리(안내문·배치 등과 함께)에서 붙는다
            o = next(x["output"] for x in self.get("/api/incidents")[1]["items"] if x["output"]["id"] == d["output"]["id"])
            if "crosscheck" in (o.get("linked") or {}):
                self.assertEqual(o["urgency"], d["output"]["urgency"])            # 대조 결과로 긴급도를 바꾸지 않음
                return o["linked"]["crosscheck"]
            time.sleep(.1)
        self.fail("신고 대조 결과가 붙지 않음")

    def test_exaggerated_then_consistent(self):
        self.water(30, 10)
        c = self.report_crosscheck("Z1 물이 허리까지 차서 집이 잠겼어요 (대조 시험 1)")
        self.assertEqual((c["verdict"], c["claimed"], c["sensor_level"]), ("과장 가능", 2, 0))
        self.assertIn("다시 확인", c["advice"])
        self.water(301, 307)
        c = self.report_crosscheck("Z1 침수돼서 물이 허리까지 찼어요 (대조 시험 2)")
        self.assertEqual((c["verdict"], c["similar_reports"]), ("일치", 1))
        c = self.report_crosscheck("Z3 침수돼서 물이 허리까지 찼어요 (대조 시험 3)")   # Z3에는 센서가 없다
        self.assertEqual(c["verdict"], "확인 불가")
        self.assertIn("crosscheck", {a["id"] for a in self.get("/api/agents")[1]["items"]})


class TestSensorGatewayDisabled(ServerBase):
    port = 18774

    def test_disabled_without_token(self):
        status, d, _ = Client(self.base).request("POST", "/api/sensor/readings", {"readings": []},
                                                 headers={"X-Gateway-Token": "x" * 20})
        self.assertEqual(status, 503, d)
        self.assertFalse(self.get("/api/sensor/status")[1]["gateway_enabled"])


class TestPublicStatus(ServerBase):
    """접수 사이트 첫 화면의 공개 상황: 로그인 없이 보이고, 확정된 단계만, 담당자 이름은 숨긴다."""
    port = 18777
    extra_env = {"WEBAPP_INCIDENT_PER_MIN": "1000", "KMA_API_KEY": ""}   # 시험에서는 기상청을 부르지 않는다

    def test_weather_without_key_degrades(self):
        self.assertFalse(Client(self.base).get("/api/public/status")[1]["weather"]["available"])
        status, w = self.get("/api/weather")
        self.assertEqual((status, w["available"]), (200, False))
        self.assertEqual(Client(self.base).get("/api/weather")[0], 401)
        self.assertEqual(self.post("/api/weather/apply-rain", {})[0], 409)   # 실제 강수 없이는 적용 안 됨

    def test_anonymous_sees_stage_shelters_without_staff_names(self):
        anon = Client(self.base)
        status, d = anon.get("/api/public/status")
        self.assertEqual(status, 200)
        self.assertEqual((d["stage"]["level"], d["stage"]["confirmed"]), ("정상", False))
        self.assertTrue(d["shelters"])
        self.assertTrue(all("capacity_left" in s for s in d["shelters"]))
        self.assertTrue(all({"id", "row", "col", "risk"} <= set(z) for z in d["zone_grid"]))   # 구역도용
        with urllib.request.urlopen(self.base + "/static/zonemap.js", timeout=2) as r:
            self.assertIn(b"renderZoneMap", r.read())
        aid = self._new_approval()
        self.assertFalse(anon.get("/api/public/status")[1]["stage"]["confirmed"])   # 승인 전에는 그대로
        self.assertEqual(self.post(f"/api/approvals/{aid}/resolve", {"approved": True, "reason": "센서 두 개 동시 확인"})[0], 200)
        d = anon.get("/api/public/status")[1]
        self.assertTrue(d["stage"]["confirmed"])
        self.assertIn("침수", d["stage"]["hazards"])
        text = json.dumps(d, ensure_ascii=False)
        self.assertNotIn("담당자", text)
        self.assertNotIn(aid, text)
        self.assertEqual(d["notices"], [])   # mock 모드는 안내문 승인 대기열을 만들지 않는다


class TestSiteAccessCode(ServerBase):
    """팀 공유용 배포: 링크와 접속 코드를 받은 사람만 화면·API를 쓴다."""
    port = 18778
    extra_env = {"WEBAPP_INCIDENT_PER_MIN": "1000", "SITE_ACCESS_CODE": "team-code-2026", "KMA_API_KEY": ""}

    def test_gate_blocks_until_code(self):
        anon = Client(self.base)
        status, d = anon.get("/api/public/status")
        self.assertEqual((status, d.get("need_access_code")), (401, True))
        self.assertEqual(anon.post("/api/incidents", {"text": "Z2 침수"})[0], 401)
        self.assertEqual(anon.get("/api/health")[0], 200)                       # 플랫폼 상태 확인은 열어 둔다
        req = urllib.request.Request(self.base + "/admin")
        with urllib.request.urlopen(req, timeout=3) as r:                       # 화면은 코드 입력 페이지로 보낸다
            self.assertIn("/access?next=/admin", r.geturl())
            self.assertIn("접속 코드".encode(), r.read())
        self.assertEqual(anon.post("/api/access", {"code": "wrong"})[0], 403)
        status, _, h = anon.request("POST", "/api/access", {"code": "team-code-2026"})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", h["Set-Cookie"])
        anon.cookie = h["Set-Cookie"].split(";")[0]
        self.assertEqual(anon.get("/api/public/status")[0], 200)
        self.assertEqual(anon.get("/api/incidents")[0], 401)                     # 코드가 있어도 담당자 기능은 로그인 필요
        self.assertEqual(self.get("/api/incidents")[0], 200)

    def test_cloud_bind_policy(self):
        # server 모듈을 불러오면 기본 DB를 건드리므로 별도 프로세스에서 검사한다(test_bind_policy와 같은 방식)
        code = ("import json;from webapp import server as s;c={'WEBAPP_CLOUD':'1'};k={**c,'SITE_ACCESS_CODE':'abcdef'};"
                "p={**c,'SITE_PUBLIC':'1'};"
                "print(json.dumps([s.check_bind_policy('0.0.0.0',c,1) is None,s.check_bind_policy('0.0.0.0',k,0) is None,"
                "s.check_bind_policy('0.0.0.0',k,1) is None,s.check_bind_policy('0.0.0.0',{},1) is None,"
                "s.check_bind_policy('0.0.0.0',p,1) is None,s.check_bind_policy('0.0.0.0',p,0) is None]))")
        env = os.environ.copy(); env["DISASTER_DB_PATH"] = str(Path(self.tempdir.name) / "policy.db")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        # 코드 없음 거부 · 담당자 없음 거부 · 둘 다 있으면 허용 · 클라우드가 아니면 0.0.0.0 금지 그대로
        # · SITE_PUBLIC=1을 명시하면 코드 없이 허용 · 그래도 담당자 계정은 필요
        self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1]), [False, False, True, False, True, False])


class TestSitePublic(ServerBase):
    """SITE_PUBLIC=1: 접속 코드가 설정돼 있어도 무시하고 누구나 연다. 담당자 기능은 그대로 로그인 필요."""
    port = 18782
    extra_env = {"WEBAPP_INCIDENT_PER_MIN": "1000", "SITE_PUBLIC": "1", "SITE_ACCESS_CODE": "team-code-2026", "KMA_API_KEY": ""}

    def test_open_without_code_but_staff_still_login(self):
        anon = Client(self.base)
        with urllib.request.urlopen(self.base + "/", timeout=2) as r:            # /access로 보내지 않는다
            self.assertEqual((r.status, r.url.rstrip("/")), (200, self.base))
        self.assertEqual(anon.get("/api/public/status")[0], 200)
        self.assertEqual(anon.post("/api/incidents", {"text": "Z1 침수됐어요 (공개 시험)"})[0], 201)
        self.assertEqual(anon.get("/api/incidents")[0], 401)                     # 담당자 기능은 로그인 필요
        self.assertEqual(anon.get("/api/approvals")[0], 401)
