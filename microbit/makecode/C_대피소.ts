/**
 * 노드 C (대피소) - 서버 연결본, 제안서 8.2 버튼 배정
 * - A: 입소 +1 → E,C,+1,번호   B: 퇴소 -1 → E,C,-1,번호   로고 터치: 물품 도착 → E,C,GOT,번호
 * - 화면: 현재 인원(이 장치에서 센 값)
 * - 5초마다 온도: S,C,temp,값  (온습도 센서 대신 우선 마이크로비트 내장 온도. 온습도는 확장 블록 확인 뒤 추가)
 * - 이벤트는 G 의 수신 확인(K,C,번호)이 올 때까지 최대 3번 보낸다
 * - G 방송 수신: L0~L3(위험 단계) → 아이콘과 소리, X(해제) → 정상
 *
 * 서버 연결(2026-11-01): 게이트웨이가 K 를 돌려보내고 관리자 화면 센서 탭 "현장 버튼 신호"에 표시한다.
 */
const GROUP = 23
const NODE = "C"
const SENSOR_EVERY_MS = 5000

radio.setGroup(GROUP)
radio.setTransmitPower(7)
basic.showString(NODE)

let count = 0
let level = 0
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

function showLevel() {
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
    basic.showNumber(count)
}

function setLevel(newLevel: number) {
    if (newLevel != level) {
        level = newLevel
        showLevel()
    }
}

input.onButtonPressed(Button.A, function () {
    count += 1
    sendEvent("+1")
    basic.showNumber(count)
})

input.onButtonPressed(Button.B, function () {
    if (count > 0) {
        count -= 1
        sendEvent("-1")
    }
    basic.showNumber(count)
})

input.onLogoEvent(TouchButtonEvent.Pressed, function () {
    sendEvent("GOT")
    basic.showIcon(IconNames.Yes)
    basic.pause(500)
    basic.showNumber(count)
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
    if (triesLeft > 0 && now - lastTry >= 400) {
        radio.sendString(pendingMsg)
        triesLeft -= 1
        lastTry = now
    }
    if (now - lastSensor >= SENSOR_EVERY_MS) {
        radio.sendString("S," + NODE + ",temp," + input.temperature())
        lastSensor = now
    }
    basic.pause(50)
})
