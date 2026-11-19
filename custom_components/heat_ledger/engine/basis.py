"""Levels: how a consumer's result is computed and which input would improve it most."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .spec import ActiveMethod, ConsumerSpec, DemandMethod, PlantSpec, SizeMethod


class Level(StrEnum):
    ESTIMATED = "estimated"
    CALCULATED = "calculated"
    MEASURED = "measured"


# Rough to precise. ON_OFF ranks with HVAC_ACTION: both only say on or off.
SIZE_ORDER = [SizeMethod.DEFAULT, SizeMethod.AREA, SizeMethod.CALCULATED, SizeMethod.NOMINAL_POWER]
DEMAND_ORDER = [DemandMethod.ALWAYS, DemandMethod.HVAC_ACTION, DemandMethod.PERCENT, DemandMethod.VALVE]
ACTIVE_ORDER = [ActiveMethod.ALWAYS, ActiveMethod.PUMP]

# Checked in this order: the first factor not at its best method is the next improvement.
IMPROVEMENTS = (
    ("demand", DEMAND_ORDER, "demand_{}"),
    ("size", SIZE_ORDER, "size_{}"),
    ("circuit", ACTIVE_ORDER, "circuit_{}"),
)


@dataclass(frozen=True)
class Basis:
    level: Level
    methods: dict[str, str]
    next_improvement: str | None


def consumer_basis(plant: PlantSpec, consumer: ConsumerSpec) -> Basis:
    if consumer.meter_key is not None:
        return Basis(Level.MEASURED, {"heat": "meter"}, None)

    circuit = plant.circuit(consumer.circuit_id)
    methods = {
        "size": consumer.size.method,
        "demand": consumer.demand.method,
        "circuit": circuit.active if circuit is not None else ActiveMethod.ALWAYS,
    }
    improvement = None
    for factor, order, hint in IMPROVEMENTS:
        if factor == "circuit" and circuit is None:
            continue
        current = methods[factor]
        if current in (DemandMethod.ON_OFF, DemandMethod.PERCENT):
            # Actuators and pumps cannot report more than on or off. A thermostat that reports a heating demand in %
            # rarely offers a valve position as well, so suggesting one would not help.
            continue
        if factor == "size" and consumer.hot_water:
            order = [SizeMethod.DEFAULT, SizeMethod.NOMINAL_POWER]  # the charging power, nothing in between
        if current not in order:  # a method this order does not know (hot water sized by area): nothing to suggest
            continue
        index = order.index(current)
        if index < len(order) - 1:
            improvement = hint.format(order[index + 1])
            break
    return Basis(Level.ESTIMATED, {k: str(v) for k, v in methods.items()}, improvement)
