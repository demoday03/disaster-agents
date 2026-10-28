"""마이크로비트 게이트웨이: USB로 연결한 수신용 마이크로비트의 시리얼 출력을 읽어 서버(/api/sensor/readings)로 보낸다.

센서 노드(microbit/node_a.py, node_b.py)가 무선(radio)으로 보낸 값을 수신기(microbit/receiver.py)가 한 줄씩 출력한다.
한 줄 형식:  S,<노드>,<센서>,<값>      예) S,A,water_low,523
그 밖의 줄(디버그 출력 등)은 무시한다.

실행 예(프로젝트 루트에서, 서버는 SENSOR_GATEWAY_TOKEN을 같은 값으로 설정해 실행 중이어야 함):
  python gateway/microbit_gateway.py --new-token                       # 토큰 하나 만들기(서버·게이트웨이에 같은 값 설정)
  python gateway/microbit_gateway.py --list-ports                      # 연결된 시리얼 포트 보기
  python gateway/microbit_gateway.py --port COM3                       # 실제 마이크로비트(Windows)
  python gateway/microbit_gateway.py --port /dev/ttyACM0               # 실제 마이크로비트(리눅스·맥)
  python gateway/microbit_gateway.py --simulate flood --ticks 10       # 기기 없이 가상 센서로 시험

실제 기기 연결에는 pyserial이 필요하다(pip install pyserial). 가상 센서 모드는 표준 라이브러리만 쓴다.
토큰은 환경변수 SENSOR_GATEWAY_TOKEN에서 읽는다(명령줄에 쓰면 기록에 남으므로 쓰지 않는다).
"""
import argparse, json, os, random, re, secrets, sys, time, urllib.error, urllib.request

LINE = re.compile(r"^S,([A-Za-z0-9_]{1,20}),([A-Za-z0-9_]{1,20}),(-?\d{1,6}(?:\.\d{1,3})?)$")
SCENARIOS = ("normal", "flood", "single", "spike", "slope", "dropout")
MAX_PENDING = 400        # 서버에 못 보낸 값은 이만큼만 들고 있는다(오래된 값은 어차피 판정에 쓰이지 않음)


def parse_line(line, ts):
    """시리얼 한 줄 → 측정값 dict. 형식이 다르면 None."""
    m = LINE.match(line.strip())
    if not m:
        return None
    node, sensor, value = m.groups()
    return {"node": node, "sensor": sensor, "value": float(value), "ts": ts}


class Simulator:
    """기기 없이 시험할 때 쓰는 가상 센서. tick()마다 노드 A·B의 한 번 측정값을 시리얼 줄 형식으로 돌려준다.
    normal: 모두 정상 / flood: 수위 두 개가 함께 높음(확정 후보) / single: 낮은 수위만 높음(보류)
    spike: 한 번만 튐(무시) / slope: 기울기+진동(사면 확정 후보) / dropout: 노드 B가 끊김(센서 이상)"""
    def __init__(self, scenario, seed=0):
        if scenario not in SCENARIOS:
            raise ValueError(f"scenario는 {SCENARIOS} 중 하나")
        self.scenario, self.n, self.rng = scenario, 0, random.Random(seed)

    def tick(self):
        s, n, r = self.scenario, self.n, self.rng
        self.n += 1
        low = high = r.randint(80, 200)
        tilt, vib = 0, r.randint(10, 120)
        if s == "flood":
            low, high = r.randint(600, 800), r.randint(520, 700)
        elif s == "single":
            low = r.randint(600, 800)
        elif s == "spike" and n == 2:
            low = high = 900
        elif s == "slope":
            tilt, vib = 1, r.randint(400, 700)
        lines = [f"S,A,water_low,{low}", f"S,A,water_high,{high}"]
        if not (s == "dropout" and n >= 1):
            lines += [f"S,B,tilt,{tilt}", f"S,B,vibration,{vib}"]
        return lines


def post(server, token, readings, rain_stage=None, timeout=5):
    body = {"readings": readings}
    if rain_stage is not None:
        body["rain_stage"] = rain_stage
    req = urllib.request.Request(server.rstrip("/") + "/api/sensor/readings", method="POST",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "X-Gateway-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {"error": e.reason}


def describe(res):
    o = res.get("output", {})
    msg = f"판정 {o.get('status')} · 제안 단계 {o.get('level')}"
    if o.get("hazards"):
        msg += f" · {'/'.join(o['hazards'])}"
    if res.get("approval_id"):
        msg += f" → 승인 대기열에 올림({res['approval_id']})"
    elif res.get("duplicate_of"):
        msg += " (같은 단계가 이미 대기·확정 중)"
    if o.get("sensor_faults"):
        msg += f" · 응답 끊긴 노드 {','.join(o['sensor_faults'])}"
    return msg


class Gateway:
    """줄을 모아 interval초마다 서버로 보낸다. 전송에 실패하면 다음 번에 다시 보낸다."""
    def __init__(self, server, token, interval=1.0, rain_stage=None, sender=post, out=print, clock=time.time):
        self.server, self.token, self.interval, self.rain_stage = server, token, interval, rain_stage
        self.sender, self.out, self.clock = sender, out, clock
        self.pending, self.last_flush, self.ignored, self.last_result = [], clock(), 0, None

    def feed(self, line):
        r = parse_line(line, self.clock())
        if r is None:
            if line.strip():
                self.ignored += 1
            return
        self.pending.append(r)
        if len(self.pending) > MAX_PENDING:
            del self.pending[:len(self.pending) - MAX_PENDING]

    def due(self):
        return self.clock() - self.last_flush >= self.interval

    def flush(self):
        self.last_flush = self.clock()
        if not self.pending:
            return None
        batch = self.pending[:]
        try:
            status, res = self.sender(self.server, self.token, batch, self.rain_stage)
        except (urllib.error.URLError, OSError) as e:
            self.out(f"[전송 실패] 서버에 연결할 수 없습니다: {getattr(e, 'reason', e)} · {len(batch)}개 보관 후 재시도")
            return None
        if status == 200:
            del self.pending[:len(batch)]
            self.last_result = res
            self.out(f"[{time.strftime('%H:%M:%S')}] {len(batch)}개 전송 · {describe(res)}")
            return res
        if status in (400, 401, 403, 421, 503):   # 다시 보내도 같은 결과인 오류는 버리고 원인을 알린다
            del self.pending[:len(batch)]
        self.out(f"[전송 거부] HTTP {status}: {res.get('error')}")
        return None


def serial_lines(port, baud):
    try:
        import serial   # pyserial
    except ImportError:
        raise SystemExit("실제 마이크로비트를 읽으려면 pyserial이 필요합니다: pip install pyserial  (기기 없이 시험: --simulate flood)")
    with serial.Serial(port, baud, timeout=0.2) as ser:
        while True:
            raw = ser.readline()
            yield raw.decode("utf-8", "replace") if raw else ""


def list_ports():
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        raise SystemExit("포트 목록을 보려면 pyserial이 필요합니다: pip install pyserial")
    ports = list(lp.comports())
    for p in ports:
        hint = " ← 마이크로비트로 보임" if "mbed" in (p.description or "").lower() or p.vid == 0x0D28 else ""
        print(f"{p.device}\t{p.description}{hint}")
    if not ports:
        print("연결된 시리얼 포트가 없습니다. USB 케이블(데이터 전송용)과 마이크로비트 전원을 확인하세요.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="마이크로비트 → 재난 안전 서버 게이트웨이")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--port", help="수신 마이크로비트의 시리얼 포트(예: COM3, /dev/ttyACM0)")
    src.add_argument("--simulate", choices=SCENARIOS, help="기기 없이 가상 센서 값으로 시험")
    src.add_argument("--new-token", action="store_true", help="게이트웨이 토큰을 하나 만들어 출력")
    src.add_argument("--list-ports", action="store_true", help="연결된 시리얼 포트 보기")
    ap.add_argument("--server", default=os.getenv("GATEWAY_SERVER", "http://127.0.0.1:8765"))
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--interval", type=float, default=1.0, help="서버로 보내는 간격(초)")
    ap.add_argument("--rain-stage", type=int, choices=range(5), default=None, help="강우 단계(0~4, 생략하면 서버 값 유지)")
    ap.add_argument("--ticks", type=int, default=0, help="가상 센서 모드에서 보낼 횟수(0이면 Ctrl+C까지)")
    args = ap.parse_args(argv)

    if args.new_token:
        print(secrets.token_urlsafe(24))
        return 0
    if args.list_ports:
        list_ports()
        return 0
    if not args.port and not args.simulate:
        ap.error("--port 또는 --simulate 중 하나가 필요합니다")
    token = os.getenv("SENSOR_GATEWAY_TOKEN", "").strip()
    if not token:
        raise SystemExit("환경변수 SENSOR_GATEWAY_TOKEN이 없습니다. 서버와 같은 값을 설정하세요(--new-token으로 생성).")

    gw = Gateway(args.server, token, args.interval, args.rain_stage)
    print(f"게이트웨이 시작: {'가상 센서(' + args.simulate + ')' if args.simulate else args.port} → {args.server}  (Ctrl+C로 종료)")
    try:
        if args.simulate:
            sim, sent = Simulator(args.simulate), 0
            while not args.ticks or sent < args.ticks:
                for line in sim.tick():
                    gw.feed(line)
                gw.flush()
                sent += 1
                if not args.ticks or sent < args.ticks:
                    time.sleep(args.interval)
        else:
            for line in serial_lines(args.port, args.baud):
                gw.feed(line)
                if gw.due():
                    gw.flush()
    except KeyboardInterrupt:
        pass
    if gw.ignored:
        print(f"형식이 다른 줄 {gw.ignored}개는 무시했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
