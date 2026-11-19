"""From config entry data to the engine, and from entity states to readings. No Home Assistant imports.

The config flow writes the formats in const.py, the runtime calls build_plant() and parse_state() with them.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .const import (
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
    KIND_HOT_WATER,
    QUANTITY_INPUT,
    SUBENTRY_CIRCUIT,
    SUBENTRY_CONSUMER,
    SUBENTRY_SOURCE,
)
from .engine import (
    ActiveMethod,
    CircuitSpec,
    ConsumerSpec,
    Demand,
    DemandMethod,
    PlantSpec,
    Reading,
    Size,
    SizeMethod,
    SourceSpec,
)
from .engine.spec import DEFAULT_HOT_WATER_POWER_W

INVALID_STATES = {"unavailable", "unknown", "none", ""}
HEATING_ACTIONS = {"heating", "preheating"}
# kWh per unit, for every energy unit Home Assistant knows (a test checks this against its EnergyConverter).
_KWH_PER_KCAL = 4.184 / 3600
ENERGY_TO_KWH = {
    "J": 1 / 3.6e6,
    "kJ": 1 / 3600,
    "MJ": 1 / 3.6,
    "GJ": 1000 / 3.6,
    "mWh": 1e-6,
    "Wh": 0.001,
    "kWh": 1.0,
    "MWh": 1000.0,
    "GWh": 1e6,
    "TWh": 1e9,
    "cal": _KWH_PER_KCAL / 1000,
    "kcal": _KWH_PER_KCAL,
    "Mcal": _KWH_PER_KCAL * 1000,
    "Gcal": _KWH_PER_KCAL * 1e6,
    "thm": 1.054804e8 / 3.6e6,  # US therm
}
# m³ per unit, for every volume unit Home Assistant knows (checked against its VolumeConverter).
_M3_PER_FT3 = 0.3048**3
VOLUME_TO_M3 = {
    "m³": 1.0,
    "L": 0.001,
    "mL": 1e-6,
    "gal": 0.003785411784,  # US gallon
    "fl. oz.": 0.003785411784 / 128,
    "ft³": _M3_PER_FT3,
    "CCF": 100 * _M3_PER_FT3,
    "MCF": 1000 * _M3_PER_FT3,
}
HEAT_UNIT = "kWh"


class InputKind(StrEnum):
    ENERGY = "energy"  # cumulative, converted to kWh if the unit is an energy unit
    PERCENT = "percent"  # 0..100 -> 0..1
    BOOL = "bool"  # on/off
    HVAC_ACTION = "hvac_action"  # climate entity: heating or not


@dataclass(frozen=True)
class Subentry:
    """The parts of a config subentry the model needs (a ConfigSubentry fits)."""

    subentry_id: str
    subentry_type: str
    title: str
    data: Mapping[str, Any]


def build_plant(options: Mapping[str, Any], subentries: Iterable[Subentry]) -> PlantSpec:
    """The plant from the entry options and subentries. Spec IDs are subentry IDs."""
    subentries = list(subentries)
    sources = tuple(_source(s) for s in subentries if s.subentry_type == SUBENTRY_SOURCE)
    circuits = tuple(_circuit(s) for s in subentries if s.subentry_type == SUBENTRY_CIRCUIT)
    circuit_ids = {c.id for c in circuits}
    consumers = tuple(_consumer(s, circuit_ids) for s in subentries if s.subentry_type == SUBENTRY_CONSUMER)
    return PlantSpec(sources, circuits, consumers, loss_kw=float(options.get(CONF_LOSS_KW) or 0.0))


def input_kinds(plant: PlantSpec) -> dict[str, InputKind]:
    """How to read every entity the plant depends on."""
    kinds: dict[str, InputKind] = {}
    for source in plant.sources:
        kinds[source.key] = InputKind.ENERGY
    for circuit in plant.circuits:
        if circuit.pump_key:
            kinds[circuit.pump_key] = InputKind.BOOL
    for consumer in plant.consumers:
        if consumer.meter_key:
            kinds[consumer.meter_key] = InputKind.ENERGY
        key, method = consumer.demand.key, consumer.demand.method
        if key is None:
            continue
        if method is DemandMethod.HVAC_ACTION:
            kinds[key] = InputKind.HVAC_ACTION
        elif method is DemandMethod.ON_OFF:
            kinds[key] = InputKind.BOOL
        else:
            kinds[key] = InputKind.PERCENT
    return kinds


def reference_units(subentries: Iterable[Subentry]) -> dict[str, str | None]:
    """The unit each meter reading is taken in, by entity.

    Heat (sources that count heat, heat meters) is read in kWh, from any energy unit. Energy input is read in the
    unit it was set up with, because its factor refers to that unit: another unit of the same kind (L instead of m³)
    is converted, one of another kind (kWh instead of m³) makes the reading unusable. None: set up before the unit was
    stored, read as it comes.
    """
    units: dict[str, str | None] = {}
    for subentry in subentries:
        data = subentry.data
        if subentry.subentry_type == SUBENTRY_SOURCE:
            units[data[CONF_ENTITY]] = data.get(CONF_UNIT) if data.get(CONF_QUANTITY) == QUANTITY_INPUT else HEAT_UNIT
        elif subentry.subentry_type == SUBENTRY_CONSUMER and data.get(CONF_METER_ENTITY):
            units[data[CONF_METER_ENTITY]] = HEAT_UNIT
    return units


def convert(value: float, unit: str | None, reference: str | None) -> float | None:
    """A meter reading in its reference unit (energy in kWh); None if the units do not fit."""
    if reference is None:  # stored before units were: energy in kWh, anything else as it comes
        return value * ENERGY_TO_KWH.get(unit, 1.0) if unit is not None else value
    if reference in ENERGY_TO_KWH:
        return value * ENERGY_TO_KWH[unit] if unit in ENERGY_TO_KWH else None
    if reference in VOLUME_TO_M3:
        return value * VOLUME_TO_M3[unit] / VOLUME_TO_M3[reference] if unit in VOLUME_TO_M3 else None
    return value if unit == reference else None


def parse_state(
    kind: InputKind,
    state: str | None,
    attributes: Mapping[str, Any] | None = None,
    reference: str | None = None,
) -> Reading:
    """The reading for an entity state; None if it is unavailable or makes no sense.

    `reference`: for energy readings, the unit to read them in (see reference_units).
    """
    attributes = attributes or {}
    if state is None or state.lower() in INVALID_STATES:
        return None
    if kind is InputKind.BOOL:
        return {"on": True, "off": False}.get(state)
    if kind is InputKind.HVAC_ACTION:
        action = attributes.get("hvac_action")
        if action is None:
            # The thermostat does not report whether it is heating (Home Assistant leaves the attribute out then):
            # it counts as heating while it is switched on. Rough, but blocking the whole split would be worse.
            return state != "off"
        return action in HEATING_ACTIONS
    try:
        value = float(state)
    except ValueError:
        return None
    if not math.isfinite(value):  # "nan" and "inf" parse as floats
        return None
    if kind is InputKind.PERCENT:
        return value / 100
    return convert(value, attributes.get("unit_of_measurement"), reference)


def _source(subentry: Subentry) -> SourceSpec:
    data = subentry.data
    is_input = data.get(CONF_QUANTITY) == QUANTITY_INPUT
    return SourceSpec(
        id=subentry.subentry_id,
        key=data[CONF_ENTITY],
        factor=float(data.get(CONF_FACTOR) or 1.0) if is_input else 1.0,
        unit=data.get(CONF_UNIT) if is_input else HEAT_UNIT,
    )


def _circuit(subentry: Subentry) -> CircuitSpec:
    pump = subentry.data.get(CONF_PUMP_ENTITY)
    return CircuitSpec(subentry.subentry_id, ActiveMethod.PUMP if pump else ActiveMethod.ALWAYS, pump or None)


def _consumer(subentry: Subentry, circuit_ids: set[str]) -> ConsumerSpec:
    data = subentry.data
    hot_water = data.get(CONF_KIND) == KIND_HOT_WATER
    circuit = data.get(CONF_CIRCUIT)
    demand_entity = data.get(CONF_DEMAND_ENTITY) or None
    demand_type = DemandMethod(data.get(CONF_DEMAND_TYPE) or DemandMethod.ALWAYS)
    if demand_entity is None:
        demand_type = DemandMethod.ALWAYS
    size_type = SizeMethod(data.get(CONF_SIZE_TYPE) or SizeMethod.DEFAULT)
    size_value = data.get(CONF_SIZE_VALUE)
    if size_type is SizeMethod.DEFAULT or size_value in (None, ""):
        size = Size(SizeMethod.DEFAULT, DEFAULT_HOT_WATER_POWER_W if hot_water else None)
    else:
        size = Size(size_type, float(size_value))
    return ConsumerSpec(
        id=subentry.subentry_id,
        # A removed circuit leaves the radiator without one (always active) instead of breaking the entry.
        circuit_id=circuit if not hot_water and circuit in circuit_ids else None,
        size=size,
        demand=Demand(demand_type, demand_entity if demand_type is not DemandMethod.ALWAYS else None),
        meter_key=data.get(CONF_METER_ENTITY) or None,
        hot_water=hot_water,
    )


def localized(language: str, names: Mapping[str, str]) -> str:
    """The text for a language such as "de" or "en-GB"; the English one for languages without their own."""
    return names.get(language.split("-")[0], names["en"])
