"""Sensor platform for Jura integration."""

import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .core import DOMAIN
from .core.entity import JuraEntity
from .core.progress import PHASE_NAMES


from .core.maintenance import NAMES as MAINTENANCE_NAMES

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Jura sensor based on a config entry."""
    device = hass.data[DOMAIN][entry.entry_id]

    # Create the total coffees sensor
    entities: list = [JuraTotalCoffeeSensor(device)]

    # Create sensors for each product
    for product in device.products:
        product_name = product["@Name"]
        if product.get("@Active") != "false":
            entities.append(JuraProductCountSensor(device, product_name))

    # Create alert sensors
    entities.append(JuraAlertSensor(device))

    # Debug (fork only): last raw progress frame
    entities.append(JuraRawProgressSensor(device))

    # Progress sensors (from the progress characteristic)
    entities += [
        JuraMachineStateSensor(device),
        JuraPhaseSensor(device),
        JuraProgressSensor(device),
    ]


    # Maintenance sensors (process types listed by the machine XML)
    for kind in device.maintenance["counters"]:
        entities.append(JuraMaintenanceCounterSensor(device, kind))
    for kind in device.maintenance["percent"]:
        entities.append(JuraMaintenancePercentSensor(device, kind))

    async_add_entities(entities)

    # Set up automatic refresh
    update_interval = hass.data[DOMAIN].get("update_interval", 60)

    async def refresh_statistics(*_):
        """Refresh statistics regularly."""
        try:
            await device.read_statistics()
            await device.read_alerts()
            await device.read_maintenance()
        except Exception as ex:
            # we log as info as this is expected if the device is off
            _LOGGER.info(f"Error refreshing statistics: {ex}")

    # Schedule regular updates
    entry.async_on_unload(
        async_track_time_interval(
            hass, refresh_statistics, timedelta(seconds=update_interval)
        )
    )

    # Do an initial refresh
    hass.async_create_task(refresh_statistics())


class JuraStatisticsSensor(JuraEntity, SensorEntity):
    """Base class for Jura statistics sensors."""

    def __init__(self, device, attr: str):
        """Initialize the sensor."""
        super().__init__(device, attr)

        # Register for updates on statistics
        device.register_statistics_update(self.internal_update)

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        return self._get_value()

    def _get_value(self) -> Any:
        """Get the value for this sensor from statistics."""
        raise NotImplementedError("Subclasses must implement this method")

    def internal_update(self):
        """Override parent method to ensure statistics are refreshed."""
        _LOGGER.debug(f"Updating sensor {self._attr_name}")
        if self.hass is not None:
            self.async_write_ha_state()


class JuraTotalCoffeeSensor(JuraStatisticsSensor):
    """Sensor for total coffee count."""

    _attr_icon = "mdi:coffee"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "products"

    def __init__(self, device):
        """Initialize the sensor."""
        super().__init__(device, "total_product")
        self._attr_name = f"{device.name} Total Products"

    def _get_value(self) -> int:
        """Get the total coffee count."""
        value = self.device.statistics.get("total_products", 0)
        _LOGGER.debug(f"Total coffee value: {value}")
        return value


class JuraProductCountSensor(JuraStatisticsSensor):
    """Sensor for individual product count."""

    _attr_icon = "mdi:coffee-outline"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "products"

    def __init__(self, device, product_name: str):
        """Initialize the sensor."""
        self.product_name = product_name
        attr_name = f"product_{product_name.lower().replace(' ', '_')}"
        super().__init__(device, attr_name)
        self._attr_name = f"{device.name} {product_name} Count"

    def _get_value(self) -> int:
        """Get the count for this specific product."""
        value = self.device.statistics.get("product_counts", {}).get(
            self.product_name, None
        )
        _LOGGER.debug(f"Product {self.product_name} count: {value}")
        return value


class JuraAlertSensor(JuraEntity, SensorEntity):
    """Sensor for machine alerts."""

    _attr_icon = "mdi:alert"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["ok", "alert"]

    def __init__(self, device):
        """Initialize the sensor."""
        super().__init__(device, "alerts")
        self._attr_name = f"{device.name} Alerts"
        self._attr_extra_state_attributes = {"active_alerts": []}

        # Register for updates on alerts
        device.register_alert_update(self.internal_update)

    @property
    def native_value(self) -> str:
        """Return the state of the sensor."""
        return self._get_value()

    def _get_value(self) -> str:
        """Get the alert status."""
        active_alerts = []
        # Filter out specific alert bits that we don't want to show
        filtered_bits = {12, 13, 36, 37, 148, 149, 150, 151}
        for bit, name in self.device.active_alerts.items():
            if bit not in filtered_bits:
                active_alerts.append({"bit": bit, "name": name})
        self._attr_extra_state_attributes["active_alerts"] = active_alerts
        return "alert" if active_alerts else "ok"

    def internal_update(self):
        """Override parent method to ensure alerts are refreshed."""
        _LOGGER.debug(f"Updating alert sensor {self._attr_name}")
        if self.hass is not None:
            self.async_write_ha_state()


class JuraRawProgressSensor(JuraEntity, SensorEntity):
    """Debug sensor: last decrypted progress frame (5a401527), as hex."""

    _attr_icon = "mdi:hexadecimal"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, device):
        super().__init__(device, "progress_raw")
        self._attr_name = f"{device.name} Progress Raw"
        device.register_debug_update(self.internal_update)

    @property
    def native_value(self) -> str | None:
        last = self.device.client.tracer.last.get("1527")
        return last["plain"] if last else None

    @property
    def extra_state_attributes(self) -> dict:
        tracer = self.device.client.tracer
        progress = tracer.last.get("1527") or {}
        status = tracer.last.get("1524") or {}
        return {
            "progress_time": progress.get("t"),
            "status": status.get("plain"),
            "status_time": status.get("t"),
            "recording": tracer.recording is not None,
        }

class JuraProgressEntity(JuraEntity, SensorEntity):
    """Base class for sensors fed by the progress characteristic."""

    def __init__(self, device, attr: str):
        super().__init__(device, attr)
        device.register_progress_update(self.internal_update)

    def internal_update(self):
        if self.hass is not None:
            self.async_write_ha_state()




class JuraMaintenanceSensor(JuraEntity, SensorEntity):
    """Base class for maintenance statistics sensors."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device, kind: str, attr: str):
        self.kind = kind
        super().__init__(device, attr)
        device.register_maintenance_update(self.internal_update)

    def internal_update(self):
        if self.hass is not None:
            self.async_write_ha_state()



class JuraMachineStateSensor(JuraProgressEntity):
    """Machine state, named from the machine XML (PROGRESS_STATE_INTAKE)."""

    _attr_icon = "mdi:coffee-maker"

    def __init__(self, device):
        super().__init__(device, "machine_state")

    @property
    def native_value(self) -> str | None:
        return self.device.state_label()

    @property
    def extra_state_attributes(self) -> dict:
        progress = self.device.progress
        if progress is None:
            return {}
        return {
            "state_code": f"{progress.state:02X}",
            "code": f"{progress.code:02X}",
            "product": self.device.code_name(progress.code),
        }


class JuraPhaseSensor(JuraProgressEntity):
    """Preparation phase: grinding, coffee water, milk, hot water..."""

    _attr_icon = "mdi:progress-clock"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PHASE_NAMES

    def __init__(self, device):
        super().__init__(device, "phase")

    @property
    def native_value(self) -> str | None:
        progress = self.device.progress
        return progress.phase if progress and progress.preparing else None


class JuraProgressSensor(JuraProgressEntity):
    """Progress of the running product (all phases) or process, in %."""

    _attr_icon = "mdi:progress-helper"
    _attr_native_unit_of_measurement = "%"

    def __init__(self, device):
        super().__init__(device, "progress")

    @property
    def native_value(self) -> int | None:
        return self.device.progress_percent()

    @property
    def extra_state_attributes(self) -> dict:
        progress = self.device.progress
        if not progress or not progress.preparing:
            return {}
        return {
            "phase": progress.phase,
            "phase_progress": progress.phase_ratio,
            "current": progress.current,
            "max": progress.max,
            "strength": progress.strength,
            "strength_start": progress.strength_start,
            "strength_max": self.device.strength_max(progress.code)
            if progress.strength is not None
            else None,
            "product": self.device.code_name(progress.code),
        }


class JuraMaintenanceCounterSensor(JuraMaintenanceSensor):
    """Number of maintenance processes done (cleanings, descalings...)."""

    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, device, kind: str):
        super().__init__(device, kind, f"{kind.lower()}_count")
        self._attr_name = f"{device.name} {MAINTENANCE_NAMES[kind]} Count"

    @property
    def native_value(self) -> int | None:
        return self.device.maintenance_counters.get(self.kind)


class JuraMaintenancePercentSensor(JuraMaintenanceSensor):
    """Maintenance wear in %, as shown by the J.O.E. app: 0 % right after the
    maintenance, 100 % when it is due (cleaning, filter change, descaling)."""

    _attr_icon = "mdi:progress-wrench"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, device, kind: str):
        super().__init__(device, kind, f"{kind.lower()}_percent")
        self._attr_name = f"{device.name} {MAINTENANCE_NAMES[kind]} Wear"

    @property
    def native_value(self) -> int | None:
        return self.device.maintenance_percent.get(self.kind)
