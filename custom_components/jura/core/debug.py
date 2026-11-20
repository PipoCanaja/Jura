"""Debug tools (fork only, not meant for upstream).

- Tracer: every BLE frame (plain + raw hex) in a ring buffer and in the log
- recording of a cycle to a JSONL file in the config folder
- services jura.raw_read / jura.raw_write / jura.record_start / jura.record_stop
"""

import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

UUID_TEMPLATE = "5a40{}-ab2e-2548-c435-08c300000710"

NAMES = {
    "1524": "STATUS",
    "1525": "START_PRODUCT",
    "1527": "PROGRESS",
    "1528": "UPDATE_PROGRESS",
    "1529": "P_MODE",
    "1530": "LOCK",
    "1531": "ABOUT",
    "1533": "STATS_CMD",
    "1534": "STATS_DATA",
    "1538": "P_MODE_READ",
}

TRACE_SIZE = 500
RECORD_MAX_FRAMES = 20000
RECORD_DEFAULT_DURATION = 600
RECORD_MAX_DURATION = 3600
# polling while connected, in seconds
POLL_IDLE = 6
POLL_RECORDING = 1


def short_uuid(uuid: str) -> str:
    return uuid.lower()[4:8]


def resolve_uuid(value: str) -> str:
    """Accept '27', '1527', '5a401527' or a full UUID."""
    v = value.strip().lower()
    if len(v) == 36:
        return v
    if len(v) == 2:
        return UUID_TEMPLATE.format("15" + v)
    if len(v) == 4:
        return UUID_TEMPLATE.format(v)
    if len(v) == 8 and v.startswith("5a40"):
        return UUID_TEMPLATE.format(v[4:])
    raise HomeAssistantError(f"Unknown characteristic: {value}")


def to_hex(data) -> str | None:
    return bytes(data).hex(" ") if data is not None else None


class Tracer:
    def __init__(self):
        self.frames: deque[dict] = deque(maxlen=TRACE_SIZE)
        self.last: dict[str, dict] = {}
        self.recording: list[dict] | None = None
        self.recording_started: float = 0
        self.recording_stop_handle = None
        self.listeners: list[Callable] = []

    def add(self, direction: str, uuid: str, plain, raw, error=None):
        short = short_uuid(uuid)
        entry = {
            "t": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "dir": direction,
            "uuid": short,
            "name": NAMES.get(short, uuid),
            "plain": to_hex(plain),
            "raw": to_hex(raw),
        }
        if error is not None:
            entry["error"] = f"{type(error).__name__}: {error}"

        self.frames.append(entry)
        if error is None:
            self.last[short] = entry

        if self.recording is not None and len(self.recording) < RECORD_MAX_FRAMES:
            self.recording.append(entry)

        _LOGGER.debug(
            "%s %s %-13s plain=%s raw=%s%s",
            direction,
            short,
            entry["name"],
            entry["plain"],
            entry["raw"],
            f" ERROR {entry['error']}" if error is not None else "",
        )

        for listener in self.listeners:
            listener()

    def as_dict(self) -> dict:
        return {
            "recording": self.recording is not None,
            "recording_frames": len(self.recording or []),
            "last": self.last,
            "frames": list(self.frames),
        }


def get_device(hass: HomeAssistant, call: ServiceCall):
    from .device import Device

    devices = {
        k: v for k, v in hass.data.get(DOMAIN, {}).items() if isinstance(v, Device)
    }

    if device_id := call.data.get("device_id"):
        entry = dr.async_get(hass).async_get(device_id)
        if entry:
            for entry_id in entry.config_entries:
                if entry_id in devices:
                    return devices[entry_id]
        raise HomeAssistantError(f"Jura device not found: {device_id}")

    if len(devices) == 1:
        return next(iter(devices.values()))
    raise HomeAssistantError("Several Jura machines: set device_id")


DEVICE_SCHEMA = {vol.Optional("device_id"): cv.string}

RAW_READ_SCHEMA = vol.Schema(
    {
        **DEVICE_SCHEMA,
        vol.Required("uuid"): cv.string,
    }
)

RAW_WRITE_SCHEMA = vol.Schema(
    {
        **DEVICE_SCHEMA,
        vol.Required("uuid"): cv.string,
        vol.Required("data"): cv.string,
        vol.Optional("encrypt", default=True): cv.boolean,
    }
)

RECORD_START_SCHEMA = vol.Schema(
    {
        **DEVICE_SCHEMA,
        vol.Optional("duration", default=RECORD_DEFAULT_DURATION): vol.All(
            vol.Coerce(int), vol.Range(min=10, max=RECORD_MAX_DURATION)
        ),
    }
)

RECORD_STOP_SCHEMA = vol.Schema(DEVICE_SCHEMA)


def parse_hex(value: str) -> bytes:
    try:
        return bytes.fromhex(value.replace(" ", "").replace(":", ""))
    except ValueError as e:
        raise HomeAssistantError(f"Invalid hex data: {value}") from e


async def record_stop(hass: HomeAssistant, device) -> dict:
    tracer: Tracer = device.client.tracer
    if tracer.recording is None:
        return {"recording": False}

    frames = tracer.recording
    tracer.recording = None
    if tracer.recording_stop_handle:
        tracer.recording_stop_handle.cancel()
        tracer.recording_stop_handle = None

    device.client.keep_alive = False
    device.client.poll = device.debug_poll(recording=False)
    device.client.ping()

    stamp = datetime.fromtimestamp(tracer.recording_started).strftime("%Y%m%d-%H%M%S")
    name = f"jura_{device.mac.replace(':', '').lower()}_{stamp}.jsonl"
    path = hass.config.path(name)
    header = {
        "type": "header",
        "model": device.model,
        "mac": device.mac,
        "key": device.client.key,
        "manufacturer_data": device.debug_info.get("manufacturer_data"),
        "started": datetime.fromtimestamp(tracer.recording_started, timezone.utc).isoformat(),
        "frames": len(frames),
    }

    def write():
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(header) + "\n")
            for frame in frames:
                f.write(json.dumps(frame) + "\n")

    await hass.async_add_executor_job(write)
    _LOGGER.info("Jura recording saved to %s (%d frames)", path, len(frames))
    return {"recording": False, "file": path, "frames": len(frames)}


def async_setup_services(hass: HomeAssistant):
    if hass.services.has_service(DOMAIN, "raw_read"):
        return

    async def raw_read(call: ServiceCall) -> dict:
        device = get_device(hass, call)
        uuid = resolve_uuid(call.data["uuid"])
        if not await device.client.wait_connected():
            raise HomeAssistantError("Machine not connected")
        try:
            raw = await device.client.read(uuid)
        except Exception as e:
            return {"uuid": uuid, "error": f"{type(e).__name__}: {e}"}
        return {
            "uuid": uuid,
            "raw": to_hex(raw),
            "plain": to_hex(device.client._decrypt(raw)),
        }

    async def raw_write(call: ServiceCall) -> dict:
        device = get_device(hass, call)
        uuid = resolve_uuid(call.data["uuid"])
        data = parse_hex(call.data["data"])
        if not data:
            raise HomeAssistantError("Empty data")
        if not await device.client.wait_connected():
            raise HomeAssistantError("Machine not connected")
        try:
            await device.client.write(data, uuid, encrypt=call.data["encrypt"])
        except Exception as e:
            return {"uuid": uuid, "ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"uuid": uuid, "ok": True, "sent": to_hex(data)}

    async def record_start(call: ServiceCall) -> dict:
        device = get_device(hass, call)
        tracer: Tracer = device.client.tracer
        duration = call.data["duration"]

        if tracer.recording is None:
            tracer.recording = []
            tracer.recording_started = time.time()

        if tracer.recording_stop_handle:
            tracer.recording_stop_handle.cancel()
        tracer.recording_stop_handle = hass.loop.call_later(
            duration, lambda: hass.async_create_task(record_stop(hass, device))
        )

        device.client.poll = device.debug_poll(recording=True)
        device.client.keep_alive = True
        device.client.ping()
        return {"recording": True, "duration": duration}

    async def record_stop_service(call: ServiceCall) -> dict:
        return await record_stop(hass, get_device(hass, call))

    hass.services.async_register(
        DOMAIN, "raw_read", raw_read, RAW_READ_SCHEMA, SupportsResponse.ONLY
    )
    hass.services.async_register(
        DOMAIN, "raw_write", raw_write, RAW_WRITE_SCHEMA, SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, "record_start", record_start, RECORD_START_SCHEMA, SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN,
        "record_stop",
        record_stop_service,
        RECORD_STOP_SCHEMA,
        SupportsResponse.OPTIONAL,
    )
