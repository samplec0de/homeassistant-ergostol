"""Sensor platform: current desk height + reported fault."""

from __future__ import annotations

from typing import ClassVar

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfLength
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ErgostolConfigEntry
from .entity import ErgostolEntity
from .protocol import error_key


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ErgostolConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        [
            ErgostolHeightSensor(entry.runtime_data),
            ErgostolErrorSensor(entry.runtime_data),
        ]
    )


class ErgostolHeightSensor(ErgostolEntity, SensorEntity):
    """Current desk height (read-only, graphable)."""

    _attr_translation_key = "current_height"
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = UnitOfLength.CENTIMETERS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_current_height"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.height_cm


class ErgostolErrorSensor(ErgostolEntity, SensorEntity):
    """Fault reported by the desk controller (what the handset displays)."""

    _attr_translation_key = "desk_error"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options: ClassVar[list[str]] = [
        "none",
        "e01",
        "e02",
        "e03",
        "e04",
        "e05",
        "hot",
        "unknown",
    ]
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_desk_error"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return error_key(self.coordinator.data.error_code)

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        data = self.coordinator.data
        return {"code": data.error_code if data else 0}
