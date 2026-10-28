"""현장 운영 기능 API(① 단계 확정, ③ 안부, ⑤ 복구, ⑥ 봉사, ⑦ 검증, ⑧ 물품) 시험. mock 모드, 실제 API 호출 없음.
실행: python -m unittest webapp.test_features -v"""
import time, unittest
from webapp.test_server import Client, ServerBase


class TestFeatures(ServerBase):
    port = 18771

    def ok(self, path, body=None, status=200):
        code, d = self.post(path, body or {}) if body is not None else self.get(path)
        self.assertEqual(code, status, d)
        return d

    def approve(self, aid, yes=True):
        return self.post(f'/api/approvals/{aid}/resolve', {'approved': yes, 'reason': '기능 시험'})

    # ① 단계 확정
    def test_stage_confirmed_after_monitor_approval(self):
        self.assertFalse(self.ok('/api/stage')['confirmed'])
        aid = self._new_approval()
        self.assertEqual(self.approve(aid)[0], 200)
        st = self.ok('/api/stage')
        self.assertEqual((st['level'], st['confirmed'], st['approved_by']), ('경계', True, '담당자'))

    # ③ 안부 확인·격상
    def test_welfare_checks_escalate_vulnerable_and_skip_no_consent(self):
        r = self.ok('/api/welfare/checks/start', {'zone': 'Z10', 'timeout_s': 5})
        self.assertEqual((r['started'], r['skipped_no_consent']), (['H19'], ['H20']))
        r = self.ok('/api/welfare/checks/start', {'zone': 'Z3', 'timeout_s': 5})
        self.assertEqual(sorted(r['started']), ['H05', 'H06', 'H07'])
        self.assertEqual(self.ok('/api/welfare/checks/H07/respond', {})['state'], '안전')
        self.assertEqual(self.post('/api/welfare/checks/H07/respond', {})[0], 409)
        time.sleep(3)   # 취약 가구는 대기 시간이 절반(2.5초)
        st = {c['household']: c['state'] for c in self.ok('/api/welfare/checks')['checks']}
        self.assertEqual((st['H05'], st['H06'], st['H07']), ('격상', '격상', '안전'))
        self.assertEqual(self.ok('/api/welfare/checks/H05/respond', {})['state'], '지연 응답')
        self.assertEqual(self.post('/api/welfare/checks/start', {'zone': 'Z99'})[0], 400)
        self.assertEqual(self.post('/api/welfare/checks/H99/respond', {})[0], 404)

    # ⑦ 검증
    def test_verify_standalone(self):
        docs = {d['id'] for d in self.ok('/api/verify/docs')['docs']}
        self.assertTrue({'G-001', 'R1'} <= docs)
        r = self.ok('/api/verify', {'draft': '재난지원금 500만원이 지급됩니다', 'doc_ids': ['R1']})
        self.assertEqual((r['verdict'], r['route'], r['blocked']), ('notGrounded', '차단', True))
        r = self.ok('/api/verify', {'draft': '주택 침수 세대는 350만원 대상일 수 있습니다', 'doc_ids': ['R1']})
        self.assertEqual(r['verdict'], 'notSure')   # 검증 모델 없음 → 담당자 확인
        self.assertFalse(r['model_connected'])
        self.assertEqual(self.post('/api/verify', {'draft': '안내', 'doc_ids': []})[0], 400)

    # ⑤ 복구·지원
    def test_recovery_support_report_case_survey(self):
        r = self.ok('/api/recovery/support', {'area': '거제시', 'damage_types': ['주택침수']})
        ids = {c['id'] for c in r['candidates']}
        self.assertTrue({'R1', 'R2', 'R3', 'R5'} <= ids)
        self.assertIn('R7', r['hidden'])                    # 확인 전 항목은 화면에 내보내지 않음
        self.assertIsNone(r['draft'])                       # 검증 통과 전 안내 문장 비표시
        self.assertEqual(r['verification']['verdict'], 'notSure')
        self.assertEqual(self.post('/api/recovery/support', {'damage_types': ['화재']})[0], 400)
        ex = self.ok('/api/recovery/extract', {'text': '임대차계약서 Z3 세대주 가구 A-05 주택 침수'})
        self.assertEqual((ex['fields']['zone'], ex['fields']['damage_type'], ex['ai_used']), ('Z3', '주택침수', False))   # mock: 규칙 추출
        self.assertTrue(self.ok('/api/recovery/extract', {'text': '흐릿한 서류'})['manual_form_required'])   # 못 읽으면 수동 입력 폼
        rp = self.ok('/api/recovery/report', {'household': 'H05', 'damage_types': ['주택침수'],
                                              'fields': {'zone': 'Z3', 'householder': '가구 H05', 'damage_type': '주택침수'}})
        self.assertIn('피해 사진', rp['checklist']); self.assertIn('초안', rp['status'])
        self.assertEqual(self.post('/api/recovery/report', {'damage_types': ['주택침수'], 'fields': {'zone': 'Z3'}})[0], 400)
        c = self.ok('/api/recovery/cases', {'household': 'H02'}, status=201)
        self.assertEqual(self.post('/api/recovery/cases', {'household': 'H02'})[0], 409)
        self.assertEqual(self.ok(f"/api/recovery/cases/{c['id']}/advance", {'to': '청소'})['stage'], '청소')
        self.assertEqual(self.post(f"/api/recovery/cases/{c['id']}/advance", {'to': '지원금'})[0], 409)   # 건너뛰기 불가
        self.ok('/api/recovery/surveys', {'zone': 'Z3', 'damage_type': '주택침수'}, status=201)
        self.ok('/api/recovery/surveys', {'zone': 'Z3', 'damage_type': '주택침수'}, status=201)
        self.assertEqual(self.ok('/api/recovery')['aggregate']['by_zone']['Z3']['주택침수'], 2)

    # ⑥ 봉사 코디
    def test_volunteer_flow(self):
        self.ok('/api/recovery/cases', {'household': 'H06'}, status=201)
        r = self.ok('/api/volunteer/requests', {'text': '마당 토사 치우고 가재도구 세척 부탁드려요', 'zone': 'Z3', 'household': 'H06'})
        tasks = {t['type']: t for t in r['tasks']}
        self.assertEqual(set(tasks), {'토사 제거', '가재도구 세척'})
        t = tasks['토사 제거']
        self.assertEqual(t['state'], '추천'); self.assertTrue(t['pending_approval'])
        self.assertEqual(self.approve(t['pending_approval'])[0], 200)
        team = {x['id']: x for x in self.ok('/api/volunteer')['tasks']}[t['id']]['team']
        busy = {v['id'] for v in self.ok('/api/volunteer')['volunteers'] if v['busy']}
        self.assertEqual(set(team), busy)
        self.assertEqual(self.post(f"/api/volunteer/tasks/{t['id']}/complete", {})[0], 409)   # 체크인 전 완료 불가
        self.assertEqual(self.ok(f"/api/volunteer/tasks/{t['id']}/checkin", {})['state'], '진행')
        done = self.ok(f"/api/volunteer/tasks/{t['id']}/complete", {})
        self.assertEqual(done['state'], '완료')
        self.assertIn(tasks['가재도구 세척']['id'], done['case_update']['remaining'])   # 남은 작업이 케이스에 표시
        self.assertFalse(any(v['busy'] for v in self.ok('/api/volunteer')['volunteers']))
        s = self.ok('/api/volunteer/sos', {'volunteer': 'V03', 'task': t['id']}, status=201)
        self.assertEqual(s['priority'], 1)
        self.ok(f"/api/volunteer/sos/{s['id']}/ack", {})
        self.assertEqual(self.post(f"/api/volunteer/sos/{s['id']}/ack", {})[0], 409)
        self.assertEqual(self.post('/api/volunteer/requests', {'text': '도와주세요', 'zone': 'Z99'})[0], 400)

    def test_volunteer_double_booking_blocked(self):
        a = self.ok('/api/volunteer/requests', {'text': '진흙 토사 제거', 'zone': 'Z8'})['tasks'][0]
        b = self.ok('/api/volunteer/requests', {'text': '토사 치워 주세요', 'zone': 'Z8'})['tasks'][0]
        self.assertEqual(self.approve(a['pending_approval'])[0], 200)
        status, err = self.approve(b['pending_approval'])
        self.assertEqual(status, 409); self.assertIn('이미 다른 작업', err['error'])
        self.assertEqual(self.approve(b['pending_approval'], yes=False)[0], 200)   # 반려 → 대기, 재추천 가능
        again = self.ok(f"/api/volunteer/tasks/{b['id']}/recommend", {})
        new_team = set(again['last_recommendation']['team'])
        self.assertFalse(new_team & set(b['last_recommendation']['team']))   # 반려된 인원 제외
        self.assertFalse(new_team & set(a['last_recommendation']['team']))   # 투입 중인 인원 제외
        for t in self.ok('/api/volunteer')['tasks']:   # 다른 테스트에 영향 없게 정리
            if t['state'] == '투입승인':
                self.post(f"/api/volunteer/tasks/{t['id']}/checkin", {}); self.post(f"/api/volunteer/tasks/{t['id']}/complete", {})

    # ⑧ 물품 수급
    def test_supply_request_to_receipt(self):
        r = self.ok('/api/supply/requests', {'household': 'H08', 'item': '기저귀', 'qty': 10, 'has_infant': True, 'when': '지금'})
        req = r['requests'][0]
        self.assertEqual((req['state'], req['route']), ('검토', 'matching'))
        self.assertLessEqual(req['urgency'], 2)
        before = {s['id']: s['qty'] for s in self.ok('/api/supply')['stock']}
        p = self.ok(f"/api/supply/requests/{req['id']}/review", {'decision': '승인'})
        aid = p['pending_approval']
        self.assertEqual({s['id']: s['qty'] for s in self.ok('/api/supply')['stock']}, before)   # 승인 전 출고 없음
        self.assertEqual(self.approve(aid)[0], 200)
        after = {s['id']: s['qty'] for s in self.ok('/api/supply')['stock']}
        self.assertEqual(sum(before.values()) - sum(after.values()), 10)
        self.assertEqual(self.ok(f"/api/supply/requests/{req['id']}/deliver", {})['state'], '이동')
        again = self.ok(f"/api/supply/requests/{req['id']}/receipt", {'received': False})   # 못 받음 → 재추천
        self.assertEqual(again['state'], '승인'); self.assertTrue(again['pending_approval'])
        self.assertEqual(self.approve(again['pending_approval'])[0], 200)
        self.ok(f"/api/supply/requests/{req['id']}/deliver", {})
        self.assertEqual(self.ok(f"/api/supply/requests/{req['id']}/receipt", {'received': True})['state'], '완료')

    def test_supply_approval_rechecks_stock_before_recording(self):
        # 승인 직전 출고가 불가능해졌으면(재고가 그사이 줄어듦) 승인을 기록하지 않고 409로 돌려준다
        from pathlib import Path
        from database import db
        r = self.ok('/api/supply/requests', {'household': 'H09', 'item': '생수', 'qty': 3})['requests'][0]
        aid = self.ok(f"/api/supply/requests/{r['id']}/review", {'decision': '승인'})['pending_approval']
        plan = next(a for a in self.ok('/api/approvals')['items'] if a['id'] == aid)['item']['plan']
        old = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "test.db"
        try:
            for row in plan:
                rec = db.get_record("stock", row['source']) or db.get_record("donation", row['source'])
                db.put_record("stock" if db.get_record("stock", row['source']) else "donation", row['source'], {**rec, "qty": 0})
        finally:
            db.DB_PATH = old
        code, d = self.approve(aid)
        self.assertEqual(code, 409, d)
        self.assertIn(aid, [a['id'] for a in self.ok('/api/approvals')['items']])   # 여전히 대기 중

    def test_supply_rules(self):
        med = self.ok('/api/supply/requests', {'household': 'H10', 'item': '처방약', 'qty': 1})['requests'][0]
        self.assertEqual(med['route'], 'manual_medicine')
        res = self.ok(f"/api/supply/requests/{med['id']}/review", {'decision': '승인'})
        self.assertNotIn('pending_approval', res)                          # 의약품은 자동 매칭 안 함
        m1 = self.ok('/api/supply/requests', {'household': 'H11', 'item': '분유', 'qty': 2})['requests'][0]
        m2 = self.ok('/api/supply/requests', {'household': 'H11', 'item': '분유', 'qty': 2})['requests'][0]
        self.assertEqual((m1['id'], m2['qty']), (m2['id'], 4))              # 같은 가구·품목은 병합
        self.assertIn('가구당 상한 초과: 담당자 검토', m2['flags'])
        memo = self.ok('/api/supply/requests', {'household': 'H12', 'memo': '담요랑 손전등이 필요해요'})['requests']
        self.assertEqual({x['item'] for x in memo}, {'담요', '손전등'})
        self.assertEqual(self.post(f"/api/supply/requests/{m1['id']}/review", {'decision': '반려'})[0], 400)   # 반려 사유 필요
        for bad in ({'donor': 'V01', 'item': '처방약', 'qty': 1, 'unit': '개', 'condition': '새것', 'zone': 'Z1'},
                    {'donor': 'V01', 'item': '즉석밥', 'qty': 5, 'unit': '개', 'condition': '새것', 'days_left': -1, 'zone': 'Z1'},
                    {'donor': 'V01', 'item': '담요', 'qty': 5, 'unit': '장', 'condition': '파손', 'zone': 'Z1'}):
            self.assertEqual(self.post('/api/supply/donations', bad)[0], 400, bad)
        d = self.ok('/api/supply/donations', {'donor': 'V01', 'item': '담요', 'qty': 5, 'unit': '장',
                                              'condition': '새것', 'zone': 'Z1'}, status=201)
        self.assertEqual(d['state'], '검수 대기')
        self.assertEqual(self.ok(f"/api/supply/donations/{d['id']}/inspect", {'accept': True})['state'], '접수 가능')
        self.assertEqual(self.post(f"/api/supply/donations/{d['id']}/inspect", {'accept': True})[0], 409)
        st = self.ok('/api/supply')
        self.assertTrue(st['forecast'])
        self.assertNotIn('처방약', {f['item'] for f in st['forecast']})

    def test_recovery_parse_text_file_and_limits(self):
        import base64
        b64 = lambda b: base64.b64encode(b).decode()
        doc = "피해 신고서\n구역 Z3\n세대주 가구 H05 연락처 010-1234-5678\n피해 내용 주택침수".encode()
        r = self.ok('/api/recovery/parse', {'filename': 'a.txt', 'data_base64': b64(doc)})
        self.assertEqual(r['parser'], 'text')
        self.assertIn('구역 Z3', r['text'])
        self.assertNotIn('1234-5678', r['text'])                               # 화면에는 마스킹한 텍스트만
        self.assertEqual(r['fields'], {'zone': 'Z3', 'householder': '가구 H05 연락처 [전화]', 'damage_type': '주택침수'})
        self.assertFalse(r['ai_used'])                                         # mock 모드: 규칙 추출
        pdf = b64(b"%PDF-1.4 fake")
        self.assertEqual(self.post('/api/recovery/parse', {'filename': 'a.pdf', 'data_base64': pdf})[0], 503)   # 파싱 API 없음
        self.assertEqual(self.post('/api/recovery/parse', {'filename': 'a.exe', 'data_base64': b64(b"MZ\x90\x00\x01")})[0], 400)
        self.assertEqual(self.post('/api/recovery/parse', {'filename': 'a.txt', 'data_base64': '***'})[0], 400)
        self.assertEqual(Client(self.base).post('/api/recovery/parse', {'filename': 'a.txt', 'data_base64': b64(doc)})[0], 401)
        self.assertFalse(self.ok('/api/recovery')['doc_parser'])

    def test_auth(self):
        anon = Client(self.base)
        for path in ('/api/supply', '/api/volunteer', '/api/recovery', '/api/welfare/checks', '/api/stage', '/api/verify/docs'):
            self.assertEqual(anon.get(path)[0], 401, path)
        self.assertEqual(anon.post('/api/verify', {'draft': 'x', 'doc_ids': ['R1']})[0], 401)
        self.assertEqual(self.staff.post('/api/verify', {'draft': 'x', 'doc_ids': ['R1']}, csrf=False)[0], 403)
        self.assertEqual(self.get('/api/supply/nothing')[0], 404)


if __name__ == "__main__":
    unittest.main()
