"""The sensors: split after an interval, account, unavailable inputs, attributes, restore."""

from __future__ import annotations

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache_with_extra_data

from custom_components.heat_ledger.const import DOMAIN

from .common import (
    BATH_SHARE,
    BOILER,
    HEAT_PUMP,
    LIVING_DEMAND,
    LIVING_SHARE,
    PUMP,
    attributes,
    entity_id,
    next_interval,
    setup,
    value,
)

pytestmark = pytest.mark.usefixtures("inputs")


async def test_heat_is_split_after_an_interval(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    await setup(hass, entry)
    hass.states.async_set(BOILER, "101.0", {"unit_of_measurement": "kWh"})
    await hass.async_block_till_done()
    assert value(hass, "living_heat") == 0.0  # nothing before the interval closes

    await next_interval(hass, freezer)
    assert value(hass, "living_heat") == pytest.approx(LIVING_SHARE)
    assert value(hass, "bath_heat") == pytest.approx(BATH_SHARE)
    assert value(hass, "hot_water_heat") == 0.0
    assert value(hass, "ground_floor_heat") == pytest.approx(1.0)
    assert value(hass, f"{entry.entry_id}_account") == pytest.approx(0.0)


async def test_energy_units_are_converted(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    hass.states.async_set(BOILER, "100000", {"unit_of_measurement": "Wh"})
    await setup(hass, entry)
    hass.states.async_set(BOILER, "102000", {"unit_of_measurement": "Wh"})
    await next_interval(hass, freezer)
    assert value(hass, "ground_floor_heat") == pytest.approx(2.0)


async def test_pump_off_keeps_the_heat_in_the_account(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    hass.states.async_set(PUMP, "off")
    await setup(hass, entry)
    hass.states.async_set(BOILER, "102.0", {"unit_of_measurement": "kWh"})
    await next_interval(hass, freezer)
    assert value(hass, "ground_floor_heat") == 0.0
    assert value(hass, f"{entry.entry_id}_account") == pytest.approx(2.0)

    hass.states.async_set(PUMP, "on")
    await next_interval(hass, freezer)
    assert value(hass, "ground_floor_heat") == pytest.approx(2.0)
    assert value(hass, f"{entry.entry_id}_account") == pytest.approx(0.0)


async def test_unavailable_input_blocks_the_split(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    await setup(hass, entry)
    hass.states.async_set(LIVING_DEMAND, "unavailable")
    hass.states.async_set(BOILER, "101.0", {"unit_of_measurement": "kWh"})
    await next_interval(hass, freezer)
    assert value(hass, "bath_heat") == 0.0
    assert value(hass, f"{entry.entry_id}_account") == pytest.approx(1.0)
    assert attributes(hass, f"{entry.entry_id}_account")["blocked_by"] == [LIVING_DEMAND]

    hass.states.async_set(LIVING_DEMAND, "50")
    await next_interval(hass, freezer)
    assert value(hass, "living_heat") == pytest.approx(LIVING_SHARE)
    assert attributes(hass, f"{entry.entry_id}_account")["blocked_by"] == []


async def test_missing_input_at_start(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    """An input that does not exist yet (its integration loads later) counts as unavailable."""
    hass.states.async_remove(PUMP)
    await setup(hass, entry)
    hass.states.async_set(BOILER, "101.0", {"unit_of_measurement": "kWh"})
    await next_interval(hass, freezer)
    assert value(hass, f"{entry.entry_id}_account") == pytest.approx(1.0)
    assert attributes(hass, f"{entry.entry_id}_account")["blocked_by"] == [PUMP]


async def test_source_sensor_only_for_converted_sources(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    await setup(hass, entry)
    assert entity_id(hass, "boiler_source_heat") is None
    assert entity_id(hass, "heat_pump_source_heat") is not None

    hass.states.async_set(HEAT_PUMP, "51.0", {"unit_of_measurement": "kWh"})
    await next_interval(hass, freezer)
    assert value(hass, "heat_pump_source_heat") == pytest.approx(3.0)  # COP 3
    assert value(hass, "ground_floor_heat") == pytest.approx(3.0)


async def test_attributes(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    await setup(hass, entry)
    hass.states.async_set(BOILER, "101.0", {"unit_of_measurement": "kWh"})
    hass.states.async_set(HEAT_PUMP, "51.0", {"unit_of_measurement": "kWh"})
    await next_interval(hass, freezer)

    living = attributes(hass, "living_heat")
    assert living["level"] == "estimated"
    assert living["basis"] == {"size": "nominal_power", "demand": "percent", "circuit": "pump"}
    assert living["next_improvement"] is None
    assert living["by_source"] == {
        "Boiler": pytest.approx(round(LIVING_SHARE, 3)),
        "Heat pump": pytest.approx(round(3 * LIVING_SHARE, 3)),
    }

    circuit = attributes(hass, "ground_floor_heat")
    assert circuit["level"] == "estimated"
    assert circuit["basis"] == {"size": "area", "demand": "always", "circuit": "pump"}
    assert circuit["next_improvement"] == "demand_hvac_action"
    assert circuit["by_source"] == {"Boiler": pytest.approx(1.0), "Heat pump": pytest.approx(3.0)}

    hot_water = attributes(hass, "hot_water_heat")
    assert hot_water["basis"] == {"size": "default", "demand": "on_off", "circuit": "always"}
    assert hot_water["next_improvement"] == "size_nominal_power"


async def test_entities_and_devices(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await setup(hass, entry)
    entities = er.async_get(hass)
    devices = dr.async_get(hass)

    living = entities.async_get(entity_id(hass, "living_heat"))
    assert living.config_subentry_id == "living"
    assert living.translation_key == "heat"
    device = devices.async_get(living.device_id)
    assert device.name == "Living room"
    assert device.identifiers == {(DOMAIN, "living")}
    assert device.entry_type is dr.DeviceEntryType.SERVICE
    state = hass.states.get(living.entity_id)
    assert state.attributes["device_class"] == SensorDeviceClass.ENERGY
    assert state.attributes["state_class"] == SensorStateClass.TOTAL_INCREASING
    assert state.attributes["unit_of_measurement"] == "kWh"

    account = entities.async_get(entity_id(hass, f"{entry.entry_id}_account"))
    assert account.entity_category is EntityCategory.DIAGNOSTIC
    assert account.config_subentry_id is None
    assert devices.async_get(account.device_id).identifiers == {(DOMAIN, entry.entry_id)}
    state = hass.states.get(account.entity_id)
    assert state.attributes["device_class"] == SensorDeviceClass.ENERGY_STORAGE
    assert state.attributes["state_class"] == SensorStateClass.MEASUREMENT


async def test_counter_continues_from_restored_value(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    """Without the ledger's store (lost or older), the counter continues from its last state."""
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "living_heat",
        suggested_object_id="living_room_heat",
        config_entry=entry,
        config_subentry_id="living",
    )
    restored = State("sensor.living_room_heat", "5.0")
    mock_restore_cache_with_extra_data(hass, [(restored, {"native_value": 5.0, "native_unit_of_measurement": "kWh"})])
    await setup(hass, entry)
    assert value(hass, "living_heat") == pytest.approx(5.0)

    hass.states.async_set(BOILER, "101.0", {"unit_of_measurement": "kWh"})
    await next_interval(hass, freezer)
    assert value(hass, "living_heat") == pytest.approx(5.0 + LIVING_SHARE)
