/**
 * 노드 A (침수) - 서버 연결본
 * - P0: 낮은 위치 수위 센서, P1: 높은 위치 수위 센서 (아날로그 0~1023)
 * - 1초마다 원값을 보낸다: S,A,water_low,값 / S,A,water_high,값
 * - 위험 판정은 서버(감시 에이전트)가 한다. 화면 아이콘은 현장 확인용일 뿐이다.
 * - 설치 점검: A 버튼 = 낮은 센서 값, B 버튼 = 높은 센서 값 표시
 */
const GROUP = 23
// 화면 표시용 기준. 서버 config/monitor.json 값과 맞춘다
const LOW_THRESHOLD = 200
const HIGH_THRESHOLD = 200

radio.setGroup(GROUP)
radio.setTransmitPower(7)
basic.showString("A")

let shown = -1

input.onButtonPressed(Button.A, function () {
    basic.showNumber(pins.analogReadPin(AnalogPin.P0))
    shown = -1
})

input.onButtonPressed(Button.B, function () {
    basic.showNumber(pins.analogReadPin(AnalogPin.P1))
    shown = -1
})

basic.forever(function () {
    let low = pins.analogReadPin(AnalogPin.P0)
    let high = pins.analogReadPin(AnalogPin.P1)
    radio.sendString("S,A,water_low," + low)
    basic.pause(20)
    radio.sendString("S,A,water_high," + high)

    // 0 정상, 1 한 센서만 반응(서버에서는 보류), 2 두 센서 모두 반응
    let level = 0
    if (low > LOW_THRESHOLD && high > HIGH_THRESHOLD) {
        level = 2
    } else if (low > LOW_THRESHOLD || high > HIGH_THRESHOLD) {
        level = 1
    }
    if (level != shown) {
        if (level == 2) {
            basic.showIcon(IconNames.Skull, 0)
        } else if (level == 1) {
            basic.showIcon(IconNames.Sad, 0)
        } else {
            basic.showIcon(IconNames.Happy, 0)
        }
        shown = level
    }
    basic.pause(980)
})
