/**
 * 노드 A (침수) - 서버 연결본 · 수심 센서 1개 + 배수 모터
 * 배선 (2026-11-20 실제 장치)
 * - P4: 수심 센서 (아날로그 0~1023, 물에 깊이 잠길수록 큼)
 * - P5 초록 LED(정상) · P8 노랑 LED(주의) · P9 빨강 LED(위험)
 * - P2: 릴레이 → 배수 모터 (1 = 켬)
 *
 * 하는 일
 * - 1초마다 원값을 G 로 무선 전송: S,A,depth,값
 * - 위험 기준을 넘으면 배수 모터를 켜서 물을 빼고 E,A,FLOOD,번호 를 보낸다
 *   → 서버가 담당자 텔레그램 단체방에 바로 알린다(주민 알림은 담당자가 위기 단계를 확정할 때)
 * - 위험에서 내려오면 모터를 끄고 E,A,DRAIN,번호 를 보낸다
 * - 이벤트는 G 의 수신 확인(K,A,번호)이 올 때까지 최대 3번 보낸다(E_봉사자.ts 와 같은 방식)
 * - 위기 단계 판정은 서버(감시 에이전트)가 한다. LED·모터는 현장에서 바로 움직이는 것뿐이다.
 * - 설치 점검: B 버튼 = 지금 수심 값 표시. A 버튼은 P5(초록 LED)와 같은 핀이라 쓰지 않는다.
 * - P4 는 화면(LED) 과 함께 쓰는 핀이다. 값이 이상하게 흔들리면 맨 위에 led.enable(false) 를 넣고
 *   기준값을 다시 잰다.
 */
const GROUP = 23
const NODE = "A"
// 서버 config/monitor.json 의 A.depth caution·threshold 와 같은 값
const CAUTION = 330
const DANGER = 360

radio.setGroup(GROUP)
radio.setTransmitPower(7)
pins.digitalWritePin(DigitalPin.P2, 0)
basic.showString(NODE)

let level = -1        // 0 정상, 1 주의, 2 위험
let shown = -1        // 화면에 그린 단계(B 로 숫자를 본 뒤 다시 그리려고 따로 둠)
let seq = 0
let pendingSeq = -1
let pendingMsg = ""
let triesLeft = 0
let lastTry = 0

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

input.onButtonPressed(Button.B, function () {
    basic.showNumber(pins.analogReadPin(AnalogPin.P4))
    shown = -1   // 다음 반복에서 아이콘을 다시 그림
})

radio.onReceivedString(function (receivedString) {
    if (receivedString == "K," + NODE + "," + pendingSeq) {
        triesLeft = 0
    }
})

basic.forever(function () {
    let depth = pins.analogReadPin(AnalogPin.P4)
    radio.sendString("S," + NODE + ",depth," + depth)

    let newLevel = 0
    if (depth > DANGER) {
        newLevel = 2
    } else if (depth >= CAUTION) {
        newLevel = 1
    }

    if (newLevel == 2) {
        setLights(0, 0, 1)
        pins.digitalWritePin(DigitalPin.P2, 1)        // 배수 모터 켬
    } else {
        if (newLevel == 1) {
            setLights(0, 1, 0)
        } else {
            setLights(1, 0, 0)
        }
        pins.digitalWritePin(DigitalPin.P2, 0)        // 배수 모터 끔
    }

    if (newLevel != level) {
        if (newLevel == 2) {
            sendEvent("FLOOD")
        } else if (level == 2) {
            sendEvent("DRAIN")
        }
        level = newLevel
    }
    if (shown != level) {
        if (level == 2) {
            basic.showIcon(IconNames.Skull, 0)
        } else if (level == 1) {
            basic.showIcon(IconNames.Sad, 0)
        } else {
            basic.showIcon(IconNames.Happy, 0)
        }
        shown = level
    }

    // 수신 확인이 안 오면 0.4초 간격으로 다시 보냄
    let now = control.millis()
    for (let i = 0; i < 2; i++) {
        if (triesLeft > 0 && now - lastTry >= 400) {
            radio.sendString(pendingMsg)
            triesLeft -= 1
            lastTry = now
        }
        basic.pause(450)
        now = control.millis()
    }
})
