/**
 * 노드 A (침수 · 송신) - 수심 센서 1개 + 배수 모터
 * MakeCode: https://makecode.microbit.org → 새 프로젝트 → 위쪽 JavaScript 탭에 통째로 붙여 넣기 → 다운로드
 *
 * 배선 (2026-11-20 실제 장치)
 * - P4: 수심 센서 (아날로그 0~1023, 물에 깊이 잠길수록 큼)
 * - P5 초록 LED(정상) · P8 노랑 LED(주의) · P9 빨강 LED(위험)
 * - P2: 릴레이 → 배수 모터 (1 = 켬)
 *
 * 하는 일
 * - 1초마다 원값을 G 로 무선 전송: S,A,depth,값
 * - 위험 기준(DANGER)을 넘으면 배수 모터를 켜서 물을 빼고 E,A,FLOOD,번호 를 보낸다
 *   → 서버가 담당자 텔레그램 단체방에 바로 알린다(주민 알림은 담당자가 위기 단계를 확정할 때)
 * - 모터는 수위가 주의 기준(CAUTION) 아래로 내려갈 때까지 계속 돌린다(기준 근처에서 1초마다 켜졌다 꺼지지 않게).
 *   멈추면 E,A,DRAIN,번호 를 보낸다
 * - 릴레이가 반대로 동작하면(평소에 돌고 위험일 때 멈춤) PUMP_ON 을 0 으로 바꾼다
 * - 이벤트는 G 의 수신 확인(K,A,번호)이 올 때까지 최대 3번 보낸다(E_봉사자.ts 와 같은 방식)
 * - 위기 단계 판정은 서버(감시 에이전트)가 한다. LED·모터는 현장에서 바로 움직이는 것뿐이다.
 *
 * 화면(5×5 LED)은 꺼 둔다: P4·P9 는 화면과 같은 핀이라 화면이 켜져 있으면 수심 값이 흔들리고 빨강 LED 가 깜빡인다.
 * 상태는 초록·노랑·빨강 LED 로 본다. 지금 수심 값은 B 버튼(잠깐 화면을 켜서 숫자 표시) 또는 담당자 화면 센서 탭에서 본다.
 * A 버튼은 P5(초록 LED)와 같은 핀이라 쓰지 않는다.
 */
const GROUP = 23
const NODE = "A"
// 서버 config/monitor.json 의 A.depth caution·threshold 와 같은 값
const CAUTION = 310
const DANGER = 320
const PUMP_ON = 1     // 릴레이를 켜는 값. 반대로 동작하는 릴레이 모듈이면 0

radio.setGroup(GROUP)
radio.setTransmitPower(7)
pins.digitalWritePin(DigitalPin.P2, 1 - PUMP_ON)   // 모터 끔
basic.showString(NODE)      // 켜질 때 'A' 한 번 보여 주고
led.enable(false)           // 화면을 끈다(P4·P9 를 센서·LED 로 쓰기 위해)

let pumping = false   // 배수 모터가 도는 중
let seq = 0
let pendingSeq = -1
let pendingMsg = ""
let triesLeft = 0
let lastTry = 0
let showing = false   // B 로 숫자를 보는 중(그동안은 수심을 읽지 않음)

function sendEvent(code: string) {
    seq = (seq + 1) % 100
    pendingSeq = seq
    pendingMsg = "E," + NODE + "," + code + "," + seq
    radio.sendString(pendingMsg)
    triesLeft = 2
    lastTry = control.millis()
}

function setLights(g: number, y: number, r: number) {
    pins.digitalWritePin(DigitalPin.P5, g)
    pins.digitalWritePin(DigitalPin.P8, y)
    pins.digitalWritePin(DigitalPin.P9, r)
}

// 설치 점검: 지금 수심 값을 숫자로 잠깐 보여 준다
input.onButtonPressed(Button.B, function () {
    let v = pins.analogReadPin(AnalogPin.P4)
    showing = true
    led.enable(true)
    basic.showNumber(v)
    basic.clearScreen()
    led.enable(false)
    showing = false
})

radio.onReceivedString(function (receivedString) {
    if (receivedString == "K," + NODE + "," + pendingSeq) {
        triesLeft = 0
    }
})

basic.forever(function () {
    if (!showing) {
        let depth = pins.analogReadPin(AnalogPin.P4)
        radio.sendString("S," + NODE + ",depth," + depth)

        // 불빛: 지금 수위 그대로
        if (depth > DANGER) {
            setLights(0, 0, 1)        // 빨강
        } else if (depth >= CAUTION) {
            setLights(0, 1, 0)        // 노랑
        } else {
            setLights(1, 0, 0)        // 초록
        }

        // 배수 모터: 위험 기준을 넘으면 켜고, 주의 기준 아래로 내려가면 끈다
        if (!pumping && depth > DANGER) {
            pumping = true
            pins.digitalWritePin(DigitalPin.P2, PUMP_ON)
            sendEvent("FLOOD")
        } else if (pumping && depth < CAUTION) {
            pumping = false
            pins.digitalWritePin(DigitalPin.P2, 1 - PUMP_ON)
            sendEvent("DRAIN")
        }
    }

    // 수신 확인(K)이 안 오면 0.45초 간격으로 다시 보냄
    for (let i = 0; i < 2; i++) {
        basic.pause(450)
        if (triesLeft > 0 && control.millis() - lastTry >= 400) {
            radio.sendString(pendingMsg)
            triesLeft -= 1
            lastTry = control.millis()
        }
    }
})
