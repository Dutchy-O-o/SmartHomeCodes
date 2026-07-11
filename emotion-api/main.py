"""Hardened emotion-inference service for the Pi.

A single process owns the only TensorFlow graph. Two callers share
`run_inference()`:
  - the HTTPS `/predict` handler in this module (LAN clients),
  - the MQTT relay subscriber spawned by the lifespan hook below.

The relay runs as an asyncio task on uvicorn's event loop — no second
Python process, no second TF instance.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import boto3
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from detector import detect_face
from logging_config import log_event
from model import MODEL_VERSION, predict_emotion
import pi_config
import telemetry

MAX_IMAGE_BYTES = 5 * 1024 * 1024
INFERENCE_TIMEOUT_S = 3.0
JPEG_MAGIC = (b"\xff\xd8\xff",)

HTTP_STATUS_FOR_CODE = {
    "invalid_image": 400,
    "no_face": 400,
    "model_unavailable": 503,
    "rate_limited": 429,
}

CFG = pi_config.load()
START_TIME = time.monotonic()
MODEL_LOADED = True

try:
    _ddb_table = boto3.resource("dynamodb", region_name="eu-central-1").Table("UserEmotionsLog")
except Exception as exc:
    _ddb_table = None
    log_event("dynamodb_init_failed", error=type(exc).__name__, msg=str(exc))


def _err_dict(code: str, message: str, started: float) -> dict:
    return {
        "status": "error",
        "code": code,
        "message": message,
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


def run_inference(jpeg_bytes: bytes) -> dict:
    """Synchronous inference shared by HTTP and MQTT callers.

    The 3 s wall-clock ceiling is enforced by the caller via
    `asyncio.wait_for(asyncio.to_thread(run_inference, ...), 3.0)`.
    """
    started = time.perf_counter()
    n = len(jpeg_bytes)
    if n == 0 or n > MAX_IMAGE_BYTES:
        return _err_dict("invalid_image", f"image must be 1..{MAX_IMAGE_BYTES} bytes", started)
    if not jpeg_bytes.startswith(JPEG_MAGIC):
        return _err_dict("invalid_image", "only JPEG is accepted", started)

    face, frame = detect_face(jpeg_bytes)
    if frame is None:
        return _err_dict("invalid_image", "image could not be decoded", started)
    if face is None:
        return _err_dict("no_face", "no face detected in image", started)

    try:
        emotion, confidence, all_scores = predict_emotion(face)
    except Exception as exc:
        return _err_dict("model_unavailable", f"inference failed: {type(exc).__name__}", started)

    return {
        "status": "success",
        "emotion": emotion,
        "confidence": confidence,
        "all_scores": all_scores,
        "source": "pi",
        "model_version": MODEL_VERSION,
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"status": "error", "code": code, "message": message},
    )


# `relay_subscriber` imports `run_inference` from this module, so we import it
# *after* `run_inference` is defined to resolve the circular reference cleanly.
import relay_subscriber  # noqa: E402

_relay_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _relay_task
    telemetry.start(CFG)
    log_event(
        "service_started",
        home_id=CFG.home_id,
        device_id=CFG.device_id,
        hostname=CFG.hostname,
        model_version=MODEL_VERSION,
        telemetry_enabled=CFG.telemetry_enabled,
    )
    _relay_task = asyncio.create_task(relay_subscriber.start(), name="relay_subscriber")
    try:
        yield
    finally:
        if _relay_task is not None and not _relay_task.done():
            _relay_task.cancel()
            try:
                await _relay_task
            except (asyncio.CancelledError, Exception):
                pass
        await relay_subscriber.stop()
        await telemetry.stop()


app = FastAPI(title="Secure Local Emotion API", version=MODEL_VERSION, lifespan=lifespan)


@app.exception_handler(StarletteHTTPException)
async def _http_exc(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = "invalid_image" if exc.status_code == 400 else "model_unavailable"
    return _error(code, str(exc.detail), exc.status_code)


@app.exception_handler(RequestValidationError)
async def _validation_exc(_: Request, exc: RequestValidationError) -> JSONResponse:
    return _error("invalid_image", "missing or malformed 'image' field", 400)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok" if MODEL_LOADED else "degraded",
        "model_loaded": MODEL_LOADED,
        "uptime_s": int(time.monotonic() - START_TIME),
        "model_version": MODEL_VERSION,
    }


@app.post("/predict")
async def predict(
    request: Request,
    image: UploadFile | None = File(default=None),
    file: UploadFile | None = File(default=None),
) -> JSONResponse:
    upload = image or file
    if upload is None:
        return _error("invalid_image", "missing 'image' (or legacy 'file') field", 400)
    contents = await upload.read()
    image_bytes = len(contents)
    client_ip = request.client.host if request.client else "unknown"

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(run_inference, contents), timeout=INFERENCE_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        log_event(
            "predict_timeout",
            timeout_s=INFERENCE_TIMEOUT_S,
            image_bytes=image_bytes,
            client_ip=client_ip,
        )
        return _error("model_unavailable", "inference exceeded 3 s", 503)

    if result["status"] == "error":
        log_event(
            "predict_rejected",
            code=result["code"],
            latency_ms=result["latency_ms"],
            image_bytes=image_bytes,
            client_ip=client_ip,
        )
        return JSONResponse(
            status_code=HTTP_STATUS_FOR_CODE.get(result["code"], 400), content=result
        )

    if _ddb_table is not None:
        asyncio.create_task(asyncio.to_thread(_ddb_log, result["emotion"], result["confidence"]))
    telemetry.enqueue(result["emotion"], result["confidence"])

    log_event(
        "predict",
        emotion=result["emotion"],
        confidence=result["confidence"],
        latency_ms=result["latency_ms"],
        image_bytes=image_bytes,
        client_ip=client_ip,
    )
    return JSONResponse(content=result)


def _ddb_log(emotion: str, confidence: float) -> None:
    try:
        _ddb_table.put_item(
            Item={
                "timestamp": str(int(time.time())),
                "emotion": emotion,
                "confidence": Decimal(str(confidence)),
                "device_id": CFG.device_id,
                "home_id": CFG.home_id,
            }
        )
    except Exception as exc:
        log_event("dynamodb_put_failed", error=type(exc).__name__, msg=str(exc))
