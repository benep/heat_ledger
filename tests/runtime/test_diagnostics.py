"""Diagnostics download."""

from __future__ import annotations

import json

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.heat_ledger.diagnostics import async_get_config_entry_diagnostics

from .common import BOILER, LIVING_DEMAND, PUMP, next_interval, setup

pytestmark = pytest.mark.usefixtures("inputs")


async def test_diagnostics_of_a_running_plant(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    await setup(hass, entry)
    hass.states.async_set(BOILER, "110.0", {"unit_of_measurement": "kWh"})
    await next_interval(hass, freezer)

    result = await async_get_config_entry_diagnostics(hass, entry)
    json.dumps(result)  # the download is JSON

    assert result["loaded"] is True
    assert result["versions"]["heat_ledger"]
    assert {s["id"] for s in result["subentries"]} == {s.subentry_id for s in entry.subentries.values()}
    assert result["inputs"][BOILER] == {
        "read_as": "energy",
        "state": "110.0",
        "unit": "kWh",
        "hvac_action": None,
        "last_changed": hass.states.get(BOILER).last_changed.isoformat(),
    }
    assert result["inputs"][PUMP]["read_as"] == "bool"
    assert result["inputs"][LIVING_DEMAND]["read_as"] == "percent"
    assert result["consumers"]["living"]["level"] == "estimated"
    assert result["circuits"]["ground_floor"]["total_kwh"] == pytest.approx(10.0)
    assert result["ledger"]["version"] == 1
    assert result["blocked_by"] == []


async def test_diagnostics_of_a_plant_that_did_not_load(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["loaded"] is False
    assert len(result["subentries"]) == len(entry.subentries)
