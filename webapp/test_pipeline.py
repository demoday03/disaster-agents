"""신고 → 다음 에이전트 연결과 예외 처리(잘못된 신고·필수 정보 누락·에이전트 오류) 시험. mock 모드, 실제 API 호출 없음.
실행: python -m unittest webapp.test_pipeline -v"""
import time, unittest, urllib.request
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
        self.assertEqual((o["state"], o["missing"], o["linked"]), ("보완 필요", ["위치"], {}))
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
        self.assertIn("위치: 없는 구역 번호", o["missing"])
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

    def test_hazard_report_runs_every_agent_then_one_approval(self):
        o = self.report("Z8 반지하 집에 물이 차서 주민 3명이 대피해야 해요 (전 에이전트 시험)")
        self.assertEqual((o["type"], o["urgency"], o["people"]), ("침수", 2, 3))
        L = self.wait_linked(o["id"], "guidance")["linked"]
        m = L["monitor"]                                                     # ① 신고만으로는 단계를 올리지 않음
        self.assertEqual((m["hazard"], m["agrees"], m["approval_id"]), ("침수", False, None))
        self.assertTrue(L["recovery"]["candidates"])                         # ⑤ 지원 후보(참고용, 승인 대상 아님)
        kinds = sorted(a["kind"] for a in self.pending(o["id"]))
        self.assertEqual(kinds, ["dispatch_public", "dispatch_shelter", "welfare_check"])   # ④ 인력·대피소, ③ 안부
        z8 = lambda: [c for c in self.get("/api/welfare/checks")[1]["checks"] if c["zone"] == "Z8"]
        self.assertEqual(z8(), [])                                           # 승인 전에는 가구에 요청하지 않음
        code, r = self.post(f"/api/incidents/{o['id']}/approve-all", {"reason": "반지하 침수, 일괄 승인"})
        self.assertEqual((code, len(r["approved"]), r["skipped"]), (200, 3, []))
        self.assertTrue(z8())
        again = self.report("Z8 반지하 침수 계속돼요 주민 2명 (전 에이전트 시험)")
        L2 = self.wait_linked(again["id"], "monitor")["linked"]
        self.assertNotIn("welfare_check", L2)                                # 같은 사건 반복 신고는 묶기만
        self.assertNotIn("shelter", L2)
        for x in self.get("/api/dispatch")[1]["assignments"]:
            self.post(f"/api/dispatch/{x['id']}/release", {})

    def test_sensor_and_report_agree_stage_joins_bundle(self):
        aid = self._new_approval()                                           # 센서: 침수 확정 후보(승인 대기)
        o = self.report("Z6 마을 길이 침수됐어요 (센서 교차 시험)")
        m = self.wait_linked(o["id"], "monitor")["linked"]["monitor"]
        self.assertEqual((m["agrees"], m["approval_id"]), (True, aid))
        code, r = self.post(f"/api/incidents/{o['id']}/approve-all", {"reason": "센서와 신고 일치"})
        self.assertIn(aid, [x["id"] for x in r["approved"]])
        self.assertTrue(self.get("/api/stage")[1]["confirmed"])
        for x in self.get("/api/dispatch")[1]["assignments"]:
            self.post(f"/api/dispatch/{x['id']}/release", {})

    def test_staff_reclassify_and_fill_zone(self):
        o = self.report("어딘지 모르겠는데 물이 차올라요 (분류 수정 시험)")
        self.assertEqual(o["state"], "보완 필요")
        path = f"/api/incidents/{o['id']}/classify"
        self.assertIn(Client(self.base).post(path, {"type": "침수", "zone": "Z4", "urgency": 2})[0], (401, 403))
        self.assertEqual(self.post(path, {"type": "없는유형", "zone": "Z4", "urgency": 2})[0], 400)
        self.assertEqual(self.post(path, {"type": "침수", "zone": "Z99", "urgency": 2})[0], 400)
        self.assertEqual(self.post(path, {"type": "침수", "zone": "Z4", "urgency": 9})[0], 400)
        code, d = self.post(path, {"type": "침수", "zone": "Z4", "urgency": 2})
        self.assertEqual((code, d["output"]["zone"], d["output"]["state"]), (200, "Z4", "접수"))   # 구역을 채우면 바로 다음 단계로
        v = self.staff_view(o["id"])
        self.assertEqual((v["classified_by"], v["label"]), ("담당자", "담당자 수정"))
        self.assertTrue(v["linked"].get("dispatch"))
        self.assertIn("담당자 분류 수정", " ".join(v["notes"]))
        for x in self.get("/api/dispatch")[1]["assignments"]:
            self.post(f"/api/dispatch/{x['id']}/release", {})

    def test_shelter_occupancy_and_daily_report(self):
        sh = lambda: next(s for s in self.get("/api/dispatch")[1]["shelters"] if s["id"] == "S3")
        before = sh()
        self.assertIn(Client(self.base).post("/api/shelters/S3/occupancy", {"delta": 1})[0], (401, 403))
        self.assertEqual(self.post("/api/shelters/S3/occupancy", {"delta": 0})[0], 400)
        self.assertEqual(self.post("/api/shelters/S9/occupancy", {"delta": 1})[0], 404)
        code, d = self.post("/api/shelters/S3/occupancy", {"delta": 2})
        self.assertEqual((code, d["occupied"]), (200, before["occupied"] + 2))
        self.assertEqual(sh()["capacity_left"], before["capacity_left"] - 2)   # 배정 추천·시민 화면이 보는 남은 자리
        self.assertEqual(self.post("/api/shelters/S3/occupancy", {"delta": -50})[0], 409)   # 0명 아래로는 못 내림
        self.post("/api/shelters/S3/occupancy", {"delta": -2})
        code, r = self.get("/api/report/daily")
        self.assertEqual(code, 200)
        self.assertIn("일일 상황 보고서", r["text"])
        self.assertGreaterEqual(r["stats"]["incidents"], 1)
        self.assertEqual(Client(self.base).get("/api/report/daily")[0], 401)

    def test_citizen_home_safe_check_and_supply_flow(self):
        anon = Client(self.base)
        code, d = anon.get("/api/public/households")
        self.assertEqual((code, len(d["items"])), (200, 20))
        self.assertEqual(anon.get("/api/public/households/H99")[0], 404)
        self.assertEqual(anon.post("/api/public/households/H05/safe", {})[0], 409)   # 안부 확인 전에는 응답할 것이 없음
        self.post("/api/welfare/checks/start", {"zone": "Z3", "timeout_s": 600})
        _, h = anon.get("/api/public/households/H05")
        self.assertEqual(h["check"]["state"], "대기")
        self.assertTrue(h["risks"])
        self.assertNotIn("의약품", " ".join(h["catalog"]))
        code, r = anon.post("/api/public/households/H05/safe", {})
        self.assertEqual((code, r["state"]), (201, "안전"))
        self.assertEqual(anon.post("/api/public/households/H05/safe", {})[0], 409)   # 두 번은 안 됨
        self.assertEqual(anon.post("/api/public/supply", {"household": "H05", "item": "처방약", "qty": 1})[0], 400)
        code, r = anon.post("/api/public/supply", {"household": "H05", "item": "담요", "qty": 2, "when": "지금"})
        self.assertEqual(code, 201, r)
        rid = r["requests"][0]["id"]
        self.assertEqual(r["requests"][0]["state"], "검토")                   # 담당자 검토 전에는 출고 없음
        self.assertEqual(anon.post(f"/api/public/supply/{rid}/receipt", {"household": "H01", "received": True})[0], 403)
        self.assertEqual(anon.post(f"/api/public/supply/{rid}/receipt", {"household": "H05", "received": True})[0], 409)
        _, h = anon.get("/api/public/households/H05")
        self.assertIn(rid, [x["id"] for x in h["requests"]])

    def test_citizen_registers_home_without_codes(self):
        """주민은 코드 대신 구역·집 형태·함께 사는 분만 고른다. 우선순위는 서버 안에서만(목록에도 안 보임)."""
        anon = Client(self.base)
        for bad in ({"zone": "Z99", "floor": "1층"}, {"zone": "Z3", "floor": "옥탑"},
                    {"zone": "Z3", "floor": "1층", "people_factors": ["외계인"]}, {"zone": "Z3", "floor": "1층", "members": 0}):
            self.assertEqual(anon.post("/api/public/households", bad)[0], 400, bad)
        code, r = anon.post("/api/public/households", {"zone": "Z3", "floor": "반지하", "people_factors": ["고령"], "members": 2})
        self.assertEqual(code, 201, r)
        hid = r["id"]
        self.assertTrue(hid.startswith("H-") and len(hid) > 8)                 # 짐작하기 어려운 ID(기기에만 저장)
        self.assertNotIn(hid, [h["id"] for h in anon.get("/api/public/households")[1]["items"]])   # 남의 집 목록에 안 나옴
        _, h = anon.get(f"/api/public/households/{hid}")
        self.assertEqual((h["household"]["factors"], h["household"]["registered"]), (["반지하", "고령"], True))
        self.assertTrue(any("반지하" in x for x in h["risks"]) and any("대피" in x for x in h["risks"]))
        self.assertEqual(anon.post(f"/api/public/households/{hid}/update", {"zone": "Z3", "floor": "2층 이상", "members": 2})[0], 201)
        self.assertEqual(anon.get(f"/api/public/households/{hid}")[1]["household"]["factors"], [])
        self.assertEqual(anon.post("/api/public/households/H05/update", {"zone": "Z3", "floor": "1층"})[0], 403)   # 시연 가구는 못 바꿈
        code, r = anon.post("/api/public/supply", {"household": hid, "item": "생수", "qty": 6, "no_water": True})
        self.assertEqual((code, r["requests"][0]["state"]), (201, "검토"))
        self.post("/api/welfare/checks/start", {"zone": "Z3", "timeout_s": 600})   # 등록한 가구도 안부 확인 대상
        self.assertEqual(anon.get(f"/api/public/households/{hid}")[1]["check"]["state"], "대기")
        self.assertEqual(anon.post(f"/api/public/households/{hid}/safe", {})[1]["state"], "안전")

    def test_home_menu_and_role_pages(self):
        for path, text in (("/", "바로 신고하기"), ("/citizen", "재난 신고하기"), ("/report", "신고 접수"), ("/volunteer", "도움이 필요한 작업")):
            with urllib.request.urlopen(self.base + path, timeout=2) as r:
                self.assertIn(text.encode(), r.read(), path)

    def test_volunteer_screen_only_acts_on_own_tasks(self):
        anon = Client(self.base)
        self.assertEqual(len(anon.get("/api/public/volunteers")[1]["items"]), 12)
        _, v = self.post("/api/volunteer/requests", {"text": "마당 토사 치워 주세요", "zone": "Z3"})
        t = next(x for x in v["tasks"] if x.get("pending_approval"))
        team = t["last_recommendation"]["team"]
        outsider = next(x["id"] for x in anon.get("/api/public/volunteers")[1]["items"] if x["id"] not in team)
        self.assertEqual(anon.post(f"/api/public/volunteers/{team[0]}/tasks/{t['id']}/checkin", {})[0], 403)   # 승인 전
        self.post(f"/api/approvals/{t['pending_approval']}/resolve", {"approved": True, "reason": "투입"})
        self.assertEqual(anon.post(f"/api/public/volunteers/{outsider}/tasks/{t['id']}/checkin", {})[0], 403)
        _, me = anon.get(f"/api/public/volunteers/{team[0]}")
        self.assertIn(t["id"], [x["id"] for x in me["my_tasks"]])
        self.assertEqual(anon.post(f"/api/public/volunteers/{team[0]}/tasks/{t['id']}/checkin", {})[1]["state"], "진행")
        self.assertEqual(anon.post(f"/api/public/volunteers/{team[0]}/tasks/{t['id']}/complete", {})[1]["state"], "완료")
        code, s = anon.post(f"/api/public/volunteers/{team[0]}/sos", {})
        self.assertEqual((code, s["open"]), (201, True))
        self.assertIn(s["id"], [x["id"] for x in self.get("/api/volunteer")[1]["sos"]])   # 담당자 화면에 뜸
        code, dn = anon.post(f"/api/public/volunteers/{team[0]}/donations",
                             {"item": "생수", "qty": 12, "unit": "병", "condition": "새것", "days_left": 200, "zone": "Z3"})
        self.assertEqual((code, dn["state"]), (201, "검수 대기"))
        with urllib.request.urlopen(f"{self.base}/api/public/volunteers/{team[0]}/activity.csv", timeout=3) as r:
            body = r.read().decode("utf-8-sig")
        self.assertIn("작업 봉사", body); self.assertIn("물품 기부", body)

    def test_written_location_instead_of_zone(self):
        """위치는 구역을 고르지 않고 글로 쓴다. 관리 구역은 가상 장소 이름이 있을 때만 추정하고, 모르면 담당자가 확인한다."""
        anon = Client(self.base)
        self.assertEqual(anon.post("/api/public/households", {"floor": "1층"})[0], 400)            # 위치는 꼭 필요
        code, r = anon.post("/api/public/households", {"address": "옥포동 ○○아파트 101동", "floor": "1층", "members": 2})
        self.assertEqual(code, 201, r)
        _, h = anon.get(f"/api/public/households/{r['id']}")
        self.assertEqual((h["household"]["zone"], h["household"]["address"]), ("미상", "옥포동 ○○아파트 101동"))
        self.assertTrue(h["risks"] and h["nearest_shelter"])                   # 구역을 몰라도 안내·대피소는 나온다
        code, r = anon.post("/api/public/households", {"address": "가람초 뒤 주택 010-1234-5678", "floor": "반지하"})
        _, h = anon.get(f"/api/public/households/{r['id']}")
        self.assertEqual(h["household"]["zone"], "Z2")                        # 가상 장소 이름으로 추정
        self.assertNotIn("1234", h["household"]["address"])                   # 전화번호는 가린다
        o = self.report("위치: 옥포동 ○○아파트 앞 · 물이 차고 있어요(침수) · 2명")
        self.assertEqual((o["zone"], o["missing"], o["state"]), ("미상", [], "접수"))   # 위치를 적었으면 다시 묻지 않음
        o = self.report("위치: 한빛중 근처 · 흙·돌이 무너졌어요(산사태·토사)")
        self.assertEqual(o["zone"], "Z8")
        # 위치를 GPS로만 보낸 긴급 신고: 구역을 몰라도 출동 추천을 바로 만들고, 신고자는 진행 상황을 본다
        o = self.report("위치: GPS 34.88012, 128.62145 (오차 약 12m) · 사람이 갇혔거나 다쳤어요(구조 요청)")
        self.assertEqual((o["zone"], o["urgency"]), ("미상", 1))
        [aid] = o["linked"]["dispatch"]
        anon_view = lambda: Client(self.base).get(f"/api/incidents/{o['id']}")[1]["progress"]
        self.assertIn("검토", anon_view()[0]["text"])
        self.assertEqual(self.post(f"/api/approvals/{aid}/resolve", {"approved": True, "reason": "GPS 위치로 출동"})[0], 200)
        self.assertTrue(any(p.get("done") and "승인" in p["text"] for p in anon_view()))
        # 위치를 모르는 신고끼리는 같은 사건으로 묶지 않는다
        o2 = self.report("위치: GPS 34.90000, 128.60000 · 물이 차고 있어요(침수) · 어르신 있음")
        self.assertEqual((o2["zone"], o2["urgency"]), ("미상", 2))
        self.assertNotEqual(o2["linked"].get("dispatch"), [aid])
        code, v = self.post("/api/volunteer/requests", {"text": "마당 토사 치워 주세요", "address": "장평동 주택가"})
        self.assertEqual(code, 200, v)
        self.assertEqual((v["tasks"][0]["zone"], v["tasks"][0]["address"]), ("미상", "장평동 주택가"))

    def test_reporter_can_edit_and_cancel(self):
        """신고자는 그 기기에 받은 비밀 값으로만 고치기·취소. 담당자가 조치를 승인한 뒤에는 고칠 수 없다."""
        anon = Client(self.base)
        code, d = anon.post("/api/incidents", {"text": "위치: 옥포동 시장 앞 · 물이 차고 있어요(침수) (수정 시험)"})
        self.assertEqual(code, 201, d)
        iid, token = d["output"]["id"], d["edit_token"]
        self.assertNotIn("owner_hash", d["output"])
        self.assertNotIn("owner_hash", str(anon.get(f"/api/incidents/{iid}")[1]))
        self.assertEqual(anon.post(f"/api/incidents/{iid}/edit", {"token": "틀린값", "text": "x"})[0], 403)
        pend_before = [a["id"] for a in self.pending(iid)]
        code, e = anon.post(f"/api/incidents/{iid}/edit", {"token": token, "text": "위치: 옥포동 시장 앞 · 사람이 갇혔거나 다쳤어요(구조 요청) (수정 시험)"})
        self.assertEqual(code, 200, e)
        self.assertEqual((e["output"]["type"], e["output"]["urgency"], e["output"]["edits"]), ("인명구조", 1, 1))
        self.assertFalse(set(pend_before) & {a["id"] for a in self.pending(iid)})          # 예전 추천은 반려되고 새로 연결
        [aid] = e["output"]["linked"]["dispatch"]
        self.assertEqual(self.post(f"/api/approvals/{aid}/resolve", {"approved": True, "reason": "출동"})[0], 200)
        self.assertEqual(anon.post(f"/api/incidents/{iid}/edit", {"token": token, "text": "다시 고침"})[0], 409)   # 승인 뒤에는 못 고침
        code, c = anon.post(f"/api/incidents/{iid}/cancel", {"token": token})
        self.assertEqual((code, c["already_acting"]), (200, True))                          # 출동 중이면 담당자에게 알림
        view = anon.get(f"/api/incidents/{iid}")[1]["result"]["output"]
        self.assertEqual((view["state"], view["cancelled_by_reporter"]), ("오접수", True))
        self.assertIn("신고자가 취소함", self.staff_view(iid)["notes"][-1])
        self.assertEqual(anon.post(f"/api/incidents/{iid}/cancel", {"token": token})[0], 409)

    def test_human_vs_ai_battle(self):
        """사람 vs AI 분류 대결: 문제에는 정답이 없고, 채점은 서버가 한다. AI 결과는 한 번만 계산해 둔다."""
        import json as _json
        anon = Client(self.base)
        _, q = anon.get("/api/public/battle")
        self.assertEqual(len(q["items"]), 20)
        self.assertNotIn("type", _json.dumps(q["items"]))                         # 정답이 화면으로 새지 않는다
        gold = {r["id"]: r["type"] for r in _json.load(open("data/eval/intake_holdout.json", encoding="utf-8"))["reports"][:20]}
        self.assertEqual(anon.post("/api/public/battle", {"answers": {"H01": "침수"}, "elapsed_ms": 5000})[0], 400)
        code, r = anon.post("/api/public/battle", {"answers": gold, "elapsed_ms": 60000, "name": "정답이"})
        self.assertEqual(code, 201, r)
        self.assertEqual((r["human"]["correct"], r["human"]["rescue_missed"]), (20, []))
        self.assertEqual(len(r["rows"]), 20)
        self.assertTrue(0 <= r["ai"]["correct"] <= 20 and r["ai"]["mode"])
        wrong = {k: "대민지원" for k in gold}
        _, r2 = anon.post("/api/public/battle", {"answers": wrong, "elapsed_ms": 30000})
        self.assertEqual(r2["ai"], r["ai"])                                      # AI 결과는 다시 계산하지 않는다
        self.assertEqual(len(r2["human"]["rescue_missed"]), sum(v == "인명구조" for v in gold.values()))
        self.assertEqual(anon.get("/api/public/battle/history")[1]["history"]["count"], 2)

    def test_volunteer_signup_knows_what_they_can_do(self):
        anon = Client(self.base)
        _, opt = anon.get("/api/public/volunteer-options")
        self.assertIn("토사 제거", next(s for s in opt["skills"] if s["id"] == "토사")["tasks"])   # 가입 화면에서 미리 보여 줌
        body = {"name": "동네삽", "password": "secret12", "address": "하천 하류 쪽 마을", "skills": ["토사"],
                "equipment": ["삽"], "available_min": 480, "mode": "작업"}
        for bad in ({**body, "password": "123"}, {**body, "skills": []}, {**body, "name": "V01"}, {**body, "address": ""}):
            self.assertEqual(anon.post("/api/public/volunteers", bad)[0], 400, bad)
        code, r = anon.post("/api/public/volunteers", body)
        self.assertEqual(code, 201, r)
        vid = r["id"]
        self.assertEqual(anon.post("/api/public/volunteers", {**body, "name": "동네삽"})[0], 409)  # 같은 별명
        self.assertEqual(anon.post("/api/public/volunteer-login", {"name": "동네삽", "password": "wrong!!"})[0], 403)
        self.assertEqual(anon.post("/api/public/volunteer-login", {"name": "동네삽", "password": "secret12"})[1]["id"], vid)
        self.assertNotIn(vid, [x["id"] for x in anon.get("/api/public/volunteers")[1]["items"]])   # ID는 목록에 안 나옴
        _, me = anon.get(f"/api/public/volunteers/{vid}")
        self.assertEqual((me["volunteer"]["name"], me["volunteer"]["zone"], me["can_do"]), ("동네삽", "Z1", ["토사 제거"]))
        self.assertNotIn("password_hash", str(me) + str(self.get("/api/volunteer")[1]))
        # 담당자가 작업을 만들면 가입한 봉사자도 추천 후보가 되고, 다른 봉사자에게는 별명만 보인다
        _, v = self.post("/api/volunteer/requests", {"text": "토사 치워 주세요", "zone": "Z1"})
        t = next(x for x in v["tasks"] if x.get("pending_approval"))
        self.assertIn(vid, t["last_recommendation"]["team"])
        self.post(f"/api/approvals/{t['pending_approval']}/resolve", {"approved": True, "reason": "투입"})
        mate = next(x for x in t["last_recommendation"]["team"] if x != vid)
        shown = anon.get(f"/api/public/volunteers/{mate}")[1]["my_tasks"]
        self.assertIn("동네삽", next(x for x in shown if x["id"] == t["id"])["team"])
        self.assertNotIn(vid, str(shown))
        self.assertEqual(anon.post(f"/api/public/volunteers/{vid}/update", {**body, "skills": ["토사", "청소"]})[0], 201)
        self.assertEqual(anon.get(f"/api/public/volunteers/{vid}")[1]["can_do"], ["토사 제거", "가재도구 세척"])
        self.assertEqual(anon.post("/api/public/volunteers/V01/update", body)[0], 403)            # 시연 봉사자는 못 바꿈

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
        self.assertEqual((code, [x for x in r["approved"] if x["kind"].startswith("dispatch")]), (200, []))
        self.assertIn("이미 다른 배치", next(x for x in r["skipped"] if x["kind"] == "dispatch_public")["reason"])          # 중복 투입은 막고 재추천을 안내
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
