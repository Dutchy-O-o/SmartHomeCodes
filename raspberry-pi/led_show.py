"""Bağımsız LED strip show — alert.py'dan ayrı çalışır.

Donanım: WS2812B, 27 piksel, GPIO 21 (board.D21), GRB sırası.
Önce ana servisi durdur (GPIO 21 onun elinde):
    sudo systemctl stop akilliev.service

Sonra root olarak çalıştır:
    sudo /home/ramazan/Desktop/python/venv/bin/python /home/ramazan/led_show.py

Ctrl+C ile çık. Bittikten sonra ana servisi geri aç:
    sudo systemctl start akilliev.service
"""

import math
import random
import time
import board
import neopixel

NUM = 27
PIN = board.D21
ORDER = neopixel.GRB

pixels = neopixel.NeoPixel(
    PIN, NUM, brightness=1.0, pixel_order=ORDER, auto_write=False,
)


def clear():
    pixels.fill((0, 0, 0))
    pixels.show()


def hsv_to_rgb(h, s=1.0, v=1.0):
    """h: 0-1, s: 0-1, v: 0-1 → (r,g,b) 0-255."""
    h = h % 1.0
    i = int(h * 6)
    f = h * 6 - i
    p = v * (1 - s)
    q = v * (1 - f * s)
    t = v * (1 - (1 - f) * s)
    r, g, b = [
        (v, t, p), (q, v, p), (p, v, t),
        (p, q, v), (t, p, v), (v, p, q),
    ][i % 6]
    return int(r * 255), int(g * 255), int(b * 255)


def scale(rgb, mult):
    return int(rgb[0] * mult), int(rgb[1] * mult), int(rgb[2] * mult)


# ---------- EFEKTLER ----------

def rainbow_cycle(duration=8, speed=0.005):
    """Tüm strip boyunca akan gökkuşağı."""
    print("• rainbow_cycle")
    end = time.time() + duration
    j = 0
    while time.time() < end:
        for i in range(NUM):
            pixels[i] = hsv_to_rgb((i / NUM + j / 360) % 1.0)
        pixels.show()
        j = (j + 1) % 360
        time.sleep(speed)


def snake(duration=10, length=6, speed=0.06):
    """Renk değiştiren yılan: başı parlak, kuyruğu sönerek gider."""
    print("• snake (multi-color)")
    end = time.time() + duration
    head = 0
    hue = 0.0
    while time.time() < end:
        pixels.fill((0, 0, 0))
        for k in range(length):
            i = (head - k) % NUM
            mult = (length - k) / length  # baş 1.0 → kuyruk 1/length
            pixels[i] = scale(hsv_to_rgb(hue), mult)
        pixels.show()
        head = (head + 1) % NUM
        hue = (hue + 0.02) % 1.0
        time.sleep(speed)


def double_snake(duration=8, length=5, speed=0.05):
    """İki yılan ters yönde, farklı renklerde."""
    print("• double_snake")
    end = time.time() + duration
    head_a = 0
    head_b = NUM // 2
    while time.time() < end:
        pixels.fill((0, 0, 0))
        # mavi yılan ileri
        for k in range(length):
            mult = (length - k) / length
            pixels[(head_a - k) % NUM] = scale((0, 80, 255), mult)
        # turuncu yılan geri
        for k in range(length):
            mult = (length - k) / length
            i = (head_b + k) % NUM
            cur = pixels[i]
            new = scale((255, 80, 0), mult)
            # iki yılan çakışırsa ortalama (daha güzel görünür)
            pixels[i] = (
                min(cur[0] + new[0], 255),
                min(cur[1] + new[1], 255),
                min(cur[2] + new[2], 255),
            )
        pixels.show()
        head_a = (head_a + 1) % NUM
        head_b = (head_b - 1) % NUM
        time.sleep(speed)


def comet(duration=8, length=8, speed=0.04):
    """Kuyruklu yıldız — renk değiştiren bir nokta hızlı geçer, kuyruk solar."""
    print("• comet")
    end = time.time() + duration
    pos = 0
    direction = 1
    hue = random.random()
    while time.time() < end:
        pixels.fill((0, 0, 0))
        for k in range(length):
            idx = pos - k * direction
            if 0 <= idx < NUM:
                mult = ((length - k) / length) ** 2  # eğri solma
                pixels[idx] = scale(hsv_to_rgb(hue), mult)
        pixels.show()
        pos += direction
        if pos >= NUM - 1 or pos <= 0:
            direction *= -1
            hue = (hue + 0.3) % 1.0
        time.sleep(speed)


def theater_chase(duration=6, speed=0.08):
    """Sinema marquee tarzı — her 3 lambadan biri yanar, döner."""
    print("• theater_chase")
    end = time.time() + duration
    j = 0
    hue = 0.0
    while time.time() < end:
        for i in range(NUM):
            if (i + j) % 3 == 0:
                pixels[i] = hsv_to_rgb(hue)
            else:
                pixels[i] = (0, 0, 0)
        pixels.show()
        j = (j + 1) % 3
        hue = (hue + 0.02) % 1.0
        time.sleep(speed)


def sparkle(duration=6, intensity=4):
    """Rastgele yıldız parlamaları."""
    print("• sparkle")
    end = time.time() + duration
    while time.time() < end:
        # yumuşat
        for i in range(NUM):
            r, g, b = pixels[i]
            pixels[i] = (max(0, r - 25), max(0, g - 25), max(0, b - 25))
        # yeni parıltılar
        for _ in range(intensity):
            i = random.randint(0, NUM - 1)
            pixels[i] = hsv_to_rgb(random.random())
        pixels.show()
        time.sleep(0.05)


def fire(duration=8):
    """Alev efekti — alttan yukarıya kıvılcımlar, kırmızı/turuncu."""
    print("• fire")
    end = time.time() + duration
    heat = [0] * NUM
    cooling = 60
    sparking = 120
    while time.time() < end:
        # 1) soğut
        for i in range(NUM):
            heat[i] = max(0, heat[i] - random.randint(0, ((cooling * 10) // NUM) + 2))
        # 2) ısı yukarı yayılsın
        for i in range(NUM - 1, 1, -1):
            heat[i] = (heat[i - 1] + heat[i - 2] + heat[i - 2]) // 3
        # 3) kıvılcım
        if random.randint(0, 255) < sparking:
            i = random.randint(0, 7)
            heat[i] = min(255, heat[i] + random.randint(160, 255))
        # 4) heat → renk
        for i in range(NUM):
            t = heat[i]
            if t < 85:
                pixels[i] = (t * 3, 0, 0)
            elif t < 170:
                pixels[i] = (255, (t - 85) * 3, 0)
            else:
                pixels[i] = (255, 255, (t - 170) * 3)
        pixels.show()
        time.sleep(0.04)


def color_wipe(duration=4):
    """Sırayla renk yakıp söndürme — temiz geçiş efekti."""
    print("• color_wipe")
    colors = [(255, 0, 80), (0, 200, 255), (255, 200, 0), (180, 0, 255)]
    end = time.time() + duration
    ci = 0
    while time.time() < end:
        for i in range(NUM):
            pixels[i] = colors[ci]
            pixels.show()
            time.sleep(0.02)
        ci = (ci + 1) % len(colors)


def police(duration=4):
    """Polis çakar — kırmızı/mavi alternans."""
    print("• police")
    end = time.time() + duration
    state = 0
    while time.time() < end:
        half = NUM // 2
        if state == 0:
            for i in range(half):
                pixels[i] = (255, 0, 0)
            for i in range(half, NUM):
                pixels[i] = (0, 0, 0)
        else:
            for i in range(half):
                pixels[i] = (0, 0, 0)
            for i in range(half, NUM):
                pixels[i] = (0, 0, 255)
        pixels.show()
        time.sleep(0.12)
        state ^= 1


# ---------- MAIN LOOP ----------

EFFECTS = [
    rainbow_cycle,
    snake,
    sparkle,
    comet,
    theater_chase,
    double_snake,
    fire,
    color_wipe,
]

EFFECT_DURATION = 600  # her efekt 10 dakika sürer


def main():
    print(f"LED show starting on {NUM} pixels @ {PIN}. Her efekt {EFFECT_DURATION//60} dk. Ctrl+C ile çık.\n")
    try:
        while True:
            for effect in EFFECTS:
                effect(duration=EFFECT_DURATION)
                clear()
                time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        clear()


if __name__ == "__main__":
    main()
