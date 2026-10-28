# 노드 B(사면): 기울기 스위치와 진동 센서를 읽어 무선으로 보낸다.
# 배선(가정): 기울기 스위치 → P8(디지털, 기울면 1), 진동(압전) 센서 → P1(아날로그 0~1023).
# 진동은 순간값이라 1초 동안 여러 번 읽은 최댓값을 보낸다.
# 기울기 스위치가 없으면 USE_ACCEL = True로 바꿔 마이크로비트 내장 가속도계로 기울기를 판단한다.
from microbit import display, pin1, pin8, sleep, accelerometer, Image
import radio

GROUP = 23
USE_ACCEL = False
TILT_MG = 500       # 내장 가속도계 사용 시 기울기 판단 기준(밀리g). 설치 점검에서 조정
radio.config(group=GROUP, length=64, power=7)
radio.on()
pin8.set_pull(pin8.PULL_DOWN)

def tilted():
    if USE_ACCEL:
        return 1 if abs(accelerometer.get_x()) > TILT_MG or abs(accelerometer.get_y()) > TILT_MG else 0
    return pin8.read_digital()

while True:
    peak = 0
    for _ in range(20):          # 1초 동안 50ms 간격으로 20번
        peak = max(peak, pin1.read_analog())
        sleep(50)
    t = tilted()
    for line in ("S,B,tilt,%d" % t, "S,B,vibration,%d" % peak):
        radio.send(line)
        print(line)
    display.show(Image.TRIANGLE if t else Image.SQUARE_SMALL)
