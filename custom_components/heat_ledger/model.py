"""From config entry data to the engine, and from entity states to readings. No Home Assistant imports.

The config flow writes the formats in const.py, the runtime calls build_plant() and parse_state() with them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .const import (
    CONF_CIRCUIT,
    CONF_CONVERSION,
    CONF_DEMAND_ENTITY,
    CONF_DEMAND_TYPE,
    CONF_ENTITY,
    CONF_FACTOR,
    CONF_FACTOR_ENTITY,
    CONF_KIND,
    CONF_LOSS_KW,
    CONF_METER_ENTITY,
    CONF_PUMP_ENTITY,
    CONF_SIZE_TYPE,
    CONF_SIZE_VALUE,
    CONVERSION_DIRECT,
    KIND_HOT_WATER,
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
ENERGY_TO_KWH = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0, "GWh": 1e6, "MJ": 1 / 3.6, "GJ": 1000 / 3.6}


class InputKind(StrEnum):
    ENERGY = "energy"  # cumulative, converted to kWh if the unit is an energy unit
    NUMBER = "number"
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
        if source.factor_key:
            kinds[source.factor_key] = InputKind.NUMBER
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
    if kind is InputKind.PERCENT:
        return value / 100
    if kind is InputKind.ENERGY:
        return value * ENERGY_TO_KWH.get(attributes.get("unit_of_measurement"), 1.0)
    return value


def _source(subentry: Subentry) -> SourceSpec:
    data = subentry.data
    direct = data.get(CONF_CONVERSION, CONVERSION_DIRECT) == CONVERSION_DIRECT
    return SourceSpec(
        id=subentry.subentry_id,
        key=data[CONF_ENTITY],
        factor=1.0 if direct else float(data.get(CONF_FACTOR) or 1.0),
        factor_key=None if direct else data.get(CONF_FACTOR_ENTITY),
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
