# 수신기(G): 노드 A·B가 보낸 무선 메시지를 받아 한 줄씩 시리얼로 출력한다. 노트북의 게이트웨이가 이 출력을 읽는다.
# 노트북과는 외부 블루투스 모듈(HC-05/HC-06, 기본) 또는 USB 케이블로 잇는다.
#   블루투스: 모듈 RXD ← P1(TX), 모듈 TXD → P2(RX), GND ↔ GND, VCC ← 쉴드 5V(없으면 3V), 9600
#             노트북에서 모듈과 페어링 → gateway/microbit_gateway.py --port COM5 --bt
#   USB:      USE_BLUETOOTH = False 로 바꾸고 USB 케이블(115200), 게이트웨이는 --bt 없이
# 무선 메시지에는 인증이 없으므로 'S,노드,센서,값' 형식만 넘기고, 판정·단계 확정은 서버와 담당자가 한다.
# (내려보내기·현장 버튼 신호까지 쓰려면 microbit/makecode/G_게이트웨이.ts 를 쓴다)
from microbit import display, Image, sleep, uart, pin1, pin2
import radio

GROUP = 23
USE_BLUETOOTH = True
if USE_BLUETOOTH:
    uart.init(baudrate=9600, tx=pin1, rx=pin2)   # 이제 print()가 블루투스 모듈로 나간다(USB 출력은 꺼짐)
radio.config(group=GROUP, length=64)
radio.on()
display.show("B" if USE_BLUETOOTH else "U")
sleep(500)
display.show(Image.HEART)

while True:
    msg = radio.receive()
    if msg and msg.startswith("S,") and msg.count(",") == 3:
        print(msg)
        display.set_pixel(4, 0, 9)   # 받을 때마다 오른쪽 위 점 깜빡임
        sleep(30)
        display.set_pixel(4, 0, 0)
    else:
        sleep(10)
