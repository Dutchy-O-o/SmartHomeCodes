"""SG90 servo deneme — GPIO 18 (Pin 12) sinyal, 5V VCC, GND ortak.
Servo: -90° -> 0° -> +90° -> 0° şeklinde gidip geliyor. Ctrl+C ile durdurabilirsin.
"""
import time
from gpiozero import AngularServo
from gpiozero.pins.lgpio import LGPIOFactory

# Pi 5'te lgpio backend en stabili
servo = AngularServo(
    18,
    min_angle=-90,
    max_angle=90,
    min_pulse_width=0.5/1000,   # 0.5 ms
    max_pulse_width=2.5/1000,   # 2.5 ms
    pin_factory=LGPIOFactory(),
)

try:
    print("Servo testi başlıyor (Ctrl+C ile çık)")
    while True:
        for angle in (-90, 0, 90, 0):
            print(f"  -> {angle}°")
            servo.angle = angle
            time.sleep(1.0)
except KeyboardInterrupt:
    print("\nDurduruldu")
finally:
    servo.detach()   # PWM sinyalini kes, servo titremesin
    servo.close()
