"""신고 → 다음 에이전트 연결과 예외 처리(잘못된 신고·필수 정보 누락·에이전트 오류) 시험. mock 모드, 실제 API 호출 없음.
실행: python -m unittest webapp.test_pipeline -v"""
import time, unittest
from pathlib import Path
from database import db
from webapp.test_server import Client, ServerBase


class TestPipeline(ServerBase):
    port = 18775

    def report(self, text):
        code, d = Client(self.base).post("/api/incidents", {"text": text})   # 신고는 로그인 없이
        self.assertEqual(code, 201, d)
        return d["output"]

    def staff_view(self, iid):
        """담당자용 신고 목록의 한 건(공개 응답에서 빠지는 메모·처리자까지 들어 있음)."""
        return next(x["output"] for x in self.get("/api/incidents")[1]["items"] if x["output"]["id"] == iid)

    def supply_requests(self, iid):
        _, s = self.get("/api/supply")
        return [r for r in s["requests"] if r.get("incident_id") == iid]

    def pending(self, iid):
        _, q = self.get("/api/approvals")
        return [a for a in q["items"] if a["item"].get("incident_id") == iid]

    def stock(self, sid):
        _, s = self.get("/api/supply")
        return next(x["qty"] for x in s["stock"] + s["donations"] if x["id"] == sid)

    def test_supply_report_goes_to_one_final_approval(self):
        o = self.report("Z3 구역 주민입니다. 생수 20병 필요해요")
        self.assertEqual((o["type"], o["state"], o["missing"]), ("물품 요청", "접수", []))
        self.assertEqual(len(o["linked"]["supply"]), 1)
        req = self.supply_requests(o["id"])[0]
        self.assertEqual((req["item"], req["qty"], req["state"]), ("생수", 20, "검토"))   # 승인 전에는 출고 없음
        [a] = self.pending(o["id"])
        self.assertEqual(a["kind"], "supply_dispatch"); self.assertTrue(a["item"]["combined_review"])
        before = sum(self.stock(r["source"]) for r in a["item"]["plan"])
        code, d = self.post(f"/api/approvals/{a['id']}/resolve", {"approved": True, "reason": "확인함"})
        self.assertEqual(code, 200, d)
        req = self.supply_requests(o["id"])[0]
        self.assertEqual(req["state"], "배치")
        self.assertEqual([h["to"] for h in req["history"]], ["검토", "승인", "배치"])
        self.assertEqual({h["by"] for h in req["history"][1:]}, {"담당자"})   # 검토·배치 모두 사람이 결정
        self.assertEqual(sum(self.stock(r["source"]) for r in a["item"]["plan"]), before - 20)

    def test_unclear_quantity_waits_for_staff_review(self):
        o = self.report("Z3 구역 기저귀랑 담요가 필요해요")
        reqs = self.supply_requests(o["id"])
        self.assertEqual(sorted(r["item"] for r in reqs), ["기저귀", "담요"])
        self.assertTrue(all(r["review_reason"] == "수량 미확인" and r["state"] == "검토" for r in reqs))
        self.assertEqual(self.pending(o["id"]), [])
        self.assertIn("물품 담당자 검토 필요", self.staff_view(o["id"])["notes"][0])
        self.assertNotIn("notes", o)                                         # 내부 메모는 공개 응답에 없음

    def test_missing_location_asks_reporter_then_continues(self):
        o = self.report("생수 10병 보내 주세요")
        self.assertEqual((o["state"], o["missing"], o["linked"]), ("보완 필요", ["위치(구역)"], {}))
        self.assertEqual(self.supply_requests(o["id"]), [])   # 위치 없이 물품을 보내지 않는다
        anon = Client(self.base)
        code, d = anon.post(f"/api/incidents/{o['id']}/supplement", {"text": "Z5 구역입니다"})
        self.assertEqual(code, 200, d)
        o2 = d["output"]
        self.assertEqual((o2["id"], o2["zone"], o2["state"], o2["supplements"]), (o["id"], "Z5", "접수", 1))
        self.assertIn("[보완] Z5 구역입니다", o2["text"])
        self.assertEqual(len(self.supply_requests(o["id"])), 1)
        code, _ = anon.post(f"/api/incidents/{o['id']}/supplement", {"text": "또 보완"})
        self.assertEqual(code, 409)
        self.assertEqual(anon.post("/api/incidents/R-NONE/supplement", {"text": "x"})[0], 404)
        self.assertEqual(anon.post(f"/api/incidents/{o['id']}/supplement", {"text": ""})[0], 400)

    def test_missing_item_needs_info(self):
        o = self.report("Z3 구역인데 물품이 좀 필요합니다")
        self.assertEqual((o["type"], o["state"], o["missing"]), ("물품 요청", "보완 필요", ["필요한 물품"]))

    def test_rescue_without_location_is_still_urgent(self):
        o = self.report("사람이 갇혀 있어요 도와주세요")
        self.assertEqual((o["type"], o["urgency"], o["notify_now"]), ("인명구조", 1, True))
        self.assertEqual(o["state"], "보완 필요")   # 위치를 묻되 긴급도·알림은 낮추지 않는다

    def test_invalidate_rejects_and_releases_everything(self):
        o = self.report("Z3 구역 담요 4장 필요해요")
        code, d = self.post(f"/api/incidents/{o['id']}/dispatch", {"kind": "shelter", "people": 2})
        self.assertEqual(code, 200, d)
        self.assertEqual(len(self.pending(o["id"])), 2)   # 물품 배치 + 대피소 배정
        anon = Client(self.base)
        self.assertIn(anon.post(f"/api/incidents/{o['id']}/invalidate", {"reason": "장난"})[0], (401, 403))
        self.assertEqual(self.post(f"/api/incidents/{o['id']}/invalidate", {"reason": ""})[0], 400)
        code, d = self.post(f"/api/incidents/{o['id']}/invalidate", {"reason": "같은 번호 장난 신고"})
        self.assertEqual(code, 200, d)
        self.assertEqual(len(d["rejected_approvals"]), 2)
        self.assertEqual(self.pending(o["id"]), [])
        req = self.supply_requests(o["id"])[0]
        self.assertEqual((req["state"], req["reservation"]), ("반려", []))
        _, pub = anon.get(f"/api/incidents/{o['id']}")
        self.assertEqual((pub["status"], pub["result"]["output"]["state"]), ("rejected", "오접수"))
        self.assertIsNone(pub["guidance"])
        self.assertEqual(self.post(f"/api/incidents/{o['id']}/dispatch", {"kind": "public"})[0], 409)
        self.assertEqual(self.post(f"/api/incidents/{o['id']}/invalidate", {"reason": "다시"})[0], 409)

    def test_invalidate_releases_approved_dispatch(self):
        o = self.report("Z4 구역 도로에 쓰러진 나무")
        _, d = self.post(f"/api/incidents/{o['id']}/dispatch", {"kind": "public"})
        self.assertEqual(self.post(f"/api/approvals/{d['approval_id']}/resolve",
                                   {"approved": True, "reason": "투입"})[0], 200)
        _, r = self.post(f"/api/incidents/{o['id']}/invalidate", {"reason": "현장 확인 결과 이상 없음"})
        self.assertEqual(r["released_dispatches"], [d["approval_id"]])
        _, st = self.get("/api/dispatch")
        self.assertNotIn(d["approval_id"], [a["id"] for a in st["assignments"]])

    def dispatches(self, iid):
        return [a for a in self.pending(iid) if a["kind"] == "dispatch_public"]

    def test_urgent_report_gets_dispatch_recommendation(self):
        o = self.report("Z7 개울에서 사람이 떠내려가요")
        self.assertEqual((o["type"], o["urgency"]), ("인명구조", 1))
        [a] = self.dispatches(o["id"])
        self.assertTrue(a["item"]["auto"]); self.assertEqual(o["linked"]["dispatch"], [a["id"]])
        self.assertEqual(self.get("/api/dispatch")[1]["assignments"], [])   # 승인 전에는 투입 없음
        code, d = self.post(f"/api/incidents/{o['id']}/dispatch", {"kind": "public"})
        self.assertEqual((code, d["approval_id"]), (409, a["id"]))           # 같은 신고에 중복 추천 없음
        o2 = self.report("Z7 다른 집에서도 사람이 떠내려가요")
        self.assertEqual(len(self.dispatches(o2["id"])), 1)                  # 인명구조는 별건으로 각각 추천

    def test_repeat_report_links_to_ongoing_dispatch(self):
        first = self.report("Z1 반지하 집에 물이 차고 있어요")
        self.assertEqual(first["urgency"], 2)
        [a] = self.dispatches(first["id"])
        again = self.report("Z1 반지하 쪽 침수 계속돼요")
        self.assertEqual(self.dispatches(again["id"]), [])
        self.assertEqual(again["linked"]["dispatch"], [a["id"]])
        self.assertIn("이미 진행 중", self.staff_view(again["id"])["notes"][0])

    def test_linked_report_recovers_when_host_dispatch_goes_away(self):
        host = self.report("Z9 반지하 집에 물이 차요 할머니 계세요")
        [a] = self.dispatches(host["id"])
        linked = self.report("Z9 반지하 침수 계속 심해져요")
        self.assertEqual(linked["linked"]["dispatch"], [a["id"]])
        _, r = self.post(f"/api/incidents/{host['id']}/invalidate", {"reason": "장난 신고로 확인"})
        self.assertEqual(r["relinked_incidents"], [linked["id"]])
        [b] = self.dispatches(linked["id"])                                  # 진짜 신고에 배치 추천을 새로 만듦
        self.assertEqual(self.staff_view(linked["id"])["linked"]["dispatch"], [b["id"]])
        third = self.report("Z9 반지하 침수 아직이에요")
        self.assertEqual(third["linked"]["dispatch"], [b["id"]])
        self.post(f"/api/approvals/{b['id']}/resolve", {"approved": False, "reason": "현장 이미 정리"})
        v = self.staff_view(third["id"])                                     # 연결만 해 둔 신고는 연결을 풀고 알림
        self.assertNotIn(b["id"], v["linked"].get("dispatch", []))
        self.assertIn("반려됨", v["notes"][-1])                               # 반려는 사람 판단이라 새로 만들지 않음
        self.assertEqual(self.dispatches(third["id"]), [])

    def test_low_urgency_and_missing_zone_wait(self):
        road = self.report("Z4 공원 벤치 사용법 문의드려요")
        self.assertEqual(road["type"], "대민지원")
        self.assertEqual(self.dispatches(road["id"]), [])                    # 문의(대민지원)는 출동 대상 아님
        o = self.report("어딘지 모르겠는데 사람이 갇혀 있어요")
        self.assertEqual(self.dispatches(o["id"]), [])                       # 위치가 없으면 추천하지 않음
        code, d = Client(self.base).post(f"/api/incidents/{o['id']}/supplement", {"text": "Z8 구역 마트 지하예요"})
        self.assertEqual(code, 200, d)
        self.assertEqual(len(self.dispatches(o["id"])), 1)                   # 보완되면 그때 추천

    def test_invalidation_details_not_public(self):
        o = self.report("Z3 구역 담요 2장 필요해요 (공개 범위 시험)")
        self.post(f"/api/incidents/{o['id']}/invalidate", {"reason": "내부 메모: 같은 번호 반복"})
        _, pub = Client(self.base).get(f"/api/incidents/{o['id']}")
        out = pub["result"]["output"]
        self.assertEqual(out["state"], "오접수")
        for k in ("invalidated_by", "invalid_reason", "notes"):
            self.assertNotIn(k, out)                                         # 담당자 이름·사유는 공개하지 않음
        self.assertEqual(self.staff_view(o["id"])["invalidated_by"], "담당자")

    def test_unknown_zone_does_not_break_agents(self):
        o = self.report("Z99 구역 산사태가 났어요 할머니 혼자 계세요")
        self.assertEqual(o["state"], "보완 필요")
        self.assertIn("위치(구역): 없는 구역 번호", o["missing"])
        _, a = self.get("/api/agents")
        self.assertFalse([x["id"] for x in a["items"] if x["status"] == "manual"])   # 입력 문제로 수동 전환하지 않음

    def test_supplement_resolves_ambiguous_zone_and_long_text(self):
        o = self.report("2구역인지 3구역인지 모르겠는데 반지하에 물이 차고 있어요")
        self.assertEqual((o["state"], o["zone"]), ("보완 필요", "미상"))
        code, d = Client(self.base).post(f"/api/incidents/{o['id']}/supplement", {"text": "3구역입니다"})
        self.assertEqual((code, d["output"]["zone"], d["output"]["state"]), (200, "Z3", "접수"))
        long = self.report("Z5 " + "어제부터 비가 많이 와서 마을 사람들이 다 같이 모여 있어요. " * 20 + "물품이 필요해요")
        self.assertEqual(long["missing"], ["필요한 물품"])
        code, d = Client(self.base).post(f"/api/incidents/{long['id']}/supplement", {"text": "생수 10병이요"})
        self.assertEqual((code, d["output"]["state"]), (200, "접수"))      # 긴 원문 뒤에 붙은 품목도 읽음
        self.assertEqual([r["item"] for r in self.supply_requests(long["id"])], ["생수"])

    def wait_linked(self, iid, key, timeout=5):
        """접수 뒤 백그라운드 후속 처리(안내문·봉사)가 끝날 때까지 기다린다."""
        end = time.time() + timeout
        while time.time() < end:
            v = self.staff_view(iid)
            if key in (v.get("linked") or {}):
                return v
            time.sleep(.1)
        self.fail(f"{iid}에 {key} 연결이 생기지 않음")

    def test_hazard_report_chains_to_dispatch_and_guidance(self):
        o = self.report("Z5 뒷산 토사가 마당까지 흘러내려요 (전체 흐름 시험)")
        self.assertEqual((o["type"], o["urgency"]), ("산사태·토사", 3))
        self.assertEqual(len(self.dispatches(o["id"])), 1)                   # 긴급도 3도 위험 유형이면 배치 추천
        g = self.wait_linked(o["id"], "guidance")["linked"]["guidance"]
        self.assertEqual((g["level"], g["status"]), ("주의", "표준 원문"))   # mock: AI 없이 표준 원문(승인 대기 없음)

    def test_labor_request_chains_to_volunteer(self):
        o = self.report("Z3 집 안 진흙 퍼낼 일손이 필요해요")
        self.assertEqual(o["type"], "대민지원")
        tasks = self.wait_linked(o["id"], "volunteer")["linked"]["volunteer"]
        self.assertTrue(tasks)
        pend = [a for a in self.pending(o["id"]) if a["kind"] == "volunteer_task"]
        self.assertEqual(len(pend), len([t for t in tasks]))                 # 봉사 투입 승인도 이 신고에 묶임

    def test_approve_all_executes_every_item_once(self):
        o = self.report("Z6 마을회관에 생수 3병이랑 담요 2장 필요해요 (일괄 승인 시험)")
        items = self.pending(o["id"])
        self.assertEqual(sorted(a["item"]["item"] for a in items), ["담요", "생수"])
        anon = Client(self.base)
        self.assertIn(anon.post(f"/api/incidents/{o['id']}/approve-all", {"reason": "x"})[0], (401, 403))
        self.assertEqual(self.post(f"/api/incidents/{o['id']}/approve-all", {"reason": ""})[0], 400)
        code, r = self.post(f"/api/incidents/{o['id']}/approve-all", {"reason": "재고 확인, 일괄 승인"})
        self.assertEqual((code, len(r["approved"]), r["skipped"]), (200, 2, []))
        self.assertEqual(self.pending(o["id"]), [])
        self.assertTrue(all(q["state"] == "배치" for q in self.supply_requests(o["id"])))
        code, r = self.post(f"/api/incidents/{o['id']}/approve-all", {"reason": "다시"})
        self.assertEqual((code, r["approved"]), (200, []))                   # 두 번 눌러도 다시 실행되지 않음

    def test_approve_all_skips_item_that_fails_recheck(self):
        a = self.report("Z2 상가 지하에 사람이 갇혀 있어요 (재검사 시험 1)")
        b = self.report("Z2 상가 옆 건물에도 사람이 갇혀 있어요 (재검사 시험 2)")
        top_a = self.dispatches(a["id"])[0]["item"]["recommendation"]["top"]["id"]
        top_b = self.dispatches(b["id"])[0]["item"]["recommendation"]["top"]["id"]
        self.assertEqual(top_a, top_b)                                       # 둘 다 같은 팀을 1순위로 추천
        self.post(f"/api/incidents/{a['id']}/approve-all", {"reason": "먼저 투입"})
        code, r = self.post(f"/api/incidents/{b['id']}/approve-all", {"reason": "다음 투입"})
        self.assertEqual((code, r["approved"]), (200, []))
        self.assertIn("이미 다른 배치", r["skipped"][0]["reason"])          # 중복 투입은 막고 재추천을 안내
        for x in self.get("/api/dispatch")[1]["assignments"]:
            self.post(f"/api/dispatch/{x['id']}/release", {})

    def test_manual_mode_shown_and_cleared_on_success(self):
        old = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "test.db"
        try:   # 오류로 수동 전환된 상태를 직접 만든다(서버와 같은 DB)
            db.put_record("agent_state", "supply", {"id": "supply", "manual": True, "since": 0, "reason": "KeyError"})
        finally:
            db.DB_PATH = old
        _, a = self.get("/api/agents")
        sup = next(x for x in a["items"] if x["id"] == "supply")
        self.assertEqual((sup["status"], sup["manual_reason"]), ("manual", "KeyError"))
        self.report("Z3 구역 마스크 5개 필요해요")   # 물품 연결이 성공하면 자동으로 풀린다
        _, a = self.get("/api/agents")
        self.assertEqual(next(x for x in a["items"] if x["id"] == "supply")["status"], "demo-ready")


if __name__ == "__main__":
    unittest.main()
