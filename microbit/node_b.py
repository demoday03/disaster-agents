# 노드 B(사면): 기울기 센서와 진동 센서를 읽어 무선으로 보낸다.
# 배선(2026-11-01 설치 점검으로 확정, Keyestudio 센서 쉴드):
#   진동 센서 AO → P0(아날로그 0~1023, 가만히 두면 0 근처), DO → P1(쓰지 않음)
#   기울기 센서(매직 라이트 컵) S → P9(디지털, 세워 두면 1·기울면 0), L → P8(모듈 LED, 기울면 켬)
# 진동은 순간값이라 1초 동안 여러 번 읽은 최댓값, 기울기는 그 1초 안에 한 번이라도 기울면 1을 보낸다.
# 기울기 센서가 없으면 USE_ACCEL = True로 바꿔 마이크로비트 내장 가속도계로 기울기를 판단한다.
# 실제 시연 보드에는 같은 동작의 MakeCode 판(하드웨어/서버연결본/B_사면.ts)을 올려 두었다.
from microbit import display, pin0, pin8, pin9, sleep, accelerometer, Image
import radio

GROUP = 23
USE_ACCEL = False
TILT_ACTIVE = 0     # 기울었을 때 기울기 핀 값(이 모듈은 기울면 0)
TILT_MG = 500       # 내장 가속도계 사용 시 기울기 판단 기준(밀리g). 설치 점검에서 조정
radio.config(group=GROUP, length=64, power=7)
radio.on()
pin9.set_pull(pin9.NO_PULL)   # 모듈에 풀업 저항이 있다

def tilted():
    if USE_ACCEL:
        return 1 if abs(accelerometer.get_x()) > TILT_MG or abs(accelerometer.get_y()) > TILT_MG else 0
    return 1 if pin9.read_digital() == TILT_ACTIVE else 0

while True:
    peak, t = 0, 0
    for _ in range(20):          # 1초 동안 50ms 간격으로 20번
        peak = max(peak, pin0.read_analog())
        t = t or tilted()
        sleep(50)
    pin8.write_digital(t)
    for line in ("S,B,tilt,%d" % t, "S,B,vibration,%d" % peak):
        radio.send(line)
        print(line)
    display.show(Image.TRIANGLE if t else Image.SQUARE_SMALL)
