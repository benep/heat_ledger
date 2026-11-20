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
    used = sum(sum(by_source.values()) for by_source in ledger.consumed.values())
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
    ("values", "booked"),
    [
        ([100, 105], 5),  # an increase
        ([100, None, 107], 7),  # unavailable: the increase is booked when it is back
        ([100, -5, 102], 2),  # negative: skipped like unavailable
        ([100, 0, 3], 3),  # a reset (a meter of the day): the new value counts in full
        ([100, 80, 85], 85),  # below 90 % of the value before: a reset as well
        ([100, 0, 105], 105),  # a template reporting 0 for a moment: a reset too, as in Home Assistant
        ([1000, 1000500], 999500),  # however large, an increase counts: no plausibility check, as in Home Assistant
        ([100, 95, 101], 1),  # a dip: what it went down is paid off by the next increases
        ([100, 99.9, 99.95, 100.2], 0.2),  # jitter
        ([100, 95, 0, 3, 10], 5),  # a dip, then a reset: the dip is still paid off
    ],
)
def test_source_counter(values, booked):
    """The rules Home Assistant applies to the statistics of a total_increasing sensor."""
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"),)))
    for i, value in enumerate(values):
        ledger.observe(i * HOUR, {"sensor.boiler": value})
    assert ledger.source_heat["boiler"] == pytest.approx(booked)


def test_dip_survives_a_restart():
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"),)))
    ledger.observe(0, {"sensor.boiler": 100.0})
    ledger.observe(300, {"sensor.boiler": 95.0})
    restored = Ledger.from_dict(ledger.plant, ledger.to_dict())
    restored.observe(600, {"sensor.boiler": 101.0})
    assert restored.source_heat["boiler"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    "stored",
    [
        [60.0, 100.0],  # an older list [last, before_drop]
        # The previous version, holding back a drop: the reading counts on from its last value, as it did.
        {"key": "sensor.boiler", "last": 60.0, "changed_t": 0.0, "before_drop": 100.0, "booked_since_drop": 2.0},
    ],
)
def test_state_of_an_older_version_counts_on_from_its_last_value(stored):
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"),)))
    data = ledger.to_dict()
    data["sources"] = {"boiler": stored}
    restored = Ledger.from_dict(ledger.plant, data)
    restored.observe(300, {"sensor.boiler": 62.0})
    restored.observe(600, {"sensor.boiler": 102.0})
    assert restored.source_heat["boiler"] == pytest.approx(42.0)


def test_jump_held_back_by_an_older_version_is_not_booked():
    """The previous version had rejected Wh reported as kWh and was waiting for the reading to come back."""
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"),)))
    data = ledger.to_dict()
    data["sources"] = {"boiler": {"key": "sensor.boiler", "last": 30000000.0, "before_jump": 30000.0}}
    restored = Ledger.from_dict(ledger.plant, data)
    restored.observe(300, {"sensor.boiler": 30000.5})
    assert restored.source_heat["boiler"] == pytest.approx(0.5)


def test_heat_a_meter_measured_waits_two_days_at_most():
    """The hot water meter reports 0 once and its reading again: 12000 kWh, counted for hot water as a reset. The
    sources cannot deliver that much: after two days it is written off, and the radiator gets its share again."""
    dhw = ConsumerSpec("dhw", None, Size(), Demand(DemandMethod.ON_OFF, "pump"), meter_key="sensor.dhw", hot_water=True)
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"), dhw)))
    ledger.observe(0, {"sensor.boiler": 0.0, "sensor.dhw": 12000.0, "pump": False})
    ledger.observe(60, {"sensor.dhw": 0.0})
    ledger.observe(120, {"sensor.dhw": 12000.0})
    assert ledger.consumer_total("dhw") == pytest.approx(12000.0)
    for day in range(1, 5):
        ledger.observe(day * DAY, {"sensor.boiler": 50.0 * day})
        ledger.close_interval(day * DAY + 300)
    # Days 1 to 3 went to the waiting heat (the reading of day 3 still covers it before the rest is written off);
    # day 4 is split again.
    assert ledger.consumer_total("a") == pytest.approx(50.0)
    assert ledger.pending("dhw") == 0.0
    assert ledger.written_off["dhw"] == pytest.approx(11850.0)
    assert ledger.consumed["dhw"] == pytest.approx({"boiler": 150.0})
    assert Ledger.from_dict(ledger.plant, ledger.to_dict()).written_off == ledger.written_off


def test_written_off_heat_stays_in_its_circuit():
    """Underfloor heating with a heat meter: what is written off is shown as not attributed in its circuit, so the
    circuit's heat by source still adds up to its total."""
    floor = ConsumerSpec("floor", "circuit", meter_key="sensor.floor")
    ledger = Ledger(PlantSpec((SOURCE,), (PUMP,), (floor, radiator("a", circuit="circuit"))))
    ledger.observe(0, {"sensor.boiler": 0.0, "sensor.floor": 5.0, "binary_sensor.pump": True})
    ledger.observe(60, {"sensor.floor": 200.0})
    ledger.observe(3 * DAY, {"sensor.boiler": 10.0})
    # The 10 kWh delivered cover part of it; the other 185 kWh are written off.
    assert ledger.circuit_written_off == {"circuit": pytest.approx(185.0)}
    received = sum(ledger.circuit_consumed["circuit"].values())
    assert ledger.circuit_total("circuit") == pytest.approx(received + 185.0)


def test_waiting_heat_is_covered_by_a_delivery_after_a_long_downtime():
    """The hot water meter measured 1 kWh before the boiler, then Home Assistant was down for 3 days. The boiler's
    reading at the restart covers it before anything is written off."""
    dhw = ConsumerSpec("dhw", None, Size(), Demand(DemandMethod.ON_OFF, "pump"), meter_key="sensor.dhw", hot_water=True)
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"), dhw)))
    ledger.observe(0, {"sensor.boiler": 0.0, "sensor.dhw": 0.0, "pump": False})
    ledger.observe(60, {"sensor.dhw": 1.0})
    restored = Ledger.from_dict(ledger.plant, ledger.to_dict())
    restored.observe(3 * DAY, {"sensor.boiler": 50.0, "sensor.dhw": 1.0, "pump": False})
    assert restored.consumed["dhw"] == pytest.approx({"boiler": 1.0})
    assert restored.written_off == {}


def test_rounding_residue_does_not_wait():
    dhw = ConsumerSpec("dhw", None, Size(), Demand(DemandMethod.ON_OFF, "pump"), meter_key="sensor.dhw", hot_water=True)
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"), dhw)))
    ledger.observe(0, {"sensor.boiler": 0.0, "sensor.dhw": 1234.56, "pump": False})
    ledger.observe(60, {"sensor.dhw": 1234.5600000000001})
    ledger.observe(120, {"sensor.boiler": 10.0})
    assert ledger.waiting == {}
    assert ledger.account_total == pytest.approx(10.0)


def test_circuit_of_a_state_without_totals_counts_its_waiting_heat():
    """Stored before totals were kept, with 2 kWh of the underfloor heating waiting: the circuit's total includes it,
    so its heat by source adds up to it once a source has delivered."""
    floor = ConsumerSpec("floor", "circuit", meter_key="sensor.floor")
    ledger = Ledger(PlantSpec((SOURCE,), (PUMP,), (floor,)))
    ledger.observe(0, {"sensor.boiler": 0.0, "sensor.floor": 0.0, "binary_sensor.pump": True})
    ledger.observe(60, {"sensor.floor": 2.0})
    data = ledger.to_dict()
    del data["totals"], data["circuit_totals"]
    restored = Ledger.from_dict(ledger.plant, data)
    restored.observe(120, {"sensor.boiler": 5.0})
    assert restored.circuit_total("circuit") == pytest.approx(sum(restored.circuit_consumed["circuit"].values()))


def test_waiting_heat_of_a_source_reporting_once_a_day_is_attributed():
    """A source reporting once a day: what the hot water meter measured meanwhile is charged to it, not written off."""
    dhw = ConsumerSpec("dhw", None, Size(), Demand(DemandMethod.ON_OFF, "pump"), meter_key="sensor.dhw", hot_water=True)
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"), dhw)))
    ledger.observe(0, {"sensor.boiler": 0.0, "sensor.dhw": 0.0, "pump": False})
    for hour in range(1, 24):
        ledger.observe(hour * HOUR, {"sensor.dhw": float(hour)})
        ledger.close_interval(hour * HOUR + 300)
    ledger.observe(DAY, {"sensor.boiler": 100.0})
    assert ledger.consumed["dhw"] == pytest.approx({"boiler": 23.0})
    assert ledger.written_off == {}


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


def test_meter_reset_every_night():
    """Energy today: back to 0 at midnight. Every day counts fully, also a day using more than the day before."""
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"),)))
    days = [9.6, 12.0, 8.0, 15.0]
    t = 0
    ledger.observe(t, {"sensor.boiler": 0.0})
    for total in days:
        for hour in range(1, 24):
            t += HOUR
            ledger.observe(t, {"sensor.boiler": round(total * hour / 23, 4)})
        t += HOUR
        ledger.observe(t, {"sensor.boiler": 0.0})  # midnight
    assert sum(ledger.source_heat.values()) == pytest.approx(sum(days))


def test_meter_set_up_again_with_another_unit_starts_over():
    """Gas in m³ with a factor, then set up again in kWh: the old reading in m³ is no baseline for kWh."""
    gas_m3 = SourceSpec("gas", "sensor.gas", factor=10.0, unit="m³")
    ledger = Ledger(PlantSpec((gas_m3,), consumers=(radiator("a"),)))
    ledger.observe(0, {"sensor.gas": 800.0})
    gas_kwh = SourceSpec("gas", "sensor.gas", factor=0.9, unit="kWh")
    restored = Ledger.from_dict(PlantSpec((gas_kwh,), consumers=(radiator("a"),)), ledger.to_dict())
    restored.observe(10 * HOUR, {"sensor.gas": 8100.0})
    restored.observe(11 * HOUR, {"sensor.gas": 8110.0})
    assert restored.source_heat["gas"] == pytest.approx(9.0)


def test_heat_meter_before_its_source_waits_for_it():
    """The hot water meter reports before the gas meter: the fireplace that never ran is not charged."""
    gas, fire = SourceSpec("gas", "sensor.gas"), SourceSpec("fire", "sensor.fire")
    dhw = ConsumerSpec("dhw", None, Size(), Demand(DemandMethod.ON_OFF, "pump"), meter_key="sensor.dhw", hot_water=True)
    ledger = Ledger(PlantSpec((gas, fire), consumers=(radiator("a"), dhw)))
    ledger.observe(0, {"sensor.gas": 0.0, "sensor.fire": 0.0, "sensor.dhw": 0.0, "pump": True})
    ledger.observe(60, {"sensor.gas": 5.0})
    ledger.close_interval(300)
    ledger.observe(400, {"sensor.dhw": 1.0})  # before the gas meter's next reading
    ledger.observe(500, {"sensor.gas": 6.0})
    ledger.close_interval(600)
    assert ledger.consumed["dhw"] == pytest.approx({"gas": 1.0, "fire": 0.0})
    assert ledger.account == pytest.approx({"gas": 0.0, "fire": 0.0})
    assert conserved(ledger)


def test_removed_source_stays_in_the_totals():
    """Gas replaced by a heat pump: what the radiators received from the gas boiler stays in their counters."""
    gas, hp = SourceSpec("gas", "sensor.gas"), SourceSpec("hp", "sensor.hp")
    plant = PlantSpec((gas, hp), (PUMP,), (radiator("a", circuit="circuit"),))
    ledger = Ledger(plant)
    ledger.observe(0, {"sensor.gas": 0.0, "sensor.hp": 0.0, "binary_sensor.pump": True})
    ledger.observe(100, {"sensor.gas": 5.0, "sensor.hp": 3.0})
    ledger.close_interval(300)
    without_gas = PlantSpec((hp,), (PUMP,), (radiator("a", circuit="circuit"),))
    restored = Ledger.from_dict(without_gas, ledger.to_dict())
    assert restored.consumer_total("a") == pytest.approx(8.0)
    assert restored.circuit_total("circuit") == pytest.approx(8.0)
    restored.observe(400, {"sensor.hp": 4.0, "binary_sensor.pump": True})
    restored.close_interval(600)
    assert restored.consumer_total("a") == pytest.approx(9.0)
    assert Ledger.from_dict(without_gas, restored.to_dict()).consumer_total("a") == pytest.approx(9.0)
    assert conserved(restored)

    # An older state without circuit counters, with the gas share in it: restored as well.
    data = ledger.to_dict()
    del data["circuit_consumed"]
    assert Ledger.from_dict(without_gas, data).circuit_total("circuit") == pytest.approx(8.0)


def test_heat_meter_before_any_source_waits_for_one():
    """A fresh plant: the hot water meter reports before the gas meter; the fireplace that never ran is not charged."""
    gas, fire = SourceSpec("gas", "sensor.gas"), SourceSpec("fire", "sensor.fire")
    dhw = ConsumerSpec("dhw", None, Size(), Demand(DemandMethod.ON_OFF, "pump"), meter_key="sensor.dhw", hot_water=True)
    ledger = Ledger(PlantSpec((gas, fire), consumers=(dhw,)))
    ledger.observe(0, {"sensor.gas": 0.0, "sensor.fire": 0.0, "sensor.dhw": 0.0, "pump": True})
    ledger.observe(60, {"sensor.dhw": 1.0})
    assert ledger.consumer_total("dhw") == pytest.approx(1.0)  # counted at once
    assert ledger.consumed["dhw"] == pytest.approx({"gas": 0.0, "fire": 0.0})
    assert conserved(ledger)
    restored = Ledger.from_dict(ledger.plant, ledger.to_dict())
    restored.observe(120, {"sensor.gas": 3.0})
    assert restored.consumed["dhw"] == pytest.approx({"gas": 1.0, "fire": 0.0})
    assert restored.account == pytest.approx({"gas": 2.0, "fire": 0.0})
    assert conserved(restored)


def test_heat_during_a_downtime_counts():
    """Home Assistant was down for 8 days at 40 kW: all of it is heat."""
    ledger = Ledger(PlantSpec((SOURCE,), consumers=(radiator("a"),)))
    ledger.observe(0, {"sensor.boiler": 1.0})
    restored = Ledger.from_dict(ledger.plant, ledger.to_dict())
    restored.observe(8 * DAY, {"sensor.boiler": 7681.0})
    assert restored.source_heat["boiler"] == pytest.approx(7680.0)


def test_pending_heat_of_a_removed_consumer_is_not_split_again():
    """Underfloor heating with a heat meter reports 2 kWh before any source and is removed. When the boiler delivers
    5 kWh, those 2 kWh are taken out of the account: the radiator in the same circuit gets the other 3."""
    floor = ConsumerSpec("floor", "circuit", meter_key="sensor.floor")
    ledger = Ledger(PlantSpec((SOURCE,), (PUMP,), (floor, radiator("a", circuit="circuit"))))
    ledger.observe(0, {"sensor.boiler": 0.0, "sensor.floor": 0.0, "binary_sensor.pump": True})
    ledger.observe(60, {"sensor.floor": 2.0})
    without = PlantSpec((SOURCE,), (PUMP,), (radiator("a", circuit="circuit"),))
    restored = Ledger.from_dict(without, ledger.to_dict())
    restored.observe(120, {"sensor.boiler": 5.0, "binary_sensor.pump": True})
    restored.close_interval(300)
    assert restored.consumer_total("a") == pytest.approx(3.0)
    assert restored.account_total == pytest.approx(0.0)
    assert restored.circuit_total("circuit") == pytest.approx(5.0)
    assert sum(restored.circuit_consumed["circuit"].values()) == pytest.approx(5.0)


def test_overdrawn_balance_of_the_previous_version_is_filled_first():
    """The previous version let a hot water meter overdraw the account by 5 kWh. The next 5 kWh of the source fill
    it; they are not split a second time."""
    gas = SourceSpec("gas", "sensor.gas")
    dhw = ConsumerSpec("dhw", None, Size(), Demand(DemandMethod.ON_OFF, "pump"), meter_key="sensor.dhw", hot_water=True)
    ledger = Ledger(PlantSpec((gas,), consumers=(radiator("a"), dhw)))
    data = ledger.to_dict()
    data.update(account={"gas": -5.0}, consumed={"a": {"gas": 0.0}, "dhw": {"gas": 5.0}}, totals={"a": 0.0, "dhw": 5.0})
    restored = Ledger.from_dict(ledger.plant, data)
    assert conserved(restored)
    restored.observe(0, {"sensor.gas": 100.0, "sensor.dhw": 50.0, "pump": False})
    restored.observe(60, {"sensor.gas": 105.0})
    restored.close_interval(300)
    assert restored.consumer_total("a") == 0.0
    assert conserved(restored)


def test_pending_heat_stays_with_the_circuit_it_was_measured_in():
    """Underfloor heating with a heat meter reports 2 kWh before any source, then moves to another circuit: the 2 kWh
    are the old circuit's, also once a source has delivered."""
    up, down = CircuitSpec("up"), CircuitSpec("down")
    floor_up = ConsumerSpec("floor", "up", meter_key="sensor.floor")
    ledger = Ledger(PlantSpec((SOURCE,), (up, down), (floor_up, radiator("a", circuit="down"))))
    ledger.observe(0, {"sensor.boiler": 0.0, "sensor.floor": 0.0})
    ledger.observe(60, {"sensor.floor": 2.0})
    moved = PlantSpec((SOURCE,), (up, down), (ConsumerSpec("floor", "down", meter_key="sensor.floor"), radiator("a")))
    restored = Ledger.from_dict(moved, ledger.to_dict())
    assert restored.circuit_pending("up") == pytest.approx(2.0)
    assert restored.circuit_pending("down") == 0.0
    restored.observe(120, {"sensor.boiler": 5.0})
    assert restored.circuit_consumed["up"] == pytest.approx({"boiler": 2.0})
    assert restored.circuit_consumed["down"] == pytest.approx({"boiler": 0.0})
    for circuit in ("up", "down"):
        assert restored.circuit_pending(circuit) == 0.0
        assert sum(restored.circuit_consumed[circuit].values()) == pytest.approx(restored.circuit_total(circuit))
