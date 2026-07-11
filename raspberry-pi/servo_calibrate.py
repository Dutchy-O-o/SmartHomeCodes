"""Servo kalibrasyon — basit interaktif yardımcı.

Her komuttan sonra servo o pozisyona gider, kısa süre tutar, sonra PWM kesilir
(detach). Bu yüzden servo titremez, sessiz durur — net görebilirsin.

Kullanım:
    python3 servo_calibrate.py

Komutlar (her satırdan sonra Enter):
    <sayı>  ms cinsinden pulse width gönder (örn: 0.5 / 1.45 / 2.2)
    o       şu anki pozisyonu OPEN olarak işaretle (bariyer dik)
    c       şu anki pozisyonu CLOSED olarak işaretle (bariyer yatay)
    ?       mevcut durumu göster
    q       çık + alert.py için snippet bas

Pulse aralığı 0.3–2.7 ms ile sınırlı.
"""
import time
from gpiozero import Servo
from gpiozero.pins.lgpio import LGPIOFactory

PIN = 18
PULSE_MIN_MS = 0.3
PULSE_MAX_MS = 2.7
HOLD_SEC = 0.6   # PWM kesilmeden önce servonun pozisyona oturması için


def ms_to_value(ms: float) -> float:
    ms = max(PULSE_MIN_MS, min(PULSE_MAX_MS, ms))
    return (ms - PULSE_MIN_MS) / (PULSE_MAX_MS - PULSE_MIN_MS) * 2 - 1


_factory = LGPIOFactory()


def goto(ms: float):
    """Tek atımlık hareket: pulse uygula → bekle → PWM kes (titreşim yok)."""
    servo = Servo(
        PIN,
        min_pulse_width=PULSE_MIN_MS / 1000,
        max_pulse_width=PULSE_MAX_MS / 1000,
        pin_factory=_factory,
    )
    try:
        servo.value = ms_to_value(ms)
        time.sleep(HOLD_SEC)
    finally:
        servo.detach()
        servo.close()


def main():
    current = 1.5
    open_ms = None
    closed_ms = None

    print("[CALIB] başlangıç 1.50ms (~merkez)")
    print("[CALIB] sayı yaz (örn 0.5), 'o'=OPEN işaretle, 'c'=CLOSED, 'q'=çık")
    goto(current)

    while True:
        try:
            raw = input(f"[{current:.2f}ms] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not raw:
            continue
        if raw == "q":
            break
        if raw == "?":
            print(f"  current : {current:.2f}ms")
            print(f"  OPEN    : {'-' if open_ms is None else f'{open_ms:.2f}ms'}")
            print(f"  CLOSED  : {'-' if closed_ms is None else f'{closed_ms:.2f}ms'}")
            continue
        if raw == "o":
            open_ms = current
            print(f"  ✓ OPEN  = {open_ms:.2f}ms")
            continue
        if raw == "c":
            closed_ms = current
            print(f"  ✓ CLOSED = {closed_ms:.2f}ms")
            continue

        try:
            ms = float(raw)
        except ValueError:
            print(f"  ?? bilinmeyen: {raw!r}")
            continue
        if not (PULSE_MIN_MS <= ms <= PULSE_MAX_MS):
            print(f"  ?? aralık dışı [{PULSE_MIN_MS}, {PULSE_MAX_MS}]")
            continue

        current = ms
        goto(current)

    print()
    print("=" * 60)
    if open_ms is None or closed_ms is None:
        print("OPEN veya CLOSED işaretlemedin — snippet yok.")
        return

    pulse_lo = min(open_ms, closed_ms)
    pulse_hi = max(open_ms, closed_ms)

    def pulse_to_angle(ms):
        return (ms - pulse_lo) / (pulse_hi - pulse_lo) * 180 - 90

    open_deg = round(pulse_to_angle(open_ms))
    closed_deg = round(pulse_to_angle(closed_ms))

    print(f"OPEN   : {open_ms:.2f}ms → {open_deg:+d}°")
    print(f"CLOSED : {closed_ms:.2f}ms → {closed_deg:+d}°")
    print()
    print("alert.py'a yapıştır:")
    print("-" * 60)
    print(f"SERVO_OPEN_DEG    = {open_deg}")
    print(f"SERVO_CLOSED_DEG  = {closed_deg}")
    print()
    print("ServoDriver.__init__ içindeki AngularServo(...) parametreleri:")
    print(f"    min_pulse_width={pulse_lo / 1000:.6f},   # {pulse_lo:.2f}ms")
    print(f"    max_pulse_width={pulse_hi / 1000:.6f},   # {pulse_hi:.2f}ms")
    print("-" * 60)


if __name__ == "__main__":
    main()
