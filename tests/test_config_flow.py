"""Config flow, options flow and the subentry flows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import probatio
import pytest
from homeassistant.components.energy.data import async_get_manager
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.heat_ledger.const import (
    CONF_CIRCUIT,
    CONF_DEMAND_ENTITY,
    CONF_DEMAND_TYPE,
    CONF_ENTITY,
    CONF_FACTOR,
    CONF_KIND,
    CONF_LOSS_KW,
    CONF_METER_ENTITY,
    CONF_PUMP_ENTITY,
    CONF_QUANTITY,
    CONF_SIZE_TYPE,
    CONF_SIZE_VALUE,
    CONF_UNIT,
    DOMAIN,
    SUBENTRY_CIRCUIT,
    SUBENTRY_CONSUMER,
    SUBENTRY_SOURCE,
)
from custom_components.heat_ledger.engine import DemandMethod, SizeMethod
from custom_components.heat_ledger.model import Subentry, build_plant

COMPONENT = Path(__file__).parents[1] / "custom_components" / DOMAIN
ACCURACY = "accuracy"

ENERGY = "sensor.boiler_energy"
POWER = "sensor.boiler_power"
GAS = "sensor.gas_meter"
METER = "sensor.radiator_heat_meter"
PUMP = "binary_sensor.circuit_pump"
HOT_WATER_PUMP = "binary_sensor.hot_water_pump"
CLIMATE = "climate.living_room"
DEMAND = "sensor.living_room_heating"
HUMIDITY = "sensor.living_room_humidity"
BATTERY = "sensor.living_room_battery"
VALVE = "sensor.study_valve"
TEMPERATURE = "sensor.study_temperature"


@pytest.fixture(autouse=True)
def no_setup():
    """The flows are tested on their own; setting up the entry would start the runtime."""
    with patch("custom_components.heat_ledger.async_setup_entry", return_value=True):
        yield


@pytest.fixture
def entities(hass: HomeAssistant) -> None:
    set_states(hass)


def set_states(hass: HomeAssistant) -> None:
    cumulative = {"state_class": "total_increasing", "unit_of_measurement": "kWh", "device_class": "energy"}
    hass.states.async_set(ENERGY, "1234.5", cumulative | {"friendly_name": "Boiler energy"})
    hass.states.async_set(METER, "12.3", cumulative)
    hass.states.async_set(POWER, "3.2", {"state_class": "measurement", "unit_of_measurement": "kW"})
    hass.states.async_set(
        GAS,
        "5432.1",
        {
            "state_class": "total_increasing",
            "unit_of_measurement": "m³",
            "device_class": "gas",
            "friendly_name": "Gas meter",
        },
    )
    hass.states.async_set(PUMP, "on")
    hass.states.async_set(HOT_WATER_PUMP, "off")
    hass.states.async_set(CLIMATE, "heat", {"hvac_action": "heating", "friendly_name": "Living room"})
    hass.states.async_set(VALVE, "40", {"state_class": "measurement", "unit_of_measurement": "%"})
    hass.states.async_set(TEMPERATURE, "21.0", {"state_class": "measurement", "unit_of_measurement": "°C"})


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="House")
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def thermostat(hass: HomeAssistant) -> None:
    """A thermostat device with a heating demand in %, next to humidity and battery in %."""
    device_entry = MockConfigEntry(domain="thermostat_vendor")
    device_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=device_entry.entry_id, identifiers={("thermostat_vendor", "living_room")}
    )
    registry = er.async_get(hass)
    for entity_id, unique_id, device_class in (
        (CLIMATE, "climate", None),
        (BATTERY, "battery", "battery"),
        (HUMIDITY, "humidity", "humidity"),
        (DEMAND, "demand", None),
    ):
        domain, object_id = entity_id.split(".")
        registry.async_get_or_create(
            domain,
            "thermostat_vendor",
            unique_id,
            suggested_object_id=object_id,
            device_id=device.id,
            config_entry=device_entry,
            original_device_class=device_class,
        )
    set_states(hass)  # after the registry, so the IDs stay as suggested
    percent = {"state_class": "measurement", "unit_of_measurement": "%"}
    hass.states.async_set(BATTERY, "80", percent | {"device_class": "battery"})
    hass.states.async_set(HUMIDITY, "55", percent | {"device_class": "humidity"})
    hass.states.async_set(DEMAND, "35", percent | {"friendly_name": "Living room heating"})


def add_subentry(hass: HomeAssistant, entry: MockConfigEntry, subentry_type: str, title: str, data: dict) -> str:
    before = set(entry.subentries)
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(data=data, subentry_type=subentry_type, title=title, unique_id=None),
    )
    (subentry_id,) = set(entry.subentries) - before
    return subentry_id


def serializable(result):
    """Every form must serialize the way the frontend receives it (sections, selectors)."""
    if result.get("data_schema") is not None:
        probatio.to_field_list(result["data_schema"], custom_serializer=cv.custom_serializer)
    return result


async def start(hass: HomeAssistant, entry: MockConfigEntry, subentry_type: str, subentry_id: str | None = None):
    context: dict[str, Any] = {"source": SOURCE_USER}
    if subentry_id is not None:
        context = {"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id}
    return serializable(
        await hass.config_entries.subentries.async_init((entry.entry_id, subentry_type), context=context)
    )


async def configure(hass: HomeAssistant, result, user_input: dict[str, Any]):
    return serializable(await hass.config_entries.subentries.async_configure(result["flow_id"], user_input))


def only_subentry(entry: MockConfigEntry, subentry_type: str):
    (subentry,) = [s for s in entry.subentries.values() if s.subentry_type == subentry_type]
    return subentry


def suggested(result, key: str, section: str | None = None) -> Any:
    schema = result["data_schema"].schema
    if section is not None:
        schema = next(v for k, v in schema.items() if k == section).schema.schema
    marker = next(k for k in schema if k == key)
    return (marker.description or {}).get("suggested_value")


# --- Main entry and options --------------------------------------------------------------------------------------


async def first_setup(hass: HomeAssistant, *steps: dict[str, Any]):
    """Start the first setup and submit the given steps; returns the last result."""
    result = serializable(await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER}))
    for step in steps:
        result = serializable(await hass.config_entries.flow.async_configure(result["flow_id"], step))
    return result


def subentries_by_type(result) -> dict[str, list[ConfigSubentry]]:
    grouped: dict[str, list[ConfigSubentry]] = {}
    for subentry in result["result"].subentries.values():
        grouped.setdefault(subentry.subentry_type, []).append(subentry)
    return grouped


HEAT_SOURCE = {CONF_ENTITY: ENERGY, CONF_QUANTITY: "heat"}


@pytest.mark.usefixtures("entities")
async def test_first_setup_minimal(hass: HomeAssistant) -> None:
    """Every step but the last shows "Next"; empty pumps and thermostats leave only the source."""
    result = await first_setup(hass)
    assert (result["step_id"], result["last_step"]) == ("user", False)
    assert suggested(result, "name") == "Heating"
    result = await first_setup(hass, {"name": " House "})
    assert (result["step_id"], result["last_step"]) == ("source", False)
    result = await first_setup(hass, {"name": "House"}, HEAT_SOURCE)
    assert (result["step_id"], result["last_step"]) == ("pumps", False)
    result = await first_setup(hass, {"name": "House"}, HEAT_SOURCE, {})
    assert (result["step_id"], result["last_step"]) == ("thermostats", True)
    result = await first_setup(hass, {"name": "House"}, HEAT_SOURCE, {}, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "House"
    assert result["result"].options == {}
    (source,) = result["result"].subentries.values()
    assert (source.subentry_type, source.title) == (SUBENTRY_SOURCE, "Boiler energy")
    assert dict(source.data) == HEAT_SOURCE


@pytest.mark.usefixtures("thermostat")
async def test_first_setup_complete(hass: HomeAssistant) -> None:
    """Gas in m³ with a factor, a thermostat with a demand in %, a study thermostat without, both pumps."""
    hass.states.async_set("climate.study", "heat", {"hvac_action": "idle", "friendly_name": "Study"})
    result = await first_setup(
        hass,
        {"name": "House"},
        {"name": " Gas boiler ", CONF_ENTITY: GAS, CONF_QUANTITY: "input"},
        {CONF_FACTOR: 9.6},
        {"circuit_pump": PUMP, "hot_water_pump": HOT_WATER_PUMP},
        {"thermostats": [CLIMATE, "climate.study", CLIMATE]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    grouped = subentries_by_type(result)
    (source,) = grouped[SUBENTRY_SOURCE]
    assert source.title == "Gas boiler"
    assert dict(source.data) == {CONF_ENTITY: GAS, CONF_QUANTITY: "input", CONF_UNIT: "m³", CONF_FACTOR: 9.6}
    (circuit,) = grouped[SUBENTRY_CIRCUIT]
    assert (circuit.title, dict(circuit.data)) == ("Heating circuit", {CONF_PUMP_ENTITY: PUMP})
    living, study, hot_water = grouped[SUBENTRY_CONSUMER]
    assert living.title == "Living room"
    assert dict(living.data) == {
        CONF_KIND: "radiator",
        CONF_SIZE_TYPE: "default",
        CONF_CIRCUIT: circuit.subentry_id,
        CONF_DEMAND_ENTITY: DEMAND,
        CONF_DEMAND_TYPE: "percent",
    }
    assert study.title == "Study"
    assert study.data[CONF_DEMAND_ENTITY] == "climate.study"
    assert study.data[CONF_DEMAND_TYPE] == "hvac_action"
    assert hot_water.title == "Hot water"
    assert dict(hot_water.data) == {
        CONF_KIND: "hot_water",
        CONF_DEMAND_ENTITY: HOT_WATER_PUMP,
        CONF_DEMAND_TYPE: "on_off",
        CONF_SIZE_TYPE: "default",
    }

    plant = build_plant(
        {}, [Subentry(s.subentry_id, s.subentry_type, s.title, s.data) for s in result["result"].subentries.values()]
    )
    assert plant.sources[0].factor == 9.6
    assert {c.circuit_id for c in plant.consumers if not c.hot_water} == {circuit.subentry_id}


@pytest.mark.usefixtures("entities")
async def test_first_setup_factor_step(hass: HomeAssistant) -> None:
    result = await first_setup(
        hass, {"name": "House"}, {"name": "Gas boiler", CONF_ENTITY: GAS, CONF_QUANTITY: "input"}
    )
    assert (result["step_id"], result["last_step"]) == ("factor", False)
    assert result["description_placeholders"] == {"name": "Gas boiler", "unit": "m³"}


@pytest.mark.usefixtures("entities")
async def test_thermostat_without_state_is_named_after_its_device(hass: HomeAssistant) -> None:
    """Right after a restart a thermostat may have no state yet; it is named after its device, as in the UI."""
    vendor = MockConfigEntry(domain="thermostat_vendor")
    vendor.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=vendor.entry_id, identifiers={("thermostat_vendor", "kitchen")}, name="Kitchen"
    )
    er.async_get(hass).async_get_or_create(
        "climate", "thermostat_vendor", "kitchen", suggested_object_id="kitchen", device_id=device.id
    )
    result = await first_setup(hass, {"name": "House"}, HEAT_SOURCE, {}, {"thermostats": ["climate.kitchen"]})
    (radiator,) = subentries_by_type(result)[SUBENTRY_CONSUMER]
    assert radiator.title == "Kitchen"


@pytest.mark.usefixtures("entities")
async def test_first_setup_without_circuit_pump_has_no_circuit(hass: HomeAssistant) -> None:
    result = await first_setup(hass, {"name": "House"}, HEAT_SOURCE, {}, {"thermostats": [CLIMATE]})
    grouped = subentries_by_type(result)
    assert SUBENTRY_CIRCUIT not in grouped
    (radiator,) = grouped[SUBENTRY_CONSUMER]
    assert CONF_CIRCUIT not in radiator.data


@pytest.mark.usefixtures("entities")
async def test_first_setup_in_german(hass: HomeAssistant) -> None:
    hass.config.language = "de"
    result = await first_setup(hass)
    assert suggested(result, "name") == "Heizung"
    result = await first_setup(
        hass,
        {"name": "Haus"},
        HEAT_SOURCE,
        {"circuit_pump": PUMP, "hot_water_pump": HOT_WATER_PUMP},
        {"thermostats": [CLIMATE]},
    )
    titles = sorted(s.title for s in result["result"].subentries.values())
    assert titles == ["Boiler energy", "Heizkreis", "Living room", "Warmwasser"]


@pytest.mark.usefixtures("entities")
async def test_first_setup_suggests_the_energy_dashboard_source(hass: HomeAssistant) -> None:
    await set_gas_sources(hass, GAS)
    result = await first_setup(hass, {"name": "House"})
    assert suggested(result, CONF_ENTITY) == GAS
    assert suggested(result, CONF_QUANTITY) == "input"


@pytest.mark.usefixtures("entities")
async def test_first_setup_errors(hass: HomeAssistant) -> None:
    result = await first_setup(hass, {"name": " "})
    assert result["errors"] == {"name": "name_required"}
    result = await first_setup(hass, {"name": "House"}, {CONF_ENTITY: POWER, CONF_QUANTITY: "heat"})
    assert result["errors"] == {CONF_ENTITY: "not_cumulative"}
    result = await first_setup(hass, {"name": "House"}, {CONF_ENTITY: GAS, CONF_QUANTITY: "input"}, {CONF_FACTOR: -1})
    assert result["errors"] == {CONF_FACTOR: "not_positive"}
    result = await first_setup(hass, {"name": "House"}, HEAT_SOURCE, {"circuit_pump": "binary_sensor.missing"})
    assert result["errors"] == {"circuit_pump": "entity_not_found"}
    result = await first_setup(hass, {"name": "House"}, HEAT_SOURCE, {}, {"thermostats": ["climate.missing"]})
    assert result["errors"] == {"thermostats": "entity_not_found"}


async def test_options_flow(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_LOSS_KW: -1})
    assert result["errors"] == {CONF_LOSS_KW: "not_positive"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_LOSS_KW: 0.5})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_LOSS_KW: 0.5}

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert suggested(result, CONF_LOSS_KW) == 0.5
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert entry.options == {}


# --- Sources -----------------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("entities")
async def test_source_heat(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await start(hass, entry, SUBENTRY_SOURCE)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source"
    assert suggested(result, CONF_ENTITY) is None
    result = await configure(hass, result, {CONF_ENTITY: ENERGY, CONF_QUANTITY: "heat"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    source = only_subentry(entry, SUBENTRY_SOURCE)
    assert source.title == "Boiler energy"
    assert dict(source.data) == {CONF_ENTITY: ENERGY, CONF_QUANTITY: "heat"}


@pytest.mark.usefixtures("entities")
async def test_source_energy_input_asks_for_the_factor(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await start(hass, entry, SUBENTRY_SOURCE)
    result = await configure(hass, result, {CONF_ENTITY: GAS, CONF_QUANTITY: "input"})
    assert result["step_id"] == "factor"
    assert result["description_placeholders"] == {"name": "Gas meter", "unit": "m³"}
    result = await configure(hass, result, {CONF_FACTOR: 0})
    assert result["errors"] == {CONF_FACTOR: "not_positive"}
    result = await configure(hass, result, {CONF_FACTOR: 9.6})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    source = only_subentry(entry, SUBENTRY_SOURCE)
    assert source.title == "Gas meter"
    assert dict(source.data) == {CONF_ENTITY: GAS, CONF_QUANTITY: "input", CONF_UNIT: "m³", CONF_FACTOR: 9.6}


@pytest.mark.usefixtures("entities")
async def test_source_factor_is_per_kwh_for_energy_readings(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Energy readings are converted to kWh before the factor, whatever their unit."""
    hass.states.async_set(ENERGY, "4321", {"state_class": "total_increasing", "unit_of_measurement": "MWh"})
    result = await start(hass, entry, SUBENTRY_SOURCE)
    result = await configure(hass, result, {CONF_ENTITY: ENERGY, CONF_QUANTITY: "input"})
    assert result["description_placeholders"]["unit"] == "kWh"


@pytest.mark.usefixtures("entities")
@pytest.mark.parametrize(
    ("user_input", "errors"),
    [
        ({CONF_ENTITY: "sensor.missing", CONF_QUANTITY: "heat"}, {CONF_ENTITY: "entity_not_found"}),
        ({CONF_ENTITY: POWER, CONF_QUANTITY: "input"}, {CONF_ENTITY: "not_cumulative"}),
        ({CONF_ENTITY: GAS, CONF_QUANTITY: "heat"}, {CONF_QUANTITY: "heat_needs_energy_unit"}),
    ],
)
async def test_source_errors(hass: HomeAssistant, entry: MockConfigEntry, user_input, errors) -> None:
    result = await start(hass, entry, SUBENTRY_SOURCE)
    result = await configure(hass, result, user_input)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors
    assert not entry.subentries


@pytest.mark.usefixtures("entities")
async def test_source_exists(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    sid = add_subentry(hass, entry, SUBENTRY_SOURCE, "Boiler", {CONF_ENTITY: ENERGY, CONF_QUANTITY: "heat"})
    result = await start(hass, entry, SUBENTRY_SOURCE)
    result = await configure(hass, result, {CONF_ENTITY: ENERGY, CONF_QUANTITY: "heat"})
    assert result["errors"] == {CONF_ENTITY: "source_exists"}
    # Its own entity is no conflict when it is reconfigured.
    result = await start(hass, entry, SUBENTRY_SOURCE, sid)
    result = await configure(hass, result, {CONF_ENTITY: ENERGY, CONF_QUANTITY: "heat"})
    assert result["type"] is FlowResultType.ABORT


@pytest.mark.usefixtures("entities")
async def test_source_reconfigure(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    data = {CONF_ENTITY: GAS, CONF_QUANTITY: "input", CONF_FACTOR: 9.6}
    sid = add_subentry(hass, entry, SUBENTRY_SOURCE, "Gas", data)
    result = await start(hass, entry, SUBENTRY_SOURCE, sid)
    assert result["step_id"] == "source"
    assert suggested(result, "name") == "Gas"
    assert suggested(result, CONF_ENTITY) == GAS
    assert suggested(result, CONF_QUANTITY) == "input"
    result = await configure(hass, result, {"name": "Gas boiler", CONF_ENTITY: GAS, CONF_QUANTITY: "input"})
    assert suggested(result, CONF_FACTOR) == 9.6
    result = await configure(hass, result, {CONF_FACTOR: 10.1})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[sid].title == "Gas boiler"
    assert dict(entry.subentries[sid].data) == {
        CONF_ENTITY: GAS,
        CONF_QUANTITY: "input",
        CONF_UNIT: "m³",
        CONF_FACTOR: 10.1,
    }

    # A meter in m³ cannot count heat.
    result = await start(hass, entry, SUBENTRY_SOURCE, sid)
    result = await configure(hass, result, {CONF_ENTITY: GAS, CONF_QUANTITY: "heat"})
    assert result["errors"] == {CONF_QUANTITY: "heat_needs_energy_unit"}
    # Switched to a heat meter: the factor and the unit are dropped.
    result = await configure(hass, result, {CONF_ENTITY: ENERGY, CONF_QUANTITY: "heat"})
    assert result["type"] is FlowResultType.ABORT
    assert list(entry.subentries) == [sid]
    assert dict(entry.subentries[sid].data) == {CONF_ENTITY: ENERGY, CONF_QUANTITY: "heat"}


async def set_gas_sources(hass: HomeAssistant, *statistics: str) -> None:
    manager = await async_get_manager(hass)
    await manager.async_update(
        {
            "energy_sources": [
                {
                    "type": "gas",
                    "stat_energy_from": statistic,
                    "stat_cost": None,
                    "entity_energy_price": None,
                    "number_energy_price": None,
                }
                for statistic in statistics
            ]
        }
    )


@pytest.mark.usefixtures("entities")
@pytest.mark.parametrize(("statistic", "quantity"), [(GAS, "input"), (ENERGY, "heat")])
async def test_source_suggested_from_energy_dashboard(
    hass: HomeAssistant, entry: MockConfigEntry, statistic: str, quantity: str
) -> None:
    """A gas source of the Energy dashboard is preselected; a volume can only be energy input."""
    await set_gas_sources(hass, "opower:gas_usage", statistic)
    result = await start(hass, entry, SUBENTRY_SOURCE)
    assert suggested(result, CONF_ENTITY) == statistic
    assert suggested(result, CONF_QUANTITY) == quantity


@pytest.mark.usefixtures("entities")
async def test_source_suggestion_skips_existing_sources(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await set_gas_sources(hass, ENERGY, GAS)
    add_subentry(hass, entry, SUBENTRY_SOURCE, "Boiler", {CONF_ENTITY: ENERGY, CONF_QUANTITY: "heat"})
    result = await start(hass, entry, SUBENTRY_SOURCE)
    assert suggested(result, CONF_ENTITY) == GAS
    add_subentry(hass, entry, SUBENTRY_SOURCE, "Gas", {CONF_ENTITY: GAS, CONF_QUANTITY: "heat"})
    result = await start(hass, entry, SUBENTRY_SOURCE)
    assert suggested(result, CONF_ENTITY) is None


# --- Circuits ----------------------------------------------------------------------------------------------------


async def test_circuit_minimal(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await start(hass, entry, SUBENTRY_CIRCUIT)
    assert result["step_id"] == "circuit"
    result = await configure(hass, result, {"name": "Ground floor"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    circuit = only_subentry(entry, SUBENTRY_CIRCUIT)
    assert circuit.title == "Ground floor"
    assert dict(circuit.data) == {}


@pytest.mark.usefixtures("entities")
async def test_circuit_pump_and_reconfigure(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await start(hass, entry, SUBENTRY_CIRCUIT)
    result = await configure(hass, result, {"name": "Ground floor", CONF_PUMP_ENTITY: "binary_sensor.missing"})
    assert result["errors"] == {CONF_PUMP_ENTITY: "entity_not_found"}
    result = await configure(hass, result, {"name": "Ground floor", CONF_PUMP_ENTITY: PUMP})
    sid = only_subentry(entry, SUBENTRY_CIRCUIT).subentry_id
    assert dict(entry.subentries[sid].data) == {CONF_PUMP_ENTITY: PUMP}

    result = await start(hass, entry, SUBENTRY_CIRCUIT, sid)
    assert suggested(result, "name") == "Ground floor"
    assert suggested(result, CONF_PUMP_ENTITY) == PUMP
    result = await configure(hass, result, {"name": "Upstairs"})
    assert result["type"] is FlowResultType.ABORT
    assert list(entry.subentries) == [sid]
    assert entry.subentries[sid].title == "Upstairs"
    assert dict(entry.subentries[sid].data) == {}


# --- Radiators ---------------------------------------------------------------------------------------------------


async def radiator_form(hass: HomeAssistant, entry: MockConfigEntry):
    result = await start(hass, entry, SUBENTRY_CONSUMER)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["radiator", "hot_water"]
    return await configure(hass, result, {"next_step_id": "radiator"})


async def test_radiator_minimal(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await radiator_form(hass, entry)
    assert result["step_id"] == "radiator"
    result = await configure(hass, result, {"name": "Bathroom", ACCURACY: {}})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    consumer = only_subentry(entry, SUBENTRY_CONSUMER)
    assert consumer.title == "Bathroom"
    assert dict(consumer.data) == {CONF_KIND: "radiator", CONF_DEMAND_TYPE: "always", CONF_SIZE_TYPE: "default"}


@pytest.mark.usefixtures("entities")
@pytest.mark.parametrize(
    ("demand", "demand_type", "expected"),
    [
        (CLIMATE, None, "hvac_action"),  # no device, so no suggestion
        (PUMP, None, "on_off"),
        (VALVE, None, "percent"),
        (VALVE, "valve", "valve"),
    ],
)
async def test_radiator_demand_type(hass: HomeAssistant, entry: MockConfigEntry, demand, demand_type, expected) -> None:
    result = await radiator_form(hass, entry)
    more = {CONF_DEMAND_TYPE: demand_type} if demand_type else {}
    result = await configure(hass, result, {"name": "Study", CONF_DEMAND_ENTITY: demand, ACCURACY: more})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = only_subentry(entry, SUBENTRY_CONSUMER).data
    assert data[CONF_DEMAND_ENTITY] == demand
    assert data[CONF_DEMAND_TYPE] == expected


@pytest.mark.usefixtures("entities")
async def test_radiator_everything(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    circuit = add_subentry(hass, entry, SUBENTRY_CIRCUIT, "Ground floor", {CONF_PUMP_ENTITY: PUMP})
    result = await radiator_form(hass, entry)
    result = await configure(
        hass,
        result,
        {
            "name": "Study",
            CONF_CIRCUIT: circuit,
            CONF_DEMAND_ENTITY: VALVE,
            ACCURACY: {
                CONF_DEMAND_TYPE: "valve",
                CONF_SIZE_TYPE: "nominal_power",
                CONF_SIZE_VALUE: 1450,
                CONF_METER_ENTITY: METER,
            },
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(only_subentry(entry, SUBENTRY_CONSUMER).data) == {
        CONF_KIND: "radiator",
        CONF_CIRCUIT: circuit,
        CONF_DEMAND_ENTITY: VALVE,
        CONF_DEMAND_TYPE: "valve",
        CONF_SIZE_TYPE: "nominal_power",
        CONF_SIZE_VALUE: 1450,
        CONF_METER_ENTITY: METER,
    }


@pytest.mark.usefixtures("entities")
@pytest.mark.parametrize(
    ("user_input", "errors"),
    [
        ({"name": " "}, {"name": "name_required"}),
        ({CONF_DEMAND_ENTITY: "climate.missing"}, {CONF_DEMAND_ENTITY: "entity_not_found"}),
        ({CONF_DEMAND_ENTITY: TEMPERATURE}, {CONF_DEMAND_ENTITY: "not_percent"}),
        ({CONF_DEMAND_ENTITY: "light.lamp"}, {CONF_DEMAND_ENTITY: "entity_not_found"}),
        ({ACCURACY: {CONF_SIZE_TYPE: "area"}}, {ACCURACY: "size_value_required"}),
        ({ACCURACY: {CONF_SIZE_TYPE: "area", CONF_SIZE_VALUE: 0}}, {ACCURACY: "not_positive"}),
        ({ACCURACY: {CONF_METER_ENTITY: POWER}}, {ACCURACY: "not_cumulative"}),
        ({ACCURACY: {CONF_METER_ENTITY: GAS}}, {ACCURACY: "not_energy"}),
    ],
)
async def test_radiator_errors(hass: HomeAssistant, entry: MockConfigEntry, user_input, errors) -> None:
    result = await radiator_form(hass, entry)
    result = await configure(hass, result, {"name": "Study", ACCURACY: {}} | user_input)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors
    assert not entry.subentries


@pytest.mark.usefixtures("entities")
async def test_radiator_wrong_domain(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    hass.states.async_set("light.lamp", "on")
    result = await radiator_form(hass, entry)
    result = await configure(hass, result, {"name": "Study", CONF_DEMAND_ENTITY: "light.lamp", ACCURACY: {}})
    assert result["errors"] == {CONF_DEMAND_ENTITY: "wrong_domain"}


@pytest.mark.usefixtures("entities")
async def test_radiator_size_value_without_type_is_dropped(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await radiator_form(hass, entry)
    result = await configure(hass, result, {"name": "Study", ACCURACY: {CONF_SIZE_VALUE: 1200}})
    data = only_subentry(entry, SUBENTRY_CONSUMER).data
    assert data[CONF_SIZE_TYPE] == "default"
    assert CONF_SIZE_VALUE not in data


@pytest.mark.usefixtures("thermostat")
@pytest.mark.parametrize(
    ("use", "entity_id", "demand_type"), [(True, DEMAND, "percent"), (False, CLIMATE, "hvac_action")]
)
async def test_radiator_suggests_percent_sensor(
    hass: HomeAssistant, entry: MockConfigEntry, use: bool, entity_id: str, demand_type: str
) -> None:
    result = await radiator_form(hass, entry)
    result = await configure(hass, result, {"name": "Living room", CONF_DEMAND_ENTITY: CLIMATE, ACCURACY: {}})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "demand_suggestion"
    assert result["description_placeholders"] == {"climate": "Living room", "sensor": "Living room heating"}
    result = await configure(hass, result, {"use_suggestion": use})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = only_subentry(entry, SUBENTRY_CONSUMER).data
    assert data[CONF_DEMAND_ENTITY] == entity_id
    assert data[CONF_DEMAND_TYPE] == demand_type


@pytest.mark.usefixtures("thermostat")
async def test_radiator_no_suggestion_without_demand_sensor(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    er.async_get(hass).async_remove(DEMAND)
    hass.states.async_remove(DEMAND)
    result = await radiator_form(hass, entry)
    result = await configure(hass, result, {"name": "Living room", CONF_DEMAND_ENTITY: CLIMATE, ACCURACY: {}})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert only_subentry(entry, SUBENTRY_CONSUMER).data[CONF_DEMAND_TYPE] == "hvac_action"


@pytest.mark.usefixtures("thermostat")
async def test_radiator_reconfigure(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    circuit = add_subentry(hass, entry, SUBENTRY_CIRCUIT, "Ground floor", {})
    data = {
        CONF_KIND: "radiator",
        CONF_CIRCUIT: circuit,
        CONF_DEMAND_ENTITY: DEMAND,
        CONF_DEMAND_TYPE: "percent",
        CONF_SIZE_TYPE: "area",
        CONF_SIZE_VALUE: 18.5,
    }
    sid = add_subentry(hass, entry, SUBENTRY_CONSUMER, "Living room", data)
    result = await start(hass, entry, SUBENTRY_CONSUMER, sid)
    assert result["step_id"] == "radiator"
    assert suggested(result, "name") == "Living room"
    assert suggested(result, CONF_CIRCUIT) == circuit
    assert suggested(result, CONF_DEMAND_ENTITY) == DEMAND
    assert suggested(result, CONF_SIZE_TYPE, ACCURACY) == "area"
    assert suggested(result, CONF_SIZE_VALUE, ACCURACY) == 18.5

    # Switching to the thermostat offers the % sensor again; accepting it ends the flow in place.
    result = await configure(
        hass,
        result,
        {
            "name": "Lounge",
            CONF_DEMAND_ENTITY: CLIMATE,
            ACCURACY: {CONF_SIZE_TYPE: "nominal_power", CONF_SIZE_VALUE: 2000},
        },
    )
    assert result["step_id"] == "demand_suggestion"
    result = await configure(hass, result, {"use_suggestion": False})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert sid in entry.subentries
    assert len(entry.subentries) == 2
    assert entry.subentries[sid].title == "Lounge"
    assert dict(entry.subentries[sid].data) == {
        CONF_KIND: "radiator",
        CONF_DEMAND_ENTITY: CLIMATE,
        CONF_DEMAND_TYPE: "hvac_action",
        CONF_SIZE_TYPE: "nominal_power",
        CONF_SIZE_VALUE: 2000,
    }


async def test_radiator_reconfigure_removed_circuit(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    sid = add_subentry(
        hass,
        entry,
        SUBENTRY_CONSUMER,
        "Bathroom",
        {CONF_KIND: "radiator", CONF_CIRCUIT: "gone", CONF_DEMAND_TYPE: "always", CONF_SIZE_TYPE: "default"},
    )
    result = await start(hass, entry, SUBENTRY_CONSUMER, sid)
    assert not any(k == CONF_CIRCUIT for k in result["data_schema"].schema)
    result = await configure(hass, result, {"name": "Bathroom", ACCURACY: {}})
    assert CONF_CIRCUIT not in entry.subentries[sid].data


# --- Hot water ---------------------------------------------------------------------------------------------------


async def hot_water_form(hass: HomeAssistant, entry: MockConfigEntry):
    result = await start(hass, entry, SUBENTRY_CONSUMER)
    return await configure(hass, result, {"next_step_id": "hot_water"})


@pytest.mark.usefixtures("entities")
async def test_hot_water_minimal(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await hot_water_form(hass, entry)
    assert result["step_id"] == "hot_water"
    result = await configure(hass, result, {"name": "Hot water", CONF_DEMAND_ENTITY: HOT_WATER_PUMP, ACCURACY: {}})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(only_subentry(entry, SUBENTRY_CONSUMER).data) == {
        CONF_KIND: "hot_water",
        CONF_DEMAND_ENTITY: HOT_WATER_PUMP,
        CONF_DEMAND_TYPE: "on_off",
        CONF_SIZE_TYPE: "default",
    }


@pytest.mark.usefixtures("entities")
@pytest.mark.parametrize(
    ("user_input", "errors"),
    [
        ({CONF_DEMAND_ENTITY: "binary_sensor.missing"}, {CONF_DEMAND_ENTITY: "entity_not_found"}),
        ({ACCURACY: {"charging_power_kw": 0}}, {ACCURACY: "not_positive"}),
        ({ACCURACY: {CONF_METER_ENTITY: "sensor.missing"}}, {ACCURACY: "entity_not_found"}),
        ({ACCURACY: {CONF_METER_ENTITY: GAS}}, {ACCURACY: "not_energy"}),
    ],
)
async def test_hot_water_errors(hass: HomeAssistant, entry: MockConfigEntry, user_input, errors) -> None:
    result = await hot_water_form(hass, entry)
    result = await configure(
        hass, result, {"name": "Hot water", CONF_DEMAND_ENTITY: HOT_WATER_PUMP, ACCURACY: {}} | user_input
    )
    assert result["errors"] == errors
    assert not entry.subentries


@pytest.mark.usefixtures("entities")
async def test_hot_water_reconfigure(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await hot_water_form(hass, entry)
    result = await configure(
        hass,
        result,
        {"name": "Hot water", CONF_DEMAND_ENTITY: HOT_WATER_PUMP, ACCURACY: {"charging_power_kw": 12}},
    )
    sid = only_subentry(entry, SUBENTRY_CONSUMER).subentry_id
    assert entry.subentries[sid].data[CONF_SIZE_TYPE] == "nominal_power"
    assert entry.subentries[sid].data[CONF_SIZE_VALUE] == 12000

    result = await start(hass, entry, SUBENTRY_CONSUMER, sid)
    assert result["step_id"] == "hot_water"
    assert suggested(result, "charging_power_kw", ACCURACY) == 12
    result = await configure(
        hass, result, {"name": "Hot water", CONF_DEMAND_ENTITY: HOT_WATER_PUMP, ACCURACY: {CONF_METER_ENTITY: METER}}
    )
    assert result["type"] is FlowResultType.ABORT
    assert list(entry.subentries) == [sid]
    assert dict(entry.subentries[sid].data) == {
        CONF_KIND: "hot_water",
        CONF_DEMAND_ENTITY: HOT_WATER_PUMP,
        CONF_DEMAND_TYPE: "on_off",
        CONF_SIZE_TYPE: "default",
        CONF_METER_ENTITY: METER,
    }


# --- The stored format is what the model reads -------------------------------------------------------------------


@pytest.mark.usefixtures("thermostat")
async def test_flows_build_a_plant(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await start(hass, entry, SUBENTRY_SOURCE)
    result = await configure(hass, result, {CONF_ENTITY: ENERGY, CONF_QUANTITY: "input"})
    await configure(hass, result, {CONF_FACTOR: 0.9})
    result = await start(hass, entry, SUBENTRY_CIRCUIT)
    await configure(hass, result, {"name": "Ground floor", CONF_PUMP_ENTITY: PUMP})
    circuit = only_subentry(entry, SUBENTRY_CIRCUIT).subentry_id
    result = await radiator_form(hass, entry)
    result = await configure(
        hass, result, {"name": "Living room", CONF_CIRCUIT: circuit, CONF_DEMAND_ENTITY: CLIMATE, ACCURACY: {}}
    )
    await configure(hass, result, {"use_suggestion": True})
    result = await radiator_form(hass, entry)
    await configure(hass, result, {"name": "Study", ACCURACY: {CONF_SIZE_TYPE: "area", CONF_SIZE_VALUE: 12}})
    result = await hot_water_form(hass, entry)
    await configure(hass, result, {"name": "Hot water", CONF_DEMAND_ENTITY: HOT_WATER_PUMP, ACCURACY: {}})

    plant = build_plant(
        entry.options,
        [Subentry(s.subentry_id, s.subentry_type, s.title, s.data) for s in entry.subentries.values()],
    )
    assert plant.sources[0].factor == 0.9
    assert plant.circuits[0].pump_key == PUMP
    by_title = {entry.subentries[c.id].title: c for c in plant.consumers}
    assert by_title["Living room"].circuit_id == circuit
    assert by_title["Living room"].demand.method is DemandMethod.PERCENT
    assert by_title["Living room"].demand.key == DEMAND
    assert by_title["Study"].demand.method is DemandMethod.ALWAYS
    assert by_title["Study"].size.method is SizeMethod.AREA
    assert by_title["Hot water"].hot_water
    assert by_title["Hot water"].demand.method is DemandMethod.ON_OFF


# --- Translations ------------------------------------------------------------------------------------------------


def _keys(data: Any, prefix: str = "") -> set[str]:
    if not isinstance(data, dict):
        return {prefix}
    return set().union(*(_keys(v, f"{prefix}.{k}") for k, v in data.items()))


def test_translations_match() -> None:
    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    english = json.loads((COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))
    german = json.loads((COMPONENT / "translations" / "de.json").read_text(encoding="utf-8"))
    assert strings == english
    assert _keys(german) == _keys(english)


def test_every_subentry_has_a_text_for_its_end() -> None:
    """Editing a subentry ends with reconfigure_successful; custom integrations get no text for it from Core."""
    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    for subentry_type in (SUBENTRY_SOURCE, SUBENTRY_CIRCUIT, SUBENTRY_CONSUMER):
        assert strings["config_subentries"][subentry_type]["abort"]["reconfigure_successful"]
