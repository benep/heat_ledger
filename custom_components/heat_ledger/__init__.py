"""Heat Ledger: splits the heat of a building between radiators and hot water."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .cards import async_register_cards
from .const import DOMAIN, STORAGE_VERSION
from .model import build_plant
from .runtime import HeatLedgerRuntime, store_key

PLATFORMS: list[Platform] = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type HeatLedgerConfigEntry = ConfigEntry[HeatLedgerRuntime]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    await async_register_cards(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HeatLedgerConfigEntry) -> bool:
    try:
        plant = build_plant(entry.options, entry.subentries.values())
    except (KeyError, TypeError, ValueError) as err:
        raise ConfigEntryError(f"Invalid plant configuration: {err}") from err

    runtime = HeatLedgerRuntime(hass, entry, plant)
    await runtime.async_start()
    entry.runtime_data = runtime
    entry.async_on_unload(runtime.cancel)
    # Subentries and options define the plant: any change rebuilds it. The state is saved on unload.
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HeatLedgerConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: HeatLedgerConfigEntry) -> None:
    await Store(hass, STORAGE_VERSION, store_key(entry.entry_id)).async_remove()


async def _async_reload(hass: HomeAssistant, entry: HeatLedgerConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
