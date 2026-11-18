"""Levels and the hint for the next improvement."""

from __future__ import annotations

import pytest

from custom_components.heat_ledger.engine import (
    ActiveMethod,
    CircuitSpec,
    ConsumerSpec,
    Demand,
    DemandMethod,
    Level,
    PlantSpec,
    Size,
    SizeMethod,
    SourceSpec,
    consumer_basis,
)

SOURCE = SourceSpec("s", "sensor.s")
PLANT = PlantSpec(
    (SOURCE,),
    circuits=(CircuitSpec("plain"), CircuitSpec("pumped", ActiveMethod.PUMP, "pump")),
)


@pytest.mark.parametrize(
    ("consumer", "level", "hint"),
    [
        (ConsumerSpec("x", "plain"), Level.ESTIMATED, "demand_hvac_action"),
        (
            ConsumerSpec("x", "plain", Size(), Demand(DemandMethod.VALVE, "v")),
            Level.ESTIMATED,
            "size_area",
        ),
        (
            ConsumerSpec("x", "plain", Size(SizeMethod.NOMINAL_POWER, 900), Demand(DemandMethod.VALVE, "v")),
            Level.ESTIMATED,
            "circuit_pump",
        ),
        (
            ConsumerSpec("x", "pumped", Size(SizeMethod.NOMINAL_POWER, 900), Demand(DemandMethod.VALVE, "v")),
            Level.ESTIMATED,
            None,
        ),
        # Hot water: on/off is all a pump can say, no circuit; the hint goes to the size.
        (ConsumerSpec("dhw", None, Size(), Demand(DemandMethod.ON_OFF, "p")), Level.ESTIMATED, "size_area"),
        (ConsumerSpec("m", meter_key="meter"), Level.MEASURED, None),
    ],
)
def test_basis(consumer, level, hint):
    basis = consumer_basis(PLANT, consumer)
    assert basis.level is level
    assert basis.next_improvement == hint


def test_methods_are_listed():
    consumer = ConsumerSpec("x", "pumped", Size(SizeMethod.AREA, 12), Demand(DemandMethod.PERCENT, "p"))
    assert consumer_basis(PLANT, consumer).methods == {"size": "area", "demand": "percent", "circuit": "pump"}
