"""① 감시 에이전트의 실시간 센서 입력(마이크로비트 게이트웨이 → 서버).
게이트웨이(gateway/microbit_gateway.py)가 보낸 측정값을 최근 구간만 메모리에 모아 두고, 받을 때마다 감시 규칙으로 다시 판정한다.
- 시각은 서버 시계 기준. 게이트웨이 시각이 조금 앞서면 서버 시각으로 맞추고, 크게 어긋나면 버린다.
- 확정 후보가 나와도 단계는 바꾸지 않고 승인 대기열에 한 번만 올린다(같은 내용의 대기 건·확정 단계가 있으면 다시 올리지 않음).
- 측정값은 DB에 저장하지 않는다(판정 결과·승인 요청만 남는다). 서버를 다시 켜면 버퍼는 비어 있다.
"""
import math, threading, time
from collections import deque

from agents import monitor

MAX_BATCH = 500          # 요청 한 번에 받는 측정값 수
MAX_BUFFER = 5000        # 메모리에 두는 측정값 수
CLOCK_SKEW_S = 5         # 게이트웨이 시계가 이만큼까지 앞서는 것은 서버 시각으로 맞춘다
NAME_MAX = 20


class SensorHub:
    def __init__(self, db, cfg=None, clock=time.time):
        self.db, self.cfg, self.clock = db, cfg or monitor.load_config(), clock
        self.buffer, self.lock = deque(maxlen=MAX_BUFFER), threading.Lock()
        self.last_eval, self.last_seen, self.received, self.rejected = None, {}, 0, 0
        self.rain_stage, self.last_batch_at = 0, None

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

    def ingest(self, readings, rain_stage=None):
        """측정값 묶음을 받아 다시 판정한다. 결과 dict(판정 + 승인 요청 ID)를 돌려준다."""
        if not isinstance(readings, list) or len(readings) > MAX_BATCH:
            raise ValueError(f"readings는 최대 {MAX_BATCH}개 항목의 배열이어야 합니다")
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
            if rain_stage is not None:
                self.rain_stage = rain_stage
            self.last_batch_at = now
            result = self._evaluate(now)
            return {**result, "accepted": accepted, "rejected": len(readings) - accepted}

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
            nodes = [{"node": n, "last_seen_age_s": round(now - self.last_seen[n], 1) if n in self.last_seen else None,
                      "online": n in self.last_seen and now - self.last_seen[n] <= self.cfg["stale_sec"]}
                     for n in self.cfg["nodes"]]
            return {"sensors": sensors, "nodes": nodes, "last_eval": self.last_eval, "received": self.received,
                    "rejected": self.rejected, "rain_stage": self.rain_stage,
                    "last_batch_age_s": round(now - self.last_batch_at, 1) if self.last_batch_at else None,
                    "calibrated": self.cfg.get("calibrated", False)}
