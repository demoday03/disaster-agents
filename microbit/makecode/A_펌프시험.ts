/**
 * 배수 모터(펌프) 배선 시험 - 노드 A 마이크로비트에 잠깐 넣어 쓰는 시험용 코드
 * 수위 센서와 상관없이 P2(릴레이)를 3초 켬 → 3초 끔을 계속 반복한다.
 * - 화면 ✔ : 릴레이 켬(P2 = 1) → 딸깍 소리가 나고 모터가 돌아야 한다
 * - 화면 ✖ : 릴레이 끔(P2 = 0) → 모터가 멈춰야 한다
 * 시험이 끝나면 A_침수.ts 를 다시 넣는다.
 *
 * 딸깍은 하는데 모터가 안 돌면 마이크로비트 코드 문제가 아니라 모터 쪽 전기 문제다(릴레이는 스위치일 뿐 전기를 만들지 않음).
 *   모터 전원(건전지·보조배터리) + → 릴레이 COM,  릴레이 NO → 모터 +,  모터 - → 모터 전원 -
 *   ✔일 때 멈추고 ✖일 때 돌면 NO 대신 NC 에 연결한 것 → NO 로 옮기거나 A_침수.ts 의 PUMP_ON = 0
 */
pins.digitalWritePin(DigitalPin.P2, 0)
basic.showString("P")

basic.forever(function () {
    pins.digitalWritePin(DigitalPin.P2, 1)
    basic.showIcon(IconNames.Yes)
    basic.pause(3000)
    pins.digitalWritePin(DigitalPin.P2, 0)
    basic.showIcon(IconNames.No)
    basic.pause(3000)
})
