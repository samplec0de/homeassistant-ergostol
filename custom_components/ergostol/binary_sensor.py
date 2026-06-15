"""Binary sensor platform: desk movement state."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ErgostolConfigEntry
from .entity import ErgostolEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ErgostolConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ErgostolMovingSensor(entry.runtime_data)])


class ErgostolMovingSensor(ErgostolEntity, BinarySensorEntity):
    """True while the desk is moving."""

    _attr_translation_key = "moving"
    _attr_device_class = BinarySensorDeviceClass.MOVING

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_moving"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.moving
