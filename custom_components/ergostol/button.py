"""Button platform: stop + preset recall."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ErgostolConfigEntry
from .coordinator import ErgostolCoordinator
from .entity import ErgostolEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ErgostolConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            ErgostolStopButton(coordinator),
            ErgostolPresetButton(coordinator, "sit"),
            ErgostolPresetButton(coordinator, "stand"),
        ]
    )


class ErgostolStopButton(ErgostolEntity, ButtonEntity):
    """Stop any motion."""

    _attr_translation_key = "stop"

    def __init__(self, coordinator: ErgostolCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_stop"

    async def async_press(self) -> None:
        await self.coordinator.async_stop()


class ErgostolPresetButton(ErgostolEntity, ButtonEntity):
    """Drive to the configured sit / stand height (set in the integration options)."""

    def __init__(self, coordinator: ErgostolCoordinator, which: str) -> None:
        super().__init__(coordinator)
        self._which = which
        self._attr_translation_key = f"preset_{which}"
        self._attr_unique_id = f"{coordinator.address}_preset_{which}"

    async def async_press(self) -> None:
        height = (
            self.coordinator.sit_height
            if self._which == "sit"
            else self.coordinator.stand_height
        )
        await self.coordinator.async_set_height(height)
