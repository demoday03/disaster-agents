"""핵심 흐름: 감시 → 접수 → 배치 추천 → 승인 → 검증 (제안서 11.2 '핵심 흐름 완주 10/10')."""
import unittest
from coordinator import Coordinator
from agents import monitor as M, intake as I, dispatch as D, welfare as W, verify as V, supply as S, recovery as R
from common.llm import SolarClient

def fake(c): return SolarClient(transport=lambda u, h, b, t: {"choices": [{"message": {"content": c}}]})
NOW = 100.0
SHELTERS = [{"id": "S1", "zone": "Z1", "capacity": 50, "capacity_left": 0, "amenities": []},
            {"id": "S2", "zone": "Z1", "capacity": 60, "capacity_left": 30, "amenities": ["엘리베이터"]}]
CTX = {"travel": {("Z3", "Z1"): {"minutes": 10, "via_blocked": False}}}
CFGS = dict(mon=M.load_config("config/monitor.json"), ic=I.load_cfg("config/intake.json"),
            dc=D.load_config("config/dispatch.json"), gc=W.load_cfg("config/guidance.json"))


def run_core_flow():
    co = Coordinator()
    # 1) 감시: 침수 센서 두 개가 함께 반응
    readings = [{"node": "A", "sensor": s, "value": 900, "ts": NOW - i} for s in ("water_low", "water_high") for i in range(5)]
    readings += [{"node": "B", "sensor": s, "value": v, "ts": NOW - i} for s, v in (("tilt", 0), ("vibration", 50)) for i in range(5)]
    mon = M.Monitor(CFGS["mon"]).evaluate(readings, NOW)
    assert mon.output["status"] == "확정후보" and mon.needs_approval
    ap = co.request_approval("위험 단계 확정", mon.output)
    assert co.pending()                                               # 담당자 확정 전에는 대기
    co.resolve(ap, True, "담당자A")
    level = mon.output["level"]
    # 2) 접수: 신고 분류
    rep = I.classify("N1", "Z3 반지하에 물이 차고 있어요 노인 2명", 10, CFGS["ic"]).output
    inbox = I.build_inbox([rep], CFGS["ic"])
    assert rep["urgency"] == 2 and inbox[0]["zone"] == "Z3"
    # 3) 배치 추천: 대피소 → 담당자 승인
    demand = {"id": "H-A03", "zone": "Z3", "people": 2, "amenities": [], "urgency": rep["urgency"]}
    rec = D.recommend("shelter", demand, SHELTERS, CTX, CFGS["dc"])
    assert rec.output["top"]["id"] == "S2" and rec.needs_approval
    board = D.Board("shelter", SHELTERS, CTX, CFGS["dc"]); board.submit(demand, rec)
    assert board.applied == {}
    board.approve("H-A03", "담당자A")
    assert board.applied == {"H-A03": "S2"}
    # 4) 검증: 맞춤 안내(③)를 ⑦ 검증으로 통과시킨 뒤에만 표시
    llm = fake('{"text":"반지하 가구는 지금 대피소로 이동하세요."}')
    g = W.make_guidance({"flags": ["반지하"]}, level, [], CFGS["gc"], client=llm,
                        verifier=V.as_verifier(fake("grounded")))
    assert g.output["displayable"] and g.output["status"] == "승인 대기"
    return co, g


class TestIntegration(unittest.TestCase):
    def test_core_flow_10_of_10(self):
        for _ in range(10):
            run_core_flow()

    def test_unverified_ai_text_never_displayed(self):
        _, g = run_core_flow()
        llm = fake('{"text":"반지하 가구는 지금 대피소로 이동하세요."}')
        no_ver = W.make_guidance({"flags": ["반지하"]}, "경계", [], CFGS["gc"], client=llm)
        self.assertFalse(no_ver.output["displayable"])
        bad = W.make_guidance({"flags": ["반지하"]}, "경계", [], CFGS["gc"], client=llm,
                              verifier=V.as_verifier(fake("notGrounded")))
        self.assertEqual(bad.output["text"], bad.output["standard"])      # 차단되면 표준 원문

    def test_supply_notice_through_verify(self):
        inv = S.Inventory([{"id": "ST1", "item": "생수", "qty": 20, "days_left": 3, "zone": "Z1"}])
        cat = S.load_catalog("config/catalog.json")
        n = S.shortage_notice(S.forecast(inv, cat, now=0), None)
        docs = {"FC": f"생수 약 {S.forecast(inv, cat, now=0)[0]['hours_left']}시간"}
        ok = V.verify(n.output["draft"], ["FC"], {"FC": n.output["draft"]}, fake("grounded"))   # 근거=예측 결과
        self.assertEqual(ok.output["route"], "승인 대기열")
        bad = V.verify(n.output["draft"] + " 99시간", ["FC"], {"FC": n.output["draft"]}, fake("grounded"))
        self.assertTrue(bad.output["blocked"])

    def test_support_guidance_through_verify(self):
        rc = R.load_rules("config/support_rules.json")
        m = R.match_support({"id": "H1", "area": "거제시", "damage_types": ["주택침수"]}, rc)
        draft, ev = R.support_text(m)
        self.assertEqual(V.verify_text(draft, ev, fake("grounded"))[0], "grounded")
        self.assertEqual(V.verify_text(draft.replace("350만원", "500만원"), ev, fake("grounded"))[0], "notGrounded")

    def test_failure_isolation(self):   # AI 전부 실패해도 핵심 판단은 규칙으로 계속
        down = SolarClient(transport=lambda *a: (_ for _ in ()).throw(RuntimeError("net")))
        o = I.classify("X", "Z3 침수됐어요", 0, CFGS["ic"], down)
        self.assertFalse(o.ai_used); self.assertEqual(o.output["type"], "침수")
        g = W.make_guidance({"flags": []}, "경계", [], CFGS["gc"], down)
        self.assertTrue(g.output["displayable"])
