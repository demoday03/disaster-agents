# 노드 A(수위): 낮은 위치·높은 위치 수위 센서 두 개를 1초마다 읽어 무선으로 보낸다.
# 배선(가정): 낮은 위치 수위 센서 → P0, 높은 위치 수위 센서 → P1 (아날로그 0~1023), 센서 전원 3V·GND.
# 서버 임계값(config/monitor.json)은 설치 점검 전 임시값 400이다. 화면의 막대로 현재 값을 확인할 수 있다.
from microbit import display, pin0, pin1, sleep, button_a
import radio

GROUP = 23          # 수신기(receiver.py)와 같은 번호. 같은 교실의 다른 팀과 겹치지 않게 바꾼다
radio.config(group=GROUP, length=64, power=7)
radio.on()

def bar(v):
    # 0~1023 값을 화면 왼쪽 막대(0~5칸)로 표시
    return min(5, v * 6 // 1024)

while True:
    low, high = pin0.read_analog(), pin1.read_analog()
    for name, v in (("water_low", low), ("water_high", high)):
        line = "S,A,%s,%d" % (name, v)
        radio.send(line)
        print(line)          # USB로 바로 연결했을 때는 게이트웨이가 이 출력을 읽어도 된다
    display.clear()
    for y in range(bar(low)):
        display.set_pixel(0, 4 - y, 9)
    for y in range(bar(high)):
        display.set_pixel(2, 4 - y, 9)
    if button_a.is_pressed():   # 설치 점검: A 버튼을 누르는 동안 숫자로 표시
        display.scroll(str(low), delay=60)
    sleep(1000)
