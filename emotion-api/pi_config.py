"""Read Pi identity at boot from /etc/smarthome/pi.toml or derive from hardware.

Layout of pi.toml:

    [device]
    home_id   = "757bfcc9-..."
    device_id = "raspi-living-1"     # optional; falls back to cpu serial
    hostname  = "ramazan.local"      # optional; informational only

    [cloud]
    telemetry_url = "https://<api>.execute-api.<region>.amazonaws.com"
    enabled       = true
"""
from __future__ import annotations

import os
import re
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("PI_CONFIG", "/etc/smarthome/pi.toml"))


@dataclass(frozen=True)
class PiConfig:
    home_id: str
    device_id: str
    hostname: str
    telemetry_url: str | None
    telemetry_enabled: bool
    iot_endpoint: str | None
    iot_cert_dir: str | None


def _cpu_serial() -> str | None:
    try:
        with open("/proc/cpuinfo", "r") as fh:
            for line in fh:
                if line.lower().startswith("serial"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def _primary_mac() -> str | None:
    base = Path("/sys/class/net")
    if not base.is_dir():
        return None
    for iface in sorted(p.name for p in base.iterdir()):
        if iface == "lo":
            continue
        addr = base / iface / "address"
        try:
            mac = addr.read_text().strip()
        except OSError:
            continue
        if mac and mac != "00:00:00:00:00:00":
            return mac
    return None


def _derive_device_id() -> str:
    serial = _cpu_serial()
    if serial:
        return f"raspi-{serial[-8:]}"
    mac = _primary_mac()
    if mac:
        return "raspi-" + mac.replace(":", "")[-8:]
    return f"raspi-{uuid.getnode():012x}"[-14:]


def _derive_home_id() -> str:
    """Last-resort home_id discovery.

    Falls back to the legacy home.conf (kept for backward compat with the
    other Pi services in this repo) before generating a stable UUID5 from
    the CPU serial so a freshly imaged Pi still boots.
    """
    legacy = Path("/home/ramazan/Desktop/python/home.conf")
    if legacy.exists():
        m = re.search(r"homeid\s*=\s*([0-9a-fA-F-]+)", legacy.read_text())
        if m:
            return m.group(1)
    seed = _cpu_serial() or _primary_mac() or "unknown"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"smarthome.pi.{seed}"))


def load() -> PiConfig:
    data: dict = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as fh:
            data = tomllib.load(fh)

    device = data.get("device", {})
    cloud = data.get("cloud", {})

    return PiConfig(
        home_id=device.get("home_id") or _derive_home_id(),
        device_id=device.get("device_id") or _derive_device_id(),
        hostname=device.get("hostname") or "ramazan.local",
        telemetry_url=cloud.get("telemetry_url") or None,
        telemetry_enabled=bool(cloud.get("enabled", False)),
        iot_endpoint=cloud.get("iot_endpoint") or None,
        iot_cert_dir=cloud.get("cert_dir") or None,
    )
