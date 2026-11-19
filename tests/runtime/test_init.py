"""Setup, persistence and reloads of the config entry."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.heat_ledger.const import (
    CONF_CIRCUIT,
    CONF_KIND,
    CONF_LOSS_KW,
    DOMAIN,
    KIND_RADIATOR,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
    SUBENTRY_CONSUMER,
)
from custom_components.heat_ledger.engine import Ledger
from custom_components.heat_ledger.model import Subentry, build_plant

from .common import (
    BOILER,
    HOT_WATER_PUMP,
    LIVING_DEMAND,
    LIVING_SHARE,
    PUMP,
    SUBENTRIES,
    entity_id,
    later,
    next_interval,
    setup,
    value,
)

pytestmark = pytest.mark.usefixtures("inputs")


def stored(hass_storage: dict[str, Any], entry: MockConfigEntry) -> dict[str, Any]:
    return hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["ledger"]


async def boiler(hass: HomeAssistant, kwh: float) -> None:
    hass.states.async_set(BOILER, str(kwh), {"unit_of_measurement": "kWh"})
    await hass.async_block_till_done()


async def test_setup_and_unload(hass: HomeAssistant, entry: MockConfigEntry, hass_storage) -> None:
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entity_id(hass, "living_heat") is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert stored(hass_storage, entry)["last_t"] is not None


async def test_state_is_saved_after_an_interval(hass: HomeAssistant, entry: MockConfigEntry, hass_storage, freezer):
    await setup(hass, entry)
    await boiler(hass, 101.0)
    await next_interval(hass, freezer)
    assert f"{DOMAIN}.{entry.entry_id}" not in hass_storage  # delayed

    await later(hass, freezer, STORAGE_SAVE_DELAY + 1)
    assert sum(stored(hass_storage, entry)["consumed"]["living"].values()) == pytest.approx(LIVING_SHARE)


async def test_state_is_saved_when_home_assistant_stops(hass: HomeAssistant, entry: MockConfigEntry, hass_storage):
    await setup(hass, entry)
    await boiler(hass, 101.0)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert stored(hass_storage, entry)["account"]["boiler"] == pytest.approx(1.0)


async def test_restart_continues_from_the_store(hass: HomeAssistant, entry: MockConfigEntry, hass_storage, freezer):
    """The stored ledger is restored, and the heat that arrived during the downtime is split after the restart."""
    plant = build_plant({}, [Subentry(**{k: v for k, v in s.items() if k != "unique_id"}) for s in SUBENTRIES])
    ledger = Ledger(plant)
    ledger.observe(0, {BOILER: 90.0, PUMP: True, LIVING_DEMAND: 0.5, HOT_WATER_PUMP: False})
    ledger.observe(100, {BOILER: 98.0})
    ledger.close_interval(300)
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}",
        "data": {"ledger": ledger.to_dict()},
    }

    await setup(hass, entry)  # the boiler now reads 100: 2 kWh arrived while Home Assistant was down
    assert value(hass, "ground_floor_heat") == pytest.approx(8.0)
    assert value(hass, f"{entry.entry_id}_account") == pytest.approx(2.0)

    await next_interval(hass, freezer)
    assert value(hass, "ground_floor_heat") == pytest.approx(10.0)
    assert value(hass, "living_heat") == pytest.approx(10 * LIVING_SHARE)


async def test_unreadable_store_starts_over(hass: HomeAssistant, entry: MockConfigEntry, hass_storage, caplog) -> None:
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}",
        "data": {"ledger": {"version": 1, "account": {"boiler": "broken"}, "sources": {"boiler": 5}}},
    }
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert "unreadable, starting over" in caplog.text
    assert value(hass, f"{entry.entry_id}_account") == pytest.approx(0.0)


async def test_implausible_jump_is_logged_and_skipped(hass: HomeAssistant, entry: MockConfigEntry, freezer, caplog):
    await setup(hass, entry)
    hass.states.async_set(BOILER, "100500.0", {"unit_of_measurement": "kWh"})  # 100400 kWh at once
    await next_interval(hass, freezer)
    assert f"Ignored a jump of {BOILER}" in caplog.text
    assert value(hass, "ground_floor_heat") == pytest.approx(0.0)


async def test_reload_keeps_the_counters(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    await setup(hass, entry)
    await boiler(hass, 101.0)
    await next_interval(hass, freezer)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert value(hass, "living_heat") == pytest.approx(LIVING_SHARE)

    await boiler(hass, 102.0)
    await next_interval(hass, freezer)
    assert value(hass, "living_heat") == pytest.approx(2 * LIVING_SHARE)


async def test_options_change_reloads(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    await setup(hass, entry)
    await boiler(hass, 101.0)
    await next_interval(hass, freezer)

    hass.config_entries.async_update_entry(entry, options={CONF_LOSS_KW: 12.0})  # 1 kWh per 5 minutes
    await hass.async_block_till_done()
    assert entry.runtime_data.plant.loss_kw == 12.0
    assert value(hass, "ground_floor_heat") == pytest.approx(1.0)

    await boiler(hass, 104.0)
    await next_interval(hass, freezer)
    assert value(hass, "ground_floor_heat") == pytest.approx(3.0)


async def test_subentry_added_and_removed(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    await setup(hass, entry)
    await boiler(hass, 101.0)
    await next_interval(hass, freezer)

    kitchen = ConfigSubentry(
        data=MappingProxyType({CONF_KIND: KIND_RADIATOR, CONF_CIRCUIT: "ground_floor"}),
        subentry_id="kitchen",
        subentry_type=SUBENTRY_CONSUMER,
        title="Kitchen",
        unique_id=None,
    )
    hass.config_entries.async_add_subentry(entry, kitchen)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert value(hass, "kitchen_heat") == 0.0
    assert value(hass, "living_heat") == pytest.approx(LIVING_SHARE)

    # Weights now: living 1000 W, bath 600 W, kitchen 1000 W (default size).
    await boiler(hass, 103.6)
    await next_interval(hass, freezer)
    assert value(hass, "kitchen_heat") == pytest.approx(1.0)
    assert value(hass, "ground_floor_heat") == pytest.approx(3.6)

    hass.config_entries.async_remove_subentry(entry, "bath")
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entity_id(hass, "bath_heat") is None
    assert value(hass, "kitchen_heat") == pytest.approx(1.0)
    assert value(hass, "living_heat") == pytest.approx(LIVING_SHARE + 1.0)
    # The circuit keeps the heat the bath took while it was there: its counter must not decrease.
    assert value(hass, "ground_floor_heat") == pytest.approx(3.6)

    # Weights now: living 1000 W, kitchen 1000 W.
    await boiler(hass, 105.6)
    await next_interval(hass, freezer)
    assert value(hass, "kitchen_heat") == pytest.approx(2.0)
    assert value(hass, "ground_floor_heat") == pytest.approx(5.6)


async def test_removed_entry_deletes_the_store(hass: HomeAssistant, entry: MockConfigEntry, hass_storage) -> None:
    await setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert f"{DOMAIN}.{entry.entry_id}" in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{entry.entry_id}" not in hass_storage


async def test_entry_without_subentries(hass: HomeAssistant, freezer) -> None:
    """Right after the main entry is created there is nothing to split yet."""
    entry = MockConfigEntry(domain=DOMAIN, title="Heating", options={CONF_LOSS_KW: 0.5})
    entry.add_to_hass(hass)
    await setup(hass, entry)
    await next_interval(hass, freezer)
    assert value(hass, f"{entry.entry_id}_account") == 0.0
