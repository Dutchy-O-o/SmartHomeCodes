# Raspberry Pi — Akıllı Hoparlör (Raspotify + MQTT)

> **Bu brief Raspi'deki Claude Code'a yapıştırılır.** Bulut tarafı zaten
> kurulu — `devices` tablosunda speaker satırı mevcut, mobil app komut
> gönderiyor. Raspi'nin görevi: Raspotify ile Pi'yi Spotify Connect hedefi
> yapmak, ALSA volume + servisi MQTT komutlarıyla yönetmek, durumu state
> topic'ine raporlamak. Mevcut `alert.py`'a entegre edilecek, yeni servis
> YOK (Raspotify hariç — o zaten ayrı bir systemd servisi).
>
> **Endüstriyel kural:** Bu Pi başka eve / başka donanıma takıldığında
> *kod değişikliği gerektirmemeli*. İsim yaşam döngüsü:
>
> 1. **İlk açılış (env dosyası YOK)** — alert.py `/etc/machine-id`'den 6 karakter
>    alır, `AkilliEv-a3f9c2` gibi bir isim üretir, **`/var/lib/akilliev/raspotify.env`
>    dosyasına yazar.**
> 2. **Sonraki tüm açılışlar (env dosyası VAR)** — alert.py sadece env dosyasını okur.
>    `/etc/machine-id`'e dokunmaz, yeniden türetmez. SD kart kopyalansa, machine-id
>    değişse bile isim sabit kalır.
>
> Yani machine-id "factory provisioning" anı — bir kez okunur, sonra unutulur.

---

## 0. Mimari özet

```
Telefondaki Spotify uygulaması ─┐
                                ├─► Pi (Raspotify) ─► USB Ses Kartı ─► Hoparlör
Mobil app (5 mood şarkısı tap) ─┘
       │
       │ (manuel: telefondan Spotify'da Pi'yi seç → çal)
       │ (otomatik: mood track tap → /me/player/play?device_id=<Pi>)
       │
       └── Pi adı: AkilliEv-<6char>  (örn. AkilliEv-a3f9c2)
           İLK açılışta alert.py /etc/machine-id'den türetir →
           /var/lib/akilliev/raspotify.env'e yazar (kalıcı).
           SONRAKİ açılışlarda env dosyasından okunur.
           Mobil app "AkilliEv-" prefix'iyle Spotify cihaz listesinde arar.

Mobil app (güç / ses kontrolü) ──► API GW ──► PublishCommandToPi Lambda
                                                   │
                                                   ▼
                                  homes/{HOME}/command (MQTT)
                                                   │
                                                   ▼
                                      SEN dinliyorsun (zaten subscribe)
                                                   │
                                                   ▼
                                  systemctl raspotify ↔ amixer set Master
                                                   │
                                                   ▼
                                  homes/{HOME}/state (durumu yayınla)
```

**Senin işin 2 kalem:**
1. **Raspotify kur** + alert.py'nın yazacağı env dosyasını okuyacak şekilde systemd override koy. Adı **alert.py** yönetir; ilk açılışta üretip kalıcı env dosyasına yazar, sonra hep oradan okur. Senin elle isim koyman gerekmiyor.
2. **Mevcut `command` callback'ine speaker dispatcher ekle** — `power on/off` ve `volume <0-100>` komutlarını işle, sonra state publish et.

---

## 1. Mevcut bulut altyapısı (zaten kuruldu, dokunma)

| Kaynak | Detay | Durum |
|---|---|---|
| `devices` row (speaker) | DB'de var, type=`speaker`. **DeviceID hardcoded değil — Pi discovery ile bulacak** | ✅ |
| `actuator_properties`: `power`, `volume`, `playback` | speaker'a bağlı | ✅ |
| Discovery: IoT rule `getHomeDevices` (homes/+/devices/get → Lambda → response) | aktif | ✅ |
| MQTT topic rule `saveDeviceState` (homes/+/state → DB) | aktif | ✅ |
| `PublishCommandToPi` Lambda (mobil → command topic) | aktif | ✅ |
| PiPolicy `homes/{HOME}/*` pub+sub | aktif | ✅ |

**Yeni AWS kaynağı yok. Sadece kod + Raspotify paketi.**

---

## 2. MQTT kontratı (kesin payload formatları)

### 2.1 Sen DİNLİYORSUN: `homes/{HOME_ID}/command`

Mevcut command subscribe'ı zaten var (TV/AC/Stove için). Aynı topic'e yeni bir `deviceID` daha düşecek. Payload formatı (mevcut TV/AC ile **birebir aynı** — `property_name` alanı kullanılıyor, `type` değil):

```json
{
  "deviceID": "<SPEAKER_DEVICE_ID — discovery ile bulunacak>",
  "commands": [
    { "property_name": "power",  "value": "on" }
  ]
}
```

veya

```json
{
  "deviceID": "<SPEAKER_DEVICE_ID — discovery ile bulunacak>",
  "commands": [
    { "property_name": "volume", "value": 65 }
  ]
}
```

`property_name` değerleri:
- `"power"` → value `"on"` veya `"off"` → systemctl start/stop raspotify
- `"volume"` → value 0-100 (int veya str) → `amixer set Master <value>%`

`"playback"` (play/pause/next/prev) — **şu an Pi tarafında işlenmeyecek.** Mobil
app bunu Spotify Web API üzerinden direkt halledecek (kullanıcının kendi token'ıyla).
Pi'ye gelirse, log'a yazıp atla.

### 2.2 Sen YAYINLIYORSUN: `homes/{HOME_ID}/state`

Komutu uyguladıktan SONRA gerçekleşen durumu yayınla (echo). Mevcut TV/AC pattern'iyle aynı:

```json
{
  "deviceID": "<SPEAKER_DEVICE_ID — discovery ile bulunacak>",
  "states": [
    { "property_name": "power",  "current_value": "on" }
  ]
}
```

```json
{
  "deviceID": "<SPEAKER_DEVICE_ID — discovery ile bulunacak>",
  "states": [
    { "property_name": "volume", "current_value": "65" }
  ]
}
```

### 2.3 İlk açılışta state publish

Boot'ta gerçek durumu yayınla:

```json
{
  "deviceID": "<SPEAKER_DEVICE_ID — discovery ile bulunacak>",
  "states": [
    { "property_name": "power",  "current_value": "off" },
    { "property_name": "volume", "current_value": "<amixer'dan oku>" }
  ]
}
```

---

## 3. Raspotify kurulumu (adı alert.py yönetir, sen sadece paketi kur)

### 3.1 Paketi yükle

```bash
sudo apt update
sudo apt install -y curl
curl -sSL https://dtcooper.github.io/raspotify/install.sh | sh
```

### 3.2 USB ses kartını seç

```bash
aplay -l
```

USB Audio satırının `card N` numarasını ve **isim** alanını not al, örn:
```
card 1: Device [USB Audio Device], device 0: ...
```
→ ALSA tanımlayıcısı: `plughw:CARD=Device,DEV=0`

### 3.3 Persistent config dizini

Bu sadece **alert.py'nın yazacağı env dosyası için boş bir dizin**. İçeriği elle yazmıyoruz — alert.py ilk çalıştığında dosyayı kendi oluşturur (`/var/lib/akilliev/raspotify.env`). Sonraki açılışlarda dosya zaten var, alert.py değiştirmiyor.

```bash
sudo mkdir -p /var/lib/akilliev
sudo chown ramazan:ramazan /var/lib/akilliev    # alert.py kullanıcısı yazabilsin
```

> Kullanıcı adı `ramazan` değilse uyumla.

### 3.4 systemd override — raspotify env dosyasını /var/lib'den okusun

```bash
sudo mkdir -p /etc/systemd/system/raspotify.service.d
sudo tee /etc/systemd/system/raspotify.service.d/override.conf > /dev/null <<'EOF'
[Service]
EnvironmentFile=
EnvironmentFile=/var/lib/akilliev/raspotify.env
EOF

sudo systemctl daemon-reload
sudo systemctl disable raspotify    # boot'ta otomatik başlamasın
sudo systemctl stop raspotify       # şu an durdur
```

> İlk boş `EnvironmentFile=` default `/etc/raspotify/conf`'u temizliyor. Sonra
> bizim user-writable dosyaya yönlendiriyoruz. Sudo şart değil çünkü alert.py
> (`ramazan`) `/var/lib/akilliev/raspotify.env`'i kendi yazıyor.
>
> `disable` → boot'ta otomatik başlatma yok. Power komutu gelince alert.py start ediyor.

### 3.5 Manuel test (alert.py entegrasyonundan SONRA yapılacak)

Bu adım §4 bittikten sonra yapılır. alert.py ilk başlatıldığında env dosyasını yazar; ondan sonra raspotify çalıştırılabilir.

---

## 4. `alert.py` entegrasyonu

### 4.1 Sabitler — dosyanın başına

**HARDCODED DEVICE ID YOK.** Speaker'ın deviceID'si Pi'nin zaten yaptığı
device discovery (`homes/{HOME}/devices/get` → `homes/{HOME}/devices/response`)
ile bulunacak — TV/AC/Stove/RFID için ne yapıyorsan aynısı.

```python
import os
import subprocess

RASPOTIFY_SERVICE  = "raspotify"
RASPOTIFY_ENV_PATH = "/var/lib/akilliev/raspotify.env"
ALSA_MIXER         = "Master"

# Default env values — yalnız ilk açılışta yazılır, sonra dokunulmaz
RASPOTIFY_DEFAULTS = {
    "LIBRESPOT_DEVICE":         "plughw:CARD=Device,DEV=0",
    "LIBRESPOT_BITRATE":        "160",
    "LIBRESPOT_INITIAL_VOLUME": "50",
    "LIBRESPOT_VOLUME_CTRL":    "fixed",
}
```

### 4.2 SpeakerDriver class'ı — `RfidDriver`'ın altına ekle

```python
class SpeakerDriver:
    """Raspotify systemd + ALSA volume wrapper, MQTT komutlarıyla sürülür.
    İlk açılışta /var/lib/akilliev/raspotify.env'i seed eder (machine-id'den
    türetilen unique LIBRESPOT_NAME ile); sonraki açılışlarda env dosyası
    olduğu gibi okunur — kullanıcı elle düzenlerse o tercih kalır."""

    def __init__(self, mqtt_publish, home_id, device_id):
        """device_id mevcut device discovery akışından gelir (TV/AC ile aynı yol).
        alert.py homes/{HOME}/devices/response dinleyip device_type=='speaker'
        olanın deviceid'sini bu constructor'a geçirir."""
        self.mqtt_publish = mqtt_publish
        self.home_id = home_id
        self.device_id = device_id
        self.spotify_name = self._ensure_raspotify_env()
        print(f"[SPEAKER] deviceID={self.device_id} | Spotify adı={self.spotify_name}")

    # ---- env file lifecycle ----

    def _ensure_raspotify_env(self):
        env = self._read_env(RASPOTIFY_ENV_PATH)
        if env.get("LIBRESPOT_NAME"):
            return env["LIBRESPOT_NAME"]

        # İlk açılış — adı türet ve dosyayı seed et
        try:
            with open("/etc/machine-id") as f:
                short = f.read().strip()[:6]
        except Exception:
            short = "unknown"
        name = f"AkilliEv-{short}"

        full = dict(RASPOTIFY_DEFAULTS)
        full["LIBRESPOT_NAME"] = name
        # Mevcut dosyada elle eklenmiş bir şey varsa koru
        full.update({k: v for k, v in env.items() if k not in full})
        self._write_env(RASPOTIFY_ENV_PATH, full)
        print(f"[SPEAKER] env seed edildi: {name}")
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

    # ---- command handlers ----

    def handle_power(self, value):
        if value == "on":
            subprocess.run(["sudo", "systemctl", "start", RASPOTIFY_SERVICE], check=False)
        else:
            subprocess.run(["sudo", "systemctl", "stop", RASPOTIFY_SERVICE], check=False)
        self._publish_state("power", value)

    def handle_volume(self, value):
        v = max(0, min(100, int(value)))
        subprocess.run(["amixer", "-q", "set", ALSA_MIXER, f"{v}%"], check=False)
        self._publish_state("volume", str(v))

    # ---- state ----

    def _publish_state(self, prop, value):
        self.mqtt_publish(f"homes/{self.home_id}/state", {
            "deviceID": self.device_id,
            "states": [{"property_name": prop, "current_value": value}],
        })

    def publish_initial_state(self):
        is_active = subprocess.run(
            ["systemctl", "is-active", RASPOTIFY_SERVICE],
            capture_output=True, text=True,
        ).stdout.strip()
        power = "on" if is_active == "active" else "off"
        vol = self._read_volume()
        self.mqtt_publish(f"homes/{self.home_id}/state", {
            "deviceID": self.device_id,
            "states": [
                {"property_name": "power",  "current_value": power},
                {"property_name": "volume", "current_value": vol},
            ],
        })

    def _read_volume(self):
        try:
            out = subprocess.run(
                ["amixer", "get", ALSA_MIXER],
                capture_output=True, text=True, check=True,
            ).stdout
            for line in out.splitlines():
                if "%" in line:
                    return line.split("[")[1].split("%")[0]
        except Exception:
            pass
        return "50"
```

### 4.3 Mevcut `on_command` callback'ine speaker dispatch ekle

`alert.py`'da `command` topic'inden gelen mesajları işleyen mevcut callback'in TV/AC/Stove dispatcher'ının yanına ekle:

```python
if speaker is not None and device_id == speaker.device_id:
    for cmd in commands:
        # Mevcut TV/AC pattern: command anahtarı 'property_name'
        prop = cmd.get("property_name") or cmd.get("type")
        v = cmd.get("value")
        if prop == "power":
            speaker.handle_power(v)
        elif prop == "volume":
            speaker.handle_volume(v)
        elif prop == "playback":
            # Mobil tarafı Spotify Web API ile direkt halledecek;
            # Pi'de işlenmiyor.
            print(f"[SPEAKER] playback command ignored (handled by mobile): {v}")
        else:
            print(f"[SPEAKER] unknown command property: {prop}")
```

### 4.4 Driver'ı başlat — devices discovery'sinden SONRA

`alert.py` zaten `homes/{HOME_ID}/devices/get` topic'ine publish edip
`homes/{HOME_ID}/devices/response`'tan cevap dinliyor (TV/AC/RFID için).
O response handler'ında speaker'ı da bul ve driver'ı başlat:

```python
HOME_ID = "757bfcc9-a80b-4886-a8cd-854392454caf"
speaker = None  # global, on_command callback'i de bunu görmeli

def on_devices_response(topic, payload, **kwargs):
    """Mevcut devices/response handler — TV/AC için olan kısma speaker'ı ekle."""
    global speaker
    try:
        data = json.loads(payload.decode())
        for d in data.get("devices", []):
            if d.get("device_type") == "speaker":
                if speaker is None:
                    speaker = SpeakerDriver(
                        mqtt_publish=mqtt_publish_helper,
                        home_id=HOME_ID,
                        device_id=d["deviceid"],
                    )
                    speaker.publish_initial_state()
                    print("[INFO] speaker driver ready")
                # Mevcut TV/AC/Stove kayıt mantığı zaten burada — değiştirme
            # ... mevcut TV/AC/Stove/RFID branchları olduğu gibi kalsın
    except Exception as e:
        print(f"[CMD] devices/response parse error: {e}")
```

> Eğer alert.py'da devices'ı bir dict'e cache'leyen bir mekanizma zaten varsa
> (`device_id_by_type` gibi), `SpeakerDriver`'ı sadece `device_id_by_type["speaker"]`
> ile başlat. Yeni bir parse logic'i kurma — sen bilirsin Pi'deki gerçek pattern'i.

### 4.5 systemd kullanıcı yetkisi (sudo passwordless)

`alert.py` `akilliev.service` kullanıcısı (muhtemelen `ramazan`) — `sudo systemctl` çağrısı için passwordless sudo gerekir:

```bash
sudo visudo -f /etc/sudoers.d/akilliev-speaker
```

İçine yaz:
```
ramazan ALL=(ALL) NOPASSWD: /bin/systemctl start raspotify, /bin/systemctl stop raspotify, /bin/systemctl is-active raspotify
```

Kullanıcı adı `ramazan` değilse uyumlandır.

### 4.6 Servisi restart

```bash
sudo systemctl restart akilliev.service
sudo journalctl -u akilliev.service -f
```

Boot log'da görmen gereken yeni satırlar:
- `[SPEAKER] env seed edildi: AkilliEv-a3f9c2`  ← sadece ilk açılış
- `[SPEAKER] Spotify Connect adı: AkilliEv-a3f9c2`  ← her açılış
- `[INFO] speaker driver ready`
- Hemen ardından `homes/757bfcc9.../state` topic'ine power=off + volume=<x> publish edildi

İkinci başlatmadan itibaren "env seed edildi" satırı **kaybolacak** — env dosyası artık var, alert.py değiştirmiyor. Bu doğru davranış.

---

## 5. Test akışı

### 5.1 İlk başlatma — env seed kontrolü

`akilliev.service` restart edildikten sonra:

```bash
cat /var/lib/akilliev/raspotify.env
```

Görmen gereken (machine-id'ne göre 6 karakter farklı olacak):
```
LIBRESPOT_NAME=AkilliEv-a3f9c2
LIBRESPOT_DEVICE=plughw:CARD=Device,DEV=0
LIBRESPOT_BITRATE=160
LIBRESPOT_INITIAL_VOLUME=50
LIBRESPOT_VOLUME_CTRL=fixed
```

Yoksa `journalctl -u akilliev.service`'te `[SPEAKER] env seed edildi: ...` satırını ara — yoksa permissions / dizin sorunu var (§3.3'e dön).

### 5.2 Mobil app açılır → cihazlar listesinde mevcut "Speaker" görünür

`power=off`, `volume=<x>` (boot'ta yayınladığın değerler) gözükmeli.

### 5.3 Mobil'den power ON

UI'dan toggle aç → log:
```
[CMD] received: speaker power on
[SPEAKER] systemctl start raspotify → ok
state publish → power=on
```

`raspotify` servisi aktif olmalı: `systemctl is-active raspotify` → `active`.

`journalctl -u raspotify -n 5` → librespot `AkilliEv-a3f9c2` adıyla başlatıldığını görmen lazım (env dosyamızdan okuduğunu doğrular).

### 5.4 Telefondan Spotify → cihazlar → AkilliEv-xxxxxx → şarkı çal

USB hoparlörden ses çıkmalı. Bu noktada *raspotify çalışıyor + Spotify Connect bağlandı*.

### 5.5 Mobil'den volume slider %30

Log:
```
[CMD] received: speaker volume 30
[SPEAKER] amixer set Master 30% → ok
state publish → volume=30
```

### 5.6 Mobil'de mood scan → 5 öneri → tap

Mobil app `/me/player/devices` ile `AkilliEv-` prefix'li ilk cihazı bulur, `/me/player/play?device_id=...` ile şarkıyı tetikler. Ses Pi'den çıkmalı.

### 5.7 Mobil'den power OFF

Raspotify durmalı. Ses kesilmeli.

---

## 6. Dikkat edilmesi gereken yerler

### 6.1 USB ses kartı CARD ismi sistemden sisteme değişebilir

`RASPOTIFY_DEFAULTS` içindeki `LIBRESPOT_DEVICE="plughw:CARD=Device,DEV=0"` sabit. Eğer `aplay -l`'de senin USB kartın `Device` değil başka bir isimde (örn. `Headset`, `USB`) çıkıyorsa **iki seçenek** var:

1. alert.py'da `RASPOTIFY_DEFAULTS["LIBRESPOT_DEVICE"]` değerini düzelt, sonra `/var/lib/akilliev/raspotify.env` dosyasını sil → bir sonraki başlatmada yeniden seed olur
2. Doğrudan `/var/lib/akilliev/raspotify.env` dosyasını elle düzenle (pratik yol)

Kart **numarası** değil, **ismi** kullanılır → reboot'lar arasında stabil kalır.

Endüstriyel iyileştirme (opsiyonel): `_ensure_raspotify_env`'i öyle yaz ki ilk seed'de USB kartı `aplay -l`'den otomatik bulsun:

```python
def _detect_usb_alsa():
    try:
        out = subprocess.run(["aplay", "-l"], capture_output=True, text=True, check=True).stdout
        for line in out.splitlines():
            if "USB" in line.upper() and line.startswith("card"):
                # "card 1: Device [USB Audio Device], device 0: ..."
                card = line.split("[")[1].split("]")[0].strip()
                return f"plughw:CARD={card},DEV=0"
    except Exception:
        pass
    return "plughw:CARD=Device,DEV=0"  # fallback
```

### 6.2 ALSA mixer kontrolü kart numarasına bağlı

`amixer set Master ...` default kartı kullanır. USB kartı default değilse:

```bash
amixer -c 1 set Master 50%
```

veya `/etc/asound.conf` ile USB'yi default yap:
```
defaults.pcm.card 1
defaults.ctl.card 1
```

### 6.3 Boot'ta otomatik başlatma kapalı kalmalı

`sudo systemctl disable raspotify` → power komutuyla yönetilecek. Asla `enable` etme.

### 6.4 raspotify ilk başlatmada gecikme

`systemctl start raspotify` → Spotify Connect ağında görünmesi 2-5sn alabilir. Power=on echo'su hemen gider, ama mood track tap'ı hemen yapılırsa "device not found" alabilir mobil. Kullanıcıya SnackBar ("Hoparlör Spotify ağında bulunamadı — Pi açık mı?") gösteriliyor, sorun değil.

### 6.5 İki farklı speaker DB satırı yok

DB'de tek bir speaker satırı var. Daha önce kazara eklenen duplicate silindi. Hem mobil hem Pi bu satıra discovery ile ulaşıyor (hardcoded UUID yok).

---

## 7. Tamamlanınca

Bana şunu söyle:
1. ✅ `cat /var/lib/akilliev/raspotify.env` çıktısı — Pi adı ne olarak seed oldu? (örn. `AkilliEv-a3f9c2`)
2. ✅ `alert.py` SpeakerDriver entegre, journalctl'de `[SPEAKER] deviceID=... | Spotify adı=...` ve `[INFO] speaker driver ready` görünüyor mu? (deviceID discovery'den geliyor olmalı, hardcoded değil)
3. ✅ Telefondan Spotify Connect listesinde bu isim görünüyor + manuel şarkı çalıyor mu?

Sonra beraber 5.3 → 5.7 testini yapacağız. Mobil tarafı paralel hazır (Spotify cihaz listesinde `AkilliEv-` prefix'iyle Pi'yi otomatik buluyor).
