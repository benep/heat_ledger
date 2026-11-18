"""Invariants of the ledger under random sequences of readings, outages, intervals and restarts."""

from __future__ import annotations

import math

from hypothesis import given, settings
from hypothesis import strategies as st

from custom_components.heat_ledger.engine import (
    ActiveMethod,
    CircuitSpec,
    ConsumerSpec,
    Demand,
    DemandMethod,
    Ledger,
    PlantSpec,
    Size,
    SizeMethod,
    SourceSpec,
)

PLANT = PlantSpec(
    sources=(SourceSpec("oil", "oil", factor=0.9), SourceSpec("wood", "wood")),
    circuits=(CircuitSpec("circuit", ActiveMethod.PUMP, "pump"),),
    consumers=(
        ConsumerSpec("a", "circuit", Size(SizeMethod.NOMINAL_POWER, 800), Demand(DemandMethod.PERCENT, "a")),
        ConsumerSpec("b", "circuit", Size(), Demand(DemandMethod.HVAC_ACTION, "b")),
        ConsumerSpec("c", "circuit"),
        ConsumerSpec("dhw", None, Size(SizeMethod.NOMINAL_POWER, 9000), Demand(DemandMethod.ON_OFF, "dhw")),
        ConsumerSpec("metered", meter_key="meter"),
    ),
    loss_kw=0.3,
    release_tau_s=1800,
)

counter_step = st.one_of(st.none(), st.floats(-5, 20, allow_nan=False))  # negative: a drop or reset
fraction = st.one_of(st.none(), st.floats(0, 1.2, allow_nan=False))
switch = st.one_of(st.none(), st.booleans())

operation = st.one_of(
    st.tuples(
        st.just("observe"),
        st.floats(0, 900, allow_nan=False),
        st.fixed_dictionaries(
            {},
            optional={
                "oil": counter_step,
                "wood": counter_step,
                "meter": st.one_of(st.none(), st.floats(0, 2, allow_nan=False)),
                "a": fraction,
                "b": switch,
                "pump": switch,
                "dhw": switch,
            },
        ),
    ),
    st.tuples(st.just("close"), st.floats(0, 900, allow_nan=False), st.just({})),
    st.tuples(st.just("restart"), st.floats(0, 7200, allow_nan=False), st.just({})),
)


def totals(ledger: Ledger) -> dict[str, float]:
    return {c.id: ledger.consumer_total(c.id) for c in PLANT.consumers}


@settings(max_examples=300, deadline=None)
@given(st.lists(operation, max_size=60))
def test_conservation_and_monotonic_counters(operations):
    ledger = Ledger(PLANT)
    t = 0.0
    counters = {"oil": 1000.0, "wood": 50.0, "meter": 10.0}
    previous = totals(ledger)
    for kind, dt, values in operations:
        t += dt
        if kind == "observe":
            readings = {}
            for key, value in values.items():
                if key in counters:
                    if value is None:
                        readings[key] = None
                    else:
                        counters[key] = max(counters[key] + value, 0.0)
                        readings[key] = counters[key]
                else:
                    readings[key] = value
            ledger.observe(t, readings)
        elif kind == "close":
            ledger.close_interval(t)
        else:
            ledger = Ledger.from_dict(PLANT, ledger.to_dict())

        current = totals(ledger)
        for consumer_id, value in current.items():
            assert value >= previous[consumer_id] - 1e-9, f"{consumer_id} decreased"
            assert value >= -1e-9
        previous = current

        heat_in = sum(ledger.source_heat.values())
        heat_out = sum(current.values()) + sum(ledger.losses.values()) + ledger.account_total
        assert math.isclose(heat_in, heat_out, rel_tol=1e-9, abs_tol=1e-6)
        assert all(v >= 0 for v in ledger.losses.values())
