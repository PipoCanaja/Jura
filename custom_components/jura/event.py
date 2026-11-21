"""Event platform: fired when a product or a process is finished."""

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .core import DOMAIN
from .core.entity import JuraEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    device = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([JuraFinishedEvent(device)])


class JuraFinishedEvent(JuraEntity, EventEntity):
    """Fired on the Enjoy screen (product or process finished)."""

    _attr_icon = "mdi:coffee"
    _attr_event_types = ["finished"]

    def __init__(self, device):
        super().__init__(device, "finished")
        device.register_finished(self.on_finished)

    def on_finished(self, name: str | None, code: int, last_progress: int | None):
        if self.hass is None:
            return
        self._trigger_event(
            "finished",
            {"product": name, "code": f"{code:02X}", "last_progress": last_progress},
        )
        self.async_write_ha_state()
