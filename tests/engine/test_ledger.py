"""Rules of the ledger, one test per rule in docs/architecture.md."""

from __future__ import annotations

import math

import pytest

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

SOURCE = SourceSpec("boiler", "sensor.boiler")
PUMP = CircuitSpec("circuit", ActiveMethod.PUMP, "binary_sensor.pump")


def radiator(name: str, watts: float | None = None, demand: str | None = None, circuit: str | None = None):
    size = Size(SizeMethod.NOMINAL_POWER, watts) if watts else Size()
    dem = Demand(DemandMethod.PERCENT, demand) if demand else Demand()
    return ConsumerSpec(name, circuit, size, dem)


def conserved(ledger: Ledger) -> bool:
    used = sum(ledger.consumer_total(c.id) for c in ledger.plant.consumers)
    return math.isclose(
        sum(ledger.source_heat.values()), used + sum(ledger.losses.values()) + ledger.account_total, abs_tol=1e-9
    )


def test_equal_radiators_share_equally():
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"), radiator("b"))))
    ledger.observe(0, {"sensor.boiler": 100.0})
    ledger.observe(100, {"sensor.boiler": 102.0})
    ledger.close_interval(300)
    assert ledger.consumer_total("a") == pytest.approx(1.0)
    assert ledger.consumer_total("b") == pytest.approx(1.0)
    assert ledger.account_total == pytest.approx(0.0)
    assert conserved(ledger)


def test_weights_follow_size_and_demand_over_time():
    plant = PlantSpec((SOURCE,), consumers=(radiator("a", 1000, "sensor.a"), radiator("b", 2000, "sensor.b")))
    ledger = Ledger(plant)
    # a: 100 % all the time; b: 100 % for the first half, 0 % for the second half.
    ledger.observe(0, {"sensor.boiler": 0.0, "sensor.a": 1.0, "sensor.b": 1.0})
    ledger.observe(150, {"sensor.b": 0.0})
    ledger.observe(299, {"sensor.boiler": 4.0})
    ledger.close_interval(300)
    # a: 1000 W × 300 s, b: 2000 W × 150 s, equal weights.
    assert ledger.consumer_total("a") == pytest.approx(2.0)
    assert ledger.consumer_total("b") == pytest.approx(2.0)


def test_pump_off_keeps_heat_in_the_account():
    plant = PlantSpec((SOURCE,), (PUMP,), (radiator("a", circuit="circuit"),))
    ledger = Ledger(plant)
    ledger.observe(0, {"sensor.boiler": 0.0, "binary_sensor.pump": False})
    ledger.observe(100, {"sensor.boiler": 5.0})
    assert ledger.close_interval(300) == {"a": 0.0}
    assert ledger.account_total == pytest.approx(5.0)
    ledger.observe(300, {"binary_sensor.pump": True})
    ledger.close_interval(600)
    assert ledger.consumer_total("a") == pytest.approx(5.0)
    assert ledger.circuit_total("circuit") == pytest.approx(5.0)


def test_unavailable_input_counts_for_no_one():
    plant = PlantSpec((SOURCE,), consumers=(radiator("a", demand="sensor.a"), radiator("b", demand="sensor.b")))
    ledger = Ledger(plant)
    ledger.observe(0, {"sensor.boiler": 0.0, "sensor.a": 1.0, "sensor.b": None})
    assert ledger.blocked_by == ("sensor.b",)
    ledger.observe(100, {"sensor.boiler": 3.0})
    ledger.close_interval(300)
    assert ledger.account_total == pytest.approx(3.0)
    # Back: b gets weight from now on only, a did not collect weight while b was unavailable.
    ledger.observe(300, {"sensor.b": 1.0})
    assert ledger.blocked_by == ()
    ledger.close_interval(600)
    assert ledger.consumer_total("a") == pytest.approx(1.5)
    assert ledger.consumer_total("b") == pytest.approx(1.5)


def test_input_never_seen_blocks_like_unavailable():
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a", demand="sensor.a"),)))
    ledger.observe(0, {"sensor.boiler": 0.0})
    assert ledger.blocked_by == ("sensor.a",)


@pytest.mark.parametrize(
    ("values", "booked"),
    [
        ([100, 0, 105], 5),  # glitch: back above the old value, only the part above counts
        ([100, 0, 3], 3),  # real reset: counts from the new baseline
        ([100, 0, 3, 110], 110),  # reset, then grows past the old value: all of it counts
        ([100, None, 107], 7),  # unavailable: increase booked when back
        ([100, 99.5, 101], 1),  # small drop and back: glitch
    ],
)
def test_source_counter(values, booked):
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"),)))
    for t, value in enumerate(values):
        ledger.observe(t, {"sensor.boiler": value})
    assert sum(ledger.source_heat.values()) == pytest.approx(booked)


def test_conversion_factor():
    gas = SourceSpec("gas", "sensor.gas_m3", factor=9.5)
    ledger = Ledger(PlantSpec((gas,), consumers=(radiator("a"),)))
    ledger.observe(0, {"sensor.gas_m3": 100.0})
    ledger.observe(1, {"sensor.gas_m3": 102.0})
    assert ledger.source_heat["gas"] == pytest.approx(19.0)


def test_hot_water_is_a_consumer_without_circuit():
    hot_water = ConsumerSpec("dhw", None, Size(SizeMethod.NOMINAL_POWER, 10000), Demand(DemandMethod.ON_OFF, "dhw"))
    plant = PlantSpec((SOURCE,), consumers=(radiator("a", 10000), hot_water))
    ledger = Ledger(plant)
    ledger.observe(0, {"sensor.boiler": 0.0, "dhw": True})
    ledger.observe(150, {"dhw": False})
    ledger.observe(299, {"sensor.boiler": 3.0})
    ledger.close_interval(300)
    assert ledger.consumer_total("dhw") == pytest.approx(1.0)
    assert ledger.consumer_total("a") == pytest.approx(2.0)


def test_measured_consumer_is_taken_out_first():
    metered = ConsumerSpec("bath", meter_key="sensor.bath_heat")
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"), radiator("b"), metered)))
    ledger.observe(0, {"sensor.boiler": 0.0, "sensor.bath_heat": 50.0})
    ledger.observe(100, {"sensor.boiler": 10.0, "sensor.bath_heat": 54.0})
    ledger.close_interval(300)
    assert ledger.consumer_total("bath") == pytest.approx(4.0)
    assert ledger.consumer_total("a") == pytest.approx(3.0)
    assert ledger.consumer_total("b") == pytest.approx(3.0)
    assert conserved(ledger)


def test_fixed_losses_are_taken_first():
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"),), loss_kw=1.2))
    ledger.observe(0, {"sensor.boiler": 0.0})
    ledger.observe(1, {"sensor.boiler": 1.0})
    ledger.close_interval(600)  # 10 min × 1.2 kW = 0.2 kWh
    assert ledger.losses["boiler"] == pytest.approx(0.2)
    assert ledger.consumer_total("a") == pytest.approx(0.8)


def test_losses_never_exceed_the_account():
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"),), loss_kw=100))
    ledger.observe(0, {"sensor.boiler": 0.0})
    ledger.observe(1, {"sensor.boiler": 1.0})
    ledger.close_interval(3600)
    assert ledger.losses["boiler"] == pytest.approx(1.0)
    assert ledger.account_total == pytest.approx(0.0)


def test_release_with_time_constant():
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"),), release_tau_s=3600))
    ledger.observe(0, {"sensor.boiler": 0.0})
    ledger.observe(1, {"sensor.boiler": 12.0})
    ledger.close_interval(300)
    assert ledger.consumer_total("a") == pytest.approx(12.0 * (1 - math.exp(-300 / 3600)))
    assert conserved(ledger)


def test_heat_of_several_sources_is_tracked_per_source():
    wood = SourceSpec("wood", "sensor.wood")
    ledger = Ledger(PlantSpec((SOURCE, wood), consumers=(radiator("a"), radiator("b"))))
    ledger.observe(0, {"sensor.boiler": 0.0, "sensor.wood": 0.0})
    ledger.observe(1, {"sensor.boiler": 2.0, "sensor.wood": 6.0})
    ledger.close_interval(300)
    assert ledger.consumed["a"] == pytest.approx({"boiler": 1.0, "wood": 3.0})


def test_restore_continues_and_skips_the_downtime():
    plant = PlantSpec((SOURCE,), consumers=(radiator("a", demand="sensor.a"), radiator("b", demand="sensor.b")))
    ledger = Ledger(plant)
    ledger.observe(0, {"sensor.boiler": 0.0, "sensor.a": 1.0, "sensor.b": 0.0})
    ledger.observe(100, {"sensor.boiler": 1.0})
    ledger.close_interval(300)

    restored = Ledger.from_dict(plant, ledger.to_dict())
    assert restored.consumer_total("a") == pytest.approx(1.0)
    # Down for an hour, meanwhile 3 kWh. Afterwards only b heats: the downtime counts for no one.
    restored.observe(3900, {"sensor.boiler": 4.0, "sensor.a": 0.0, "sensor.b": 1.0})
    restored.close_interval(4200)
    assert restored.consumer_total("a") == pytest.approx(1.0)
    assert restored.consumer_total("b") == pytest.approx(3.0)


def test_restore_with_changed_plant():
    plant = PlantSpec((SOURCE,), consumers=(radiator("a"),))
    ledger = Ledger(plant)
    ledger.observe(0, {"sensor.boiler": 0.0})
    ledger.observe(1, {"sensor.boiler": 2.0})
    ledger.close_interval(300)
    bigger = PlantSpec((SOURCE, SourceSpec("wood", "sensor.wood")), consumers=(radiator("a"), radiator("b")))
    restored = Ledger.from_dict(bigger, ledger.to_dict())
    assert restored.consumer_total("a") == pytest.approx(2.0)
    assert restored.consumer_total("b") == 0.0
    assert restored.account["wood"] == 0.0


def test_unknown_state_version_starts_fresh():
    plant = PlantSpec((SOURCE,), consumers=(radiator("a"),))
    assert Ledger.from_dict(plant, {"version": 99, "account": {"boiler": 5}}).account_total == 0.0


@pytest.mark.parametrize(
    "plant_args",
    [
        {"sources": (SOURCE, SOURCE)},
        {"sources": (SOURCE,), "consumers": (radiator("a", circuit="nope"),)},
        {"sources": (SOURCE,), "consumers": (ConsumerSpec("a", demand=Demand(DemandMethod.PERCENT)),)},
        {"sources": (SOURCE,), "circuits": (CircuitSpec("c", ActiveMethod.PUMP),)},
    ],
)
def test_invalid_plants_are_rejected(plant_args):
    with pytest.raises(ValueError):
        PlantSpec(**plant_args)
