"""Heat Ledger: splits the heat of a building between radiators and hot water."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .backfill import async_backfill, async_backfill_at_setup, issue_id
from .cards import async_register_cards
from .const import CONF_BACKFILL, DOMAIN, STORAGE_VERSION
from .model import build_plant
from .runtime import HeatLedgerRuntime, store_key

PLATFORMS: list[Platform] = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_BACKFILL = "backfill"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"

type HeatLedgerConfigEntry = ConfigEntry[HeatLedgerRuntime]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    await async_register_cards(hass)

    async def backfill(call: ServiceCall) -> ServiceResponse:
        """Calculate the days before a plant was set up, for one already set up without it."""
        entry = hass.config_entries.async_get_entry(call.data[ATTR_CONFIG_ENTRY_ID])
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="entry_not_found")
        if entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="entry_not_loaded")
        return await async_backfill(hass, entry)

    # Only for administrators: it writes statistics once, for good.
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_BACKFILL,
        backfill,
        schema=vol.Schema({vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string}),
        supports_response=SupportsResponse.OPTIONAL,
    )
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
    if entry.data.get(CONF_BACKFILL) and runtime.backfill is None:
        # Chosen in the first setup and not done yet. In the background: reading days of states takes a moment.
        entry.async_create_background_task(hass, async_backfill_at_setup(hass, entry), f"{DOMAIN} backfill at setup")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HeatLedgerConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: HeatLedgerConfigEntry) -> None:
    await Store(hass, STORAGE_VERSION, store_key(entry.entry_id)).async_remove()
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry))


async def _async_reload(hass: HomeAssistant, entry: HeatLedgerConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
