"""LLM_MODE=solar 경로를 실제 API 없이 검사한다.
로컬 가짜 OpenAI 호환 서버(127.0.0.1)를 띄우고 SOLAR_BASE_URL을 그쪽으로 돌린다.
UPSTAGE_API_KEY에는 가짜 문자열만 넣는다. 실제 Upstage 호출은 일어나지 않는다.
실행: python -m unittest webapp.test_server_solar -v
"""
import json, os, sqlite3, subprocess, sys, tempfile, threading, time, unittest, urllib.error, urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from webapp import test_server as ts   # 시험 계정 생성·로그인 클라이언트 재사용(모듈로 가져와 테스트 중복 실행 방지)

ROOT = Path(__file__).resolve().parents[1]
FAKE_PORT, APP_PORT, APP_PORT_NOVERIFY = 18766, 18767, 18768
STATE = {"verdict": "grounded", "label": "가", "error": False, "verify_error": False, "calls": []}


class FakeUpstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path.endswith("/document-digitization"):   # 업스테이지 문서 파싱(multipart)
            STATE["calls"].append("document-parse")
            STATE["parse_body"] = raw
            if STATE["error"]:
                self.send_response(500); self.end_headers(); return
            text = "피해 신고서 구역 Z3 세대주 가구 H05 연락처 010-9876-5432 피해 내용 주택침수"
            out = json.dumps({"content": {"text": text}, "usage": {"pages": 1}}, ensure_ascii=False).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers()
            self.wfile.write(out); return
        body = json.loads(raw)
        STATE["calls"].append(body["model"])
        if STATE["error"]:
            self.send_response(500); self.end_headers(); return
        judge = body["messages"][0]["content"].startswith("너는 재난 안내문 검증기다")   # solar 모델 판정(폴백)
        if body["model"] == "fake-ground" or judge:
            if STATE["verify_error"]:
                self.send_response(500); self.end_headers(); return
            content = STATE["verdict"]
        elif "서류 텍스트에서 필드를 추출" in body["messages"][0]["content"]:   # ⑤ 서류 필드 추출
            STATE["extract_input"] = body["messages"][1]["content"]
            content = json.dumps({"zone": "Z3", "householder": "가구 H05", "damage_type": "주택침수"}, ensure_ascii=False)
        else:  # 안내문 다듬기: 숫자 없는 초안(숫자 선검사에 걸리지 않게)
            content = json.dumps({"text": f"AI가 다듬은 안내({STATE['label']}) 낮은 곳을 피해 이동하세요"},
                                 ensure_ascii=False)
        out = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers(); self.wfile.write(out)


def start_app(port, db_path, groundedness=True):
    env = os.environ.copy()
    env.update({"WEBAPP_PORT": str(port), "LLM_MODE": "solar",
                "UPSTAGE_API_KEY": "fake-key-for-offline-test",
                "SOLAR_BASE_URL": f"http://127.0.0.1:{FAKE_PORT}/v1",
                "SOLAR_MODEL": "fake-solar", "DISASTER_DB_PATH": str(db_path),
                "DOCUMENT_PARSE_URL": f"http://127.0.0.1:{FAKE_PORT}/v1/document-digitization",
                "WEBAPP_INCIDENT_PER_MIN": "1000",
                "AUTO_FOLLOWUPS": "0"})   # 여기서는 담당자가 요청하는 안내문 경로를 시험(자동 후속 처리는 test_pipeline)
    if groundedness:
        env["GROUNDEDNESS_MODEL"] = "fake-ground"
    else:
        env.pop("GROUNDEDNESS_MODEL", None)
        env["VERIFY_MODEL"] = "fake-judge"   # 안내문 작성(fake-solar)과 다른 판정 모델
    proc = subprocess.Popen([sys.executable, "webapp/server.py"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=.2).read(); return proc, base
        except Exception:
            time.sleep(.1)
    proc.kill(); raise RuntimeError("server did not start")


class SolarPathBase(unittest.TestCase):
    groundedness = True
    port = APP_PORT

    @classmethod
    def setUpClass(cls):
        cls.fake = ThreadingHTTPServer(("127.0.0.1", FAKE_PORT), FakeUpstream)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory(prefix="disaster-solar-test-")
        cls.db_path = Path(cls.tmp.name) / "t.db"
        pw = ts.make_users(cls.db_path, {"담당자": "operator"})
        cls.proc, cls.base = start_app(cls.port, cls.db_path, cls.groundedness)
        cls.client = ts.Client(cls.base)
        assert cls.client.login("담당자", pw["담당자"])[0] == 200

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try: cls.proc.wait(timeout=3)
        except subprocess.TimeoutExpired: cls.proc.kill()
        cls.fake.shutdown(); cls.fake.server_close(); cls.tmp.cleanup()

    def setUp(self):
        STATE.update(verdict="grounded", error=False, verify_error=False, calls=[])

    def get(self, path):
        return self.client.get(path)

    def post(self, path, obj):
        return self.client.post(path, obj)

    def incident(self, text):
        STATE["error"] = True   # 접수 분류는 규칙 폴백으로(이 테스트의 대상이 아님)
        status, d = self.post("/api/incidents", {"text": text})
        STATE["error"] = False
        self.assertIn(status, (200, 201))
        return d["output"]["id"]

    def guidance(self, iid, flag):
        STATE["calls"] = []
        return self.post(f"/api/incidents/{iid}/guidance",
                         {"level": "경계", "flags": [flag], "control_zones": []})


class TestSolarPathWithVerifier(SolarPathBase):
    def test_grounded_goes_to_queue_and_ai_text_is_withheld(self):
        iid = self.incident("Z2 구역 침수 (grounded 테스트)")
        STATE["label"] = "가"
        status, g = self.guidance(iid, "flag-grounded")
        self.assertEqual(status, 200)
        self.assertIn("fake-ground", STATE["calls"])           # 검증 모델이 실제로 호출됨
        self.assertIn("approval_id", g)
        out = g["output"]
        self.assertEqual(out["verdict"], "grounded")
        self.assertNotIn("AI가 다듬은", out["text"])          # 승인 전 AI 문구 비노출
        self.assertEqual(out["text"], out["standard"])
        self.assertNotIn("draft", out)
        self.assertTrue(out["ai_text_withheld"])
        _, q = self.get("/api/approvals")
        item = [a for a in q["items"] if a["id"] == g["approval_id"]][0]
        self.assertIn("AI가 다듬은 안내(가)", item["item"]["guidance"]["draft"])  # 담당자는 초안을 볼 수 있음

    def queue_for(self, iid, kind):
        _, q = self.get("/api/approvals")
        return [a for a in q["items"] if a["kind"] == kind and a["item"].get("incident_id") == iid]

    def test_not_grounded_is_blocked_logged_and_not_queued(self):
        iid = self.incident("Z2 구역 침수 (차단 테스트)")
        STATE.update(verdict="notGrounded", label="나")
        status, g = self.guidance(iid, "f-ng")
        self.assertEqual(status, 200)
        self.assertTrue(g["blocked"])
        self.assertNotIn("approval_id", g); self.assertNotIn("review_id", g)
        self.assertNotIn("AI가 다듬은", json.dumps(g, ensure_ascii=False))
        self.assertEqual(self.queue_for(iid, "welfare_guidance"), [])
        self.assertEqual(self.queue_for(iid, "welfare_guidance_review"), [])
        with closing(sqlite3.connect(self.db_path)) as conn:   # Windows: 연결을 닫아야 임시 DB 삭제 가능
            n = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='guidance_blocked' AND target_id=?",
                             (iid,)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_not_sure_goes_to_review_queue_without_exposing_draft(self):
        iid = self.incident("Z2 구역 침수 (불확실 테스트)")
        cases = [("notSure", "다", "f-ns", False), ("not grounded", "라", "f-amb", False),
                 ("ungrounded", "마", "f-ung", False), ("grounded", "바", "f-verr", True)]
        review_ids = []
        for verdict, label, flag, verify_err in cases:
            STATE.update(verdict=verdict, label=label, verify_error=verify_err)
            status, g = self.guidance(iid, flag)
            self.assertEqual(status, 200, verdict)
            self.assertEqual(g["output"]["verdict"], "notSure", verdict)   # 모호·호출 실패도 notSure
            self.assertIn("review_id", g, verdict)
            self.assertNotIn("approval_id", g, verdict)
            self.assertNotIn("AI가 다듬은", json.dumps(g, ensure_ascii=False), verdict)
            self.assertEqual(g["output"]["text"], g["output"]["standard"])
            review_ids.append((g["review_id"], label))
        queued = {a["id"]: a for a in self.queue_for(iid, "welfare_guidance_review")}
        for rid, label in review_ids:
            self.assertIn(f"AI가 다듬은 안내({label})", queued[rid]["item"]["guidance"]["draft"])
        self.assertEqual(self.queue_for(iid, "welfare_guidance"), [])     # 승인 대기열(통과분)과 섞이지 않음

    def test_review_item_resolution_rules(self):
        iid = self.incident("Z2 구역 침수 (확인 처리 테스트)")
        STATE.update(verdict="notSure", label="사")
        _, g = self.guidance(iid, "f-res")
        rid = g["review_id"]
        path = f"/api/approvals/{rid}/resolve"
        self.assertEqual(self.post(path, {"approved": True, "by": "담당자", "reason": ""})[0], 400)
        status, r = self.post(path, {"approved": True, "by": "담당자", "reason": "근거 문서와 대조함"})
        self.assertEqual((status, r["state"]), (200, "approved"))
        self.assertEqual(self.post(path, {"approved": False, "by": "담당자", "reason": "x"})[0], 409)
        self.assertEqual(self.queue_for(iid, "welfare_guidance_review"), [])

    def resolve(self, aid, approved):
        return self.post(f"/api/approvals/{aid}/resolve",
                         {"approved": approved, "by": "담당자", "reason": "근거 문서와 대조함"})

    def test_approved_guidance_is_shown_on_receipt_lookup(self):
        iid = self.incident("Z2 구역 침수 (승인 후 표시 테스트)")
        STATE.update(verdict="grounded", label="아")
        _, g = self.guidance(iid, "f-show")
        self.assertIsNone(self.get(f"/api/incidents/{iid}")[1]["guidance"])   # 승인 전에는 표시 안 함
        self.assertEqual(self.resolve(g["approval_id"], True)[0], 200)
        shown = self.get(f"/api/incidents/{iid}")[1]["guidance"]
        self.assertIn("AI가 다듬은 안내(아)", shown["text"])
        self.assertEqual((shown["verdict"], shown["approved"]), ("grounded", True))
        self.assertEqual(shown["evidence"], g["output"]["standard"])          # 근거 원문을 함께 보여 줌
        self.assertEqual(shown["doc_ids"], g["output"]["doc_ids"])
        self.assertNotIn("담당자", json.dumps(shown, ensure_ascii=False))   # 승인자 이름은 공개하지 않음

    def test_approved_guidance_on_public_status_until_invalidated(self):
        iid = self.incident("Z2 구역 침수 (공개 상황판 테스트)")
        STATE.update(verdict="grounded", label="카")
        _, g = self.guidance(iid, "f-pub")
        anon = ts.Client(self.base)
        notices = lambda: [n for n in anon.get("/api/public/status")[1]["notices"] if "AI가 다듬은 안내(카)" in n["text"]]
        self.assertEqual(notices(), [])   # 승인 전에는 공개하지 않음
        self.assertEqual(self.resolve(g["approval_id"], True)[0], 200)
        shown = notices()
        self.assertEqual(len(shown), 1)
        self.assertEqual((shown[0]["zone"], shown[0]["evidence"]), ("Z2", g["output"]["standard"]))
        self.assertNotIn("공개 상황판 테스트", json.dumps(shown, ensure_ascii=False))   # 신고 원문은 내보내지 않음
        self.assertEqual(self.post(f"/api/incidents/{iid}/invalidate", {"reason": "현장 이상 없음"})[0], 200)
        self.assertEqual(notices(), [])   # 오접수 처리하면 내려감

    def test_rejected_or_review_guidance_is_not_shown(self):
        iid = self.incident("Z2 구역 침수 (반려·확인 건 비표시 테스트)")
        STATE.update(verdict="grounded", label="자")
        _, g = self.guidance(iid, "f-rej")
        self.assertEqual(self.resolve(g["approval_id"], False)[0], 200)
        STATE.update(verdict="notSure", label="차")
        _, r = self.guidance(iid, "f-rev")
        self.assertEqual(self.resolve(r["review_id"], True)[0], 200)   # 검증 불확실 건은 승인해도 표시 안 함
        self.assertIsNone(self.get(f"/api/incidents/{iid}")[1]["guidance"])

    def test_upstream_failure_falls_back_to_standard(self):
        iid = self.incident("Z2 구역 침수 (장애 테스트)")
        STATE["error"] = True
        status, g = self.guidance(iid, "f-err")
        self.assertEqual(status, 200)
        self.assertFalse(g["ai_used"])
        self.assertEqual(g["output"]["status"], "표준 원문")
        self.assertNotIn("approval_id", g)


    def test_document_parse_then_extract_fields(self):
        import base64
        body = {"filename": "홍길동_계약서.pdf", "data_base64": base64.b64encode(b"%PDF-1.4 fake").decode()}
        status, d = self.post("/api/recovery/parse", body)
        self.assertEqual(status, 400, d)                                      # 외부 전송 동의 없으면 보내지 않음
        self.assertNotIn("document-parse", STATE["calls"])
        status, d = self.post("/api/recovery/parse", {**body, "consent_external": True})
        self.assertEqual(status, 200, d)
        self.assertEqual((d["parser"], d["pages"], d["ai_used"]), ("upstage", 1, True))
        self.assertEqual(d["fields"], {"zone": "Z3", "householder": "가구 H05", "damage_type": "주택침수"})
        self.assertFalse(d["manual_form_required"])
        self.assertNotIn("9876", d["text"])                                   # 화면에 돌려주는 텍스트는 마스킹
        self.assertNotIn("9876", STATE["extract_input"])                      # LLM에도 마스킹한 텍스트만
        self.assertNotIn("홍길동".encode(), STATE["parse_body"])               # 원래 파일 이름은 보내지 않음
        STATE["error"] = True                                                 # 파싱 API 오류 → 502, 수동 입력 안내
        status, d = self.post("/api/recovery/parse", {**body, "consent_external": True})
        self.assertEqual(status, 502, d)
        self.assertIn("직접 붙여 넣어", d["error"])


class TestSolarPathJudgeFallback(SolarPathBase):
    groundedness = False
    port = APP_PORT_NOVERIFY

    def test_without_groundedness_model_solar_judges(self):
        iid = self.incident("Z2 구역 침수 (solar 판정 테스트)")
        status, g = self.guidance(iid, "f-judge")
        self.assertEqual(status, 200)
        self.assertNotIn("fake-ground", STATE["calls"])
        self.assertEqual(STATE["calls"], ["fake-solar", "fake-judge"])   # 안내문 다듬기(작성 모델) + 판정(다른 모델)
        self.assertEqual(g["output"]["verdict"], "grounded")
        self.assertIn("approval_id", g)
        _, q = self.get("/api/approvals")
        item = next(a for a in q["items"] if a["id"] == g["approval_id"])["item"]
        self.assertEqual(item["verifier"], {"model": "fake-judge", "mode": "judge", "same_as_writer": False})
        STATE.update(verdict="notGrounded", label="사")
        status, g = self.guidance(iid, "f-judge-ng")
        self.assertEqual(status, 200)
        self.assertTrue(g["blocked"])
        self.assertNotIn("approval_id", g)

if __name__ == "__main__":
    unittest.main()
