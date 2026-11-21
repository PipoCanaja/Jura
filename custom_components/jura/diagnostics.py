"""Diagnostics (fork only): Settings > Devices > Jura > Download diagnostics."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .core import DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    device = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if device is None:
        return {"error": "device not discovered yet (no advertisement received)"}

    client = device.client
    return {
        "model": device.model,
        "mac": device.mac,
        "key": client.key,
        "connected": device.connected,
        "connection": {k: str(v) for k, v in device.conn_info.items()},
        "advertisement": device.debug_info,
        "products": len(device.products),
        "alerts_defined": len(device.alerts),
        "active_alerts": device.active_alerts,
        "statistics": device.statistics,
        "client": {
            "keep_alive": client.keep_alive,
            "queue": len(client.queue),
            "poll": {k[4:8]: v for k, v in client.poll.items()},
        },
        "trace": client.tracer.as_dict(),
    }
