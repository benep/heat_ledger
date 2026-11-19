"""A small plant for the Home Assistant layer: two sources, one circuit with two radiators, hot water."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.heat_ledger.const import (
    CONF_CIRCUIT,
    CONF_DEMAND_ENTITY,
    CONF_DEMAND_TYPE,
    CONF_ENTITY,
    CONF_FACTOR,
    CONF_KIND,
    CONF_PUMP_ENTITY,
    CONF_QUANTITY,
    CONF_SIZE_TYPE,
    CONF_SIZE_VALUE,
    DOMAIN,
    KIND_HOT_WATER,
    KIND_RADIATOR,
    QUANTITY_HEAT,
    QUANTITY_INPUT,
    SUBENTRY_CIRCUIT,
    SUBENTRY_CONSUMER,
    SUBENTRY_SOURCE,
)

BOILER = "sensor.boiler_energy"
HEAT_PUMP = "sensor.heat_pump_energy"
PUMP = "binary_sensor.circuit_pump"
LIVING_DEMAND = "sensor.living_demand"
HOT_WATER_PUMP = "binary_sensor.hot_water_pump"

SUBENTRIES = [
    {
        "subentry_id": "boiler",
        "subentry_type": SUBENTRY_SOURCE,
        "title": "Boiler",
        "unique_id": None,
        "data": {CONF_ENTITY: BOILER, CONF_QUANTITY: QUANTITY_HEAT},
    },
    {
        "subentry_id": "heat_pump",
        "subentry_type": SUBENTRY_SOURCE,
        "title": "Heat pump",
        "unique_id": None,
        "data": {CONF_ENTITY: HEAT_PUMP, CONF_QUANTITY: QUANTITY_INPUT, CONF_FACTOR: 3.0},
    },
    {
        "subentry_id": "ground_floor",
        "subentry_type": SUBENTRY_CIRCUIT,
        "title": "Ground floor",
        "unique_id": None,
        "data": {CONF_PUMP_ENTITY: PUMP},
    },
    {
        "subentry_id": "living",
        "subentry_type": SUBENTRY_CONSUMER,
        "title": "Living room",
        "unique_id": None,
        "data": {
            CONF_KIND: KIND_RADIATOR,
            CONF_CIRCUIT: "ground_floor",
            CONF_DEMAND_ENTITY: LIVING_DEMAND,
            CONF_DEMAND_TYPE: "percent",
            CONF_SIZE_TYPE: "nominal_power",
            CONF_SIZE_VALUE: 2000,
        },
    },
    {
        "subentry_id": "bath",
        "subentry_type": SUBENTRY_CONSUMER,
        "title": "Bath",
        "unique_id": None,
        "data": {CONF_KIND: KIND_RADIATOR, CONF_CIRCUIT: "ground_floor", CONF_SIZE_TYPE: "area", CONF_SIZE_VALUE: 10},
    },
    {
        "subentry_id": "hot_water",
        "subentry_type": SUBENTRY_CONSUMER,
        "title": "Hot water",
        "unique_id": None,
        "data": {CONF_KIND: KIND_HOT_WATER, CONF_DEMAND_ENTITY: HOT_WATER_PUMP, CONF_DEMAND_TYPE: "on_off"},
    },
]

# Weights with the initial states: living 2000 W × 0.5, bath 10 m² × 60 W/m², hot water pump off.
LIVING_SHARE = 1000 / 1600
BATH_SHARE = 600 / 1600


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def next_interval(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Move the clock to the next 5-minute boundary and let the interval close."""
    now = dt_util.utcnow().replace(second=0, microsecond=0)
    target = now + timedelta(minutes=5 - now.minute % 5)
    freezer.move_to(target)
    async_fire_time_changed(hass, target)
    await hass.async_block_till_done()


async def later(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def entity_id(hass: HomeAssistant, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)


def value(hass: HomeAssistant, unique_id: str) -> float:
    eid = entity_id(hass, unique_id)
    assert eid is not None, unique_id
    state = hass.states.get(eid)
    assert state is not None, eid
    return float(state.state)


def attributes(hass: HomeAssistant, unique_id: str) -> dict:
    eid = entity_id(hass, unique_id)
    assert eid is not None, unique_id
    return dict(hass.states.get(eid).attributes)
