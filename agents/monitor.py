"""① 감시 에이전트 (규칙 기반, LLM은 근거 요약에만 선택적으로 사용).
- 침수: 서로 다른 높이의 수위 센서 둘이 함께 임계 초과 + 연속 N회 → 확정 후보
- 사면: 기울기와 진동이 짧은 시간 안에 함께 반응 → 확정 후보
- 단독 반응은 '보류', 일시 튐은 '정상'(로그만). 단계 확정은 담당자.
"""
import json
from collections import defaultdict
from common.schema import AgentResult

LEVELS = ["정상", "주의", "경계", "위험"]


def load_config(path="config/monitor.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Monitor:
    def __init__(self, config, summarizer=None):
        self.cfg, self.summarizer = config, summarizer

    def evaluate(self, readings, now, rain_stage=0):
        cfg, n, win = self.cfg, self.cfg["sustain_n"], self.cfg["window_sec"]
        by = defaultdict(list)
        # 미래 시각·오래된 샘플·잘못된 입력은 위험 판정에서 제외한다.
        for r in readings:
            try:
                node, sensor = r["node"], r["sensor"]
                ts, value = float(r["ts"]), float(r["value"])
                key = f"{node}.{sensor}"
                if node not in cfg["nodes"] or key not in cfg["sensors"]: continue
                if ts > now or ts < now - cfg["stale_sec"]: continue
                by[key].append({"node": node, "sensor": sensor, "ts": ts, "value": value})
            except (KeyError, TypeError, ValueError):
                continue
        for samples in by.values(): samples.sort(key=lambda r: r["ts"])

        # 노드 연결 상태와 개별 센서 데이터 신선도를 따로 검사한다.
        faults, stale_sensors = [], []
        for node in cfg["nodes"]:
            ts = [r["ts"] for k, values in by.items() if k.startswith(node + ".") for r in values]
            if not ts or now - max(ts) > cfg["stale_sec"]: faults.append(node)
        for key in cfg["sensors"]:
            samples = by.get(key, [])
            if not samples or now - samples[-1]["ts"] > cfg["stale_sec"]: stale_sensors.append(key)

        def thr(k): return cfg["sensors"][k]["threshold"]
        def usable(k): return k.split(".")[0] not in faults and k not in stale_sensors
        def recent(k): return [x for x in by.get(k, []) if now - win <= x["ts"] <= now]
        def sustained(k):
            samples = recent(k)
            return usable(k) and len(samples) >= n and all(x["value"] > thr(k) for x in samples[-n:])
        def spiked(k): return usable(k) and any(x["value"] > thr(k) for x in recent(k))
        def in_window(k): return usable(k) and any(x["value"] > thr(k) for x in recent(k))

        hazards, matched, log, hold = [], [], [], bool(stale_sensors)
        for sensor in stale_sensors:
            log.append(f"센서 {sensor} 최신 데이터 없음/오래됨 → 판단 보류")
        for name, rule in cfg["rules"].items():
            keys = rule["keys"]
            test = sustained if rule["mode"] == "sustained" else in_window
            hits = [k for k in keys if test(k)]
            label = {"flood": "침수", "slope": "사면"}.get(name, name)
            if len(hits) == len(keys):
                hazards.append(label); matched += hits
            elif hits:
                hold = True
                log.append(f"{label}: {hits[0]} 단독 반응 → 보류(오탐 가능)")
            elif any(spiked(k) for k in keys):
                log.append(f"{label}: 일시 튐 → 무시")
        for node in faults:
            log.append(f"노드 {node} 응답 끊김 → 센서 이상, 수동 입력 허용")

        if hazards:
            status, level = "확정후보", "경계"
            if rain_stage >= cfg["danger_rain_stage"]:
                level = "위험"
        elif hold:
            status, level = "보류", "주의"
        else:
            status, level = "정상", "정상"

        summary, ai_used, reason = self._summary(status, hazards, matched, level)
        return AgentResult(
            agent="monitor",
            output={"status": status, "level": level, "hazards": hazards,
                    "matched_sensors": matched, "false_alarm_log": log,
                    "sensor_faults": faults, "stale_sensors": stale_sensors,
                    "summary": summary, "calibrated": cfg.get("calibrated", False)},
            ai_used=ai_used, fallback_reason=reason,
            needs_approval=(status == "확정후보"),  # 단계 확정은 담당자
            evidence=matched)

    def _summary(self, status, hazards, matched, level):
        default = (f"{'·'.join(hazards)} 신호가 {', '.join(matched)}에서 함께 확인되어 "
                   f"'{level}' 단계를 제안합니다." if hazards else
                   {"보류": "단독 반응만 있어 확정하지 않고 지켜봅니다.",
                    "정상": "임계 초과 신호가 없습니다."}[status])
        if status != "확정후보" or self.summarizer is None:
            return default, False, None if status != "확정후보" else "요약기 없음"
        try:
            return self.summarizer(hazards, matched, level), True, None
        except Exception as e:
            return default, False, f"요약 실패: {e}"
