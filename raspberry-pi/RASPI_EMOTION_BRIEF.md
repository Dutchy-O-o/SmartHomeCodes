# Raspberry Pi — Emotion Inference Service (Pi Primary Path)

> **This brief documents the hardened FastAPI emotion service on the Pi.**
> The Pi is the **primary** emotion-inference path for the mobile app; the
> AWS service exists only as a fallback that the mobile client switches to
> when `/healthz` is unreachable. Nothing in this service implements
> failover — the contract just has to be predictable enough that the mobile
> client can decide in under 2 seconds.

---

## 0. Where this service sits in the system

```
                        ┌─────────────────────────────┐
                        │  Mobile app                 │
                        │  (timeouts: 2 s connect,    │
                        │   4 s total)                │
                        └──────────────┬──────────────┘
                                       │ HTTPS (LAN, self-signed)
                          GET /healthz │ POST /predict
                                       ▼
                        ┌─────────────────────────────┐
                        │  Pi: uvicorn + FastAPI      │
                        │  ramazan.local:8000         │
                        │  Keras FER model (7-class)  │
                        └──────┬──────────────┬───────┘
                               │              │
                  fire-and-forget         best-effort
                               │              │
                               ▼              ▼
              POST /prod/{homeId}/   DynamoDB UserEmotionsLog
              emotion/telemetry      (existing)
              (cloud summary)
```

**Pi's job, in 2 lines:**
1. On `POST /predict` — validate JPEG → detect face → run Keras model → respond inside 3 s or fail clean with `model_unavailable`.
2. On `GET /healthz` — answer in <50 ms with no model work, so the mobile client can probe LAN reachability before committing to a predict call.

---

## 1. Identity & configuration (no hardcoded IDs)

This is an industrial product. `home_id`, `device_id`, and the cloud
telemetry endpoint are read from `/etc/smarthome/pi.toml` at boot:

```toml
[device]
home_id   = "757bfcc9-a80b-4886-a8cd-854392454caf"
device_id = "raspi-livingroom-1"     # optional; falls back to CPU serial
hostname  = "ramazan.local"          # informational

[cloud]
telemetry_url = "https://abcd1234.execute-api.eu-central-1.amazonaws.com"
enabled       = true
```

If the file is missing, `pi_config.py` derives:
- `device_id` from `/proc/cpuinfo` Serial (`raspi-<last8>`),
- `home_id` from the legacy `Desktop/python/home.conf` `homeid=` line, else a deterministic UUID5 from the CPU serial,
- `hostname` defaults to `ramazan.local`.

The reference example lives at `emotion_api/pi.toml.example` — copy it into `/etc/smarthome/` during provisioning.

---

## 2. Hard contract (must match AWS fallback exactly)

### 2.1 `POST /predict`

| Field | Spec |
|---|---|
| Method | `POST` |
| URL | `https://<pi>:8000/predict` |
| Body | `multipart/form-data` with field `image` |
| Image | JPEG only, magic bytes `FF D8 FF`, ≤ 5 MB |

**200 response:**
```json
{
  "status": "success",
  "emotion": "happy",
  "confidence": 0.83,
  "all_scores": {
    "angry": 0.02, "disgust": 0.00, "fear": 0.01, "happy": 0.83, "sad": 0.05,
    "surprise": 0.07, "neutral": 0.02, "calm": 0.0, "excited": 0.0, "tired": 0.0
  },
  "source": "pi",
  "model_version": "1.0.0-fer7",
  "latency_ms": 142
}
```

`status`, `emotion`, `confidence`, `all_scores` are kept verbatim for backward
compatibility — the mobile client keeps reading them for one release. `source`,
`model_version`, `latency_ms` are the new fields.

### 2.2 Error responses (`4xx` / `503`)

```json
{ "status": "error", "code": "<machine>", "message": "<human>" }
```

| code | http | trigger |
|---|---|---|
| `invalid_image` | 400 | missing/empty/oversize body, non-JPEG magic, decode failure |
| `no_face` | 400 | Haar cascade returned 0 faces |
| `model_unavailable` | 503 | inference exceeded 3 s, or Keras raised |
| `rate_limited` | 429 | reserved — not currently emitted by the Pi |

### 2.3 `GET /healthz`

Returns in **<50 ms** without touching the model. No auth.

```json
{ "status": "ok", "model_loaded": true, "uptime_s": 142, "model_version": "1.0.0-fer7" }
```

Mobile clients call `/healthz` first; a slow or failing probe is the signal to fall back to AWS.

---

## 3. Label vocabulary mismatch (read this once)

The deployed Keras model is the standard **7-class FER**:
`angry, disgust, fear, happy, neutral, sad, surprise`.

The contract requires the full **10-class** vocabulary including `calm,
excited, tired`. We do **not** retrain. Instead, `model.py` always emits all
10 keys in `all_scores`, with the three classes the FER model cannot
produce reported as `0.0`. `emotion` is always one of the 7 model classes.

`MODEL_VERSION = "1.0.0-fer7"` — bump this string when the model file
changes. The `-fer7` suffix is a deliberate marker that the underlying
model is 7-class.

---

## 4. Implementation layout

```
emotion_api/
├── main.py             FastAPI app, /healthz, /predict, error handlers
├── model.py            Keras inference + MODEL_VERSION + 10-label remap
├── detector.py         OpenCV Haar cascade face detector (unchanged)
├── pi_config.py        /etc/smarthome/pi.toml + hardware-derived fallbacks
├── logging_config.py   JSON-line logger, 50 MB rotation, 5 backups
├── telemetry.py        Bounded asyncio.Queue → AWS fire-and-forget POST
├── pi.toml.example     Reference config
├── Dockerfile          uvicorn HTTPS, /var/log/smarthome volume
└── models/emotion_model.keras
```

### 4.1 Inference timeout
`main.py` wraps the synchronous Keras call in `asyncio.wait_for(...,
timeout=3.0)` on a worker thread. On expiry, the request is aborted and
the client sees `code: "model_unavailable"`, status `503`. The worker
thread keeps running (we cannot kill TF mid-graph) but the socket is
released — this is exactly the behaviour mobile failover needs.

### 4.2 Structured logging
Every `/predict` outcome is one JSON line written to
`/var/log/smarthome/emotion.jsonl`:

```json
{"event":"predict","ts":"2026-05-15T22:51:03+0000","level":"info",
 "emotion":"happy","confidence":0.83,"latency_ms":142,
 "image_bytes":18437,"client_ip":"192.168.1.42"}
```

`RotatingFileHandler` cuts the file at 50 MB and keeps 5 backups
(`emotion.jsonl.1` … `emotion.jsonl.5`). If `/var/log/smarthome/` isn't
writable, the logger falls back to stderr so the container still boots.

### 4.3 Cloud telemetry queue
Only fires on **successful** predictions. `telemetry.py` exposes
`enqueue(emotion, confidence)`; the producer is a non-blocking
`put_nowait` against an `asyncio.Queue(maxsize=64)`. A single background
worker drains it and POSTs `{home_id, emotion, confidence, ts}` to
`<telemetry_url>/prod/{home_id}/emotion/telemetry`. Worker HTTP timeout is
2 s. When the queue is full the event is dropped and a
`telemetry_dropped` line is logged — the predict response is **never**
delayed by cloud latency.

Telemetry is opt-in: it stays disabled unless `cloud.enabled = true` in
`pi.toml`.

---

## 5. Test flow

### 5.1 Health probe
```bash
curl -k --max-time 1 https://ramazan.local:8000/healthz
# {"status":"ok","model_loaded":true,"uptime_s":...,"model_version":"1.0.0-fer7"}
```

### 5.2 Happy-path predict
```bash
curl -k -F image=@happy.jpg https://ramazan.local:8000/predict
# 200 with emotion: "happy", confidence > 0.6
```

### 5.3 Garbage bytes
```bash
echo "not an image" > nonsense.jpg
curl -k -F image=@nonsense.jpg https://ramazan.local:8000/predict
# 400 {"status":"error","code":"invalid_image",...}
```

### 5.4 Faceless image
```bash
curl -k -F image=@black.jpg https://ramazan.local:8000/predict
# 400 {"status":"error","code":"no_face",...}
```

### 5.5 Network-cable pull
`/healthz` must still answer — it touches no remote services.

### 5.6 Telemetry verification
With `cloud.enabled = true`, tail `emotion.jsonl` and watch for
`event=predict` followed by a successful POST (or `telemetry_post_failed`
if the cloud endpoint is unreachable). Either way, response times for the
caller stay under the 3 s ceiling.

---

## 6. Gotchas

### 6.1 mDNS hostname is `ramazan.local`
Avahi advertises the Pi as `ramazan.local`. The mobile app discovery code
expects this exact name. If you have to rename, update both the
`/etc/hostname` and the mobile client's bonjour service name — and edit
the **Hostname** line in this brief.

### 6.2 The self-signed cert
The mobile client does **NOT** pin this cert. In `kDebugMode` the Flutter
app installs a permissive `badCertificateCallback` that accepts any cert;
release builds use a default `http.Client` that will **REJECT** this
self-signed cert. Therefore: a release build of the mobile app cannot
talk to this Pi over HTTPS as currently configured.

Resolution before any release: either (a) install a CA-signed cert
(Let's Encrypt DNS-01 with a real hostname), or (b) add cert pinning to
the mobile client, or (c) ship the LAN service over plain HTTP and rely
on network isolation. Do not regenerate the existing self-signed cert
without coordinating with the mobile team.

### 6.3 First request after boot is slow
TensorFlow's first `predict()` after process start can take 1–2 s while
the graph specialises. The 3 s timeout is set with that headroom in
mind; if you reduce it, do a warm-up call inside `_startup()` first.

### 6.4 7-class model in a 10-class contract
See §3. Do **not** read `all_scores["calm"]` and expect signal — it is
always 0.0 from the Pi until the model is retrained.

### 6.5 Log directory permissions
Inside Docker, `/var/log/smarthome` must be a writable mount. The
recommended `docker run` flags:
```
-v /etc/smarthome:/etc/smarthome:ro
-v /var/log/smarthome:/var/log/smarthome
```

### 6.6 DynamoDB writes are best-effort
The legacy `UserEmotionsLog` PutItem is offloaded to a background task.
Failures are logged but never affect the predict response. `device_id`
and `home_id` are now sourced from `pi.toml`, not hardcoded.

---

## 7. Handoff checklist

When you finish, report:
- `model_version` string (currently `1.0.0-fer7`).
- p50 / p95 `latency_ms` over 50 sample images.
- The exact `/healthz` response body.
- Confirmation that `pi.toml.example` exists and `/etc/smarthome/pi.toml`
  is documented as the production source of truth.

---

## 8. MQTT Relay Subscriber (cloud-routed inference)

The LAN HTTPS path is the **primary** entry point. The relay only fires
when mobile failed to reach `ramazan.local:8000` and asked AWS to forward
the JPEG. The relay runs **in the same container**, shares the same
loaded Keras model, and goes through the same `run_inference()` callable
as `/predict`. There is exactly one inference code path.

### 8.1 Topics

```
SUBSCRIBE: homes/{home_id}/emotion/predict_request   (QoS 1)
PUBLISH:   homes/{home_id}/emotion/predict_response  (QoS 1)
```

`{home_id}` is `pi_config.load().home_id` — same value `alert.py` uses.
No hardcoded UUID.

### 8.2 Request / response payloads

**Request (AWS → Pi):**
```json
{
  "request_id": "uuid-...",
  "image_url":  "https://<bucket>.s3.amazonaws.com/...?X-Amz-...",
  "ts":         1736030000
}
```

`image_url` is a pre-signed GET URL with ~60 s validity. The Pi downloads
the JPEG with `httpx` (10 s timeout), enforces `Content-Type: image/jpeg`
and the same 5 MB ceiling the HTTPS path uses.

**Response (Pi → AWS):** the success/error dict `run_inference()` returns,
with `request_id` echoed back. Success body matches the `/predict` 200
response field-for-field (`status, emotion, confidence, all_scores,
source:"pi", model_version, latency_ms`); error body matches the error
contract (`status:"error", code, message, source:"pi"`).

### 8.3 Connection

```python
mqtt_connection_builder.mtls_from_path(
    endpoint=CFG.iot_endpoint,
    cert_filepath=".../permanent-cert.pem.crt",
    pri_key_filepath=".../permanent-private.key",
    ca_filepath=".../AmazonRootCA1.pem",
    client_id=f"{CFG.home_id}-emotion-relay",
    clean_session=False,
    keep_alive_secs=30,
)
```

`clean_session=False` plus QoS 1 means AWS IoT buffers messages we miss
during a brief disconnect and redelivers on reconnect. `awscrt` handles
reconnect with exponential backoff internally.

**`client_id` must be distinct from `alert.py`'s.** `alert.py` connects
as `client_id={home_id}`; the relay connects as
`client_id={home_id}-emotion-relay`. The thing's IoT policy must permit
`Connect` for both. If first-run fails with `CLIENT_ID_REJECTED`, widen
the policy to `Resource: client/${iot:Connection.Thing.ThingName}*`.

### 8.4 Process supervision — single process, asyncio task

The MQTT subscriber runs as an **asyncio task spawned from uvicorn's
lifespan startup hook** (`main.lifespan`). Single Python process, single
TF graph, one resident model — RSS stays at ~510 MB instead of doubling
to ~1 GB. The relay shares uvicorn's event loop, so subscriber callbacks
**must not block**; the CPU-heavy `run_inference()` is dispatched via
`asyncio.to_thread`.

If `relay_subscriber.start()` raises mid-flight, an outer `while True:
try ... except ... await asyncio.sleep(5)` loop logs the failure and
reconnects without killing uvicorn. awscrt's in-session reconnect
handles brief network blips invisibly; the outer loop covers the
"initial connect failed at boot" case and any unexpected exceptions.

Docker's `--restart unless-stopped` still survives Pi reboot.

### 8.5 Run-time bind mounts

```
-v /etc/smarthome:/etc/smarthome:ro                            (pi.toml)
-v /home/ramazan/Desktop/python/certs:/app/certs:ro            (AWS IoT thing cert)
-v /var/log/smarthome:/var/log/smarthome                       (JSON-line logs)
```

### 8.6 What lands in the log on each relay request

One JSON line per outcome on `/var/log/smarthome/emotion.jsonl`:

```json
{"event":"relay_predict","request_id":"...","status":"success",
 "emotion":"happy","confidence":0.83,"latency_ms":142,"image_bytes":18437}
```

Failure paths emit `relay_invalid_payload`, `relay_download_failed`,
`relay_timeout`, etc. with the same `request_id`.

### 8.7 Crash isolation

There is no external supervisor. If `relay_subscriber.start()` ever
crashes in a way the inner try/except can't catch, uvicorn keeps
serving `/predict` and `/healthz` — the LAN path stays healthy — but
the relay is dead until the next process restart (Docker
`--restart unless-stopped` handles that on actual exit). Watch the JSON
log for `relay_error` and `relay_reconnecting` lines to confirm the
self-heal worked.
