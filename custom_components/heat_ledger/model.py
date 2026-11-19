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
# Units not listed here are not energy (m³, L, ...) and pass unchanged: the source factor converts them.
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


def parse_state(kind: InputKind, state: str | None, attributes: Mapping[str, Any] | None = None) -> Reading:
    """The reading for an entity state; None if it is unavailable or makes no sense."""
    attributes = attributes or {}
    if state is None or state.lower() in INVALID_STATES:
        return None
    if kind is InputKind.BOOL:
        return {"on": True, "off": False}.get(state)
    if kind is InputKind.HVAC_ACTION:
        action = attributes.get("hvac_action")
        if action is None:
            return False if state == "off" else None
        return action in HEATING_ACTIONS
    try:
        value = float(state)
    except ValueError:
        return None
    if not math.isfinite(value):  # "nan" and "inf" parse as floats
        return None
    if kind is InputKind.PERCENT:
        return value / 100
    return value * ENERGY_TO_KWH.get(attributes.get("unit_of_measurement"), 1.0)


def _source(subentry: Subentry) -> SourceSpec:
    data = subentry.data
    is_input = data.get(CONF_QUANTITY) == QUANTITY_INPUT
    return SourceSpec(
        id=subentry.subentry_id,
        key=data[CONF_ENTITY],
        factor=float(data.get(CONF_FACTOR) or 1.0) if is_input else 1.0,
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
