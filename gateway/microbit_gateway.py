"""마이크로비트 게이트웨이: USB로 연결한 수신용 마이크로비트의 시리얼 출력을 읽어 서버(/api/sensor/readings)로 보낸다.

센서 노드(microbit/node_a.py, node_b.py)가 무선(radio)으로 보낸 값을 수신기(microbit/receiver.py)가 한 줄씩 출력한다.
한 줄 형식:  S,<노드>,<센서>,<값>      예) S,A,water_low,523
             E,<노드>,<신호>,<번호>    예) E,E,SOS,7   (현장 버튼 신호. 받으면 수신기로 K,E,7을 돌려보내 재전송을 멈춘다)
그 밖의 줄(디버그 출력 등)은 무시한다.
반대 방향: 서버에서 담당자가 확정한 단계를 몇 초마다 물어 바뀌면 수신기로 L0~L3(정상이면 X)을,
안부 확인이 시작되면 Q,D를 보낸다. 수신기가 현장 노드에 무선으로 방송한다.

실행 예(프로젝트 루트에서, 서버는 SENSOR_GATEWAY_TOKEN을 같은 값으로 설정해 실행 중이어야 함):
  python gateway/microbit_gateway.py --new-token                       # 토큰 하나 만들기(서버·게이트웨이에 같은 값 설정)
  python gateway/microbit_gateway.py --list-ports                      # 연결된 시리얼 포트 보기
  python gateway/microbit_gateway.py --port COM3                       # 실제 마이크로비트(Windows)
  python gateway/microbit_gateway.py --port /dev/ttyACM0               # 실제 마이크로비트(리눅스·맥)
  python gateway/microbit_gateway.py --simulate flood --ticks 10       # 기기 없이 가상 센서로 시험

G 노드 외부 모듈(블루투스): 블루투스 센서 모듈(HC-05/HC-06, ESP32 등 블루투스 시리얼)을 이 노트북과 페어링하면
운영체제가 시리얼 포트(Windows는 'COM5' 같은 이름)를 만든다. --bt-port로 그 포트를 함께 읽어 노드 G의 값으로 서버에 보낸다.
(G 마이크로비트는 무선(radio)으로 A~E를 받고 있어 블루투스를 함께 쓸 수 없다. 그래서 G가 꽂힌 노트북이 블루투스를 받는다.)
모듈이 보내는 한 줄은 아래 모양 중 하나면 된다(줄 끝은 줄바꿈):
  S,G,temp,23.5            마이크로비트와 같은 형식
  temp=23.5,humidity=61    이름=값(쉼표·세미콜론·띄어쓰기로 여러 개)
  T:23.5 H:61              이름:값
  23.5,61                  숫자만(--bt-fields temp,humidity로 이름을 붙인다. 없으면 v1, v2)
  python gateway/microbit_gateway.py --port COM3 --bt-port COM5            # G 마이크로비트 + 블루투스 모듈
  python gateway/microbit_gateway.py --bt-port COM5 --bt-fields temp,gas   # 블루투스 모듈만
  python gateway/microbit_gateway.py --simulate normal --bt-simulate       # 둘 다 기기 없이 시험

실제 기기 연결에는 pyserial이 필요하다(pip install pyserial). 가상 센서 모드는 표준 라이브러리만 쓴다.
토큰은 환경변수 SENSOR_GATEWAY_TOKEN에서 읽는다(명령줄에 쓰면 기록에 남으므로 쓰지 않는다).
"""
import argparse, json, os, queue, random, re, secrets, sys, threading, time, urllib.error, urllib.request

LINE = re.compile(r"^S,([A-Za-z0-9_]{1,20}),([A-Za-z0-9_]{1,20}),(-?\d{1,6}(?:\.\d{1,3})?)$")
EVENT_LINE = re.compile(r"^E,([A-Za-z0-9_]{1,20}),([A-Za-z0-9+\-]{1,8}),(\d{1,3})$")
SCENARIOS = ("normal", "flood", "single", "spike", "slope", "dropout", "sos")
MAX_PENDING = 400        # 서버에 못 보낸 값은 이만큼만 들고 있는다(오래된 값은 어차피 판정에 쓰이지 않음)
RETRY_S = 3.0            # USB 시리얼이 끊겼을 때 다시 연결을 시도하는 간격(초)
EVENT_DEDUP_S = 60       # 노드는 수신 확인(K)이 올 때까지 같은 신호를 다시 보낸다. 이 시간 안의 같은 신호는 한 번만 올린다
DOWNLINK_S = 3.0         # 서버에 확정 단계를 묻는 간격(초)
RESEND_LEVEL_S = 30.0    # 무선은 유실될 수 있어 같은 단계도 이 간격으로 다시 방송한다(노드는 같은 단계면 무시)


def parse_line(line, ts):
    """시리얼 한 줄 → 측정값 dict. 형식이 다르면 None."""
    m = LINE.match(line.strip())
    if not m:
        return None
    node, sensor, value = m.groups()
    return {"node": node, "sensor": sensor, "value": float(value), "ts": ts}


MODULE_PAIR = re.compile(r"([^\s=:,;]{1,20})\s*[=:]\s*(-?\d{1,6}(?:\.\d{1,3})?)(?![\d.])")
MODULE_NUM = re.compile(r"^-?\d{1,6}(?:\.\d{1,3})?$")
MODULE_NAME = re.compile(r"^\w{1,20}$")


def parse_module_line(line, node, ts, fields=()):
    """블루투스 외부 모듈의 한 줄 → 측정값 목록(형식이 다르면 빈 목록).
    'S,G,temp,23.5' / 'temp=23.5,humidity=61' / 'T:23.5 H:61' / '23.5,61'(fields로 이름, 없으면 v1·v2)."""
    line = line.strip()
    if not line:
        return []
    r = parse_line(line, ts)
    if r is not None:
        return [r]
    pairs = MODULE_PAIR.findall(line)
    if pairs:
        return [{"node": node, "sensor": k, "value": float(v), "ts": ts} for k, v in pairs if MODULE_NAME.match(k)]
    parts = [p for p in re.split(r"[\s,;]+", line) if p]
    if 0 < len(parts) <= 10 and all(MODULE_NUM.match(p) for p in parts):
        names = list(fields)[:len(parts)] + [f"v{i + 1}" for i in range(len(fields), len(parts))]
        return [{"node": node, "sensor": names[i], "value": float(p), "ts": ts} for i, p in enumerate(parts)]
    return []


class ModuleSimulator:
    """기기 없이 시험할 때 쓰는 가상 블루투스 모듈: 온도·습도·가스 값을 조금씩 바꿔 'temp=..,humidity=..,gas=..' 줄로 준다."""
    def __init__(self, seed=0):
        self.rng, self.t, self.h, self.g = random.Random(seed), 24.0, 60.0, 150.0

    def line(self):
        r = self.rng
        self.t = min(40, max(10, self.t + r.uniform(-0.3, 0.3)))
        self.h = min(99, max(20, self.h + r.uniform(-1, 1)))
        self.g = min(900, max(50, self.g + r.uniform(-15, 15)))
        return f"temp={self.t:.1f},humidity={self.h:.0f},gas={self.g:.0f}"


def parse_event(line, ts):
    """시리얼 한 줄 → 현장 신호 dict. 형식이 다르면 None."""
    m = EVENT_LINE.match(line.strip())
    if not m:
        return None
    node, code, seq = m.groups()
    return {"node": node, "code": code, "seq": int(seq), "ts": ts}


class Simulator:
    """기기 없이 시험할 때 쓰는 가상 센서. tick()마다 노드 A·B의 한 번 측정값을 시리얼 줄 형식으로 돌려준다.
    normal: 모두 정상 / flood: 수위 두 개가 함께 높음(확정 후보) / single: 낮은 수위만 높음(보류)
    spike: 한 번만 튐(무시) / slope: 기울기+진동(사면 확정 후보) / dropout: 노드 B가 끊김(센서 이상)
    sos: 센서는 정상, 첫 회에 봉사자 노드 E의 SOS 버튼 신호(같은 신호를 무선 재전송처럼 두 번)"""
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
        if s == "sos" and n == 0:
            lines += ["E,E,SOS,1", "E,E,SOS,1"]
        return lines


def post(server, token, readings, rain_stage=None, events=(), timeout=5):
    body = {"readings": readings}
    if rain_stage is not None:
        body["rain_stage"] = rain_stage
    if events:
        body["events"] = list(events)
    req = urllib.request.Request(server.rstrip("/") + "/api/sensor/readings", method="POST",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "X-Gateway-Token": token})
    return _send(req, timeout)


def get_downlink(server, token, timeout=5):
    req = urllib.request.Request(server.rstrip("/") + "/api/sensor/downlink", headers={"X-Gateway-Token": token})
    return _send(req, timeout)


def _send(req, timeout):
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
    if res.get("events"):
        msg += " · 현장 신호 " + ", ".join(f"{e['node']} {e['label']}" + (f"({e['note']})" if e.get("note") else "")
                                         for e in res["events"])
    return msg


class Gateway:
    """줄을 모아 interval초마다 서버로 보낸다. 전송에 실패하면 다음 번에 다시 보낸다.
    writer가 있으면(실제 기기) 현장 신호에 수신 확인 K를 돌려보내고, 서버의 확정 단계를 L/X·Q로 내려보낸다."""
    def __init__(self, server, token, interval=1.0, rain_stage=None, sender=post, out=print, clock=time.time,
                 writer=None, fetcher=get_downlink):
        self.server, self.token, self.interval, self.rain_stage = server, token, interval, rain_stage
        self.sender, self.out, self.clock, self.writer, self.fetcher = sender, out, clock, writer, fetcher
        self.pending, self.last_flush, self.ignored, self.last_result = [], clock(), 0, None
        self.pending_events, self.event_seen = [], {}
        self.last_poll, self.sent_level, self.sent_level_at, self.sent_check, self.downlink_ok = None, None, 0, False, True

    def feed(self, line):
        now = self.clock()
        r = parse_line(line, now)
        if r is not None:
            self.pending.append(r)
            if len(self.pending) > MAX_PENDING:
                del self.pending[:len(self.pending) - MAX_PENDING]
            return
        e = parse_event(line, now)
        if e is None:
            if line.strip():
                self.ignored += 1
            return
        self.write(f"K,{e['node']},{e['seq']}")   # 중복이어도 확인은 다시 보낸다(앞선 K가 무선에서 유실됐을 수 있음)
        self.event_seen = {k: t for k, t in self.event_seen.items() if now - t < EVENT_DEDUP_S}
        key = (e["node"], e["code"], e["seq"])
        if key in self.event_seen:
            return
        self.event_seen[key] = now
        self.out(f"[현장 신호] 노드 {e['node']} · {e['code']}")
        self.pending_events.append(e)
        if len(self.pending_events) > MAX_PENDING:
            del self.pending_events[:len(self.pending_events) - MAX_PENDING]

    def feed_module(self, line, node="G", fields=()):
        """블루투스 외부 모듈(G 노드)이 보낸 한 줄. 처음 보는 센서는 한 번 알려 준다."""
        rows = parse_module_line(line, node, self.clock(), fields)
        if not rows:
            if line.strip():
                self.ignored += 1
            return 0
        seen = getattr(self, "module_seen", set())
        for r in rows:
            if (r["node"], r["sensor"]) not in seen:
                seen.add((r["node"], r["sensor"]))
                self.out(f"[블루투스 모듈] 노드 {r['node']} · {r['sensor']} 값 받기 시작 ({r['value']:g})")
        self.module_seen = seen
        self.pending.extend(rows)
        if len(self.pending) > MAX_PENDING:
            del self.pending[:len(self.pending) - MAX_PENDING]
        return len(rows)

    def write(self, msg):
        """수신기 마이크로비트로 한 줄 보낸다(무선 방송용). 기기가 없거나 끊겼으면 False."""
        return bool(self.writer and self.writer(msg))

    def due(self):
        return self.clock() - self.last_flush >= self.interval

    def flush(self):
        self.last_flush = self.clock()
        if not self.pending and not self.pending_events:
            return None
        batch, events = self.pending[:], self.pending_events[:]
        try:
            status, res = self.sender(self.server, self.token, batch, self.rain_stage, events)
        except (urllib.error.URLError, OSError) as e:
            self.out(f"[전송 실패] 서버에 연결할 수 없습니다: {getattr(e, 'reason', e)} · {len(batch) + len(events)}개 보관 후 재시도")
            return None
        if status in (200, 400, 401, 403, 421, 503):   # 다시 보내도 같은 결과인 오류는 버리고 원인을 알린다
            del self.pending[:len(batch)]
            del self.pending_events[:len(events)]
        if status == 200:
            self.last_result = res
            self.out(f"[{time.strftime('%H:%M:%S')}] {len(batch)}개 전송 · {describe(res)}")
            return res
        self.out(f"[전송 거부] HTTP {status}: {res.get('error')}")
        return None

    def poll_downlink(self):
        """DOWNLINK_S초마다 서버에 확정 단계를 묻고, 바뀌었으면 L0~L3(정상은 X), 안부 확인이 새로 시작되면 Q,D를 보낸다."""
        now = self.clock()
        if self.writer is None or (self.last_poll is not None and now - self.last_poll < DOWNLINK_S):
            return None
        self.last_poll = now
        try:
            status, d = self.fetcher(self.server, self.token)
            if status != 200:
                raise OSError(f"HTTP {status}: {d.get('error')}")
        except (urllib.error.URLError, OSError) as e:
            if self.downlink_ok:
                self.out(f"[내려보내기 실패] 확정 단계를 받지 못했습니다: {getattr(e, 'reason', e)} · 계속 다시 시도합니다")
                self.downlink_ok = False
            return None
        self.downlink_ok = True
        sent = []
        level = d.get("level") if d.get("level") in (0, 1, 2, 3) else 0
        if level != self.sent_level or now - self.sent_level_at >= RESEND_LEVEL_S:
            msg = "X" if level == 0 else f"L{level}"
            if self.write(msg):
                if level != self.sent_level:
                    self.out(f"[내려보냄] 확정 단계 {d.get('level_name', level)} → {msg}")
                self.sent_level, self.sent_level_at = level, now
                sent.append(msg)
        check = bool(d.get("safety_check"))
        if check and not self.sent_check and self.write("Q,D"):
            self.out("[내려보냄] 안부 확인 요청 → Q,D")
            sent.append("Q,D")
        if check != self.sent_check and (not check or "Q,D" in sent):
            self.sent_check = check
        return sent


def serial_lines(port, baud, retry_s=RETRY_S, opener=None, out=print, sleep=time.sleep, on_open=None):
    """시리얼 줄을 계속 돌려준다. USB가 끊기면(노트북 절전·케이블 흔들림) 멈추지 않고 retry_s초마다 다시 연결한다.
    on_open(ser)은 연결될 때 열린 포트로, 끊길 때 None으로 불린다(수신기로 보내는 쪽이 지금 포트를 알게)."""
    on_open = on_open or (lambda ser: None)
    if opener is None:
        try:
            import serial   # pyserial
        except ImportError:
            raise SystemExit("실제 마이크로비트를 읽으려면 pyserial이 필요합니다: pip install pyserial  (기기 없이 시험: --simulate flood)")
        opener = lambda: serial.Serial(port, baud, timeout=0.2)
    lost = False
    while True:
        try:
            with opener() as ser:
                on_open(ser)
                if lost:
                    out(f"[재연결] {port}에 다시 연결했습니다")
                    lost = False
                while True:
                    raw = ser.readline()
                    yield raw.decode("utf-8", "replace") if raw else ""
        except OSError as e:   # pyserial의 SerialException도 OSError
            on_open(None)
            if not lost:
                out(f"[연결 끊김] {port}: {e} · {retry_s:g}초마다 다시 연결합니다")
                lost = True
            yield ""   # 끊긴 동안에도 호출한 쪽이 보관해 둔 값을 서버로 보낼 수 있게
            sleep(retry_s)


def list_ports():
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        raise SystemExit("포트 목록을 보려면 pyserial이 필요합니다: pip install pyserial")
    ports = list(lp.comports())
    for p in ports:
        desc = (p.description or "").lower()
        hint = (" ← 마이크로비트로 보임(--port)" if "mbed" in desc or p.vid == 0x0D28
                else " ← 블루투스 시리얼로 보임(--bt-port)" if "bluetooth" in desc or "블루투스" in desc else "")
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
    ap.add_argument("--bt-port", help="G 노드 외부 모듈의 블루투스 시리얼 포트(페어링하면 생김, 예: COM5, /dev/rfcomm0)")
    ap.add_argument("--bt-baud", type=int, default=9600, help="블루투스 모듈 통신 속도(HC-05/06 기본 9600)")
    ap.add_argument("--bt-node", default="G", help="블루투스 모듈 값에 붙일 노드 이름(기본 G)")
    ap.add_argument("--bt-fields", default="", help="숫자만 보내는 모듈의 값 이름(쉼표로, 예: temp,humidity)")
    ap.add_argument("--bt-simulate", action="store_true", help="블루투스 모듈 없이 가상 온도·습도·가스 값으로 시험")
    args = ap.parse_args(argv)

    if args.new_token:
        print(secrets.token_urlsafe(24))
        return 0
    if args.list_ports:
        list_ports()
        return 0
    if not (args.port or args.simulate or args.bt_port or args.bt_simulate):
        ap.error("--port, --simulate, --bt-port, --bt-simulate 중 하나가 필요합니다")
    if args.bt_port and args.bt_simulate:
        ap.error("--bt-port와 --bt-simulate는 함께 쓸 수 없습니다")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,20}", args.bt_node):
        ap.error("--bt-node는 영문·숫자 20자 이하")
    fields = [f.strip() for f in args.bt_fields.split(",") if f.strip()]
    token = os.getenv("SENSOR_GATEWAY_TOKEN", "").strip()
    if not token:
        raise SystemExit("환경변수 SENSOR_GATEWAY_TOKEN이 없습니다. 서버와 같은 값을 설정하세요(--new-token으로 생성).")

    link = {"ser": None}   # 지금 열려 있는 시리얼 포트(끊기면 None)

    def write(msg):
        ser = link["ser"]
        if ser is None:
            return False
        try:
            ser.write((msg + "\n").encode("ascii"))
            return True
        except OSError:   # 끊긴 순간이면 다음 재연결 뒤 다시 보낸다
            return False

    gw = Gateway(args.server, token, args.interval, args.rain_stage, writer=write if args.port else None)
    # 블루투스 모듈은 따로 읽어 줄을 쌓아 두고, 아래 반복에서 꺼내 G 노드 값으로 넣는다
    bt_q, module_sim = queue.Queue(maxsize=2000), ModuleSimulator() if args.bt_simulate else None
    if args.bt_port:
        def read_bt():
            for line in serial_lines(args.bt_port, args.bt_baud):
                if line.strip():
                    try:
                        bt_q.put_nowait(line)
                    except queue.Full:
                        pass
        threading.Thread(target=read_bt, daemon=True).start()

    def drain_bt():
        while True:
            try:
                gw.feed_module(bt_q.get_nowait(), args.bt_node, fields)
            except queue.Empty:
                return

    src = [args.port or (f"가상 센서({args.simulate})" if args.simulate else ""),
           f"블루투스 모듈 {args.bt_port}" if args.bt_port else "가상 블루투스 모듈" if args.bt_simulate else ""]
    print(f"게이트웨이 시작: {' + '.join(x for x in src if x)} → {args.server}  (Ctrl+C로 종료)")
    try:
        if args.port:
            last_sim = 0.0
            for line in serial_lines(args.port, args.baud, on_open=lambda ser: link.update(ser=ser)):
                gw.feed(line)
                drain_bt()
                if module_sim and time.time() - last_sim >= args.interval:
                    gw.feed_module(module_sim.line(), args.bt_node)
                    last_sim = time.time()
                if gw.due():
                    gw.flush()
                gw.poll_downlink()
        else:   # 가상 센서, 또는 블루투스 모듈만
            sim, sent = (Simulator(args.simulate) if args.simulate else None), 0
            while not args.ticks or sent < args.ticks:
                for line in sim.tick() if sim else []:
                    gw.feed(line)
                if module_sim:
                    gw.feed_module(module_sim.line(), args.bt_node)
                end = time.time() + args.interval
                while time.time() < end:   # 블루투스 줄은 기다리는 동안에도 받는다
                    try:
                        gw.feed_module(bt_q.get(timeout=max(0.01, end - time.time())), args.bt_node, fields)
                    except queue.Empty:
                        pass
                drain_bt()
                gw.flush()
                sent += 1
    except KeyboardInterrupt:
        pass
    if gw.ignored:
        print(f"형식이 다른 줄 {gw.ignored}개는 무시했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
