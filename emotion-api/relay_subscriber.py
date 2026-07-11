"""MQTT relay subscriber.

Runs as an asyncio task spawned from uvicorn's lifespan hook in main.py,
sharing the single TensorFlow graph already loaded for `/predict`. When
mobile can't reach the LAN, AWS publishes the JPEG URL on
`homes/{home_id}/emotion/predict_request`; this task downloads, runs
`run_inference()`, and publishes the response on the sibling topic.

awscrt's MQTT client auto-reconnects internally. The outer `start()`
loop is a second line of defense: if the initial connect fails (e.g.
network down at boot) or any other exception bubbles out of the inner
serve loop, we log it and try again after 5 s.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from awscrt import mqtt
from awsiot import mqtt_connection_builder

from logging_config import log_event
from main import INFERENCE_TIMEOUT_S, MAX_IMAGE_BYTES, run_inference
import pi_config

CFG = pi_config.load()
CLIENT_ID = f"{CFG.home_id}-emotion-relay"
REQUEST_TOPIC = f"homes/{CFG.home_id}/emotion/predict_request"
RESPONSE_TOPIC = f"homes/{CFG.home_id}/emotion/predict_response"
DOWNLOAD_TIMEOUT_S = 10.0
KEEP_ALIVE_S = 30
RECONNECT_DELAY_S = 5.0

_mqtt_conn = None
_stopped = False


def _publish(payload: dict) -> None:
    if _mqtt_conn is None:
        return
    _mqtt_conn.publish(
        topic=RESPONSE_TOPIC,
        payload=json.dumps(payload, separators=(",", ":")),
        qos=mqtt.QoS.AT_LEAST_ONCE,
    )


def _publish_error(request_id, code: str, message: str) -> None:
    _publish({
        "request_id": request_id,
        "status": "error",
        "code": code,
        "message": message,
        "source": "pi",
    })


async def _handle(payload_bytes: bytes, http_client: httpx.AsyncClient) -> None:
    request_id = None
    try:
        req = json.loads(payload_bytes)
        request_id = req.get("request_id")
        image_url = req.get("image_url")
    except Exception:
        log_event("relay_invalid_payload", raw=payload_bytes[:200].decode("utf-8", "replace"))
        return

    if not request_id or not image_url:
        _publish_error(request_id, "invalid_image", "missing request_id or image_url")
        return

    try:
        resp = await http_client.get(image_url, timeout=DOWNLOAD_TIMEOUT_S)
    except Exception as exc:
        log_event("relay_download_failed", request_id=request_id, error=type(exc).__name__, msg=str(exc))
        _publish_error(request_id, "invalid_image", f"download failed: {type(exc).__name__}")
        return

    if resp.status_code != 200:
        _publish_error(request_id, "invalid_image", f"download http {resp.status_code}")
        return

    content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type and content_type != "image/jpeg":
        _publish_error(request_id, "invalid_image", f"unsupported content-type {content_type}")
        return

    jpeg_bytes = resp.content
    if len(jpeg_bytes) > MAX_IMAGE_BYTES:
        _publish_error(request_id, "invalid_image", f"image exceeds {MAX_IMAGE_BYTES} bytes")
        return

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(run_inference, jpeg_bytes), timeout=INFERENCE_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        log_event("relay_timeout", request_id=request_id, image_bytes=len(jpeg_bytes))
        _publish_error(request_id, "model_unavailable", "inference exceeded 3 s")
        return

    result["request_id"] = request_id
    _publish(result)
    log_event(
        "relay_predict",
        request_id=request_id,
        status=result["status"],
        code=result.get("code"),
        emotion=result.get("emotion"),
        confidence=result.get("confidence"),
        latency_ms=result.get("latency_ms"),
        image_bytes=len(jpeg_bytes),
    )


def _make_callback(loop: asyncio.AbstractEventLoop, http_client: httpx.AsyncClient):
    def _on_message(topic, payload, dup, qos, retain, **kwargs):
        # Runs on awscrt's IO thread; bounce to the event loop.
        asyncio.run_coroutine_threadsafe(_handle(payload, http_client), loop)
    return _on_message


def _connect_blocking(endpoint: str, cert_dir: Path):
    conn = mqtt_connection_builder.mtls_from_path(
        endpoint=endpoint,
        cert_filepath=str(cert_dir / "permanent-cert.pem.crt"),
        pri_key_filepath=str(cert_dir / "permanent-private.key"),
        ca_filepath=str(cert_dir / "AmazonRootCA1.pem"),
        client_id=CLIENT_ID,
        clean_session=False,
        keep_alive_secs=KEEP_ALIVE_S,
    )
    conn.connect().result()
    return conn


async def _connect_and_serve(endpoint: str, cert_dir: Path) -> None:
    global _mqtt_conn
    log_event("relay_connecting", client_id=CLIENT_ID, endpoint=endpoint)
    _mqtt_conn = await asyncio.to_thread(_connect_blocking, endpoint, cert_dir)
    log_event("relay_connected", client_id=CLIENT_ID)

    async with httpx.AsyncClient() as http_client:
        loop = asyncio.get_running_loop()

        def _subscribe_blocking():
            # awscrt.mqtt.Connection.subscribe returns (Future, packet_id).
            sub_future, _packet_id = _mqtt_conn.subscribe(
                topic=REQUEST_TOPIC,
                qos=mqtt.QoS.AT_LEAST_ONCE,
                callback=_make_callback(loop, http_client),
            )
            return sub_future.result()

        suback = await asyncio.to_thread(_subscribe_blocking)
        log_event("relay_subscribed", topic=REQUEST_TOPIC, qos=str(suback.get("qos")))
        # Park here until cancelled — awscrt handles in-session reconnects internally.
        await asyncio.Event().wait()


async def start() -> None:
    """Connect MQTT, subscribe, run until cancelled. Self-heals on transient errors."""
    cert_dir = Path(CFG.iot_cert_dir) if CFG.iot_cert_dir else None
    have_certs = cert_dir is not None and cert_dir.is_dir() \
        and (cert_dir / "permanent-cert.pem.crt").exists() \
        and (cert_dir / "permanent-private.key").exists() \
        and (cert_dir / "AmazonRootCA1.pem").exists()

    if not CFG.iot_endpoint or not have_certs:
        log_event(
            "relay_disabled",
            reason="iot_endpoint or cert files missing",
            iot_endpoint=bool(CFG.iot_endpoint),
            cert_dir=str(cert_dir) if cert_dir else None,
        )
        return

    attempt = 0
    while not _stopped:
        attempt += 1
        try:
            await _connect_and_serve(CFG.iot_endpoint, cert_dir)
            # _connect_and_serve only returns when cancelled or on disconnect-with-no-exception;
            # if we got here normally, just loop and reconnect.
        except asyncio.CancelledError:
            log_event("relay_cancelled")
            raise
        except Exception as exc:
            log_event(
                "relay_error",
                attempt=attempt,
                error=type(exc).__name__,
                msg=str(exc),
            )
            fatal_markers = (
                "CLIENT_ID_REJECTED", "AUTH_FAILED",
                "UNAUTHORIZED", "TLS_NEGOTIATION_ERROR",
            )
            err_str = (type(exc).__name__ + " " + str(exc)).upper()
            if any(m in err_str for m in fatal_markers):
                log_event(
                    "relay_fatal_auth_giving_up",
                    error=type(exc).__name__, msg=str(exc),
                )
                return
            log_event("relay_reconnecting", delay_s=RECONNECT_DELAY_S, next_attempt=attempt + 1)
            try:
                await asyncio.sleep(RECONNECT_DELAY_S)
            except asyncio.CancelledError:
                raise


async def stop() -> None:
    """Disconnect cleanly. Idempotent."""
    global _mqtt_conn, _stopped
    if _stopped:
        return
    _stopped = True
    conn = _mqtt_conn
    _mqtt_conn = None
    if conn is not None:
        try:
            await asyncio.to_thread(lambda: conn.disconnect().result())
            log_event("relay_disconnected")
        except Exception as exc:
            log_event("relay_disconnect_failed", error=type(exc).__name__, msg=str(exc))
