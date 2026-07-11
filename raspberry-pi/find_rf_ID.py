
try:
    import RPi.GPIO as GPIO
except ImportError:
    import lgpio as GPIO

import spidev
from mfrc522 import SimpleMFRC522
import RPi.GPIO as GPIO
import time

reader = SimpleMFRC522()

try:
    print("Waiting for card... Please bring the card closer to the reader.")
    id, text = reader.read()
    print(f"\nSUCCESS!")
    print(f"ID: {id}")
    
finally:
    GPIO.cleanup()