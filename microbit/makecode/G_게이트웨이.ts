/**
 * G (게이트웨이) - 서버 연결본. 노트북과는 외부 블루투스 모듈(기본) 또는 USB 케이블로 잇는다.
 *   [A·B·C·D·E] ─무선─▶ [G] ─블루투스 모듈(HC-05/HC-06)─▶ 노트북 gateway/microbit_gateway.py --port COM5 --bt ─▶ 서버
 * - 블루투스 모듈 배선: 모듈 RXD ← G P1(TX), 모듈 TXD → G P2(RX), GND ↔ GND, VCC ← 쉴드 5V(없으면 3V). 속도 9600
 *   마이크로비트 자체 블루투스는 무선과 함께 못 쓰지만, 핀에 단 블루투스 모듈은 시리얼이라 무선과 함께 쓸 수 있다
 * - USB로 쓰려면 아래 USE_BLUETOOTH 를 false 로 바꾼다(속도 115200, 게이트웨이는 --bt 없이)
 * - 무선 → 노트북: 노드가 보낸 S(센서값)·E(이벤트) 메시지를 한 줄씩 시리얼로 출력
 *     PC 의 gateway/microbit_gateway.py 가 S·E 줄을 읽어 서버로 보내고, E 에는 K 를 돌려보낸다
 * - 노트북 → 무선: PC 가 보낸 L(단계)·Q(안전 확인 요청)·K(수신 확인)·X(해제) 줄을 노드에 방송
 *     무선은 유실될 수 있어 3번 반복해서 보낸다(노드는 같은 메시지를 한 번만 처리)
 * - 경보·단계는 담당자가 확정한 것만 PC 에서 내려온다. G 는 스스로 경보를 만들지 않는다.
 * - 화면: 시작할 때 B(블루투스)·U(USB) → 하트 + 오른쪽 위 점(받을 때마다 깜빡) + 왼쪽 위 점(방송할 때마다 깜빡)
 */
const GROUP = 23
const USE_BLUETOOTH = true   // true: 외부 블루투스 모듈(P1·P2, 9600) / false: USB 케이블(115200)

radio.setGroup(GROUP)
radio.setTransmitPower(7)
if (USE_BLUETOOTH) {
    serial.redirect(SerialPin.P1, SerialPin.P2, BaudRate.BaudRate9600)   // TX = P1 → 모듈 RXD, RX = P2 ← 모듈 TXD
    basic.showString("B")
} else {
    serial.redirectToUSB()
    serial.setBaudRate(BaudRate.BaudRate115200)
    basic.showString("U")
}
basic.showIcon(IconNames.Heart)

radio.onReceivedString(function (receivedString) {
    let c = receivedString.charAt(0)
    if ((c == "S" || c == "E") && receivedString.charAt(1) == ",") {
        serial.writeLine(receivedString)
        led.toggle(4, 0)
    }
})

serial.onDataReceived(serial.delimiters(Delimiters.NewLine), function () {
    let line = serial.readUntil(serial.delimiters(Delimiters.NewLine)).trim()
    let c = line.charAt(0)
    if (line.length == 0 || line.length > 19) {
        return
    }
    if (c == "L" || c == "Q" || c == "K" || c == "X") {
        for (let i = 0; i < 3; i++) {
            radio.sendString(line)
            basic.pause(100)
        }
        led.toggle(0, 0)
    }
})
