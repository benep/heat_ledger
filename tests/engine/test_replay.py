"""The replay: a ledger run over recorded readings, with the totals at every full hour."""

from __future__ import annotations

import pytest

from custom_components.heat_ledger.engine import (
    ActiveMethod,
    CircuitSpec,
    ConsumerSpec,
    Demand,
    DemandMethod,
    Ledger,
    PlantSpec,
    Replay,
    Size,
    SizeMethod,
    SourceSpec,
)

SOURCE = SourceSpec("boiler", "sensor.boiler")
CIRCUIT = CircuitSpec("circuit", ActiveMethod.PUMP, "binary_sensor.pump")


def consumer(name: str, circuit: str | None, watts: float, method: DemandMethod, key: str) -> ConsumerSpec:
    return ConsumerSpec(name, circuit, Size(SizeMethod.NOMINAL_POWER, watts), Demand(method, key))


PLANT = PlantSpec(
    (SOURCE,),
    (CIRCUIT,),
    (
        consumer("living", "circuit", 2000, DemandMethod.PERCENT, "sensor.a"),
        consumer("bath", "circuit", 1000, DemandMethod.PERCENT, "sensor.b"),
        consumer("hot_water", None, 10000, DemandMethod.ON_OFF, "pump"),
    ),
)

HOUR = 3600.0
T0 = 1000 * HOUR  # a full hour
ON = {"binary_sensor.pump": True, "sensor.a": 1.0, "sensor.b": 1.0, "pump": False}


def test_totals_at_every_full_hour():
    replay = Replay(PLANT)
    replay.observe(T0 + 600, {"sensor.boiler": 100.0, **ON})
    replay.observe(T0 + 1200, {"sensor.boiler": 103.0})  # hour 0: 3 kWh, living 2 : bath 1
    replay.observe(T0 + HOUR + 60, {"sensor.boiler": 109.0})  # hour 1: 6 kWh
    replay.observe(T0 + 2 * HOUR + 60, {"sensor.b": 0.0})  # hour 2: nothing new
    replay.observe(T0 + 3 * HOUR + 600, {"sensor.boiler": 112.0})  # hour 3, not over at the end: living alone

    result = replay.finish(T0 + 3 * HOUR + 900)

    assert result.start == T0 + 600
    assert result.hours == (T0, T0 + HOUR, T0 + 2 * HOUR)
    assert result.consumers["living"] == pytest.approx((2.0, 6.0, 6.0))
    assert result.consumers["bath"] == pytest.approx((1.0, 3.0, 3.0))
    assert result.consumers["hot_water"] == (0.0, 0.0, 0.0)
    assert result.circuits["circuit"] == pytest.approx((3.0, 9.0, 9.0))
    assert result.consumer_totals == pytest.approx({"living": 9.0, "bath": 3.0, "hot_water": 0.0})
    assert result.circuit_totals == pytest.approx({"circuit": 12.0})
    assert result.source_heat == pytest.approx(12.0)
    assert result.unassigned == pytest.approx(0.0)


def test_same_as_a_ledger_closed_on_the_clock():
    events = [
        (T0 + 10, {"sensor.boiler": 5.0, **ON}),
        (T0 + 130, {"sensor.a": 0.3}),
        (T0 + 290, {"sensor.boiler": 6.5}),
        (T0 + 300, {"pump": True}),  # exactly on a boundary: the interval before it closes first
        (T0 + 700, {"sensor.boiler": 9.0, "binary_sensor.pump": False}),
        (T0 + 1500, {"sensor.boiler": 9.75, "pump": False, "binary_sensor.pump": True}),
    ]
    end = T0 + 1700.0
    replay = Replay(PLANT)
    for t, readings in events:
        replay.observe(t, readings)
    result = replay.finish(end)

    ledger = Ledger(PLANT)
    close = T0 + 300
    for t, readings in events:
        while close <= t:
            ledger.close_interval(close)
            close += 300
        ledger.observe(t, readings)
    while close <= end:
        ledger.close_interval(close)
        close += 300
    ledger.close_interval(end)

    assert result.consumer_totals == ledger.totals
    assert result.circuit_totals == ledger.circuit_totals
    assert result.unassigned == ledger.account_total
    assert sum(result.consumer_totals.values()) + result.unassigned == pytest.approx(result.source_heat)


def test_the_last_interval_is_closed_as_far_as_it_got():
    replay = Replay(PLANT)
    replay.observe(T0, {"sensor.boiler": 0.0, **ON})
    replay.observe(T0 + 400, {"sensor.boiler": 3.0})  # arrives in the interval the replay ends in

    result = replay.finish(T0 + 450)

    assert result.consumer_totals["living"] == pytest.approx(2.0)
    assert result.consumer_totals["bath"] == pytest.approx(1.0)
    assert result.hours == ()


def test_heat_no_consumer_took_is_left_unassigned():
    replay = Replay(PLANT)
    replay.observe(T0, {"sensor.boiler": 0.0, **ON, "binary_sensor.pump": False})
    replay.observe(T0 + 100, {"sensor.boiler": 4.0})

    result = replay.finish(T0 + HOUR)

    assert result.hours == (T0,)
    assert result.consumer_totals == {"living": 0.0, "bath": 0.0, "hot_water": 0.0}
    assert result.source_heat == pytest.approx(4.0)
    assert result.unassigned == pytest.approx(4.0)


def test_an_unavailable_input_holds_the_heat_back_until_it_returns():
    replay = Replay(PLANT)
    replay.observe(T0, {"sensor.boiler": 0.0, **ON, "sensor.b": None})
    replay.observe(T0 + 100, {"sensor.boiler": 3.0})
    replay.observe(T0 + HOUR + 100, {"sensor.b": 1.0})

    result = replay.finish(T0 + 2 * HOUR)

    assert result.consumers["living"] == pytest.approx((0.0, 2.0))
    assert result.consumers["bath"] == pytest.approx((0.0, 1.0))


def test_a_gap_in_the_recording_closes_every_hour_in_it():
    replay = Replay(PLANT)
    replay.observe(T0 + 60, {"sensor.boiler": 0.0, **ON})
    replay.observe(T0 + 5 * HOUR + 60, {"sensor.boiler": 6.0})

    result = replay.finish(T0 + 6 * HOUR)

    assert result.hours == tuple(T0 + n * HOUR for n in range(6))
    assert result.circuits["circuit"] == pytest.approx((0.0, 0.0, 0.0, 0.0, 0.0, 6.0))


def test_without_readings_nothing_is_booked():
    result = Replay(PLANT).finish(T0)

    assert result.start is None
    assert result.hours == ()
    assert result.consumer_totals == {"living": 0.0, "bath": 0.0, "hot_water": 0.0}
    assert result.source_heat == 0.0


def test_a_time_going_back_counts_as_the_time_before():
    replay = Replay(PLANT)
    replay.observe(T0 + 200, {"sensor.boiler": 0.0, **ON})
    replay.observe(T0 + 100, {"sensor.boiler": 3.0})

    result = replay.finish(T0 + 300)

    assert sum(result.consumer_totals.values()) == pytest.approx(3.0)
