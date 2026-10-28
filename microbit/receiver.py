# 수신기: 노드 A·B가 보낸 무선 메시지를 받아 USB 시리얼로 한 줄씩 출력한다. PC에서 게이트웨이가 이 출력을 읽는다.
# 이 마이크로비트는 PC에 USB로 꽂아 둔다(전원도 USB).
# 무선 메시지에는 인증이 없으므로 'S,노드,센서,값' 형식만 넘기고, 판정·단계 확정은 서버와 담당자가 한다.
from microbit import display, Image, sleep
import radio

GROUP = 23
radio.config(group=GROUP, length=64)
radio.on()
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
