"""① 감시 에이전트의 실시간 센서 입력(마이크로비트 게이트웨이 → 서버).
게이트웨이(gateway/microbit_gateway.py)가 보낸 측정값을 최근 구간만 메모리에 모아 두고, 받을 때마다 감시 규칙으로 다시 판정한다.
- 시각은 서버 시계 기준. 게이트웨이 시각이 조금 앞서면 서버 시각으로 맞추고, 크게 어긋나면 버린다.
- 확정 후보가 나와도 단계는 바꾸지 않고 승인 대기열에 한 번만 올린다(같은 내용의 대기 건·확정 단계가 있으면 다시 올리지 않음).
- 측정값은 DB에 저장하지 않는다(판정 결과·승인 요청만 남는다). 서버를 다시 켜면 버퍼는 비어 있다.
- 현장 버튼 신호(E 메시지: SOS·체크인·입소 등)는 최근 것만 메모리에 두고 감사 기록에 남긴다. 단계를 바꾸지 않는다.
  급한 신호(SOS 등)는 담당자가 '확인'을 누를 때까지 열려 있다.
- 내려보내기(downlink): 담당자가 확정한 단계와 안부 확인 요청을 게이트웨이에 알려 현장 마이크로비트로 방송하게 한다.
- 외부 모듈(G 노드 블루투스 센서 등)처럼 감시 설정(config/monitor.json)에 없는 센서 값도 받아 담당자 화면에 보여 준다.
  임계값이 없으므로 단계 판정에는 쓰지 않는다(표시만).
"""
import itertools, math, threading, time
from collections import deque

from agents import monitor

MAX_BATCH = 500          # 요청 한 번에 받는 측정값 수
MAX_BUFFER = 5000        # 메모리에 두는 측정값 수
MAX_EVENTS = 200         # 메모리에 두는 현장 신호 수
EVENT_DEDUP_S = 60       # 같은 노드·코드·번호가 이 시간 안에 다시 오면 무선 재전송으로 보고 버린다
CLOCK_SKEW_S = 5         # 게이트웨이 시계가 이만큼까지 앞서는 것은 서버 시각으로 맞춘다
NAME_MAX = 20
# 마이크로비트 버튼 신호(microbit/makecode의 C·D·E 노드). 여기 없는 코드는 받지 않는다
EVENT_LABELS = {"SOS": "SOS 요청", "IN": "봉사자 도착 체크인", "DONE": "물품 전달 완료",
                "HEAT": "폭염 경고(온도 기준 넘음)", "COOL": "온도 정상으로 돌아옴",
                "+1": "대피소 입소 +1", "-1": "대피소 퇴소 -1", "GOT": "대피소 물품 도착",
                "SAFE": "가구 안전 확인 응답", "NEED": "가구 급한 물품 요청",
                "DARK": "가구 정전(어두워짐)", "LIGHT": "가구 전기 돌아옴"}
URGENT_EVENTS = {"SOS", "NEED", "HEAT", "DARK"}


class SensorHub:
    def __init__(self, db, cfg=None, clock=time.time):
        self.db, self.cfg, self.clock = db, cfg or monitor.load_config(), clock
        self.buffer, self.lock = deque(maxlen=MAX_BUFFER), threading.Lock()
        self.last_eval, self.last_seen, self.received, self.rejected = None, {}, 0, 0
        self.rain_stage, self.last_batch_at = 0, None
        self.events, self.event_seen, self.event_ids = deque(maxlen=MAX_EVENTS), {}, itertools.count(1)

    def _clean(self, r, now):
        """측정값 하나를 검사해 {'node','sensor','ts','value'}로 돌려준다. 쓸 수 없으면 None."""
        if not isinstance(r, dict):
            return None
        node, sensor, value, ts = r.get("node"), r.get("sensor"), r.get("value"), r.get("ts", now)
        if not all(isinstance(x, str) and 0 < len(x) <= NAME_MAX for x in (node, sensor)):
            return None
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in (value, ts)):
            return None
        if ts > now + CLOCK_SKEW_S or ts < now - self.cfg["stale_sec"]:
            return None   # 시계가 크게 틀렸거나 이미 오래된 값
        return {"node": node, "sensor": sensor, "ts": min(float(ts), now), "value": float(value)}

    def ingest(self, readings, rain_stage=None, events=None):
        """측정값 묶음(과 현장 신호)을 받아 다시 판정한다. 결과 dict(판정 + 승인 요청 ID + 새 현장 신호)를 돌려준다."""
        if not isinstance(readings, list) or len(readings) > MAX_BATCH:
            raise ValueError(f"readings는 최대 {MAX_BATCH}개 항목의 배열이어야 합니다")
        if events is not None and (not isinstance(events, list) or len(events) > MAX_BATCH):
            raise ValueError(f"events는 최대 {MAX_BATCH}개 항목의 배열이어야 합니다")
        if rain_stage is not None and (isinstance(rain_stage, bool) or not isinstance(rain_stage, int)
                                       or not 0 <= rain_stage <= 4):
            raise ValueError("rain_stage는 0~4 정수여야 합니다")
        with self.lock:
            now = self.clock()
            accepted = 0
            for r in readings:
                row = self._clean(r, now)
                if row is None:
                    self.rejected += 1
                    continue
                self.buffer.append(row)
                self.last_seen[row["node"]] = max(self.last_seen.get(row["node"], 0), row["ts"])
                accepted += 1
            self.received += accepted
            new_events = [e for e in (self._add_event(e, now) for e in events or []) if e]
            if rain_stage is not None:
                self.rain_stage = rain_stage
            self.last_batch_at = now
            result = self._evaluate(now)
            return {**result, "accepted": accepted, "rejected": len(readings) - accepted, "events": new_events}

    def _add_event(self, e, now):
        """현장 신호 하나를 검사해 기록한다. 형식이 틀렸거나 무선 재전송(중복)이면 None."""
        if not isinstance(e, dict):
            return None
        node, code, seq, ts = e.get("node"), e.get("code"), e.get("seq"), e.get("ts", now)
        if not (isinstance(node, str) and 0 < len(node) <= NAME_MAX and code in EVENT_LABELS
                and isinstance(seq, int) and not isinstance(seq, bool) and 0 <= seq <= 999):
            return None
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts) \
                or not now - self.cfg["stale_sec"] <= ts <= now + CLOCK_SKEW_S:
            ts = now
        self.event_seen = {k: t for k, t in self.event_seen.items() if now - t < EVENT_DEDUP_S}
        if (node, code, seq) in self.event_seen:
            return None
        self.event_seen[(node, code, seq)] = now
        urgent = code in URGENT_EVENTS
        row = {"id": f"EV{next(self.event_ids)}", "node": node, "code": code, "label": EVENT_LABELS[code],
               "ts": min(float(ts), now), "urgent": urgent, "open": urgent}
        self.events.append(row)
        self.last_seen[node] = max(self.last_seen.get(node, 0), row["ts"])
        self.db.log_audit("field_event", "node", node, {"code": code, "seq": seq, "event_id": row["id"]})
        return row

    def annotate(self, event_id, **fields):
        """현장 신호에 웹 기능 반영 결과(applied·note 등)를 붙인다."""
        with self.lock:
            row = next((e for e in self.events if e["id"] == event_id), None)
            if row is not None:
                row.update(fields)

    def ack_event(self, event_id, by):
        """담당자가 급한 현장 신호를 확인했다고 표시한다."""
        with self.lock:
            row = next((e for e in self.events if e["id"] == event_id), None)
            if row is None:
                raise KeyError(event_id)
            if row["open"]:
                row.update(open=False, acked_by=by, acked_at=self.clock())
                self.db.log_audit("field_event_ack", "node", row["node"], {"event_id": event_id, "by": by})
            return dict(row)

    def downlink(self):
        """게이트웨이가 현장으로 방송할 내용: 확정된 단계(0~3)와 안부 확인 진행 여부. 확정 전 후보는 내려보내지 않는다."""
        stage = self.db.get_record("stage", "current") or {}
        level = stage.get("level") if stage.get("confirmed") else "정상"
        checks = self.db.list_records("check")
        return {"level": monitor.LEVELS.index(level) if level in monitor.LEVELS else 0,
                "level_name": level if level in monitor.LEVELS else "정상",
                "safety_check": any(c.get("state") in ("대기", "격상") for c in checks)}

    def _evaluate(self, now):
        keep = now - 2 * self.cfg["stale_sec"]
        while self.buffer and self.buffer[0]["ts"] < keep:   # ts 순서가 조금 섞여도 판정은 monitor가 정렬해서 한다
            self.buffer.popleft()
        row = monitor.Monitor(self.cfg).evaluate(list(self.buffer), now, self.rain_stage).to_dict()
        approval_id, duplicate_of = None, None
        if row["needs_approval"]:
            duplicate_of = self._same_stage_pending_or_confirmed(row["output"])
            if duplicate_of is None:
                approval_id = self.db.create_approval("monitor_stage", {**row["output"], "source": "gateway"})["id"]
        self.last_eval = {"at": now, "output": row["output"], "approval_id": approval_id, "duplicate_of": duplicate_of}
        return {"output": row["output"], "needs_approval": row["needs_approval"],
                "approval_id": approval_id, "duplicate_of": duplicate_of}

    def _same_stage_pending_or_confirmed(self, out):
        """같은 단계·위험 유형이 이미 승인 대기 중이거나 확정돼 있으면 그 ID(또는 'confirmed'). 1초마다 승인 요청이 쌓이지 않게 한다."""
        key = (out["level"], tuple(sorted(out["hazards"])))
        for a in self.db.list_pending_approvals():
            if a["kind"] == "monitor_stage" and (a["item"].get("level"), tuple(sorted(a["item"].get("hazards", [])))) == key:
                return a["id"]
        cur = self.db.get_record("stage", "current")
        if cur and cur.get("confirmed") and (cur.get("level"), tuple(sorted(cur.get("hazards", [])))) == key:
            return "confirmed"
        return None

    def window(self, seconds):
        """최근 seconds초 동안 센서별 최댓값과 마지막 값의 나이. 신고 대조에 쓴다(순간 값 하나보다 덜 흔들린다)."""
        with self.lock:
            now, out = self.clock(), {}
            for r in self.buffer:
                if r["ts"] < now - seconds:
                    continue
                k = f"{r['node']}.{r['sensor']}"
                w = out.setdefault(k, {"max": r["value"], "age_s": now - r["ts"]})
                w["max"] = max(w["max"], r["value"])
                w["age_s"] = round(min(w["age_s"], now - r["ts"]), 1)
            return out

    def thresholds(self):
        return {k: v["threshold"] for k, v in self.cfg["sensors"].items()}

    def status(self):
        with self.lock:
            now = self.clock()
            latest = {}
            for r in self.buffer:
                k = f"{r['node']}.{r['sensor']}"
                if k not in latest or r["ts"] >= latest[k]["ts"]:
                    latest[k] = r
            sensors = [{"key": k, "value": latest[k]["value"] if k in latest else None,
                        "age_s": round(now - latest[k]["ts"], 1) if k in latest else None,
                        "threshold": v["threshold"],
                        "over": k in latest and latest[k]["value"] > v["threshold"]}
                       for k, v in self.cfg["sensors"].items()]
            extra = sorted(({"key": k, "node": r["node"], "sensor": r["sensor"], "value": r["value"],
                             "age_s": round(now - r["ts"], 1), "online": now - r["ts"] <= self.cfg["stale_sec"]}
                            for k, r in latest.items() if k not in self.cfg["sensors"]), key=lambda x: x["key"])[:60]
            names = list(self.cfg["nodes"]) + sorted(n for n in self.last_seen if n not in self.cfg["nodes"])
            nodes = [{"node": n, "last_seen_age_s": round(now - self.last_seen[n], 1) if n in self.last_seen else None,
                      "online": n in self.last_seen and now - self.last_seen[n] <= self.cfg["stale_sec"]}
                     for n in names]
            events = [dict(e) for e in reversed(self.events)]
            return {"sensors": sensors, "extra": extra, "nodes": nodes, "last_eval": self.last_eval, "received": self.received,
                    "events": events[:30] + [e for e in events[30:] if e["open"]], "open_urgent": sum(e["open"] for e in events),
                    "rejected": self.rejected, "rain_stage": self.rain_stage,
                    "last_batch_age_s": round(now - self.last_batch_at, 1) if self.last_batch_at else None,
                    "calibrated": self.cfg.get("calibrated", False)}
