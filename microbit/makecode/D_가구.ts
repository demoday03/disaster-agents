/**
 * 노드 D (가구) - 서버 연결본, 제안서 8.2 버튼 배정
 * - A: 안전 확인 → E,D,SAFE,번호   B: SOS → E,D,SOS,번호   A+B: 급한 물품 요청 → E,D,NEED,번호
 * - P1 조도 센서: 손으로 가리면 정전 모의 → E,D,DARK,번호 / 다시 밝아지면 E,D,LIGHT,번호
 *   5초마다 원값: S,D,light,값
 * - 이벤트는 G 의 수신 확인(K,D,번호)이 올 때까지 최대 3번 보낸다
 * - G 방송 수신: L0~L3(위험 단계) → 단계별 소리와 아이콘, Q,D(안전 확인 요청) → "?" 표시 후 A 를 누르면 응답,
 *   X(해제) → 정상
 *
 * 서버 연결(2026-11-01): E 신호는 센서 탭 "현장 버튼 신호"에, 확정 단계(L/X)·안부 확인(Q,D)은 게이트웨이가 내려보낸다.
 */
const GROUP = 23
const NODE = "D"
const DARK_THRESHOLD = 300
const HYSTERESIS = 30
const SENSOR_EVERY_MS = 5000
// 조도 값이 이만큼 연속(100ms 간격)으로 바뀐 상태여야 정전/복구로 본다. 센서가 빠져 값이 튀어도 신호가 쏟아지지 않게
const STEADY_COUNT = 5

radio.setGroup(GROUP)
radio.setTransmitPower(7)
basic.showString(NODE)

let level = 0
let dark = false
let changedFor = 0
let seq = 0
let pendingSeq = -1
let pendingMsg = ""
let triesLeft = 0
let lastTry = 0
let lastSensor = 0

function sendEvent(code: string) {
    seq = (seq + 1) % 100
    pendingSeq = seq
    pendingMsg = "E," + NODE + "," + code + "," + seq
    radio.sendString(pendingMsg)
    triesLeft = 2
    lastTry = control.millis()
}

// 단계별 소리: 1 주의(짧게 1번), 2 경계(2번), 3 위험(사이렌 3번)
function alarm() {
    if (level >= 3) {
        basic.showIcon(IconNames.Skull)
        for (let i = 0; i < 3; i++) {
            music.playTone(988, music.beat(BeatFraction.Half))
            music.playTone(784, music.beat(BeatFraction.Half))
        }
    } else if (level == 2) {
        basic.showIcon(IconNames.Surprised)
        for (let i = 0; i < 2; i++) {
            music.playTone(880, music.beat(BeatFraction.Quarter))
            music.rest(music.beat(BeatFraction.Quarter))
        }
    } else if (level == 1) {
        basic.showIcon(IconNames.Sad)
        music.playTone(660, music.beat(BeatFraction.Quarter))
    } else {
        basic.showIcon(IconNames.Happy)
    }
}

function setLevel(newLevel: number) {
    if (newLevel != level) {
        level = newLevel
        alarm()
    }
}

input.onButtonPressed(Button.A, function () {
    sendEvent("SAFE")
    basic.showIcon(IconNames.Yes)
})

input.onButtonPressed(Button.B, function () {
    sendEvent("SOS")
    basic.showIcon(IconNames.No)
    music.playTone(988, music.beat(BeatFraction.Quarter))
})

input.onButtonPressed(Button.AB, function () {
    sendEvent("NEED")
    basic.showString("N")
})

radio.onReceivedString(function (receivedString) {
    if (receivedString == "K," + NODE + "," + pendingSeq) {
        triesLeft = 0
    } else if (receivedString.length == 2 && receivedString.charAt(0) == "L") {
        setLevel(parseInt(receivedString.charAt(1)))
    } else if (receivedString == "X") {
        setLevel(0)
    } else if (receivedString == "Q," + NODE) {
        basic.showString("?")
        music.playTone(784, music.beat(BeatFraction.Quarter))
        music.playTone(988, music.beat(BeatFraction.Quarter))
    }
})

basic.forever(function () {
    let now = control.millis()
    let lightValue = pins.analogReadPin(AnalogPin.P1)
    let nowDark = dark ? lightValue < DARK_THRESHOLD + HYSTERESIS : lightValue < DARK_THRESHOLD
    changedFor = nowDark != dark ? changedFor + 1 : 0
    if (changedFor >= STEADY_COUNT) {
        dark = nowDark
        changedFor = 0
        sendEvent(dark ? "DARK" : "LIGHT")
    }
    if (dark) {
        led.plot(0, 0)
    } else {
        led.unplot(0, 0)
    }
    if (triesLeft > 0 && now - lastTry >= 400) {
        radio.sendString(pendingMsg)
        triesLeft -= 1
        lastTry = now
    }
    if (now - lastSensor >= SENSOR_EVERY_MS) {
        radio.sendString("S," + NODE + ",light," + lightValue)
        lastSensor = now
    }
    basic.pause(100)
})
