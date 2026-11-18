/**
 * 노드 A (침수) - 마이크로비트 한 대 + 블루투스 모듈 버전 (2026-11-18 수조 시험 구성)
 *   [수위 센서 P4] → 마이크로비트 → 블루투스 모듈(HC-05/HC-06, Keyes BT_BOARD V2.0) → 노트북
 *   노트북: python gateway/microbit_gateway.py --port COM번호 --bt  (블루투스 '나가는' COM 포트)
 * - 무선 수신기(G) 없이 이 한 대가 수위 값을 바로 블루투스로 보낸다: 1초마다 S,A,depth,값
 * - 현장 표시: LED 3개(낮음 P5 · 중간 P8 · 높음 P9), 펌프 릴레이 P2
 *   P4는 LED 화면 핀이라 화면을 끈다(led.enable(false)). 그래서 아이콘 대신 LED로 보여 준다.
 * - 위험 판정은 서버(감시 에이전트)가 한다: 주의 355를 3번 연속 넘으면 '주의(지켜봄)',
 *   위험 400을 3번 연속 넘으면 '침수 확정 후보' → 담당자 승인. 서버 config/monitor.json 과 숫자를 맞춘다.
 *
 * 배선 (Keyestudio 확장보드, 흰 선을 KEY 쪽부터 1~6번)
 *   1 KEY   : 안 꽂음
 *   2 VCC   : 빨간 핀(V1, 주황 점퍼 5V)   모듈 전원 3.6~6V라 3V로는 부족하다
 *   3 GND   : 검정 핀(G)
 *   4 TXD   : P0 노란 핀  (마이크로비트가 받는 핀)
 *   5 RXD   : P1 노란 핀  (마이크로비트가 보내는 핀)
 *   6 STATE : 안 꽂음
 *   수위 센서: S → P4, + → 3V, - → GND   /   LED: P5·P8·P9   /   릴레이 IN: P2
 *   나중에 방수 온도센서(DS18B20)는 P12나 P13에 단다(P1은 블루투스가 쓴다). 데이터선과 3V 사이에 4.7kΩ
 *
 * 블루투스 확인: 노트북 터미널(줄 끝 Newline 또는 CR+LF)에서 PING 을 보내면 OK:PING 이 돌아온다.
 * 시리얼을 블루투스 핀으로 돌렸으므로 USB 시리얼 모니터에는 값이 나오지 않는다(블루투스 COM 포트로 본다).
 */
const LOW_MAX = 355     // 이하: 낮음(P5)        서버 주의 기준과 같게
const MID_MAX = 400     // 이하: 중간(P8), 초과: 높음(P9)   서버 위험 기준과 같게
const PUMP_ON = 410     // 넘으면 펌프 켜기
const PUMP_OFF = 380    // 아래로 내려가면 펌프 끄기(켜고 끄는 기준을 달리해 1초마다 켜졌다 꺼지는 것을 막는다)
const RELAY_ON = 1      // 릴레이가 0일 때 켜지는 방식이면 0으로 바꾼다

led.enable(false)
serial.redirect(SerialPin.P1, SerialPin.P0, BaudRate.BaudRate9600)   // TX = P1 → 모듈 RXD, RX = P0 ← 모듈 TXD

let pump = false
function setPump(on: boolean) {
    pump = on
    pins.digitalWritePin(DigitalPin.P2, on ? RELAY_ON : 1 - RELAY_ON)
}
function showLevel(v: number) {
    pins.digitalWritePin(DigitalPin.P5, v <= LOW_MAX ? 1 : 0)
    pins.digitalWritePin(DigitalPin.P8, v > LOW_MAX && v <= MID_MAX ? 1 : 0)
    pins.digitalWritePin(DigitalPin.P9, v > MID_MAX ? 1 : 0)
}

setPump(false)
// 시작 표시: LED 3개를 0.5초 켰다 끈다(화면을 꺼서 아이콘을 못 쓴다)
pins.digitalWritePin(DigitalPin.P5, 1)
pins.digitalWritePin(DigitalPin.P8, 1)
pins.digitalWritePin(DigitalPin.P9, 1)
basic.pause(500)
showLevel(0)

basic.forever(function () {
    let sum = 0
    for (let i = 0; i < 5; i++) {              // 5번 재서 평균(값 흔들림 줄이기)
        sum += pins.analogReadPin(AnalogPin.P4)
        basic.pause(20)
    }
    let v = Math.idiv(sum, 5)
    showLevel(v)
    if (!pump && v > PUMP_ON) {
        setPump(true)
    } else if (pump && v < PUMP_OFF) {
        setPump(false)
    }
    serial.writeLine("S,A,depth," + v)          // 노트북 게이트웨이가 읽는 형식
    basic.pause(900)
})

// 노트북에서 온 줄: PING 이면 답한다(블루투스 양방향 확인). 서버가 내려보내는 L·Q·K·X 줄은 이 한 대 구성에서는 쓰지 않는다
serial.onDataReceived(serial.delimiters(Delimiters.NewLine), function () {
    let msg = serial.readUntil(serial.delimiters(Delimiters.NewLine)).trim()
    if (msg == "PING") {
        serial.writeLine("OK:PING")
    }
})
