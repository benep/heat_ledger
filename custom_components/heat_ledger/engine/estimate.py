"""A rough estimate of older periods, from hourly values, calibrated on a period the replay calculated exactly.

Home Assistant keeps the states of the inputs for days only, but hourly statistics for good: the mean of a sensor,
the sum of a meter. On/off inputs (pumps, actuators, thermostats reporting hvac_action) have none. So the estimate
runs the ledger over hours, with what an hour tells:

- a demand in percent or a valve position is its mean over the hour;
- a pump with a sensor standing in for it (its power) ran the share of the hour that sensor's mean tells;
- every other demand counts as always on, and every other circuit as always active;
- a meter (source, heat meter) is read at the end of every hour.

That alone misjudges the split badly: a hot water pump that ran for ten minutes of an hour weighs on all of it, and
a mean does not tell whether a room asked for heat while the burner ran. The weights are therefore calibrated: on
the period calculated exactly from the states, each consumer's weight is scaled until the hourly calculation gives
it the share the exact one gave it. The older hours are then calculated with these weights.

Pure Python like the ledger.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from .ledger import INTERVAL_S, Readings
from .replay import HOUR_S, Replay, ReplayResult
from .spec import ActiveMethod, Demand, DemandMethod, PlantSpec, Size, SizeMethod

HOURLY_DEMANDS = (DemandMethod.PERCENT, DemandMethod.VALVE)
# A weight is scaled within this range of the typical one: a consumer that got nothing in the calibration period
# keeps a little, in case it heats in other seasons.
MAX_FACTOR = 100.0
ITERATIONS = 30
TOLERANCE = 0.002  # shares agree to within this, relative
# The rooms together need at least this share of the heat in the calibration period: calibrated outside the heating
# season, on hot water alone, the rooms' weights say nothing about winter.
MIN_ROOM_SHARE = 0.25
# A meter's gain in an hour is spread evenly over its intervals, as heat arrives while the burner runs, so that a fixed
# loss is taken every interval, as it is live. Each piece is read this long before its interval closes.
_STEPS = round(HOUR_S / INTERVAL_S)
_BEFORE_S = 1.0


@dataclass(frozen=True)
class Hour:
    """What the statistics tell of one hour: the mean of every demand (None: no value), and every meter at its end,
    as a reading that never decreases (the statistics' sum, with drops left out)."""

    start: float
    demands: Readings
    meters: Readings


def hourly_demands(plant: PlantSpec) -> set[str]:
    """The inputs of the weights an hourly mean says something about: demands in percent, and the sensors standing in
    for pumps (history keys), whose readings in an Hour are the share of the hour the pump ran."""
    keys = {c.demand.key for c in plant.consumers if c.demand.method in HOURLY_DEMANDS and c.demand.key}
    keys |= {c.history_key for c in plant.consumers if c.history_key and c.demand.method is DemandMethod.ON_OFF}
    return keys | {c.history_key for c in plant.circuits if c.history_key and c.active is ActiveMethod.PUMP}


def history_keys(plant: PlantSpec) -> set[str]:
    """The sensors standing in for pumps."""
    return hourly_demands(plant) - {c.demand.key for c in plant.consumers if c.demand.method in HOURLY_DEMANDS}


def meter_keys(plant: PlantSpec) -> set[str]:
    keys = {source.key for source in plant.sources}
    return keys | {consumer.meter_key for consumer in plant.consumers if consumer.meter_key}


def split_consumers(plant: PlantSpec) -> list[str]:
    """The consumers that take part in the split (not the measured ones)."""
    return [consumer.id for consumer in plant.consumers if not consumer.meter_key]


def rough_plant(
    plant: PlantSpec, factors: Mapping[str, float] | None = None, hourly: set[str] | None = None
) -> PlantSpec:
    """The plant for hourly values: a pump with a sensor standing in for it reads that one, demands and pumps without
    hourly values are always on, sizes are scaled.

    `hourly`: the inputs with hourly values (default: all that could have them).
    """
    hourly = hourly_demands(plant) if hourly is None else hourly
    factors = factors or {}
    consumers = []
    for consumer in plant.consumers:
        if consumer.demand.key in hourly:
            demand = consumer.demand
        elif consumer.history_key in hourly and consumer.demand.method is DemandMethod.ON_OFF:
            demand = Demand(DemandMethod.ON_OFF, consumer.history_key)
        else:
            demand = Demand(DemandMethod.ALWAYS)
        size = Size(SizeMethod.NOMINAL_POWER, consumer.size.watts * factors.get(consumer.id, 1.0))
        consumers.append(replace(consumer, demand=demand, size=size))
    circuits = tuple(
        replace(circuit, pump_key=circuit.history_key)
        if circuit.history_key in hourly and circuit.active is ActiveMethod.PUMP
        else replace(circuit, active=ActiveMethod.ALWAYS, pump_key=None)
        for circuit in plant.circuits
    )
    return replace(plant, circuits=circuits, consumers=tuple(consumers))


def replay_hours(plant: PlantSpec, hours: Sequence[Hour], demands: set[str]) -> ReplayResult:
    """Run the ledger over hours, in order. The first hour only gives the meters their baseline. What the meters
    gained during missing hours (Home Assistant was down) counts for the next hour there is. A demand without a value
    in an hour stops the split for that hour: its heat waits in the account, as it does live.

    Meter readings must not decrease (see Hour)."""
    replay = Replay(plant)
    end = 0.0
    last: dict[str, float] = {}
    for hour in hours:
        replay.observe(hour.start, {key: hour.demands.get(key) for key in demands})
        end = hour.start + HOUR_S
        for step in range(1, _STEPS + 1):
            readings = {}
            for key, value in hour.meters.items():
                before = last.get(key)
                if value is None or before is None or step == _STEPS:
                    readings[key] = value
                else:
                    readings[key] = before + (value - before) * step / _STEPS
            replay.observe(hour.start + step * INTERVAL_S - _BEFORE_S, readings)
        last.update({key: value for key, value in hour.meters.items() if value is not None})
    return replay.finish(end)


@dataclass(frozen=True)
class Calibration:
    factors: dict[str, float]  # consumer ID -> factor its weight is scaled by
    exact: dict[str, float]  # share of each consumer in the exact calculation
    estimated: dict[str, float]  # and in the hourly one, with the factors


def calibrate(plant: PlantSpec, hours: Sequence[Hour], demands: set[str], exact: Mapping[str, float]) -> Calibration:
    """Factors for the weights, so that the hourly calculation over `hours` splits like the exact one did.

    `exact`: heat each consumer received in these hours, calculated exactly. Only the shares matter: the sources'
    statistics and their recorded states need not agree to the last kWh. Raises ValueError if either calculation
    split nothing, or if the rooms got less than MIN_ROOM_SHARE of the heat.
    """
    ids = split_consumers(plant)
    total = sum(exact.get(cid, 0.0) for cid in ids)
    if total <= 0:
        raise ValueError("nothing was split in the calibration period")
    target = {cid: exact.get(cid, 0.0) / total for cid in ids}
    rooms = [c.id for c in plant.consumers if c.id in target and not c.hot_water]
    if rooms and sum(target[cid] for cid in rooms) < MIN_ROOM_SHARE:
        raise ValueError("the rooms got too little heat in the calibration period")
    factors = dict.fromkeys(ids, 1.0)
    for iteration in range(ITERATIONS + 1):
        result = replay_hours(rough_plant(plant, factors, demands), hours, demands)
        estimated = sum(result.consumer_totals[cid] for cid in ids)
        if estimated <= 0:
            raise ValueError("the hourly values split nothing in the calibration period")
        shares = {cid: result.consumer_totals[cid] / estimated for cid in ids}
        if iteration == ITERATIONS or all(_agrees(shares[cid], target[cid], factors[cid]) for cid in ids):
            return Calibration(factors, target, shares)  # the shares of these factors
        for cid in ids:
            if shares[cid] > 0:
                factors[cid] *= target[cid] / shares[cid] if target[cid] > 0 else 1 / MAX_FACTOR
        factors = _normalized(factors, target)
    raise AssertionError("unreachable")


def _agrees(share: float, target: float, factor: float) -> bool:
    if target == 0:  # as low as it goes
        return share == 0 or factor <= 1 / MAX_FACTOR * (1 + TOLERANCE)
    return abs(share - target) <= TOLERANCE * target


def _normalized(factors: dict[str, float], target: Mapping[str, float]) -> dict[str, float]:
    """Factors relative to the typical one (geometric mean of those that received heat), within MAX_FACTOR of it."""
    heated = [factors[cid] for cid in factors if target[cid] > 0 and factors[cid] > 0]
    mean = math.exp(sum(math.log(f) for f in heated) / len(heated)) if heated else 1.0
    return {cid: min(MAX_FACTOR, max(1 / MAX_FACTOR, f / mean)) for cid, f in factors.items()}
