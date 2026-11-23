"""The estimate of older periods: the ledger over hourly values, its weights calibrated on an exact calculation."""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.heat_ledger.engine import (
    ActiveMethod,
    CircuitSpec,
    ConsumerSpec,
    Demand,
    DemandMethod,
    PlantSpec,
    Size,
    SizeMethod,
    SourceSpec,
)
from custom_components.heat_ledger.engine.estimate import (
    MAX_FACTOR,
    Hour,
    calibrate,
    hourly_demands,
    meter_keys,
    replay_hours,
    rough_plant,
)

HOUR = 3600.0
T0 = 1000 * HOUR


def consumer(name: str, watts: float, method: DemandMethod, key: str | None, **kwargs) -> ConsumerSpec:
    return ConsumerSpec(
        name, kwargs.pop("circuit", "circuit"), Size(SizeMethod.NOMINAL_POWER, watts), Demand(method, key), **kwargs
    )


PLANT = PlantSpec(
    (SourceSpec("boiler", "sensor.boiler"),),
    (CircuitSpec("circuit", ActiveMethod.PUMP, "binary_sensor.pump"),),
    (
        consumer("living", 2000, DemandMethod.PERCENT, "sensor.living"),
        consumer("bath", 1000, DemandMethod.VALVE, "sensor.bath"),
        consumer("office", 1000, DemandMethod.HVAC_ACTION, "climate.office"),
        consumer("hot_water", 10000, DemandMethod.ON_OFF, "binary_sensor.hot_water", circuit=None, hot_water=True),
        consumer("cellar", 1000, DemandMethod.ALWAYS, None, meter_key="sensor.cellar_meter"),
    ),
)
DEMANDS = {"sensor.living", "sensor.bath"}


def hours(*rows: tuple[float, float, float]) -> list[Hour]:
    """(living %, bath %, boiler reading at the end of the hour) per hour, from T0; the first is the baseline."""
    return [
        Hour(
            T0 + i * HOUR,
            {"sensor.living": living, "sensor.bath": bath},
            {"sensor.boiler": boiler, "sensor.cellar_meter": 0.0},
        )
        for i, (living, bath, boiler) in enumerate(rows)
    ]


def test_inputs_with_hourly_values():
    assert hourly_demands(PLANT) == DEMANDS
    assert meter_keys(PLANT) == {"sensor.boiler", "sensor.cellar_meter"}


def test_rough_plant_has_only_what_an_hour_tells():
    plant = rough_plant(PLANT, {"living": 2.0, "hot_water": 0.1})

    by_id = {c.id: c for c in plant.consumers}
    assert by_id["living"].demand == Demand(DemandMethod.PERCENT, "sensor.living")
    assert by_id["bath"].demand == Demand(DemandMethod.VALVE, "sensor.bath")
    # No statistics for on/off and hvac_action: always on.
    assert by_id["office"].demand == Demand(DemandMethod.ALWAYS)
    assert by_id["hot_water"].demand == Demand(DemandMethod.ALWAYS)
    assert by_id["cellar"].meter_key == "sensor.cellar_meter"
    assert plant.circuits == (CircuitSpec("circuit", ActiveMethod.ALWAYS, None),)
    assert by_id["living"].size.watts == 4000
    assert by_id["hot_water"].size.watts == pytest.approx(1000)
    assert by_id["bath"].size.watts == 1000
    # A demand without hourly values (no statistics) is always on as well.
    assert rough_plant(PLANT, hourly={"sensor.bath"}).consumers[0].demand == Demand(DemandMethod.ALWAYS)


def test_hours_split_by_their_means():
    plant = rough_plant(PLANT, {"office": 0.0, "hot_water": 0.0})  # only living and bath
    result = replay_hours(plant, hours((1.0, 1.0, 100.0), (0.5, 1.0, 104.0), (1.0, 0.0, 107.0)), DEMANDS)

    assert result.hours == (T0, T0 + HOUR, T0 + 2 * HOUR)
    # Hour 0 is the baseline; hour 1: living 1000 W, bath 1000 W; hour 2: living alone.
    assert result.consumers["living"] == pytest.approx((0.0, 2.0, 5.0))
    assert result.consumers["bath"] == pytest.approx((0.0, 2.0, 2.0))
    assert result.circuits["circuit"] == pytest.approx((0.0, 4.0, 7.0))
    assert result.sources["boiler"] == pytest.approx((0.0, 4.0, 7.0))


def test_a_missing_hour_counts_for_the_next_one():
    plant = rough_plant(PLANT, {"office": 0.0, "hot_water": 0.0})
    rows = hours((1.0, 1.0, 100.0), (1.0, 1.0, 102.0), (0.0, 0.0, 0.0), (1.0, 0.0, 110.0))
    del rows[2]  # Home Assistant was down

    result = replay_hours(plant, rows, DEMANDS)

    # Hour 1: living 2000 W, bath 1000 W; the 8 kWh of the hours since go by the hour there is: living alone.
    assert result.consumer_totals["living"] == pytest.approx(4 / 3 + 8.0)
    assert result.consumer_totals["bath"] == pytest.approx(2 / 3)


def test_a_demand_without_value_holds_the_heat_back():
    plant = rough_plant(PLANT, {"office": 0.0, "hot_water": 0.0})
    rows = hours((1.0, 1.0, 100.0), (1.0, None, 102.0), (0.0, 1.0, 103.0))

    result = replay_hours(plant, rows, DEMANDS)

    assert result.consumers["bath"] == pytest.approx((0.0, 0.0, 3.0))
    assert result.consumers["living"] == pytest.approx((0.0, 0.0, 0.0))


def test_calibrated_weights_split_like_the_exact_calculation():
    rows = hours(
        (0.0, 0.0, 100.0), *[(0.2 + 0.1 * (i % 5), 0.5 + 0.1 * (i % 3), 100.0 + 3 * (i + 1)) for i in range(24)]
    )
    exact = {"living": 30.0, "bath": 20.0, "office": 10.0, "hot_water": 12.0}

    calibration = calibrate(PLANT, rows, DEMANDS, exact)

    assert set(calibration.factors) == {"living", "bath", "office", "hot_water"}  # not the measured cellar
    assert calibration.exact["living"] == pytest.approx(30 / 72)
    result = replay_hours(rough_plant(PLANT, calibration.factors, DEMANDS), rows, DEMANDS)
    split = sum(result.consumer_totals[c] for c in exact)
    for cid, kwh in exact.items():
        assert result.consumer_totals[cid] / split == pytest.approx(kwh / 72, rel=0.01)
        assert calibration.estimated[cid] == pytest.approx(kwh / 72, rel=0.01)
    # Relative to the typical weight: hot water always on at 10 kW gets far less than its size.
    assert calibration.factors["hot_water"] < 0.2


def test_a_consumer_without_heat_in_the_calibration_keeps_a_little():
    rows = hours((0.0, 0.0, 100.0), *[(0.5, 0.5, 100.0 + 2 * (i + 1)) for i in range(12)])

    calibration = calibrate(PLANT, rows, DEMANDS, {"living": 10.0, "bath": 10.0, "office": 0.0, "hot_water": 4.0})

    factors = calibration.factors
    assert factors["office"] == pytest.approx(1 / MAX_FACTOR)  # relative to the typical weight
    assert calibration.estimated["office"] < 0.01
    # The shares returned are the ones of these factors.
    result = replay_hours(rough_plant(PLANT, factors, DEMANDS), rows, DEMANDS)
    split = sum(result.consumer_totals[c] for c in factors)
    assert calibration.estimated == pytest.approx({c: result.consumer_totals[c] / split for c in factors})


@pytest.mark.parametrize(
    ("exact", "boiler"),
    [
        ({"living": 0.0, "bath": 0.0, "office": 0.0, "hot_water": 0.0}, 3.0),
        ({"living": 1.0}, 0.0),
        ({"living": 0.5, "bath": 0.5, "office": 0.0, "hot_water": 10.0}, 3.0),
    ],
    ids=["nothing split exactly", "nothing in the hours", "outside the heating season"],
)
def test_nothing_to_calibrate_on(exact: dict[str, float], boiler: float):
    """Calibrated on hot water alone (set up in summer), the rooms' weights would say nothing about winter."""
    rows = hours((0.5, 0.5, 100.0), (0.5, 0.5, 100.0 + boiler))
    with pytest.raises(ValueError):
        calibrate(PLANT, rows, DEMANDS, exact)


def test_a_fixed_loss_is_taken_every_interval():
    """As live: the heat of an hour arrives over the hour, and the loss is taken from it every 5 minutes."""
    plant = rough_plant(replace(PLANT, loss_kw=1.2), {"office": 0.0, "hot_water": 0.0})

    result = replay_hours(plant, hours((1.0, 1.0, 100.0), (1.0, 1.0, 110.0), (1.0, 1.0, 120.0)), DEMANDS)

    assert sum(result.consumer_totals[c] for c in ("living", "bath")) == pytest.approx(2 * (10.0 - 1.2))
