# Raspberry Pi — RFID + Servo Bariyer Sürücüsü

> **Bu brief Raspi'deki Claude Code'a yapıştırılır.** Bulut tarafı zaten
> kurulu ve test edildi (denied path'te FCM telefona gidiyor); Raspi'nin
> tek görevi MFRC522'den okuduğu UID'i AWS'e gönderip sonucu bekleyerek
> servoyu kontrol etmek. Mevcut `alert.py`'a entegre edilecek, yeni
> servis YOK.

---

## 0. AWS mimarisinin Raspi tarafına bakan yüzü

Senin için netleştirmek gerekirse, **bulut tarafında kurulu sistem şu şekilde çalışıyor**:

```
              MQTT topic: homes/{HOME}/rfid_check
                          │ (sen yayınlıyorsun)
                          ▼
            ┌─────────────────────────────┐
            │ AWS IoT Topic Rule          │
            │ "rfidAccessCheck"           │
            │ SQL: SELECT * FROM ...      │
            │ Action: invoke Lambda       │
            └──────────────┬──────────────┘
                           │
                           ▼
            ┌─────────────────────────────┐
            │ verifyRfidAccess Lambda     │
            │  1. SELECT authorized_cards │
            │  2. INSERT access_log       │
            │  3. denied? → publish alert │
            │  4. publish rfid_result     │
            └──────────────┬──────────────┘
                           │
        ┌──────────────────┴────────────────────┐
        ▼                                       ▼
  homes/{HOME}/alert                 homes/{HOME}/rfid_result
        │ (denied'da)                    │ (her durumda)
        ▼                                ▼
  pushAlertNotificationstoUsers    SEN dinliyorsun (subscribe)
        │                                │
        ▼                                ▼
  FCM → telefona push              servo 
```

**Senin Raspi'de yapacağın iş 2 satır özet**:
1. Kart okutulduğunda → `homes/{HOME}/rfid_check` topic'ine UID yayınla
2. `homes/{HOME}/rfid_result` topic'inden gelen cevaba göre servoyu (authorized) veya LED'i (denied) tetikle

Bulut tarafı sana **soft real-time** cevap döner (~300-500ms). Yetki kontrolünü, audit log'u ve telefona push bildirimi tamamen Lambda halleder — sen sadece iki MQTT mesajı arasındaki boşluğu doldurmak zorundasın.

---

## 1. Mevcut bulut altyapısı (zaten kuruldu, dokunulmayacak)

| Kaynak | Konum | Durum |
|---|---|---|
| `authorized_cards` tablosu | RDS Postgres `iot-home` | ✅ |
| `access_log` tablosu | RDS Postgres | ✅ |
| `verifyRfidAccess` Lambda | AWS Lambda (us-east-1, VPC-dışı, 10s timeout) | ✅ |
| IoT Topic Rule `rfidAccessCheck` | `homes/+/rfid_check` topic | ✅ |
| Lambda invoke permission | IoT → verifyRfidAccess | ✅ |
| IAM `iot:Publish` policy | UpdateActuatorState role'üne attach | ✅ |
| Test kart `TEST_AUTH_001` (label="Test Card 1") | DB'de authorized | ✅ |
| Push notification (denied → FCM) | `pushAlertNotificationstoUsers` lambda | ✅ test edildi |

**Sen yeni bir AWS kaynağı oluşturmayacaksın. Sertifika yok, IAM yok, Lambda yok. Sadece kod.**

---

## 2. MQTT kontratı (kesin payload formatları)

### 2.1 Sen YAYINLIYORSUN: `homes/{HOME_ID}/rfid_check`

Kart okunduğunda hemen yayınla. QoS 1.

```json
{
  "homeID":   "757bfcc9-a80b-4886-a8cd-854392454caf",
  "card_uid": "AB12CD34"
}
```

- `homeID`: sabit, kendi ev ID'n
- `card_uid`: MFRC522'den okuduğun UID (hex string, büyük harf, ayraçsız önerilir)

### 2.2 Sen DİNLİYORSUN: `homes/{HOME_ID}/rfid_result`

Lambda kararını verdikten sonra ~300-500ms içinde bu payload düşer. QoS 1.

```json
{
  "card_uid": "AB12CD34",
  "result":   "authorized",
  "label":    "Test Card 1"
}
```

- `result`: `"authorized"` veya `"denied"`
- `label`: authorized ise dolu (kart sahibinin ismi), denied ise `null`
- `card_uid`: hangi UID için cevap olduğu (idempotency / yarış durumu için işine yarayabilir)

### 2.3 Sen YAYINLIYORSUN: `homes/{HOME_ID}/state` (opsiyonel ama önerilen)

Mevcut state akışına paralel — RFID Reader cihazı için "last_scan" ve "barrier" durumu yayınla, böylece mobil app `_buildRfidCard` widget'ı güncellenir.

```json
{
  "deviceID": "18f122ab-b9d3-499c-b068-0f6cb7aef8fb",
  "states": [
    { "property_name": "last_scan", "current_value": "AB12CD34" },
    { "property_name": "status",    "current_value": "active" },
    { "property_name": "barrier",   "current_value": "open" }
  ]
}
```

> **Not**: `actuator_properties` tablosunda RFID Reader için bu üç property
> kayıtlı DEĞİL. State publish edilirse `UpdateActuatorState` Lambda
> "property bulunamadı" diye log'a yazıp atlar — sorun değil. Faz 2'de
> property row'ları eklenecek. Yine de yayınla, ileride hazır olsun.

### 2.4 PiPolicy yetkilerin

Mevcut `PiPolicy` zaten `homes/{HOME_ID}/*` altındaki tüm topic'lere pub+sub veriyor. Yeni izin ekleme gerekmez. Topic'lerin tamamı kapsamda:
- ✅ `homes/757bfcc9.../rfid_check` (publish)
- ✅ `homes/757bfcc9.../rfid_result` (subscribe)
- ✅ `homes/757bfcc9.../state` (publish, mevcut akışla paralel)

---

## 3. Donanım

### 3.1 MFRC522 (RFID okuyucu) — SPI bus

| MFRC522 pin | Raspi BCM | Fiziksel pin |
|---|---|---|
| **SDA / SS** | GPIO8 (CE0) | 24 |
| **SCK** | GPIO11 | 23 |
| **MOSI** | GPIO10 | 19 |
| **MISO** | GPIO9 | 21 |
| **RST** | GPIO25 | 22 |
| **GND** | GND | 6 veya 9 |
| **3.3V** | 3.3V | 1 veya 17 |
| IRQ | bağlama | — |

⚠️ **3.3V** ile çalışır, **5V'a bağlama** (yanar). SPI etkin olmalı:
```bash
sudo raspi-config   # Interfaces → SPI → Enable
sudo reboot
```

### 3.2 SG90 servo (bariyer motoru) — PWM

| SG90 tel | Raspi pin | Not |
|---|---|---|
| Sarı (Signal) | **GPIO12** (BCM, fiziksel pin 32) | Hardware PWM |
| Kırmızı (VCC) | 5V (pin 2 veya 4) ya da harici 5V | ~150mA pik |
| Kahverengi (GND) | GND, raspi ile ortak | — |

**Açı kontratı** (fiziksel test sonucuna göre kullanıcı doğrulayacak):
- `0°` → bariyer **KAPALI** (kol yatay, geçiş yok)
- `90°` → bariyer **AÇIK** (kol dik, geçiş serbest)
- Otomatik kapanma süresi: **5 saniye**

Ters çıkarsa açıları kodda 0 ↔ 90 olarak swap et.

### 3.3 Kırmızı LED (denied feedback) — opsiyonel ama önerilen

GPIO5'e 220-330Ω direnç + LED bağla. Yetkisiz kart için 2 sn yansın.

---

## 4. Yapacaklarınm — adım adım

### 4.1 Bağımlılıkları kur

```bash
sudo apt update
sudo apt install -y python3-spidev
source /home/ramazan/led-env/bin/activate
pip install mfrc522 spidev
```

### 4.2 `alert.py` içine yeni driver class'ları ekle

`LedStripDriver`'ın altına ekle. **Mevcut `mqtt_connection`'ı paylaş, yeni connection AÇMA** (PiPolicy aynı client ID ile çoklu connection'a izin vermez).

```python
import threading
from mfrc522 import SimpleMFRC522

# RFID device ID — bulut tarafında zaten kayıtlı
RFID_DEVICE_ID    = "18f122ab-b9d3-499c-b068-0f6cb7aef8fb"
SERVO_PIN         = 12   # BCM
LED_DENIED_PIN    = 5    # BCM, opsiyonel — yoksa None geç
SERVO_OPEN_DEG    = 90
SERVO_CLOSED_DEG  = 0
BARRIER_OPEN_SEC  = 5
SCAN_COOLDOWN_SEC = 2.0  # aynı kartı tekrar okumayı engelle


class ServoDriver:
    """SG90 wrapper. PWM 50Hz, jitter'ı azaltmak için duty 0'a sıfırlanır."""

    def __init__(self, pin=SERVO_PIN):
        self.pin = pin
        GPIO.setup(self.pin, GPIO.OUT)
        self.pwm = GPIO.PWM(self.pin, 50)  # SG90 = 50Hz
        self.pwm.start(0)
        self._barrier = "closed"
        self.set_angle(SERVO_CLOSED_DEG)

    def set_angle(self, angle):
        duty = 2 + (angle / 18.0)   # 0°=2%, 90°=7%, 180°=12%
        self.pwm.ChangeDutyCycle(duty)
        time.sleep(0.5)
        self.pwm.ChangeDutyCycle(0)  # jitter'ı kapat

    def open_for(self, seconds=BARRIER_OPEN_SEC):
        self.set_angle(SERVO_OPEN_DEG)
        self._barrier = "open"
        def close_later():
            time.sleep(seconds)
            self.set_angle(SERVO_CLOSED_DEG)
            self._barrier = "closed"
        threading.Thread(target=close_later, daemon=True).start()

    @property
    def barrier(self):
        return self._barrier


class RfidDriver:
    """Continuous reader thread + AWS verification dispatcher."""

    def __init__(self, mqtt_publish, home_id, servo, denied_led_pin=LED_DENIED_PIN):
        self.reader = SimpleMFRC522()
        self.mqtt_publish = mqtt_publish      # (topic: str, payload: dict) -> None
        self.home_id = home_id
        self.servo = servo
        self.denied_led_pin = denied_led_pin
        if denied_led_pin is not None:
            GPIO.setup(denied_led_pin, GPIO.OUT)
            GPIO.output(denied_led_pin, GPIO.LOW)
        self._last_uid = None
        self._last_seen = 0.0

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                uid_int, _text = self.reader.read_no_block()
                if uid_int is None:
                    time.sleep(0.2)
                    continue

                uid = format(uid_int, 'X')
                now = time.time()
                # debounce: aynı kartı SCAN_COOLDOWN_SEC içinde tekrar okuma
                if uid == self._last_uid and (now - self._last_seen) < SCAN_COOLDOWN_SEC:
                    time.sleep(0.2)
                    continue
                self._last_uid = uid
                self._last_seen = now

                print(f"[RFID] card scanned: {uid}")

                # 1) State publish (mobil app last_scan UI'ı için)
                self.mqtt_publish(f"homes/{self.home_id}/state", {
                    "deviceID": RFID_DEVICE_ID,
                    "states": [
                        {"property_name": "last_scan", "current_value": uid},
                        {"property_name": "status",    "current_value": "active"},
                    ],
                })

                # 2) Verification request — Lambda'ya
                self.mqtt_publish(f"homes/{self.home_id}/rfid_check", {
                    "homeID":   self.home_id,
                    "card_uid": uid,
                })

                time.sleep(0.5)
            except Exception as e:
                print(f"[RFID] read error: {e}")
                time.sleep(1)

    def handle_result(self, payload):
        """Called from rfid_result subscribe callback."""
        result = payload.get("result")
        label = payload.get("label")
        uid = payload.get("card_uid")
        print(f"[RFID] verify result for {uid}: {result} ({label})")

        if result == "authorized":
            print("[RFID] -> opening barrier")
            self.servo.open_for()
            # State publish: barrier=open
            self.mqtt_publish(f"homes/{self.home_id}/state", {
                "deviceID": RFID_DEVICE_ID,
                "states": [
                    {"property_name": "barrier", "current_value": "open"},
                ],
            })
        else:
            print("[RFID] -> denied (push notif Lambda tarafından gitti)")
            if self.denied_led_pin is not None:
                GPIO.output(self.denied_led_pin, GPIO.HIGH)
                threading.Timer(
                    2.0,
                    lambda: GPIO.output(self.denied_led_pin, GPIO.LOW),
                ).start()
```

### 4.3 Driver'ları başlat — AWS connect SONRASI

`alert.py`'da `mqtt_connection` hazır olduktan sonra:

```python
HOME_ID = "757bfcc9-a80b-4886-a8cd-854392454caf"

def mqtt_publish_helper(topic, payload):
    """Wrapper — RfidDriver bunu kullanacak."""
    mqtt_connection.publish(
        topic=topic,
        payload=json.dumps(payload),
        qos=mqtt.QoS.AT_LEAST_ONCE,
    )

# Hardware init
servo = ServoDriver()
rfid = RfidDriver(mqtt_publish_helper, HOME_ID, servo)
rfid.start()
```

### 4.4 `rfid_result` topic'ine subscribe

Mevcut `command` subscribe'ının yanına ekle:

```python
def on_rfid_result(topic, payload, **kwargs):
    try:
        data = json.loads(payload.decode())
        rfid.handle_result(data)
    except Exception as e:
        print(f"[RFID] result parse error: {e}")

mqtt_connection.subscribe(
    topic=f"homes/{HOME_ID}/rfid_result",
    qos=mqtt.QoS.AT_LEAST_ONCE,
    callback=on_rfid_result,
)
print(f"[INFO] subscribed to homes/{HOME_ID}/rfid_result")
```

### 4.5 Servisi restart

```bash
sudo systemctl restart akilliev.service
sudo journalctl -u akilliev.service -f
```

Boot'ta görmen gereken satırlar:
- `[INFO] subscribed to homes/757bfcc9.../command`
- `[INFO] subscribed to homes/757bfcc9.../rfid_result`     ← yeni
- `[INFO] subscribed to homes/757bfcc9.../rfid_register`   (varsa, opsiyonel)
- ServoDriver 0°'ye konumlandı (donanım klik sesi)

---

## 5. Test akışı

### 5.1 İlk kart okutma — UID yakala

Maketteki RFID okuyucusuna **gerçek bir kart** yaklaştır. Log'da şöyle bir satır görmen lazım:

```
[RFID] card scanned: A1B2C3D4
[RFID] verify result for A1B2C3D4: denied (None)
[RFID] -> denied
```

`denied` çıkması beklenen — kart henüz `authorized_cards` tablosunda yok. Telefonuna **"🔒 Yetkisiz Giriş Denemesi"** push bildirimi gelmiş olmalı (kullanıcıya teyit ettir).

UID'i kullanıcıya söyle (`A1B2C3D4` gibi). **Kullanıcı bu UID'i bana iletecek**, ben bulut tarafında DB'ye row olarak ekleyeceğim.

### 5.2 Aynı karti tekrar okut — bariyer açılmalı

Bulut tarafından kart eklendikten sonra (kullanıcı bana UID'i verdiğinde script'le ekleyeceğim) tekrar okut:

```
[RFID] card scanned: A1B2C3D4
[RFID] verify result for A1B2C3D4: authorized (Emre)
[RFID] -> opening barrier
```

Servo 0° → 90° dönmeli, 5sn sonra 0°'ye geri dönmeli.

### 5.3 Random/yetkisiz kart — denied + push

Başka bir kart deneyin: `denied` dönmeli, kırmızı LED 2 saniye yanmalı, push bildirim telefona gelmeli.

### 5.4 Hızlı tekrar okutma testi (debounce)

Aynı kartı 1 saniye içinde 5 kez okut: log'da sadece 1 kez `card scanned` görmen lazım (cooldown 2sn).

---

## 6. Dikkat edilmesi gereken yerler

### 6.1 systemd kullanıcısı

`alert.py` zaten `akilliev.service` altında çalışıyor. Servis kullanıcısı **root değilse**, MFRC522 SPI erişimi için `gpio` ve `spi` gruplarına dahil olmalı:

```bash
sudo usermod -a -G spi,gpio ramazan
```

Pi'de zaten varsa atla. SPI permission denied hatası alırsan grup yetkisi eksiktir.

### 6.2 Aynı GPIO pin çakışması

Mevcut alert.py'da kullanılan GPIO pin'leri:
- TV LED (16), Oven LED (27), AC LED (22), LED Strip data (21)

Yeni eklenenler:
- Servo (12), Denied LED (5), MFRC522 RST (25), MFRC522 SPI (8/9/10/11)

Çakışma yok ama eklemeden önce `GPIO.setup(...)` çağrısı bir kez geçtiğinden emin ol.

### 6.3 IoT bağlantı koparsa

Mevcut `alert.py`'da reconnect mantığı varsa (büyük ihtimalle var) — RFID akışı da bu sayede otomatik toparlanır. Subscribe callback'ler reconnect sonrası tekrar register edilmeli; mevcut kod bunu nasıl yapıyorsa aynı pattern'i takip et.

### 6.4 Yarış durumu — 2 kartın hızlı okutulması

A kartı için `rfid_check` publish, hemen ardından B kartı publish edilirse bulut iki cevabı sırasız gönderebilir. `handle_result` çağrılan UID'i ne ise onu işliyor — yarış riski yok ama `card_uid` kontrolü yapmak istersen şunu ekle:

```python
if uid != self._last_uid:
    print(f"[RFID] stale result for {uid}, ignored")
    return
```

Maket için zorunlu değil, "nice to have".

### 6.5 Servo açıları

Manuel test ederken hangi açıların kapalı/açık olduğunu doğrula. Ters çıkarsa kodda swap et:
```python
SERVO_OPEN_DEG   = 0   # eskiden 90
SERVO_CLOSED_DEG = 90  # eskiden 0
```

---

## 7. Tamamlanınca

Bana **`"RFID hazır, kart okutuyorum, UID: <gerçek_UID>"`** diye yaz. Ben:

1. PC'mden o UID'i `authorized_cards` tablosuna `INSERT` edeceğim (script var, ~10 saniye)
2. Sana "kartını tekrar okut" diyeceğim
3. Beraber log'larda `authorized` cevabını ve servoyu izleyeceğiz
4. Sonra random bir kart denersin, `denied` + push bildirimi telefona gelmeli

End-to-end test böyle olur. Maket bariyeri, mobil app, AWS, DB — hepsi çalışıyor olmalı.
