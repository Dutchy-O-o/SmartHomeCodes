import board
import neopixel
import time

NUM_PIXELS = 30
PIXEL_ORDER = neopixel.GRB
PIXEL_PIN = board.D21

pixels = neopixel.NeoPixel(
    PIXEL_PIN,
    NUM_PIXELS,
    pixel_order=PIXEL_ORDER,
    brightness=0.2,
    auto_write=False,
)

try:
    while True:
        print("Kirmizi...")
        pixels.fill((255, 0, 0))
        pixels.show()
        time.sleep(1)

        print("Yesil...")
        pixels.fill((0, 255, 0))
        pixels.show()
        time.sleep(1)

        print("Mavi...")
        pixels.fill((0, 0, 255))
        pixels.show()
        time.sleep(1)

        print("Sirayla beyaz yakiliyor...")
        pixels.fill((0, 0, 0))
        for i in range(NUM_PIXELS):
            pixels[i] = (255, 255, 255)
            pixels.show()
            time.sleep(0.05)
        time.sleep(1)

        print("Gokkusagi...")
        for j in range(256):
            for i in range(NUM_PIXELS):
                pos = (i * 256 // NUM_PIXELS + j) & 255
                if pos < 85:
                    pixels[i] = (pos * 3, 255 - pos * 3, 0)
                elif pos < 170:
                    pos -= 85
                    pixels[i] = (255 - pos * 3, 0, pos * 3)
                else:
                    pos -= 170
                    pixels[i] = (0, pos * 3, 255 - pos * 3)
            pixels.show()
            time.sleep(0.01)

except KeyboardInterrupt:
    pass
finally:
    pixels.fill((0, 0, 0))
    pixels.show()
    print("Bitti.")
