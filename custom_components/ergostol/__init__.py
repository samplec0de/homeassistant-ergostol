"""The Ergostol Desk integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_ADDRESS
from .coordinator import ErgostolCoordinator

PLATFORMS = [
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
]

type ErgostolConfigEntry = ConfigEntry[ErgostolCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ErgostolConfigEntry) -> bool:
    """Set up Ergostol Desk from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    coordinator = ErgostolCoordinator(hass, entry, address)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ErgostolConfigEntry) -> None:
    """Reload when options (quiet hours) change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ErgostolConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded
