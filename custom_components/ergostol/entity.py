"""Base entity for the Ergostol Desk integration."""
from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ErgostolCoordinator


class ErgostolEntity(CoordinatorEntity[ErgostolCoordinator]):
    """Common base: shared device + availability."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ErgostolCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            identifiers={(DOMAIN, coordinator.address)},
            name="Ergostol Desk",
            manufacturer="Ergostol",
        )

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self.coordinator.data.available
        )
