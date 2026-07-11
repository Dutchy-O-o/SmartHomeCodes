"""Fire-and-forget cloud telemetry with a bounded queue.

A single background worker drains an asyncio.Queue (maxsize=64) and POSTs
each summary to AWS. When the queue is full the producer drops the event
silently — the predict response is never blocked or delayed by telemetry.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import httpx

from logging_config import log_event
from pi_config import PiConfig

QUEUE_MAX = 64
HTTP_TIMEOUT = 2.0  # seconds; cloud being slow must never matter

_queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=QUEUE_MAX)
_worker: Optional[asyncio.Task] = None
_client: Optional[httpx.AsyncClient] = None
_cfg: Optional[PiConfig] = None


def enqueue(emotion: str, confidence: float) -> None:
    if _cfg is None or not _cfg.telemetry_enabled or not _cfg.telemetry_url:
        return
    payload = {
        "home_id": _cfg.home_id,
        "emotion": emotion,
        "confidence": confidence,
        "ts": int(time.time()),
    }
    try:
        _queue.put_nowait(payload)
    except asyncio.QueueFull:
        log_event("telemetry_dropped", reason="queue_full", qsize=_queue.qsize())


async def _worker_loop() -> None:
    assert _client is not None and _cfg is not None
    url = f"{_cfg.telemetry_url.rstrip('/')}/prod/{_cfg.home_id}/emotion/telemetry"
    while True:
        payload = await _queue.get()
        try:
            await _client.post(url, json=payload, timeout=HTTP_TIMEOUT)
        except Exception as exc:
            log_event("telemetry_post_failed", error=type(exc).__name__, msg=str(exc))
        finally:
            _queue.task_done()


def start(cfg: PiConfig) -> None:
    global _worker, _client, _cfg
    _cfg = cfg
    if not cfg.telemetry_enabled or not cfg.telemetry_url:
        log_event("telemetry_disabled", reason="config")
        return
    _client = httpx.AsyncClient()
    _worker = asyncio.create_task(_worker_loop(), name="telemetry-worker")


async def stop() -> None:
    global _worker, _client
    if _worker is not None:
        _worker.cancel()
        try:
            await _worker
        except (asyncio.CancelledError, Exception):
            pass
        _worker = None
    if _client is not None:
        await _client.aclose()
        _client = None
