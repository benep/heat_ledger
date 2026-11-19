"""Config entry data to plant, entity states to readings."""

from __future__ import annotations

import pytest
from homeassistant.const import UnitOfEnergy
from homeassistant.util.unit_conversion import EnergyConverter

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
    KIND_HOT_WATER,
    KIND_RADIATOR,
    QUANTITY_HEAT,
    QUANTITY_INPUT,
    SUBENTRY_CIRCUIT,
    SUBENTRY_CONSUMER,
    SUBENTRY_SOURCE,
)
from custom_components.heat_ledger.engine import ActiveMethod, DemandMethod, SizeMethod
from custom_components.heat_ledger.model import (
    ENERGY_TO_KWH,
    InputKind,
    Subentry,
    build_plant,
    input_kinds,
    parse_state,
)

SUBENTRIES = [
    Subentry("src", SUBENTRY_SOURCE, "Boiler", {CONF_ENTITY: "sensor.gas", CONF_QUANTITY: QUANTITY_HEAT}),
    Subentry(
        "hp",
        SUBENTRY_SOURCE,
        "Heat pump",
        {CONF_ENTITY: "sensor.hp", CONF_QUANTITY: QUANTITY_INPUT, CONF_FACTOR: 3.5},
    ),
    Subentry("c1", SUBENTRY_CIRCUIT, "Radiators", {CONF_PUMP_ENTITY: "binary_sensor.pump"}),
    Subentry("c2", SUBENTRY_CIRCUIT, "Floor", {}),
    Subentry(
        "living",
        SUBENTRY_CONSUMER,
        "Living room",
        {
            CONF_KIND: KIND_RADIATOR,
            CONF_CIRCUIT: "c1",
            CONF_DEMAND_ENTITY: "sensor.living_heating",
            CONF_DEMAND_TYPE: "percent",
            CONF_SIZE_TYPE: "nominal_power",
            CONF_SIZE_VALUE: 1450,
        },
    ),
    Subentry(
        "bath",
        SUBENTRY_CONSUMER,
        "Bath",
        {
            CONF_KIND: KIND_RADIATOR,
            CONF_CIRCUIT: "gone",
            CONF_DEMAND_ENTITY: "climate.bath",
            CONF_DEMAND_TYPE: "hvac_action",
        },
    ),
    Subentry(
        "dhw",
        SUBENTRY_CONSUMER,
        "Hot water",
        {CONF_KIND: KIND_HOT_WATER, CONF_DEMAND_ENTITY: "binary_sensor.dhw_pump", CONF_DEMAND_TYPE: "on_off"},
    ),
    Subentry("meter", SUBENTRY_CONSUMER, "Workshop", {CONF_KIND: KIND_RADIATOR, CONF_METER_ENTITY: "sensor.hm"}),
]


def test_build_plant():
    plant = build_plant({CONF_LOSS_KW: 0.2}, SUBENTRIES)
    assert plant.loss_kw == 0.2
    assert [(s.id, s.factor) for s in plant.sources] == [
        ("src", 1.0),
        ("hp", 3.5),
    ]
    assert [(c.id, c.active) for c in plant.circuits] == [("c1", ActiveMethod.PUMP), ("c2", ActiveMethod.ALWAYS)]
    living, bath, dhw, meter = plant.consumers
    assert living.circuit_id == "c1"
    assert living.size.method is SizeMethod.NOMINAL_POWER and living.size.watts == 1450
    assert living.demand.method is DemandMethod.PERCENT
    assert bath.circuit_id is None  # the circuit was removed
    assert bath.size.watts == 1000
    assert dhw.hot_water and dhw.circuit_id is None and dhw.size.watts == 10000
    assert meter.meter_key == "sensor.hm" and meter.demand.method is DemandMethod.ALWAYS


def test_input_kinds():
    assert input_kinds(build_plant({}, SUBENTRIES)) == {
        "sensor.gas": InputKind.ENERGY,
        "sensor.hp": InputKind.ENERGY,
        "binary_sensor.pump": InputKind.BOOL,
        "sensor.living_heating": InputKind.PERCENT,
        "climate.bath": InputKind.HVAC_ACTION,
        "binary_sensor.dhw_pump": InputKind.BOOL,
        "sensor.hm": InputKind.ENERGY,
    }


def test_demand_type_without_entity_is_always():
    sub = Subentry("x", SUBENTRY_CONSUMER, "X", {CONF_KIND: KIND_RADIATOR, CONF_DEMAND_TYPE: "percent"})
    assert build_plant({}, [*SUBENTRIES[:1], sub]).consumers[0].demand.method is DemandMethod.ALWAYS


@pytest.mark.parametrize(
    ("kind", "state", "attributes", "expected"),
    [
        (InputKind.BOOL, "on", None, True),
        (InputKind.BOOL, "off", None, False),
        (InputKind.BOOL, "unavailable", None, None),
        (InputKind.HVAC_ACTION, "auto", {"hvac_action": "heating"}, True),
        (InputKind.HVAC_ACTION, "heat", {"hvac_action": "preheating"}, True),
        (InputKind.HVAC_ACTION, "auto", {"hvac_action": "idle"}, False),
        (InputKind.HVAC_ACTION, "off", {}, False),
        (InputKind.HVAC_ACTION, "heat", {}, None),
        (InputKind.PERCENT, "74", None, 0.74),
        (InputKind.PERCENT, "unknown", None, None),
        (InputKind.ENERGY, "1500", {"unit_of_measurement": "Wh"}, 1.5),
        (InputKind.ENERGY, "3.6", {"unit_of_measurement": "MJ"}, 1.0),
        (InputKind.ENERGY, "2", {"unit_of_measurement": "Gcal"}, 2 * 4184 * 1000 / 3600),
        (InputKind.ENERGY, "10", {"unit_of_measurement": "thm"}, 10 * 105480400 / 3.6e6),
        (InputKind.ENERGY, "12", {"unit_of_measurement": "m³"}, 12.0),
        (InputKind.ENERGY, "abc", None, None),
        (InputKind.ENERGY, None, None, None),
    ],
)
def test_parse_state(kind, state, attributes, expected):
    result = parse_state(kind, state, attributes)
    assert result == (pytest.approx(expected) if isinstance(expected, float) else expected)


@pytest.mark.parametrize("unit", sorted(UnitOfEnergy))
def test_energy_units_match_home_assistant(unit):
    """Every energy unit of Home Assistant is converted, with its own factor."""
    assert unit in ENERGY_TO_KWH
    assert ENERGY_TO_KWH[unit] == pytest.approx(EnergyConverter.convert(1, unit, UnitOfEnergy.KILO_WATT_HOUR))
