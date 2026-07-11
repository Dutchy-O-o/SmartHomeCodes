"""JSON-line prediction logger with size-based rotation (50 MB / 5 backups)."""
from __future__ import annotations

import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(os.environ.get("PI_LOG_DIR", "/var/log/smarthome"))
LOG_FILE = LOG_DIR / "emotion.jsonl"
MAX_BYTES = 50 * 1024 * 1024
BACKUP_COUNT = 5


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, "payload", None)
        if payload is None:
            payload = {"msg": record.getMessage()}
        payload.setdefault("ts", self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"))
        payload.setdefault("level", record.levelname.lower())
        return json.dumps(payload, separators=(",", ":"), default=str)


def _make_handler() -> logging.Handler:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
    except (OSError, PermissionError):
        # Container or test env without write access — fall back to stderr.
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLineFormatter())
    return handler


def get_logger() -> logging.Logger:
    logger = logging.getLogger("emotion")
    if not logger.handlers:
        logger.addHandler(_make_handler())
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def log_event(event: str, **fields) -> None:
    logger = get_logger()
    logger.info(event, extra={"payload": {"event": event, **fields}})
