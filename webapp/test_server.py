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
    def __init__(self, base):
        self.base, self.cookie, self.csrf = base, None, ""

    def request(self, method, path, obj=None, headers=None, csrf=True):
        h = {"Content-Type": "application/json"}
        if self.cookie: h["Cookie"] = self.cookie
        if method == "POST" and csrf and self.csrf: h["X-CSRF-Token"] = self.csrf
        h.update(headers or {})
        data = json.dumps(obj).encode() if obj is not None else (b"{}" if method == "POST" else None)
        req = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status, json.loads(r.read()), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), e.headers

    def get(self, path): return self.request("GET", path)[:2]
    def post(self, path, obj, **kw): return self.request("POST", path, obj, **kw)[:2]

    def login(self, username, password):
        status, d, headers = self.request("POST", "/api/login", {"username": username, "password": password})
        if status == 200:
            self.cookie = headers["Set-Cookie"].split(";")[0]
            self.csrf = d["csrf_token"]
        return status, d


class ServerBase(unittest.TestCase):
    """임시 DB와 시험 계정으로 서버를 띄우고 담당자로 로그인해 둔다."""
    port = None

    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory(prefix="disaster-db-test-")
        db_path = Path(cls.tempdir.name) / "test.db"
        cls.pw = make_users(db_path, {"담당자": "operator", "주민": "citizen", "잠금시험": "operator"})
        env = os.environ.copy(); env["WEBAPP_PORT"] = str(cls.port); env["LLM_MODE"] = "mock"
        env["DISASTER_DB_PATH"] = str(db_path)
        cls.proc = subprocess.Popen([sys.executable, "webapp/server.py"], cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.base = f"http://127.0.0.1:{cls.port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(cls.base + "/api/health", timeout=.2).read(); break
            except Exception: time.sleep(.1)
        else: raise RuntimeError("dashboard server did not start")
        cls.staff = Client(cls.base)
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


if __name__=='__main__': unittest.main()
