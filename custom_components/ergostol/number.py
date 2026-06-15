"""Number platform: target/current desk height in cm."""
from __future__ import annotations

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ErgostolConfigEntry
from .entity import ErgostolEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ErgostolConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ErgostolHeightNumber(entry.runtime_data)])


class ErgostolHeightNumber(ErgostolEntity, NumberEntity):
    """Settable desk height. Reads current height; setting drives the desk."""

    _attr_translation_key = "height"
    _attr_device_class = NumberDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = UnitOfLength.CENTIMETERS
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_height"

    @property
    def native_min_value(self) -> float:
        return self.coordinator.min_cm

    @property
    def native_max_value(self) -> float:
        return self.coordinator.max_cm

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.height_cm

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_height(value)
