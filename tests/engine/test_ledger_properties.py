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
        ConsumerSpec("floor", "circuit", meter_key="floor_meter"),
    ),
    loss_kw=0.3,
    release_tau_s=1800,
)

WITHOUT_WOOD = PlantSpec(
    sources=(PLANT.sources[0],),
    circuits=PLANT.circuits,
    consumers=PLANT.consumers,
    loss_kw=PLANT.loss_kw,
    release_tau_s=PLANT.release_tau_s,
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
                "meter": st.one_of(st.none(), st.floats(-1, 2, allow_nan=False)),  # negative: a dip or reset
                "floor_meter": st.one_of(st.none(), st.floats(-1.5, 3, allow_nan=False)),
                "a": fraction,
                "b": switch,
                "pump": switch,
                "dhw": switch,
            },
        ),
    ),
    st.tuples(st.just("close"), st.floats(0, 900, allow_nan=False), st.just({})),
    st.tuples(st.just("restart"), st.floats(0, 7200, allow_nan=False), st.just({})),
    st.tuples(st.just("remove_wood"), st.floats(0, 900, allow_nan=False), st.just({})),
)


def totals(ledger: Ledger) -> dict[str, float]:
    return {c.id: ledger.consumer_total(c.id) for c in PLANT.consumers}


def circuit_total(ledger: Ledger) -> float:
    return ledger.circuit_total("circuit")


@settings(max_examples=300, deadline=None)
@given(st.lists(operation, max_size=60))
def test_conservation_and_monotonic_counters(operations):
    ledger = Ledger(PLANT)
    t = 0.0
    counters = {"oil": 1000.0, "wood": 50.0, "meter": 10.0, "floor_meter": 5.0}
    plant = PLANT
    previous = totals(ledger)
    previous_circuit = circuit_total(ledger)
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
        elif kind == "restart":
            ledger = Ledger.from_dict(plant, ledger.to_dict())
        else:  # the wood source is removed from the plant: it stays in the books
            plant = WITHOUT_WOOD
            ledger = Ledger.from_dict(plant, ledger.to_dict())

        current = totals(ledger)
        for consumer_id, value in current.items():
            # Strictly: Home Assistant warns about any decrease of a total_increasing sensor.
            assert value >= previous[consumer_id], f"{consumer_id} decreased"
            assert value >= -1e-9
        previous = current
        assert circuit_total(ledger) >= previous_circuit, "the circuit decreased"
        previous_circuit = circuit_total(ledger)

        heat_in = sum(ledger.source_heat.values())
        # Heat a meter measured beyond what the sources held is counted, but not (yet, or ever: written off) taken out
        # of the account.
        pending = sum(ledger.unattributed.values()) + sum(ledger.written_off.values())
        heat_out = sum(current.values()) - pending + sum(ledger.losses.values()) + ledger.account_total
        assert pending >= 0
        assert math.isclose(heat_in, heat_out, rel_tol=1e-9, abs_tol=1e-6)
        # A circuit's total is what it received by source, and what waits or was written off.
        by_source = sum(ledger.circuit_consumed["circuit"].values())
        unattributed = ledger.circuit_pending("circuit") + ledger.circuit_written_off.get("circuit", 0.0)
        assert math.isclose(circuit_total(ledger), by_source + unattributed, rel_tol=1e-9, abs_tol=1e-6)
        assert all(v >= 0 for v in ledger.losses.values())
        assert all(v >= 0 for v in ledger.account.values()), "a balance went negative"
