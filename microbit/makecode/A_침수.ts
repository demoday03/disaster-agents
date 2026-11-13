/**
 * 노드 A (침수) - 서버 연결본 · 수심 센서 1개
 * - P0: 수심 센서 (아날로그 0~1023, 물에 깊이 잠길수록 큼)
 * - 1초마다 원값을 G 로 무선 전송: S,A,depth,값
 *   G 는 받은 값을 외부 블루투스 모듈로 노트북에 넘긴다(노트북: gateway/microbit_gateway.py --port COM5 --bt)
 * - 위험 판정은 서버(감시 에이전트)가 한다: 주의 기준을 3번 연속 넘으면 '주의(지켜봄)',
 *   위험 기준을 3번 연속 넘으면 '침수 확정 후보' → 담당자 승인. 화면 아이콘은 현장 확인용일 뿐이다.
 * - 설치 점검: A 버튼 = 지금 수심 값 표시. 마른 상태·발목·무릎 높이 값을 재서 아래 기준과 서버 config/monitor.json 을 함께 맞춘다
 */
const GROUP = 23
// 화면 표시용 기준. 서버 config/monitor.json 의 A.depth caution·threshold 와 맞춘다
const CAUTION = 150
const DANGER = 400

radio.setGroup(GROUP)
radio.setTransmitPower(7)
basic.showString("A")

let shown = -1

input.onButtonPressed(Button.A, function () {
    basic.showNumber(pins.analogReadPin(AnalogPin.P0))
    shown = -1
})

basic.forever(function () {
    let depth = pins.analogReadPin(AnalogPin.P0)
    radio.sendString("S,A,depth," + depth)

    // 0 정상, 1 주의 기준 넘음, 2 위험 기준 넘음
    let level = 0
    if (depth > DANGER) {
        level = 2
    } else if (depth > CAUTION) {
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
    basic.pause(1000)
})
