/**
 * G (게이트웨이) - 서버 연결본. 이 마이크로비트만 노트북 USB에 꽂는다.
 * - 무선 → USB: 노드가 보낸 S(센서값)·E(이벤트) 메시지를 한 줄씩 시리얼(115200)로 출력
 *     PC 의 gateway/microbit_gateway.py 가 S·E 줄을 읽어 서버로 보내고, E 에는 K 를 돌려보낸다
 * - USB → 무선: PC 가 보낸 L(단계)·Q(안전 확인 요청)·K(수신 확인)·X(해제) 줄을 노드에 방송
 *     무선은 유실될 수 있어 3번 반복해서 보낸다(노드는 같은 메시지를 한 번만 처리)
 * - 경보·단계는 담당자가 확정한 것만 PC 에서 내려온다. G 는 스스로 경보를 만들지 않는다.
 * - 화면: 하트 + 오른쪽 위 점(받을 때마다 깜빡) + 왼쪽 위 점(방송할 때마다 깜빡)
 */
const GROUP = 23

radio.setGroup(GROUP)
radio.setTransmitPower(7)
serial.redirectToUSB()
serial.setBaudRate(BaudRate.BaudRate115200)
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
