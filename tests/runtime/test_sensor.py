"""The sensors: split after an interval, account, unavailable inputs, attributes, restore."""

from __future__ import annotations

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import area_registry as ar
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


async def test_source_sensor_only_for_energy_input(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    await setup(hass, entry)
    assert entity_id(hass, "boiler_source_heat") is None
    assert entity_id(hass, "heat_pump_source_heat") is not None

    hass.states.async_set(HEAT_PUMP, "51.0", {"unit_of_measurement": "kWh"})
    await next_interval(hass, freezer)
    assert value(hass, "heat_pump_source_heat") == pytest.approx(3.0)  # factor 3
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


async def test_consumer_lands_in_the_area_of_its_thermostat(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """The Energy dashboard groups consumers by floor and area: take the area of the demand entity's device."""
    other = MockConfigEntry(domain="thermostats")
    other.add_to_hass(hass)
    area = ar.async_get(hass).async_create("Living room")
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("thermostats", "living")}, name="Thermostat"
    )
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    state = hass.states.get(LIVING_DEMAND)
    hass.states.async_remove(LIVING_DEMAND)  # else the registry picks another entity ID
    thermostat = er.async_get(hass).async_get_or_create(
        "sensor", "thermostats", "living_demand", suggested_object_id="living_demand", device_id=device.id
    )
    assert thermostat.entity_id == LIVING_DEMAND
    hass.states.async_set(LIVING_DEMAND, state.state, state.attributes)

    await setup(hass, entry)

    def device_area(unique_id: str) -> str | None:
        entity = er.async_get(hass).async_get(entity_id(hass, unique_id))
        return dr.async_get(hass).async_get(entity.device_id).area_id

    assert device_area("living_heat") == area.id
    assert device_area("hot_water_heat") is None  # its pump is in no area


@pytest.mark.parametrize(
    "in_boiler_room",
    [
        pytest.param((PUMP, HEAT_PUMP, BOILER), id="meters in the boiler room"),
        pytest.param((PUMP,), id="only the pump: source meters are templates without area"),
    ],
)
async def test_circuit_source_and_plant_get_the_area_of_their_input(
    hass: HomeAssistant, entry: MockConfigEntry, in_boiler_room: tuple[str, ...]
) -> None:
    """The dialog after the setup comes prefilled: the circuit by its pump, source and plant by the source meter,
    or by the pump when the meter has no area."""
    boiler_room = ar.async_get(hass).async_create("Boiler room")
    registry = er.async_get(hass)
    for entity in in_boiler_room:
        state = hass.states.get(entity)
        hass.states.async_remove(entity)  # else the registry picks another entity ID
        domain, object_id = entity.split(".")
        registered = registry.async_get_or_create(domain, "meters", object_id, suggested_object_id=object_id)
        registry.async_update_entity(registered.entity_id, area_id=boiler_room.id)
        hass.states.async_set(entity, state.state, state.attributes)

    await setup(hass, entry)

    def device_area(unique_id: str) -> str | None:
        entity = registry.async_get(entity_id(hass, unique_id))
        return dr.async_get(hass).async_get(entity.device_id).area_id

    assert device_area("ground_floor_heat") == boiler_room.id
    assert device_area("heat_pump_source_heat") == boiler_room.id
    assert device_area(f"{entry.entry_id}_account") == boiler_room.id


@pytest.mark.parametrize(
    ("language", "models"),
    [
        ("en", ["Room heating", "Hot water heating", "Heating circuit", "Heat source", "Heating system"]),
        ("de", ["Raumheizung", "Warmwasserbereitung", "Heizkreis", "Wärmequelle", "Heizungsanlage"]),
    ],
)
async def test_devices_say_what_they_are(
    hass: HomeAssistant, entry: MockConfigEntry, language: str, models: list[str]
) -> None:
    """Home Assistant shows the model next to the icon (after the setup, in device lists): the kind of device."""
    hass.config.language = language
    await setup(hass, entry)

    def device(unique_id: str) -> dr.DeviceEntry:
        return dr.async_get(hass).async_get(er.async_get(hass).async_get(entity_id(hass, unique_id)).device_id)

    keys = ["living_heat", "hot_water_heat", "ground_floor_heat", "heat_pump_source_heat", f"{entry.entry_id}_account"]
    assert [device(key).model for key in keys] == models
    assert {device(key).manufacturer for key in keys} == {"Heat Ledger"}


async def test_source_switched_to_heat_leaves_no_leftover(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await setup(hass, entry)
    assert entity_id(hass, "heat_pump_source_heat") is not None
    subentry = entry.subentries["heat_pump"]
    hass.config_entries.async_update_subentry(entry, subentry, data={"entity_id": HEAT_PUMP, "quantity": "heat"})
    await hass.async_block_till_done()
    assert entity_id(hass, "heat_pump_source_heat") is None
