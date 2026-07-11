import os
import time
import math
import json
import subprocess
import threading
from pathlib import Path
from awscrt import mqtt
from awsiot import mqtt_connection_builder
import board
import adafruit_dht
from gpiozero import DigitalInputDevice, LED, Buzzer, Button, Servo
from gpiozero.pins.lgpio import LGPIOFactory
import neopixel
from provisioning import run_provisioning, ENDPOINT, SERIAL_NUMBER, PERM_CERT, PERM_KEY, ROOT_CA
from datetime import datetime, timezone
from mpu6050_driver import get_accel, magnitude
from mfrc522 import MFRC522
import RPi.GPIO as RPI_GPIO

# --- DONANIM ---
buzzer = Buzzer(24)
gas_sensor = DigitalInputDevice(14, pull_up=True)
dht_device = adafruit_dht.DHT11(board.D15)
EARTHQUAKE_THRESHOLD_G = 0.5  # |a|-1g sapması bu değeri geçince deprem sayılır

# --- GÖSTERGE LED'LERİ (gerçek IR/röle donanımı eklenene kadar göstergedir) ---
led_tv   = LED(16)
led_oven = LED(17)
led_ac   = LED(22)

# Alarm sistemini geçici olarak devre dışı bırakma anahtarı
ALARM_ENABLED = True

# ============================================================
# AKTÜATÖR CİHAZLARI (TV / Klima / Fırın / LED Strip)
# Cihaz UUID'leri runtime'da AWS'ten my_devices ile gelir.
# Driver'lar device_type ile keylenir; UUID hardcoded değildir.
# ============================================================

# WS2812B yapılandırması (27 piksel, GPIO21 — Pi 5 neopixel backend)
LED_COUNT      = 27
LED_PIN        = board.D21
LED_PIXEL_ORDER = neopixel.GRB

STATE_FILE = Path.home() / ".smart-home" / "state.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

# --- RASPOTIFY / SPEAKER ---
RASPOTIFY_SERVICE  = "raspotify"
RASPOTIFY_ENV_PATH = "/var/lib/akilliev/raspotify.env"
ALSA_MIXER         = "Speaker"
ALSA_CARD          = "2"   # USB ses kartı, aplay -l çıktısından

# Sadece ilk açılışta env dosyasına yazılır; sonraki açılışlarda dokunulmaz.
RASPOTIFY_DEFAULTS = {
    "LIBRESPOT_DEVICE":         "plughw:CARD=Device,DEV=0",
    "LIBRESPOT_BITRATE":        "160",
    "LIBRESPOT_INITIAL_VOLUME": "50",
    "LIBRESPOT_VOLUME_CTRL":    "fixed",
}

# device_type → varsayılan property'ler
DEFAULT_TYPE_STATE = {
    "ac":        {"power": "off", "target_temp": 22, "mode": "cool", "fan_speed": "auto"},
    "stove":     {"power": "off", "target_temp": 180, "mode": "bake", "timer": 0},
    "tv":        {"power": "off", "volume": 30, "channel": 1},
    "led_strip": {"power": "off", "brightness": 80, "color": "#FFFFFF"},
    "speaker":   {"power": "off", "volume": 50},
}

# AC ve fırın için izinli enum değerleri (app DB ile birebir aynı olmalı)
AC_MODES        = ("cool", "heat", "auto", "fan", "dry")
AC_FAN_SPEEDS   = ("low", "mid", "high", "auto")
OVEN_MODES      = ("bake", "broil", "fan", "defrost")


def load_device_state():
    """state.json device_type ile keylenir (UUID değişse bile state korunsun)."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                stored = json.load(f)
            # Eski format (UUID anahtarlı) varsa bypass et
            if stored and any("-" in k and len(k) == 36 for k in stored.keys()):
                print("[INFO] Eski state.json formatı (UUID anahtarlı) bypass edildi, varsayılana dönülüyor")
                return {dt: dict(d) for dt, d in DEFAULT_TYPE_STATE.items()}
            return {
                dt: {**defaults, **stored.get(dt, {})}
                for dt, defaults in DEFAULT_TYPE_STATE.items()
            }
        except Exception as e:
            print(f"[WARN] State dosyası okunamadı, varsayılan kullanılıyor: {e}")
    return {dt: dict(d) for dt, d in DEFAULT_TYPE_STATE.items()}


def save_device_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(device_state, f, indent=2)
    except Exception as e:
        print(f"[WARN] State kaydedilemedi: {e}")


device_state = load_device_state()


# --- SÜRÜCÜ STUB'LARI (şimdilik LED + log; sonra IR/röle eklenecek) ---
class AcDriver:
    """Mock klima: power gösterge LED'ine bağlı, diğer property'ler echo-only.
    Gerçek IR transmitter eklenince set_* method'larının gövdeleri doldurulur."""
    name = "ac"
    def set_power(self, on: bool):
        led_ac.on() if on else led_ac.off()
        print(f"[AC] power -> {'on' if on else 'off'}")
    def set_target_temp(self, t: int):
        print(f"[AC] target_temp -> {t}°C  (mock)")
    def set_mode(self, m: str):
        print(f"[AC] mode -> {m}  (mock)")
    def set_fan_speed(self, s: str):
        print(f"[AC] fan_speed -> {s}  (mock)")

class OvenDriver:
    """Mock fırın: power gösterge LED'ine bağlı, diğer property'ler echo-only.
    Gerçek röle/SSR eklenince set_* method'larının gövdeleri doldurulur."""
    name = "oven"
    def set_power(self, on: bool):
        led_oven.on() if on else led_oven.off()
        print(f"[OVEN] power -> {'on' if on else 'off'}")
    def set_target_temp(self, t: int):
        print(f"[OVEN] target_temp -> {t}°C  (mock)")
    def set_mode(self, m: str):
        print(f"[OVEN] mode -> {m}  (mock)")
    def set_timer(self, mins: int):
        print(f"[OVEN] timer -> {mins} min  (mock, geri sayım yok)")

class TvDriver:
    name = "tv"
    def set_power(self, on: bool):
        led_tv.on() if on else led_tv.off()
        print(f"[TV] power -> {'on' if on else 'off'}")
    def set_volume(self, level: int):
        # TODO: IR / HDMI-CEC ile gerçek volume kontrolü
        print(f"[TV] volume -> {level}")
    def set_channel(self, ch: int):
        # TODO: IR / HDMI-CEC ile gerçek kanal kontrolü
        print(f"[TV] channel -> {ch}")


class LedStripDriver:
    """power=on iken sürekli yumuşak nefes (breathing) animasyonu çalar.
    AWS state kontratı aynı: dışarıya raporlanan {power, color, brightness}
    statik değerler; animasyon sadece lokal görseldir."""

    name = "led_strip"

    BREATH_PERIOD = 3.5    # tam bir nefes döngüsü saniye
    BREATH_MIN    = 0.30   # nefesin en sönük noktası (commanded brightness'ın %30'u)
    BREATH_TICK   = 0.05   # 50ms render aralığı (~20 FPS)

    def __init__(self):
        # brightness=1.0 → kütüphane scaling'i bypass; bizim _brightness'imiz değeri etkiler
        self.strip = neopixel.NeoPixel(
            LED_PIN, LED_COUNT,
            brightness=1.0,
            pixel_order=LED_PIXEL_ORDER,
            auto_write=False,
        )
        self._power = False
        self._color_hex = "#FFFFFF"
        self._brightness = 80  # 0-100
        # ilk frame: tamamen kapalı
        self.strip.fill((0, 0, 0))
        self.strip.show()
        threading.Thread(target=self._animator, daemon=True).start()
        print(f"[LED_STRIP] strip initialized ({LED_COUNT} px, GPIO21, breathing)")

    @staticmethod
    def _hex_to_rgb(h: str):
        h = h.lstrip("#")
        if len(h) != 6:
            raise ValueError(f"Geçersiz hex renk: {h!r}")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    def _animator(self):
        """Sürekli render thread'i. power=off iken siyah; power=on iken nefes."""
        t0 = time.monotonic()
        last_off_rendered = False
        while True:
            if not self._power:
                if not last_off_rendered:
                    self.strip.fill((0, 0, 0))
                    self.strip.show()
                    last_off_rendered = True
                time.sleep(0.1)
                continue
            last_off_rendered = False

            # 0..1 arası yumuşak sinüs (cosinüs ters çevirme)
            t = time.monotonic() - t0
            phase = 0.5 - 0.5 * math.cos(2 * math.pi * t / self.BREATH_PERIOD)
            breath = self.BREATH_MIN + (1.0 - self.BREATH_MIN) * phase

            try:
                r, g, b = self._hex_to_rgb(self._color_hex)
            except ValueError:
                r, g, b = 255, 255, 255

            scale = (max(0, min(self._brightness, 100)) / 100.0) * breath
            color = (int(r * scale), int(g * scale), int(b * scale))
            try:
                self.strip.fill(color)
                self.strip.show()
            except Exception as e:
                print(f"[LED_STRIP] render error: {e}")
                time.sleep(0.5)
            time.sleep(self.BREATH_TICK)

    def set_power(self, on: bool):
        self._power = bool(on)
        print(f"[LED_STRIP] power -> {'on' if on else 'off'}")

    def set_brightness(self, level: int):
        self._brightness = max(0, min(int(level), 100))
        print(f"[LED_STRIP] brightness -> {self._brightness}")

    def set_color(self, hex_str: str):
        s = hex_str if hex_str.startswith("#") else f"#{hex_str}"
        self._hex_to_rgb(s)  # validate
        self._color_hex = s.upper()
        print(f"[LED_STRIP] color -> {self._color_hex}")

    def restore(self, power, brightness, color_hex):
        """Boot sonrası state'i animator thread'in okuyacağı alanlara yükler."""
        self._brightness = max(0, min(int(brightness), 100))
        s = color_hex if str(color_hex).startswith("#") else f"#{color_hex}"
        try:
            self._hex_to_rgb(s)
            self._color_hex = s.upper()
        except ValueError:
            print(f"[LED_STRIP] Geçersiz kayıtlı renk {color_hex!r}, varsayılana dönülüyor")
            self._color_hex = "#FFFFFF"
        # power'ı en sona bırak ki animator yeni renk/parlaklıkla başlasın
        self._power = (power == "on") if isinstance(power, str) else bool(power)


class SpeakerDriver:
    """Raspotify systemd + ALSA volume wrapper.
    İlk açılışta /var/lib/akilliev/raspotify.env'i seed eder (machine-id'den
    türetilen unique LIBRESPOT_NAME ile). Sonraki açılışlarda env dosyası VAR;
    alert.py dokunmaz — kullanıcının elle yaptığı değişiklikler korunur.
    Yani machine-id okuması "factory provisioning" anı: bir kez."""

    name = "speaker"

    def __init__(self):
        self.spotify_name = self._ensure_raspotify_env()
        print(f"[SPEAKER] Spotify Connect adı: {self.spotify_name}")

    def _ensure_raspotify_env(self):
        env = self._read_env(RASPOTIFY_ENV_PATH)
        if env.get("LIBRESPOT_NAME"):
            return env["LIBRESPOT_NAME"]

        try:
            with open("/etc/machine-id") as f:
                short = f.read().strip()[:6]
        except Exception:
            short = "unknown"
        name = f"AkilliEv-{short}"

        full = dict(RASPOTIFY_DEFAULTS)
        full["LIBRESPOT_NAME"] = name
        # Mevcut dosyada elle eklenen şeyleri koru
        full.update({k: v for k, v in env.items() if k not in full})
        try:
            self._write_env(RASPOTIFY_ENV_PATH, full)
            print(f"[SPEAKER] env seed edildi: {name}")
        except Exception as e:
            print(f"[SPEAKER] env seed başarısız ({RASPOTIFY_ENV_PATH}): {e}")
        return name

    @staticmethod
    def _read_env(path):
        result = {}
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    result[k.strip()] = v.strip().strip('"').strip("'")
        except FileNotFoundError:
            pass
        return result

    @staticmethod
    def _write_env(path, kv):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            for k, v in kv.items():
                f.write(f'{k}={v}\n')

    def set_power(self, on: bool):
        action = "start" if on else "stop"
        result = subprocess.run(
            ["sudo", "systemctl", action, RASPOTIFY_SERVICE],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"[SPEAKER] systemctl {action} {RASPOTIFY_SERVICE} başarısız: {result.stderr.strip()}")
        else:
            print(f"[SPEAKER] power -> {'on' if on else 'off'}")

    def set_volume(self, level: int):
        v = max(0, min(100, int(level)))
        result = subprocess.run(
            ["amixer", "-c", ALSA_CARD, "-q", "set", ALSA_MIXER, f"{v}%"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"[SPEAKER] amixer -c {ALSA_CARD} set {ALSA_MIXER} {v}% başarısız: {result.stderr.strip()}")
        else:
            print(f"[SPEAKER] volume -> {v}")


# ============================================================
# RFID + SERVO BARRIER
# ============================================================
RFID_DEVICE_ID    = "18f122ab-b9d3-499c-b068-0f6cb7aef8fb"
SERVO_PIN         = 18

# Kalibrasyondan gelen pulse genişlikleri (ms cinsinden).
# servo_calibrate.py ile yeniden ölçülürse buradaki iki değeri güncelle, başka
# yere dokunmaya gerek yok.
SERVO_OPEN_PULSE_MS   = 0.30   # bariyer dik (kart okutulunca)
SERVO_CLOSED_PULSE_MS = 1.00   # bariyer yatay (varsayılan)

# gpiozero Servo objesinin "value" mapping aralığı; pulse_ms'ten value'ya çevirmek için.
# Geniş tutuluyor ki kalibrasyon değeri hangi aralıkta olursa olsun temsil edilebilsin.
_SERVO_PULSE_FLOOR_MS = 0.10
_SERVO_PULSE_CEIL_MS  = 3.00

# Servonun hareketi tamamlaması için bolca pay (SG90: ~0.3s/60° → 180° ≈ 1s).
SERVO_MOVE_TIME_SEC   = 1.2

BARRIER_OPEN_SEC  = 5
# Aynı kartı çok hızlı tekrar okutmaya karşı rate limit (saniye). BARRIER_OPEN_SEC'ten
# uzun olmalı — yoksa bariyer kapandıktan kısa süre sonra RFID glitch'i (kart hâlâ
# üstteyken sahte "kalktı" tetiği) yeni publish üretip bariyeri tekrar açıyor.
SCAN_COOLDOWN_SEC = 8.0
# Bariyer kapandıktan sonra X saniye içinde gelen yeni open_for çağrılarını yok say.
# AWS duplicate delivery'ye veya gecikmiş ikinci sonuç mesajına karşı son güvenlik ağı.
BARRIER_POST_CLOSE_GRACE_SEC = 3.0
RFID_RST_PIN      = 25   # BCM — brief'e göre fiziksel pin 22


class ServoDriver:
    """Bariyer servo wrapper — one-shot pulse pattern + detaylı debug logu.

    SG90 feedback'siz — pozisyonu doğrudan okuyamayız. Bunun yerine:
    - Gönderilen son pulse_ms kaydedilir (_current_pulse_ms)
    - Her _goto, open_for, close_later olayı zaman damgalı log basar
    - Her 5 saniyede bir 'heartbeat' satırı barrier state ve son komutu yazdırır

    Eğer log'da "yavaşça açılırken" yeni bir _goto görmüyorsan → mekanik drift
    (donanım sorunu). Görüyorsan → kim çağırdı, kaynağa bakılır."""

    HEARTBEAT_SEC = 5.0

    def __init__(self, pin=SERVO_PIN):
        self._pin = pin
        self._factory = LGPIOFactory()
        self._lock = threading.Lock()
        self._barrier = "closed"
        self._last_close_time = 0.0
        self._current_pulse_ms = None
        self._last_command_time = 0.0
        self._last_command_label = "none"
        # Heartbeat thread başlat
        threading.Thread(target=self._heartbeat, daemon=True).start()
        # Boot'ta CLOSED pozisyonuna getir
        self._goto(SERVO_CLOSED_PULSE_MS, label="boot")

    @staticmethod
    def _ts():
        return time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"

    @staticmethod
    def _ms_to_value(pulse_ms):
        v = (pulse_ms - _SERVO_PULSE_FLOOR_MS) / (_SERVO_PULSE_CEIL_MS - _SERVO_PULSE_FLOOR_MS) * 2 - 1
        return max(-1.0, min(1.0, v))

    def _goto(self, pulse_ms, label=""):
        """Tek atımlık hareket: pulse uygula → bekle → PWM kes ve pini bırak."""
        import traceback
        # Çağıran çerçeveyi log'a al — hangi yoldan tetiklendiğini görmek için
        stack = traceback.extract_stack(limit=4)
        caller = f"{stack[-2].name}:{stack[-2].lineno}" if len(stack) >= 2 else "?"
        with self._lock:
            print(f"[SERVO {self._ts()}] _goto({label}) pulse={pulse_ms:.2f}ms ← {caller}")
            servo = Servo(
                self._pin,
                initial_value=None,
                min_pulse_width=_SERVO_PULSE_FLOOR_MS / 1000,
                max_pulse_width=_SERVO_PULSE_CEIL_MS / 1000,
                pin_factory=self._factory,
            )
            try:
                servo.value = self._ms_to_value(pulse_ms)
                time.sleep(SERVO_MOVE_TIME_SEC)
                self._current_pulse_ms = pulse_ms
                self._last_command_time = time.time()
                self._last_command_label = label
            finally:
                servo.detach()
                servo.close()
            print(f"[SERVO {self._ts()}] _goto({label}) tamamlandı, PWM kesildi")

    def open_for(self, seconds=BARRIER_OPEN_SEC):
        ts = self._ts()
        if self._barrier == "open":
            print(f"[SERVO {ts}] open_for ignored — barrier zaten açık")
            return
        since_close = time.time() - self._last_close_time
        if since_close < BARRIER_POST_CLOSE_GRACE_SEC:
            print(f"[SERVO {ts}] open_for ignored — kapanmadan {since_close:.1f}s sonra geldi (grace={BARRIER_POST_CLOSE_GRACE_SEC}s)")
            return
        print(f"[SERVO {ts}] OPEN_FOR çağrıldı, {seconds}s sonra kapanacak")
        self._goto(SERVO_OPEN_PULSE_MS, label="open")
        self._barrier = "open"

        def close_later():
            time.sleep(seconds)
            print(f"[SERVO {self._ts()}] auto-close timer fired ({seconds}s sonra)")
            self._goto(SERVO_CLOSED_PULSE_MS, label="auto-close")
            self._barrier = "closed"
            self._last_close_time = time.time()
            print(f"[SERVO {self._ts()}] barrier=closed, _last_close_time set")

        threading.Thread(target=close_later, daemon=True).start()

    def _heartbeat(self):
        """Her 5 saniyede bir mevcut state'i yazdır. 'Yavaşça açılma' anında
        log'da yeni _goto YOKSA mekanik drift; VARSA komut kaynağına bak."""
        while True:
            time.sleep(self.HEARTBEAT_SEC)
            t = time.time()
            since_cmd = t - self._last_command_time if self._last_command_time else 0
            print(f"[SERVO {self._ts()} HB] barrier={self._barrier} "
                  f"last_pulse={self._current_pulse_ms}ms "
                  f"last_cmd={self._last_command_label} ({since_cmd:.1f}s önce)")

    @property
    def barrier(self):
        return self._barrier


class RfidDriver:
    """Continuous reader thread + AWS verification dispatcher.
    Bare MFRC522 (SimpleMFRC522 değil) — Mifare auth/crypto adımları atlanıyor
    ve doğru RST pini (BCM 25) sürülüyor. Lib defaultu BCM 22, led_ac ile çakışıyordu."""

    def __init__(self, mqtt_publish, home_id, servo):
        self._mfrc = MFRC522(pin_rst=RFID_RST_PIN, pin_mode=RPI_GPIO.BCM)
        self.mqtt_publish = mqtt_publish
        self.home_id = home_id
        self.servo = servo
        self._last_uid = None
        self._last_published = 0.0

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    @staticmethod
    def _uid_to_num(uid):
        n = 0
        for i in range(5):
            n = n * 256 + uid[i]
        return n

    def _detect_uid(self):
        """Request + Anticoll: UID için yeterli, auth/crypto yok.
        REQALL (WUPA) kullanılıyor — HALT durumundaki kartları da uyandırır,
        aksi halde _halt() sonrası aynı kart bir daha tespit edilmiyor."""
        (status, _tag) = self._mfrc.MFRC522_Request(self._mfrc.PICC_REQALL)
        if status != self._mfrc.MI_OK:
            return None
        (status, uid) = self._mfrc.MFRC522_Anticoll()
        if status != self._mfrc.MI_OK:
            return None
        return self._uid_to_num(uid)

    def _halt(self):
        """PICC_HALT komutu + StopCrypto1."""
        try:
            buf = [self._mfrc.PICC_HALT, 0x00]
            crc = self._mfrc.CalulateCRC(buf)
            buf.append(crc[0])
            buf.append(crc[1])
            self._mfrc.MFRC522_ToCard(self._mfrc.PCD_TRANSCEIVE, buf)
        except Exception:
            pass
        try:
            self._mfrc.MFRC522_StopCrypto1()
        except Exception:
            pass

    def _hw_reset(self):
        """RST pinini fiziksel olarak toggle ederek chip'i komple sıfırla."""
        try:
            RPI_GPIO.output(RFID_RST_PIN, RPI_GPIO.LOW)
            time.sleep(0.05)
            RPI_GPIO.output(RFID_RST_PIN, RPI_GPIO.HIGH)
            time.sleep(0.05)
            self._mfrc.MFRC522_Init()
            print("[RFID] hardware reset done")
        except Exception as e:
            print(f"[RFID] hw reset failed: {e}")

    def _loop(self):
        # Edge-detection state machine: yalnızca kart YOKKEN→VAR geçişinde publish.
        # - Kart üstte tutuldukça (presence sürerken) ek publish yok → bariyer
        #   kapanır kapanmaz aynı kart ikinci kez tetiklemiyor.
        # - Kartın "kalktı" sayılması için ABSENCE_THRESHOLD süresi boyunca chip
        #   None döndürmeli; kısa MFRC522 glitch'leri false-lift yaratmıyor.
        # MFRC522 register'ları sürekli REQ/Anticoll çevriminde bozulabiliyor;
        # IDLE_REINIT_AFTER iterasyon sonra soft init ile refresh.
        ABSENCE_THRESHOLD = 1.0    # >1s detection yoksa kart kalkmış sayılır
        IDLE_REINIT_AFTER = 25     # ~5s @ 0.2s sleep — chip stuck'sa hızlı toparla
        idle_count = 0
        card_present = False
        last_detection = 0.0
        # Önceki run'dan bozuk chip state ihtimaline karşı temiz başlangıç
        self._hw_reset()
        print("[RFID] reader thread started, waiting for card...")
        while True:
            try:
                uid_int = self._detect_uid()
                now = time.time()

                if uid_int is None:
                    idle_count += 1
                    if idle_count >= IDLE_REINIT_AFTER:
                        try:
                            self._mfrc.MFRC522_Init()
                            print("[RFID] idle soft-init (chip refresh)")
                        except Exception as e:
                            print(f"[RFID] periodic init failed: {e}")
                        idle_count = 0
                    if card_present and (now - last_detection) > ABSENCE_THRESHOLD:
                        card_present = False
                    time.sleep(0.2)
                    continue
                idle_count = 0

                uid = format(uid_int, 'X')
                last_detection = now

                # Aynı presence içinde tekrar tekrar okutma — sadece yükselen kenar
                if card_present:
                    self._halt()
                    time.sleep(0.2)
                    continue

                card_present = True

                # Kart kaldırılıp çok hızlı geri konduysa rate limit
                if uid == self._last_uid and (now - self._last_published) < SCAN_COOLDOWN_SEC:
                    self._halt()
                    time.sleep(0.2)
                    continue

                self._last_uid = uid
                self._last_published = now

                print(f"[RFID] card scanned: {uid}")

                self.mqtt_publish(f"homes/{self.home_id}/rfid_check", {
                    "homeID":   self.home_id,
                    "card_uid": uid,
                })

                self._halt()
                time.sleep(0.5)
            except Exception as e:
                print(f"[RFID] read error: {e}")
                self._hw_reset()
                time.sleep(1)

    def handle_result(self, payload):
        result = payload.get("result")
        label = payload.get("label")
        uid = payload.get("card_uid")
        ts = time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
        print(f"[RFID {ts}] verify result for {uid}: {result} ({label})")

        if result == "authorized":
            print(f"[RFID {ts}] -> opening barrier")
            self.servo.open_for()
            trigger_rfid_authorized_beep()
        else:
            print(f"[RFID {ts}] -> denied (push notif Lambda tarafından gitti)")
            trigger_rfid_denied_beep()


DRIVERS = {
    "ac":        AcDriver(),
    "stove":     OvenDriver(),
    "tv":        TvDriver(),
    "led_strip": LedStripDriver(),
    "speaker":   SpeakerDriver(),
}


def apply_command(device_type, prop_name, value):
    """Tek bir komutu uygular ve state'i günceller. Başarı durumunda True döner."""
    driver = DRIVERS.get(device_type)
    if driver is None:
        return False

    try:
        if prop_name == "power":
            # power her zaman string ("on" / "off")
            on = str(value).strip().lower() == "on"
            driver.set_power(on)
            device_state[device_type]["power"] = "on" if on else "off"

        elif prop_name == "volume" and isinstance(driver, (TvDriver, SpeakerDriver)):
            try:
                level = int(value)  # mobil "50" string olarak gönderebilir
            except (TypeError, ValueError):
                print(f"[WARN] volume için geçersiz değer: {value!r}")
                return False
            level = max(0, min(100, level))
            driver.set_volume(level)
            device_state[device_type]["volume"] = level

        elif prop_name == "playback" and isinstance(driver, SpeakerDriver):
            # Playback (play/pause/next/prev) Pi'de işlenmiyor — mobil app
            # kendi Spotify Web API token'ıyla direkt halletmeli.
            print(f"[SPEAKER] playback komutu yok sayıldı (mobil tarafça halledilecek): {value!r}")
            return False

        elif prop_name == "channel" and isinstance(driver, TvDriver):
            try:
                ch = int(value)
            except (TypeError, ValueError):
                print(f"[WARN] channel için geçersiz değer: {value!r}")
                return False
            driver.set_channel(ch)
            device_state[device_type]["channel"] = ch

        elif prop_name == "brightness" and isinstance(driver, LedStripDriver):
            try:
                level = int(value)
            except (TypeError, ValueError):
                print(f"[WARN] brightness için geçersiz değer: {value!r}")
                return False
            driver.set_brightness(level)
            device_state[device_type]["brightness"] = max(0, min(level, 100))

        elif prop_name == "color" and isinstance(driver, LedStripDriver):
            try:
                driver.set_color(str(value))
            except ValueError as ve:
                print(f"[WARN] color için geçersiz değer: {value!r} ({ve})")
                return False
            device_state[device_type]["color"] = driver._color_hex

        # --- AC mock-echo property'leri ---
        elif prop_name == "target_temp" and isinstance(driver, AcDriver):
            try:
                t = max(16, min(30, int(value)))
            except (TypeError, ValueError):
                print(f"[WARN] AC target_temp için geçersiz değer: {value!r}")
                return False
            driver.set_target_temp(t)
            device_state[device_type]["target_temp"] = t

        elif prop_name == "mode" and isinstance(driver, AcDriver):
            s = str(value).strip().lower()
            if s not in AC_MODES:
                print(f"[WARN] AC mode için geçersiz değer: {value!r} (izinli: {AC_MODES})")
                return False
            driver.set_mode(s)
            device_state[device_type]["mode"] = s

        elif prop_name == "fan_speed" and isinstance(driver, AcDriver):
            s = str(value).strip().lower()
            if s not in AC_FAN_SPEEDS:
                print(f"[WARN] AC fan_speed için geçersiz değer: {value!r} (izinli: {AC_FAN_SPEEDS})")
                return False
            driver.set_fan_speed(s)
            device_state[device_type]["fan_speed"] = s

        # --- Fırın (stove) mock-echo property'leri ---
        elif prop_name == "target_temp" and isinstance(driver, OvenDriver):
            try:
                raw = int(value)
            except (TypeError, ValueError):
                print(f"[WARN] Oven target_temp için geçersiz değer: {value!r}")
                return False
            # 10°C adım: en yakın 10'a yuvarla, sonra 50-250 arasına clamp
            t = max(50, min(250, int(round(raw / 10.0)) * 10))
            driver.set_target_temp(t)
            device_state[device_type]["target_temp"] = t

        elif prop_name == "mode" and isinstance(driver, OvenDriver):
            s = str(value).strip().lower()
            if s not in OVEN_MODES:
                print(f"[WARN] Oven mode için geçersiz değer: {value!r} (izinli: {OVEN_MODES})")
                return False
            driver.set_mode(s)
            device_state[device_type]["mode"] = s

        elif prop_name == "timer" and isinstance(driver, OvenDriver):
            try:
                raw = int(value)
            except (TypeError, ValueError):
                print(f"[WARN] Oven timer için geçersiz değer: {value!r}")
                return False
            # 5 dk adım: en yakın 5'e yuvarla, sonra 0-180 arasına clamp
            mins = max(0, min(180, int(round(raw / 5.0)) * 5))
            driver.set_timer(mins)
            device_state[device_type]["timer"] = mins

        else:
            print(f"[WARN] {driver.name} cihazı için '{prop_name}' özelliği desteklenmiyor")
            return False

        save_device_state()
        return True

    except Exception as e:
        print(f"[ERROR] Komut uygulanırken hata ({driver.name}.{prop_name}={value!r}): {e}")
        return False


def restore_physical_state():
    """Boot sonrası kayıtlı state'e göre LED'leri/sürücüleri geri yükle."""
    for device_type, driver in DRIVERS.items():
        props = device_state.get(device_type, {})
        if isinstance(driver, LedStripDriver):
            driver.restore(
                power=props.get("power", "off"),
                brightness=props.get("brightness", 80),
                color_hex=props.get("color", "#FFFFFF"),
            )
            continue
        if "power" in props:
            driver.set_power(props["power"] == "on")
        if isinstance(driver, TvDriver):
            if "volume" in props: driver.set_volume(props["volume"])
            if "channel" in props: driver.set_channel(props["channel"])
        if isinstance(driver, SpeakerDriver):
            if "volume" in props: driver.set_volume(props["volume"])


def publish_initial_state(mqtt_connection):
    """Boot anındaki state'i AWS'e gönder. my_devices artık {deviceid: device_type}
    olduğu için aynı device_type'tan birden fazla cihaz (ör. iki klima) varsa
    her birine ayrı ayrı initial state yollanır."""
    for device_id, device_type in my_devices.items():
        props = device_state.get(device_type)
        if not props:
            print(f"[INFO] '{device_type}' için default state yok, initial state atlandı ({device_id})")
            continue
        states = [{"property_name": k, "current_value": str(v)} for k, v in props.items()]
        publish_device_state(mqtt_connection, device_id, states)

# Global değişkenler - Cihaz listesi için
devices_loaded = False
# --- SENSÖR DURUM BAYRAKLARI ---
is_earthquake = False
is_gas_leak_detected = False

# Mesajın sürekli (spam) gitmesini engellemek için hafıza bayrakları
alert_sent_earthquake = False
alert_sent_gas = False
my_devices = {}

# --- BUZZER ALARM AYARLARI ---
BUZZER_ALARM_DURATION = 4.5  # saniye
buzzer_alarm_done = False     # bu olay için alarm zaten çaldı mı?

def play_alarm_music(duration=BUZZER_ALARM_DURATION):
    """Aktif buzzer ile 'duration' saniye alarm melodisi çalar:
    4 hızlı kısa bip + 1 uzun bip pattern'i tekrar eder."""
    end = time.time() + duration
    try:
        while time.time() < end:
            for _ in range(4):
                if time.time() >= end: break
                buzzer.on();  time.sleep(0.12)
                buzzer.off(); time.sleep(0.08)
            if time.time() >= end: break
            buzzer.on();  time.sleep(0.40)
            buzzer.off(); time.sleep(0.20)
    finally:
        buzzer.off()

def trigger_alarm_music():
    threading.Thread(target=play_alarm_music, daemon=True).start()


def _play_rfid_authorized():
    """Yetkili kart: kısa-kısa iki bip."""
    for _ in range(2):
        buzzer.on();  time.sleep(0.10)
        buzzer.off(); time.sleep(0.08)


def _play_rfid_denied():
    """Yetkisiz kart: tek uzun bip."""
    buzzer.on();  time.sleep(0.6)
    buzzer.off()


def trigger_rfid_authorized_beep():
    threading.Thread(target=_play_rfid_authorized, daemon=True).start()


def trigger_rfid_denied_beep():
    threading.Thread(target=_play_rfid_denied, daemon=True).start()

from datetime import datetime, timezone
import json


def publish_device_state(mqtt_connection, device_id, states, execution_id=None):
    """Cihaz bir komutu yerine getirdikten sonra yeni durumunu (state) AWS'e bildirir.
    Komutta executionID geldiyse state'te de geri yansıtılır."""
    try:
        current_time = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        state_payload = {
            "deviceID": str(device_id),
            "states": states,
            "last_updated": current_time,
        }
        if execution_id is not None:
            state_payload["executionID"] = execution_id

        topic = f"homes/{SERIAL_NUMBER}/state"

        mqtt_connection.publish(
            topic=topic,
            payload=json.dumps(state_payload),
            qos=mqtt.QoS.AT_LEAST_ONCE
        )
        suffix = f" (exec={execution_id})" if execution_id else ""
        print(f"[✅ STATE] Durum başarıyla güncellendi{suffix} -> {topic}")

    except Exception as e:
        print(f"[ERROR] State gönderilirken hata oluştu: {e}")


def get_command_callback(mqtt_connection):
    """AWS IoT'den gelen komutları yakalar ve sürücülere yönlendirir.
    deviceID → device_type eşlemesi runtime'da my_devices üzerinden yapılır."""
    def on_command_received(topic, payload, dup, qos, retain, **kwargs):
        try:
            data = json.loads(payload.decode("utf-8"))
            device_id = data.get("deviceID")
            execution_id = data.get("executionID")
            commands = data.get("commands", [])

            if not device_id or not commands:
                print("[WARNING] Eksik veya hatalı komut formatı alındı.")
                return

            # device_id → device_type (my_devices artık {deviceid: device_type})
            device_type = my_devices.get(device_id)
            if device_type is None or device_type not in DRIVERS:
                print(f"[INFO] unknown device, skipping: {device_id}")
                return

            exec_suffix = f" exec={execution_id}" if execution_id else ""
            print(f"\n[COMMAND] {device_type} ({device_id}){exec_suffix} için {len(commands)} komut alındı")

            states_to_report = []
            for cmd in commands:
                prop_name = cmd.get("property_name")
                target_value = cmd.get("value")
                print(f"  -> {prop_name} = {target_value!r}")

                if apply_command(device_type, prop_name, target_value):
                    states_to_report.append({
                        "property_name": prop_name,
                        "current_value": str(device_state[device_type][prop_name]),
                    })

            if states_to_report:
                publish_device_state(mqtt_connection, device_id, states_to_report, execution_id)

        except Exception as e:
            print(f"[ERROR] Komut işlenirken kritik hata: {e}")

    return on_command_received
      
def check_and_publish_alerts(mqtt_connection):
    """Deprem veya gaz kaçağı durumlarını kontrol edip alert topiğine mesaj atar."""
    global is_earthquake, is_gas_leak_detected
    global alert_sent_earthquake, alert_sent_gas

    # --- GERÇEK SENSÖR OKUMALARI ---
    try:
        a = get_accel()
        shake = abs(magnitude(a) - 1.0)
        is_earthquake = shake > EARTHQUAKE_THRESHOLD_G
    except Exception as e:
        print(f"[WARN] MPU6050 okunamadı: {e}")
        is_earthquake = False

    is_gas_leak_detected = bool(gas_sensor.value)

    # --- BUZZER TETİKLEME ---
    # Tehlike algılandığında ~4-5 sn'lik alarm melodisini bir kez çal.
    # Tehlike geçip tekrar gelirse yeniden çalsın diye bayrağı sıfırlıyoruz.
    global buzzer_alarm_done
    alarm_now = is_earthquake or is_gas_leak_detected
    if ALARM_ENABLED and alarm_now and not buzzer_alarm_done:
        trigger_alarm_music()
        buzzer_alarm_done = True
    elif not alarm_now:
        buzzer_alarm_done = False

    topic = f"homes/{SERIAL_NUMBER}/alert"
    current_time = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    # -------------------------
    # 1. DEPREM KONTROLÜ
    # -------------------------
    if is_earthquake:
        if not alert_sent_earthquake: # Daha önce atılmadıysa at
            payload_dict = {
                "event_type": "earthquake",
                "severity": "critical",
                "message": "Evde sarsıntı algılandı!",
                "value": round(magnitude(a), 3),
                "timestamp": current_time
            }
            mqtt_connection.publish(
                topic=topic, 
                payload=json.dumps(payload_dict), 
                qos=mqtt.QoS.AT_LEAST_ONCE
            )
            print(f"[🚨 ALERT] Deprem alarmı gönderildi! -> {topic}")
            alert_sent_earthquake = True # Hafızaya al, bir daha atma
    else:
        # Tehlike geçtiyse hafızayı sıfırla ki bir sonraki depremde tekrar çalışsın
        alert_sent_earthquake = False

    # -------------------------
    # 2. GAZ KAÇAĞI KONTROLÜ
    # -------------------------
    if is_gas_leak_detected:
        if not alert_sent_gas: # Daha önce atılmadıysa at
            payload_dict = {
                "event_type": "gas_leak",
                "severity": "critical",
                "message": "Mutfakta yüksek seviyede gaz kaçağı algılandı!",
                "value": 450, # İleride MQ sensöründen analog/dijital değer alacaksın
                "timestamp": current_time
            }
            mqtt_connection.publish(
                topic=topic, 
                payload=json.dumps(payload_dict), 
                qos=mqtt.QoS.AT_LEAST_ONCE
            )
            print(f"[🚨 ALERT] Gaz kaçağı alarmı gönderildi! -> {topic}")
            alert_sent_gas = True # Hafızaya al, bir daha atma
    else:
        # Gaz seviyesi normale dönerse hafızayı sıfırla
        alert_sent_gas = False

def publish_temp_humidity_data(mqtt_connection):
    """Sadece DHT sensöründen Isı ve Nem verilerini okuyup AWS IoT'ye gönderir."""
    global my_devices
    
    try:
        # 1. Güvenlik Kontrolü: Bu evde temp_humidity cihazı var mı?
        # my_devices artık {deviceid: device_type}; ters yön lookup.
        device_id = next(
            (did for did, dt in my_devices.items() if dt == 'temp_humidity'),
            None,
        )
        if device_id is None:
            # Cihaz yoksa sessizce çık, boşuna işlem yapma
            return

        # 2. DHT22 sensöründen gerçek değerleri oku
        try:
            temperature = dht_device.temperature
            humidity = dht_device.humidity
        except RuntimeError as e:
            # DHT22 ara sıra okuma hatası verir, bu döngüyü atla
            print(f"[DHT] Okuma hatası, atlanıyor: {e}")
            return
        if temperature is None or humidity is None:
            return

        # 3. Anlık zaman damgasını oluştur (UTC)
        current_time = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        # 4. Sadece bu sensöre özel JSON Payload
        payload_dict = {
            "deviceID": str(device_id),
            "data": {
                "temperature": {
                    "value": str(temperature),
                    "unit": "C"
                },
                "humidity": {
                    "value": str(humidity),
                    "unit": "%"
                }
            },
            "timestamp": current_time
        }
        
        payload_json = json.dumps(payload_dict)
        
        # Topic'i sensör tipine göre de özelleştirebilirsin (opsiyonel)
        # Şimdilik senin istediğin standart topic formatında bırakıyorum
        topic = f"homes/{SERIAL_NUMBER}/sensor"

        # 5. Gönder
        mqtt_connection.publish(
            topic=topic, 
            payload=payload_json, 
            qos=mqtt.QoS.AT_LEAST_ONCE
        )
        
        print(f"[DHT22] Veri gönderildi: {temperature}°C, {humidity}% -> {topic}")

    except Exception as error:
        print(f"[ERROR] Isı ve nem verisi gönderilirken hata: {error}")


def on_devices_received(topic, payload, dup, qos, retain, **kwargs):
    """AWS'ten cihaz listesi yanıtı geldiğinde çalışacak callback"""
    global devices_loaded, my_devices
    
    try:
        # 1. Payload'u JSON'a çevir
        data = json.loads(payload.decode('utf-8'))
        
        # Eğer senin Lambda fonksiyonun devices adında bir liste dönüyorsa:
        device_list = data.get("devices", [])

        # 2. {deviceid: device_type} sözlüğüne çevir.
        # Önceki versiyon {device_type: deviceid} idi — aynı device_type'tan iki
        # cihaz (ör. iki klima) varsa sonuncu birinciyi sessizce eziyordu. Ters
        # yön benzersiz: bir deviceID tek bir device_type'a denk gelir, dispatch
        # de zaten deviceID üzerinden geliyor.
        my_devices = {dev["deviceid"]: dev["device_type"] for dev in device_list}

        print(f"[SUCCESS] Cihaz listesi başarıyla çekildi: {len(my_devices)} cihaz bulundu.")
        print("Detay:", json.dumps(my_devices, indent=2)) # İstersen ekrana bas
        
    except Exception as e:
        print(f"[ERROR] Cihaz listesi işlenirken hata oluştu: {e}")
    finally:
        # 3. Her durumda bayrağı kaldır ki program devam etsin
        devices_loaded = True


def run_normal_operation():
    global devices_loaded, my_devices

    # 1. AWS IoT'ye Kalıcı Sertifikalarla Bağlan
    print(f"[INFO] Connecting to AWS IoT as Thing: {SERIAL_NUMBER}")
    mqtt_connection = mqtt_connection_builder.mtls_from_path(
        endpoint=ENDPOINT,
        cert_filepath=PERM_CERT,
        pri_key_filepath=PERM_KEY,
        ca_filepath=ROOT_CA,
        client_id=SERIAL_NUMBER,
        clean_session=False,
        keep_alive_secs=30)

    mqtt_connection.connect().result()
    print("[SUCCESS] AWS IoT connection established.")

    # ---------------------------------------------------------
    # YENİ EKLENEN KISIM: CİHAZLARI ÇEKME (İSTEK-YANIT)
    # ---------------------------------------------------------
    
    # Topic isimlerini home_id (SERIAL_NUMBER) ile oluştur
    response_topic = f"homes/{SERIAL_NUMBER}/devices/response"
    request_topic = f"homes/{SERIAL_NUMBER}/devices/get"

    print(f"[INFO] Subscribing to {response_topic} to get devices...")
    # Yanıtı dinlemeye başla
    mqtt_connection.subscribe(
        topic=response_topic,
        qos=mqtt.QoS.AT_LEAST_ONCE,
        callback=on_devices_received
    )

    print("[INFO] Requesting device list from AWS...")
    # İsteği gönder (AWS'teki Kural/Lambda bunu dinleyecek)
    mqtt_connection.publish(
        topic=request_topic,
        payload=json.dumps({"action": "get_devices", "home_id": SERIAL_NUMBER}),
        qos=mqtt.QoS.AT_LEAST_ONCE
    )

    # Yanıt gelene kadar (veya timeout olana kadar) bekle
    print("[WAITING] Waiting for device list from AWS...")
    timeout_counter = 0
    while not devices_loaded:
        time.sleep(0.5)
        timeout_counter += 0.5
        if timeout_counter >= 15.0:  # 15 saniye timeout koruması
            print("[WARNING] Device list request timed out. Continuing with empty list.")
            devices_loaded = True

    # İşimiz bitti, o topic'ten çık (Unsubscribe)
    print(f"[INFO] Unsubscribing from {response_topic}...")
    mqtt_connection.unsubscribe(topic=response_topic)

    # ---------------------------------------------------------
    # ASIL ÇALIŞMA DÖNGÜSÜ (NORMAL SENSÖR OKUMALARI)
    # ---------------------------------------------------------
    print("\n[SYSTEM] Ready! Starting normal sensor operations...\n")


    command_topic = f"homes/{SERIAL_NUMBER}/command"
    print(f"[INFO] Komut topiği dinleniyor: {command_topic}")

    subscribe_future, _ = mqtt_connection.subscribe(
        topic=command_topic,
        qos=mqtt.QoS.AT_LEAST_ONCE,
        callback=get_command_callback(mqtt_connection)
    )
    subscribe_future.result()
    print(f"[INFO] subscribed to {command_topic}")

    # --- RFID + Servo bariyer ---
    def mqtt_publish_helper(topic, payload):
        mqtt_connection.publish(
            topic=topic,
            payload=json.dumps(payload),
            qos=mqtt.QoS.AT_LEAST_ONCE,
        )

    servo = ServoDriver()
    rfid = RfidDriver(mqtt_publish_helper, SERIAL_NUMBER, servo)

    def on_rfid_result(topic, payload, **kwargs):
        try:
            data = json.loads(payload.decode())
            rfid.handle_result(data)
        except Exception as e:
            print(f"[RFID] result parse error: {e}")

    rfid_result_topic = f"homes/{SERIAL_NUMBER}/rfid_result"
    subscribe_future, _ = mqtt_connection.subscribe(
        topic=rfid_result_topic,
        qos=mqtt.QoS.AT_LEAST_ONCE,
        callback=on_rfid_result,
    )
    subscribe_future.result()
    print(f"[INFO] subscribed to {rfid_result_topic}")

    rfid.start()

    # Boot sonrası fiziksel state'i (LED'ler) ve AWS DB'yi hizala
    print("[INFO] Restoring physical state from disk...")
    restore_physical_state()
    print("[INFO] Publishing initial state to AWS...")
    publish_initial_state(mqtt_connection)

    while True:
        try:

            publish_temp_humidity_data(mqtt_connection)
            check_and_publish_alerts(mqtt_connection)

        except Exception as error:
            print(f"Error occurred: {error}")
            time.sleep(1.0)

        time.sleep(5.0) # Saniyede bir 2 kere atmamak için biraz uzattım :)


if __name__ == "__main__":
    if not os.path.exists(PERM_CERT):
        run_provisioning()
    else:
        run_normal_operation()