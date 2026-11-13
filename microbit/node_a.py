# 노드 A(침수): 수심 센서 1개를 1초마다 읽어 G로 무선 전송한다(S,A,depth,값).
# G는 받은 값을 외부 블루투스 모듈로 노트북 게이트웨이에 넘긴다.
# 배선: 수심 센서 신호(S) → P0 (아날로그 0~1023, 깊이 잠길수록 큼), 센서 전원 3V·GND.
# 서버 기준(config/monitor.json): 주의 150 · 위험 400(설치 점검 전 임시값). 화면 막대로 현재 값을 확인한다.
from microbit import display, pin0, sleep, button_a
import radio

GROUP = 23          # G(receiver.py / G_게이트웨이.ts)와 같은 번호. 같은 교실의 다른 팀과 겹치지 않게 바꾼다
radio.config(group=GROUP, length=64, power=7)
radio.on()

def bar(v):
    # 0~1023 값을 막대(0~5칸)로 표시
    return min(5, v * 6 // 1024)

while True:
    depth = pin0.read_analog()
    radio.send("S,A,depth,%d" % depth)
    display.clear()
    for y in range(bar(depth)):
        for x in (1, 2, 3):
            display.set_pixel(x, 4 - y, 9)
    if button_a.is_pressed():   # 설치 점검: A 버튼을 누르는 동안 숫자로 표시
        display.scroll(str(depth), delay=60)
    sleep(1000)
