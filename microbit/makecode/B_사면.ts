/**
 * 노드 B (사면) - 서버 연결본
 * - 실제 배선(쉴드): 진동 센서 AO → S0(P0, 아날로그), DO → S1(P1)
 *                    기울기 센서(Keyestudio 매직 라이트 컵) S → S9(P9, 디지털), L → S8(P8, 모듈 LED)
 *                    이 모듈은 세워 두면 1, 많이 기울이거나 돌리면 0 → TILT_ACTIVE = 0
 *                    기울면 모듈 LED 를 켠다
 *                    온습도 센서 S → S3(P3) 는 아직 읽지 않는다(P3 은 LED 화면과 핀을 같이 씀)
 * - 1초 동안 50ms 간격으로 20번 읽어서 보낸다:
 *     S,B,tilt,0/1 (1초 안에 한 번이라도 기울면 1), S,B,vibration,최댓값
 * - 위험 판정은 서버가 한다(기울기와 진동이 10초 안에 함께 와야 사면 위험 후보).
 * - 설치 점검: A 버튼 = 마지막 진동 최댓값, B 버튼 = 기울기 핀 원값(0/1) 표시
 */
const GROUP = 23
// 화면 표시용 기준. 서버 config/monitor.json 값과 맞춘다
const VIBRATION_THRESHOLD = 300
const TILT_PIN = DigitalPin.P9
const TILT_LED_PIN = DigitalPin.P8
// 기울었을 때 기울기 핀이 내는 값
const TILT_ACTIVE = 0

radio.setGroup(GROUP)
radio.setTransmitPower(7)
// 모듈에 풀업 저항이 있어 마이크로비트 쪽 풀업·풀다운은 쓰지 않는다
pins.setPull(TILT_PIN, PinPullMode.PullNone)
basic.showString("B")

let lastPeak = 0
let shown = -1

input.onButtonPressed(Button.A, function () {
    basic.showNumber(lastPeak)
    shown = -1
})

input.onButtonPressed(Button.B, function () {
    basic.showNumber(pins.digitalReadPin(TILT_PIN))
    shown = -1
})

basic.forever(function () {
    let peak = 0
    let tilt = 0
    for (let i = 0; i < 20; i++) {
        peak = Math.max(peak, pins.analogReadPin(AnalogPin.P0))
        if (pins.digitalReadPin(TILT_PIN) == TILT_ACTIVE) {
            tilt = 1
        }
        basic.pause(50)
    }
    lastPeak = peak
    pins.digitalWritePin(TILT_LED_PIN, tilt)
    radio.sendString("S,B,tilt," + tilt)
    basic.pause(20)
    radio.sendString("S,B,vibration," + peak)

    let vib = peak > VIBRATION_THRESHOLD ? 1 : 0
    let state = tilt + vib
    if (state != shown) {
        if (state == 2) {
            basic.showIcon(IconNames.Skull, 0)
        } else if (state == 1) {
            basic.showIcon(IconNames.Triangle, 0)
        } else {
            basic.showIcon(IconNames.SmallSquare, 0)
        }
        shown = state
    }
})
