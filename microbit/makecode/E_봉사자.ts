/**
 * 노드 E (봉사자) - 서버 연결본, 제안서 8.2 버튼 배정
 * - A: 체크인 → E,E,IN,번호   B: SOS → E,E,SOS,번호   로고 터치: 물품 전달 완료 → E,E,DONE,번호
 * - P0 온도 센서(또는 슬라이더): 기준을 넘으면 폭염 모의 경고(소리·화면) + E,E,HEAT,번호
 *   내려가면 E,E,COOL,번호. 5초마다 원값: S,E,temp,값
 * - 온도가 오를수록 값이 내려가는 센서(서미스터 등)는 INVERTED = true
 * - 설치 점검: A+B = 현재 센서 원값 표시
 * - 이벤트는 G 의 수신 확인(K,E,번호)이 올 때까지 최대 3번 보낸다
 * - G 방송 수신: L0~L3(위험 단계) → 아이콘과 소리, X(해제) → 정상
 *
 * 서버 연결(2026-11-01): 게이트웨이가 K 를 돌려보내고 관리자 화면 센서 탭 "현장 버튼 신호"에 표시한다.
 */
const GROUP = 23
const NODE = "E"
const HEAT_THRESHOLD = 600
const HYSTERESIS = 30
const INVERTED = false
const SENSOR_EVERY_MS = 5000

radio.setGroup(GROUP)
radio.setTransmitPower(7)
basic.showString(NODE)

let level = 0
let hot = false
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

function readHeat() {
    let v = pins.analogReadPin(AnalogPin.P0)
    return INVERTED ? 1023 - v : v
}

function setLevel(newLevel: number) {
    if (newLevel == level) {
        return
    }
    level = newLevel
    if (level >= 3) {
        basic.showIcon(IconNames.Skull)
    } else if (level == 2) {
        basic.showIcon(IconNames.Surprised)
    } else if (level == 1) {
        basic.showIcon(IconNames.Sad)
    } else {
        basic.showIcon(IconNames.Happy)
    }
    for (let i = 0; i < level; i++) {
        music.playTone(880, music.beat(BeatFraction.Quarter))
        music.rest(music.beat(BeatFraction.Quarter))
    }
}

input.onButtonPressed(Button.A, function () {
    sendEvent("IN")
    basic.showIcon(IconNames.Yes)
})

input.onButtonPressed(Button.B, function () {
    sendEvent("SOS")
    basic.showIcon(IconNames.No)
    music.playTone(988, music.beat(BeatFraction.Quarter))
})

input.onLogoEvent(TouchButtonEvent.Pressed, function () {
    sendEvent("DONE")
    basic.showIcon(IconNames.Diamond)
})

input.onButtonPressed(Button.AB, function () {
    basic.showNumber(pins.analogReadPin(AnalogPin.P0))
})

radio.onReceivedString(function (receivedString) {
    if (receivedString == "K," + NODE + "," + pendingSeq) {
        triesLeft = 0
    } else if (receivedString.length == 2 && receivedString.charAt(0) == "L") {
        setLevel(parseInt(receivedString.charAt(1)))
    } else if (receivedString == "X") {
        setLevel(0)
    }
})

basic.forever(function () {
    let now = control.millis()
    let heat = readHeat()
    let nowHot = hot ? heat >= HEAT_THRESHOLD - HYSTERESIS : heat >= HEAT_THRESHOLD
    if (nowHot != hot) {
        hot = nowHot
        if (hot) {
            sendEvent("HEAT")
            basic.showIcon(IconNames.Angry)
            for (let i = 0; i < 2; i++) {
                music.playTone(1047, music.beat(BeatFraction.Quarter))
                music.playTone(784, music.beat(BeatFraction.Quarter))
            }
        } else {
            sendEvent("COOL")
            basic.showIcon(IconNames.Happy)
        }
    }
    if (triesLeft > 0 && now - lastTry >= 400) {
        radio.sendString(pendingMsg)
        triesLeft -= 1
        lastTry = now
    }
    if (now - lastSensor >= SENSOR_EVERY_MS) {
        radio.sendString("S," + NODE + ",temp," + heat)
        lastSensor = now
    }
    basic.pause(200)
})
