"""Fixtures for the Home Assistant layer."""

from __future__ import annotations

import importlib.util
import types

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigFlow
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.heat_ledger.const import DOMAIN

from .common import BOILER, HEAT_PUMP, HOT_WATER_PUMP, LIVING_DEMAND, PUMP, SUBENTRIES

CONFIG_FLOW = "custom_components.heat_ledger.config_flow"


@pytest.fixture(autouse=True)
async def config_flow_stub(hass: HomeAssistant) -> None:
    """Setting up an entry imports the config flow platform. Stand in for it until it exists."""
    if importlib.util.find_spec(CONFIG_FLOW) is not None:
        return

    class StubConfigFlow(ConfigFlow, domain=DOMAIN):
        VERSION = 1

    module = types.ModuleType(CONFIG_FLOW)
    module.StubConfigFlow = StubConfigFlow
    integration = await async_get_integration(hass, DOMAIN)
    integration._cache[f"{DOMAIN}.config_flow"] = module


@pytest.fixture(autouse=True)
def start_time(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to("2026-01-05 10:00:30+00:00")


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Heating", options={}, subentries_data=SUBENTRIES)
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def inputs(hass: HomeAssistant) -> None:
    """Initial states: pump on, living room at 50 % demand, hot water pump off."""
    hass.states.async_set(BOILER, "100.0", {"unit_of_measurement": "kWh"})
    hass.states.async_set(HEAT_PUMP, "50.0", {"unit_of_measurement": "kWh"})
    hass.states.async_set(PUMP, "on")
    hass.states.async_set(LIVING_DEMAND, "50")
    hass.states.async_set(HOT_WATER_PUMP, "off")
