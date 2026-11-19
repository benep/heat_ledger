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


def test_moved_consumer_leaves_its_history_with_the_old_circuit():
    """Moved from "up" to "down": "down" does not jump, "up" keeps what it received; from now on it is "down"."""
    up, down = CircuitSpec("up"), CircuitSpec("down")
    before = PlantSpec((SOURCE,), (up, down), (radiator("bath", circuit="up"), radiator("hall", circuit="down")))
    ledger = Ledger(before)
    ledger.observe(0, {"sensor.boiler": 0.0})
    ledger.observe(100, {"sensor.boiler": 10.0})
    ledger.close_interval(300)
    assert (ledger.circuit_total("up"), ledger.circuit_total("down")) == pytest.approx((5.0, 5.0))

    after = PlantSpec((SOURCE,), (up, down), (radiator("bath", circuit="down"), radiator("hall", circuit="down")))
    moved = Ledger.from_dict(after, ledger.to_dict())
    assert (moved.circuit_total("up"), moved.circuit_total("down")) == pytest.approx((5.0, 5.0))
    moved.observe(400, {"sensor.boiler": 10.0})
    moved.observe(500, {"sensor.boiler": 14.0})
    moved.close_interval(600)
    assert (moved.circuit_total("up"), moved.circuit_total("down")) == pytest.approx((5.0, 9.0))
    assert moved.consumer_total("bath") == pytest.approx(7.0)  # the consumer keeps all of its own history


def test_state_from_before_circuit_counters_starts_from_the_sum():
    plant = PlantSpec((SOURCE,), (PUMP,), (radiator("a", circuit="circuit"), radiator("b", circuit="circuit")))
    ledger = Ledger(plant)
    ledger.observe(0, {"sensor.boiler": 0.0, "binary_sensor.pump": True})
    ledger.observe(100, {"sensor.boiler": 6.0})
    ledger.close_interval(300)
    data = ledger.to_dict()
    del data["circuit_consumed"]
    assert Ledger.from_dict(plant, data).circuit_total("circuit") == pytest.approx(6.0)


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


HOUR, DAY = 3600, 86400


@pytest.mark.parametrize(
    ("values", "step", "booked"),
    [
        ([100, 0, 105], HOUR, 5),  # glitch: back above the old value, only the part above counts
        ([100, 0, 3], HOUR, 0),  # below the old value for an hour: held, not yet a reset
        ([100, 0, 3], DAY, 3),  # below for a day: a real reset, counted from its lowest value
        ([100, 0, 3, 110], DAY, 110),  # reset, then grows past the old value: all of it counts
        ([100, None, 107], HOUR, 7),  # unavailable: increase booked when back
        ([100, 99.5, 101], HOUR, 1),  # small drop and back: glitch
        ([100, 60, 61, 62, 102.5], 300, 2.5),  # a template sum missing one part for a while, recovering in steps
        ([100, 99.9, 99.95, 100.2], 300, 0.2),  # jitter
    ],
)
def test_source_counter(values, step, booked):
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"),)))
    for i, value in enumerate(values):
        ledger.observe(i * step, {"sensor.boiler": value})
    assert sum(ledger.source_heat.values()) == pytest.approx(booked)


def test_held_drop_survives_a_restart():
    """Held after a drop, then restarted: still held, and the glitch rule applies when the reading comes back."""
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"),)))
    ledger.observe(0, {"sensor.boiler": 100.0})
    ledger.observe(300, {"sensor.boiler": 60.0})
    ledger.observe(600, {"sensor.boiler": 61.0})
    restored = Ledger.from_dict(ledger.plant, ledger.to_dict())
    restored.observe(900, {"sensor.boiler": 101.0})
    assert restored.source_heat["boiler"] == pytest.approx(1.0)


def test_held_drop_from_an_older_state_is_still_held():
    """An older state stored the drop as [last, before_drop] without its time."""
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"),)))
    ledger.observe(0, {"sensor.boiler": 60.0})
    data = ledger.to_dict()
    data["sources"] = {"boiler": [60.0, 100.0]}
    restored = Ledger.from_dict(ledger.plant, data)
    restored.observe(300, {"sensor.boiler": 62.0})
    restored.observe(600, {"sensor.boiler": 102.0})
    assert restored.source_heat["boiler"] == pytest.approx(2.0)


def test_implausible_jump_is_not_booked():
    """Wh reported as kWh for a moment: the jump is skipped, the counter goes on from where it was before."""
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"),)))
    ledger.observe(0, {"sensor.boiler": 1000.0})
    ledger.observe(600, {"sensor.boiler": 1000500.0})  # 999500 kWh in 10 minutes
    assert ledger.rejected == [("sensor.boiler", pytest.approx(999500.0))]
    ledger.observe(900, {"sensor.boiler": 1001.0})  # back to kWh
    ledger.observe(1800, {"sensor.boiler": 1003.0})
    assert sum(ledger.source_heat.values()) == pytest.approx(3.0)


def test_jump_that_stays_is_a_new_meter():
    """A meter swapped for one with a higher reading: the jump is skipped, then it counts on from the new value."""
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"),)))
    ledger.observe(0, {"sensor.boiler": 1000.0})
    ledger.observe(300, {"sensor.boiler": 50000.0})
    ledger.observe(3600, {"sensor.boiler": 50004.0})
    assert sum(ledger.source_heat.values()) == pytest.approx(4.0)


def test_limit_is_an_average_since_the_last_change():
    """After a long pause, a large increase is plausible: the limit scales with the time since the last change."""
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"),)))
    ledger.observe(0, {"sensor.boiler": 0.0})
    ledger.observe(10, {"sensor.boiler": 50.0})  # 50 kWh: within the limit for the minimum span of 5 minutes
    ledger.observe(3 * 86400, {"sensor.boiler": 1500.0})  # three days later: 20 kW on average
    assert ledger.rejected == []
    assert sum(ledger.source_heat.values()) == pytest.approx(1500.0)


def test_limit_applies_to_heat_after_the_factor():
    """A gas meter in m³ with a factor of 10: 120 m³ in 5 minutes are 1200 kWh, too much."""
    gas = SourceSpec("gas", "sensor.gas", factor=10.0)
    ledger = Ledger(PlantSpec((gas,), consumers=(radiator("a"),)))
    ledger.observe(0, {"sensor.gas": 0.0})
    ledger.observe(300, {"sensor.gas": 120.0})
    assert ledger.source_heat["gas"] == 0.0
    ledger.observe(600, {"sensor.gas": 121.0})
    assert ledger.source_heat["gas"] == pytest.approx(10.0)


def test_other_entity_after_restore_starts_over():
    """A source switched to another meter: the difference between the two meters is not heat."""
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"),)))
    ledger.observe(0, {"sensor.boiler": 100.0})
    switched = PlantSpec((SourceSpec("boiler", "sensor.new_meter"),), consumers=(radiator("a"),))
    restored = Ledger.from_dict(switched, ledger.to_dict())
    restored.observe(3600, {"sensor.new_meter": 5000.0})
    restored.observe(7200, {"sensor.new_meter": 5004.0})
    assert restored.source_heat["boiler"] == pytest.approx(4.0)


def test_state_without_entity_is_restored():
    """Stored before the entity was saved with the counter: taken as the same entity."""
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"),)))
    ledger.observe(0, {"sensor.boiler": 100.0})
    data = ledger.to_dict()
    data["sources"] = {"boiler": [100.0, None]}
    restored = Ledger.from_dict(ledger.plant, data)
    restored.observe(3600, {"sensor.boiler": 104.0})
    assert restored.source_heat["boiler"] == pytest.approx(4.0)


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
