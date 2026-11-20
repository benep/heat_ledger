"""The ledger: books source heat into an account and splits it between consumers by their weights.

Pure Python, no Home Assistant, no clock: every call gets the time in seconds (any epoch, monotonic per ledger).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .spec import ActiveMethod, ConsumerSpec, DemandMethod, PlantSpec

Reading = float | bool | None
Readings = Mapping[str, Reading]

STATE_VERSION = 1

# A drop below this fraction of the reading before is a reset, as Home Assistant treats a total_increasing sensor: a
# meter of the day back at 0, or a new meter. A smaller drop is a dip of an inaccurate meter.
RESET_FRACTION = 0.9
# Heat a meter measured before the sources delivered waits at most this long for them. A source reporting once a day
# still catches up; heat that never arrives (a heat meter that jumped, a reset of a meter of a lifetime) is written off
# instead of taking every delivery for months.
WAIT_MAX_S = 48 * 3600.0
# Heat a meter measures within this time and circuit waits as one part.
INTERVAL_S = 300.0
# Balances below this are rounding residue, not heat.
EPSILON_KWH = 1e-9


class _Counter:
    """Follows a cumulative reading and returns its increases, by the rules Home Assistant applies to the statistics of
    a total_increasing sensor, so the counters agree with the source in the Energy dashboard:

    - An increase counts in full, however large. Like Home Assistant, the counter does not judge whether it is
      plausible; a spike is corrected in the statistics, as for any other sensor.
    - A drop below RESET_FRACTION of the reading before is a reset: the new reading counts in full, as a new cycle
      starting at 0 (a meter of the day after midnight).
    - A smaller drop is a dip. Home Assistant's sum goes down by it and up again; a counter here never decreases, so
      the dip is kept as a debt that the next increases pay off first.
    - Unavailable or negative: skipped; the next valid reading is compared with the last valid one.
    - A meter is identified by its entity and the unit it is read in; restored with another one, it starts over.
    """

    __slots__ = ("debt", "key", "last", "unit")

    def __init__(self, key: str, unit: str | None = None) -> None:
        self.key = key
        self.unit = unit
        self.last: float | None = None
        self.debt = 0.0

    def update(self, value: Reading) -> float:
        if value is None or isinstance(value, bool) or value < 0:
            return 0.0
        last, self.last = self.last, float(value)
        if last is None:
            return 0.0
        if self.last < last * RESET_FRACTION:
            increase = self.last
        elif self.last < last:
            self.debt += last - self.last
            return 0.0
        else:
            increase = self.last - last
        paid = min(increase, self.debt)
        self.debt -= paid
        return increase - paid

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "unit": self.unit, "last": self.last, "debt": self.debt}

    def same_meter(self, stored: Any) -> bool:
        """Whether a stored counter is this meter: same entity, read in the same unit (older states lack them)."""
        if isinstance(stored, Mapping):
            key, unit = stored.get("key"), stored.get("unit", self.unit)
        else:
            key, unit = (stored[2] if len(stored) > 2 else None), self.unit
        return (key is None or key == self.key) and unit == self.unit

    def restore(self, stored: Any) -> None:
        """From to_dict, or from the older list [last, before_drop(, key)]. Older versions also kept a drop or a jump
        they were holding back: after a drop, the reading counts on from its last value, as they did; after a jump they
        had not booked, from the value before it, so a glitch that comes back books nothing. (A next reading still at
        the scale of the jump counts it in full, as any increase does now.)"""
        if isinstance(stored, Mapping) and stored.get("before_jump") is not None:
            last = stored["before_jump"]
        else:
            last = stored.get("last") if isinstance(stored, Mapping) else stored[0]
        self.last = float(last) if last is not None else None
        self.debt = float(stored.get("debt") or 0.0) if isinstance(stored, Mapping) else 0.0


class Ledger:
    """Sources fill an account per source; consumers take from it by weight, or by their own heat meter.

    Balances never go negative: heat is only taken from sources that hold some, up to what they hold. Heat a meter
    measures beyond that waits until a source delivers, for WAIT_MAX_S at most. Totals of consumers and circuits are
    kept separately from their split by source, so they only ever grow. (An older version could overdraw a balance;
    such a balance is kept as it was, so the next deliveries fill it first instead of being split a second time.)
    """

    def __init__(self, plant: PlantSpec) -> None:
        self.plant = plant
        source_ids = [s.id for s in plant.sources]
        self.account: dict[str, float] = dict.fromkeys(source_ids, 0.0)
        self.source_heat: dict[str, float] = dict.fromkeys(source_ids, 0.0)
        self.losses: dict[str, float] = dict.fromkeys(source_ids, 0.0)
        # By source, for costs and the `by_source` attribute.
        self.consumed: dict[str, dict[str, float]] = {c.id: dict.fromkeys(source_ids, 0.0) for c in plant.consumers}
        # A circuit counts what its consumers received while they belonged to it: moving one to another circuit
        # leaves its history with the old circuit, and the new one does not jump by it.
        self.circuit_consumed: dict[str, dict[str, float]] = {
            c.id: dict.fromkeys(source_ids, 0.0) for c in plant.circuits
        }
        self.totals: dict[str, float] = dict.fromkeys((c.id for c in plant.consumers), 0.0)
        self.circuit_totals: dict[str, float] = dict.fromkeys((c.id for c in plant.circuits), 0.0)
        # Heat a meter measured beyond what the sources held: counted for its consumer at once, taken out of the
        # account when a source delivers. Per consumer as [time, kWh, circuit] in the order measured, so it is charged
        # to the circuit the consumer belonged to then, also if it has been moved since.
        self.waiting: dict[str, list[list[Any]]] = {}
        # What waited longer than WAIT_MAX_S: never taken out of the account, shown as not attributed for good.
        self.written_off: dict[str, float] = {}
        self.circuit_written_off: dict[str, float] = {}
        self._circuit_of = {c.id: c.circuit_id for c in plant.consumers if c.circuit_id in self.circuit_consumed}
        self._sources = {s.id: _Counter(s.key, s.unit) for s in plant.sources}
        self._meters = {c.id: _Counter(c.meter_key) for c in plant.consumers if c.meter_key is not None}
        self._split = [c for c in plant.consumers if c.meter_key is None]
        self._readings: dict[str, Reading] = {}
        self._rates: dict[str, float] | None = None
        self._blocked: tuple[str, ...] = ()
        self._integral: dict[str, float] = {c.id: 0.0 for c in self._split}
        self._last_t: float | None = None
        self._interval_start: float | None = None
        self.restored = False  # from a stored state (from_dict), not started fresh

    # --- Input -------------------------------------------------------------------------------------------------

    def observe(self, t: float, readings: Readings) -> None:
        """New readings at time t. Readings not given keep their previous value."""
        t = self._advance(t)
        self._readings.update(readings)
        for source in self.plant.sources:
            increase = self._update(self._sources[source.id])
            if increase > 0:
                heat = increase * source.factor
                self.account[source.id] += heat
                self.source_heat[source.id] += heat
        for consumer in self.plant.consumers:
            if consumer.meter_key is not None:
                heat = self._update(self._meters[consumer.id])
                if heat > 0:
                    self._count(consumer.id, heat)
                    self._wait(consumer.id, heat, t)
        self._attribute(t)
        self._rates, self._blocked = self._compute_rates()

    def has_reading(self, key: str, reading: Reading) -> bool:
        """Whether this reading is already the current one of the input: observing it again changes nothing."""
        return key in self._readings and self._readings[key] == reading and type(self._readings[key]) is type(reading)

    def rename_input(self, old: str, new: str) -> None:
        """An input entity got a new ID: its meter counts on under it, as the same meter (not another one)."""
        for counter in (*self._sources.values(), *self._meters.values()):
            if counter.key == old:
                counter.key = new
        if old in self._readings:
            self._readings[new] = self._readings.pop(old)

    def close_interval(self, t: float) -> dict[str, float]:
        """Release the account by the weights integrated since the last close. Returns kWh per consumer."""
        t = self._advance(t)
        start = self._interval_start if self._interval_start is not None else t
        duration = t - start
        self._interval_start = t

        if self.plant.loss_kw > 0 and duration > 0:
            loss = min(self.plant.loss_kw * duration / 3600, self.available)
            for source_id, amount in self._take(loss).items():
                self.losses[source_id] += amount

        released = dict.fromkeys(self._integral, 0.0)
        total_weight = sum(self._integral.values())
        available = self.available
        if total_weight > 0 and available > 0:
            tau = self.plant.release_tau_s
            amount = available if tau <= 0 else available * (1 - math.exp(-duration / tau))
            taken = self._take(amount)
            amount = sum(taken.values())
            for consumer_id, weight in self._integral.items():
                share = weight / total_weight
                for source_id, heat in taken.items():
                    self._book(consumer_id, source_id, heat * share)
                self._count(consumer_id, amount * share)
                released[consumer_id] = amount * share
        self._integral = dict.fromkeys(self._integral, 0.0)
        return released

    # --- Results -----------------------------------------------------------------------------------------------

    @property
    def account_total(self) -> float:
        return sum(self.account.values())

    @property
    def available(self) -> float:
        """Heat the sources hold that is not assigned yet."""
        return sum(v for v in self.account.values() if v > EPSILON_KWH)

    @property
    def blocked_by(self) -> tuple[str, ...]:
        """Input keys that are unavailable and stop the split right now."""
        return self._blocked

    def consumer_total(self, consumer_id: str) -> float:
        return self.totals[consumer_id]

    @property
    def unattributed(self) -> dict[str, float]:
        """Heat waiting to be attributed to a source, per consumer."""
        return {consumer_id: self.pending(consumer_id) for consumer_id in self.waiting}

    def pending(self, consumer_id: str) -> float:
        """Heat a consumer's meter measured that is not attributed to a source yet."""
        return sum(heat for _, heat, _ in self.waiting.get(consumer_id, ()))

    def circuit_pending(self, circuit_id: str) -> float:
        return sum(heat for parts in self.waiting.values() for _, heat, circuit in parts if circuit == circuit_id)

    def circuit_total(self, circuit_id: str) -> float:
        return self.circuit_totals[circuit_id]

    # --- State -------------------------------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "account": dict(self.account),
            "source_heat": dict(self.source_heat),
            "losses": dict(self.losses),
            "consumed": {k: dict(v) for k, v in self.consumed.items()},
            "circuit_consumed": {k: dict(v) for k, v in self.circuit_consumed.items()},
            "totals": dict(self.totals),
            "circuit_totals": dict(self.circuit_totals),
            "waiting": {k: [list(part) for part in v] for k, v in self.waiting.items()},
            "written_off": dict(self.written_off),
            "circuit_written_off": dict(self.circuit_written_off),
            "sources": {k: c.to_dict() for k, c in self._sources.items()},
            "meters": {k: c.to_dict() for k, c in self._meters.items()},
            "integral": dict(self._integral),
            "last_t": self._last_t,
            "interval_start": self._interval_start,
        }

    @classmethod
    def from_dict(cls, plant: PlantSpec, data: Mapping[str, Any]) -> Ledger:
        """Restore a ledger. Sources, consumers or circuits added since start at zero; removed consumers and circuits
        are dropped. A removed source stays in the books (what it delivered, what it holds, what consumers received
        from it), so no total goes down and what it still holds is released as before.

        The time between the stored state and the first new reading counts for no consumer: the weights during a
        downtime are unknown. Heat that arrived meanwhile is split by the weights after the restart.

        A source or meter that now reads another entity, or in another unit, starts over: its next reading becomes
        the baseline, so the difference between the old and the new meter is not booked. Raises on data that is not a
        stored ledger.
        """
        ledger = cls(plant)
        if data.get("version") != STATE_VERSION:
            return ledger
        ledger.restored = True
        for name in ("account", "source_heat", "losses"):
            getattr(ledger, name).update({k: float(v) for k, v in data.get(name, {}).items()})
        for name, target in (("consumed", ledger.consumed), ("circuit_consumed", ledger.circuit_consumed)):
            for owner, by_source in data.get(name, {}).items():
                if owner in target:
                    target[owner].update({k: float(v) for k, v in by_source.items()})
        last_t = data.get("last_t")
        # Also of consumers removed since: the sources have not delivered that heat yet, so it is taken out of the
        # account when they do, instead of being split among the others.
        if "waiting" in data:
            for consumer_id, parts in data["waiting"].items():
                kept = [[float(at), float(heat), circuit] for at, heat, circuit in parts if float(heat) > 0]
                if kept:
                    ledger.waiting[consumer_id] = kept
        else:  # stored as a sum per consumer: waiting since the stored state, in the circuit it belongs to now
            for consumer_id, heat in data.get("unattributed", {}).items():
                if float(heat) > 0:
                    at = float(last_t) if last_t is not None else 0.0
                    ledger.waiting[consumer_id] = [[at, float(heat), ledger._circuit_of.get(consumer_id)]]
        ledger.written_off = {k: float(v) for k, v in data.get("written_off", {}).items()}
        ledger.circuit_written_off = {
            k: float(v) for k, v in data.get("circuit_written_off", {}).items() if k in ledger.circuit_totals
        }
        if "circuit_consumed" not in data:
            # Stored before circuits had their own counters: they were the sum of their consumers, start from that.
            for consumer_id, circuit_id in ledger._circuit_of.items():
                target = ledger.circuit_consumed[circuit_id]
                for source_id, heat in ledger.consumed[consumer_id].items():
                    target[source_id] = target.get(source_id, 0.0) + heat
        # Every split by source knows every source in the account, removed ones included.
        for by_source in (*ledger.consumed.values(), *ledger.circuit_consumed.values()):
            for source_id in ledger.account:
                by_source.setdefault(source_id, 0.0)
        totals, circuit_totals = data.get("totals"), data.get("circuit_totals")
        for consumer_id in ledger.totals:
            if isinstance(totals, Mapping) and consumer_id in totals:
                ledger.totals[consumer_id] = float(totals[consumer_id])
            else:  # stored before totals were kept: what it received plus what waits
                received = sum(ledger.consumed[consumer_id].values())
                ledger.totals[consumer_id] = received + ledger.pending(consumer_id)
        for circuit_id in ledger.circuit_totals:
            if isinstance(circuit_totals, Mapping) and circuit_id in circuit_totals:
                ledger.circuit_totals[circuit_id] = float(circuit_totals[circuit_id])
            else:
                received = sum(ledger.circuit_consumed[circuit_id].values())
                ledger.circuit_totals[circuit_id] = received + ledger.circuit_pending(circuit_id)
        for name, counters in (("sources", ledger._sources), ("meters", ledger._meters)):
            for counter_id, stored in data.get(name, {}).items():
                counter = counters.get(counter_id)
                if counter is None or not counter.same_meter(stored):
                    continue  # another entity or unit: it starts over from its next reading
                counter.restore(stored)
        ledger._integral.update({k: float(v) for k, v in data.get("integral", {}).items() if k in ledger._integral})
        ledger._last_t = last_t
        ledger._interval_start = data.get("interval_start")
        return ledger

    # --- Internals ---------------------------------------------------------------------------------------------

    def _update(self, counter: _Counter) -> float:
        return counter.update(self._readings.get(counter.key))

    def _wait(self, consumer_id: str, heat: float, t: float) -> None:
        """Heat a meter measured waits for a source; parts measured within an interval in the same circuit merge."""
        circuit_id = self._circuit_of.get(consumer_id)
        parts = self.waiting.setdefault(consumer_id, [])
        if parts and parts[-1][2] == circuit_id and t - parts[-1][0] < INTERVAL_S:
            parts[-1][1] += heat
        else:
            parts.append([t, heat, circuit_id])

    def _write_off(self, t: float) -> None:
        """Heat that waited longer than WAIT_MAX_S is written off: no source will deliver it any more."""
        for consumer_id in list(self.waiting):
            parts = self.waiting[consumer_id]
            while parts and t - parts[0][0] > WAIT_MAX_S:
                _, heat, circuit_id = parts.pop(0)
                self.written_off[consumer_id] = self.written_off.get(consumer_id, 0.0) + heat
                if circuit_id in self.circuit_totals:
                    self.circuit_written_off[circuit_id] = self.circuit_written_off.get(circuit_id, 0.0) + heat
            if not parts:
                del self.waiting[consumer_id]

    def _attribute(self, t: float) -> None:
        """Take what meters measured out of the account, as far as the sources hold heat, per consumer the oldest part
        first; then write off what has waited too long. In this order, heat delivered late (after a downtime) still
        covers what waited for it."""
        for consumer_id in list(self.waiting):
            parts = self.waiting[consumer_id]
            waiting = sum(heat for _, heat, _ in parts)
            if waiting <= EPSILON_KWH:  # rounding residue, not heat
                del self.waiting[consumer_id]
                continue
            available = self.available
            if available <= 0:
                break
            taken = self._take(min(waiting, available))
            amount = total = sum(taken.values())
            if consumer_id in self.consumed:  # not removed since
                for source_id, heat in taken.items():
                    self.consumed[consumer_id][source_id] += heat
            # Each part, in the order measured, is charged to the circuit it was measured in.
            while parts and amount > EPSILON_KWH:
                part = min(parts[0][1], amount)
                if (circuit_id := parts[0][2]) in self.circuit_consumed:
                    for source_id, heat in taken.items():
                        self.circuit_consumed[circuit_id][source_id] += heat * part / total
                amount -= part
                parts[0][1] -= part
                if parts[0][1] <= EPSILON_KWH:
                    parts.pop(0)
            if not parts:
                del self.waiting[consumer_id]
        self._write_off(t)

    def _count(self, consumer_id: str, heat: float) -> None:
        """Add to the totals, which only ever grow."""
        self.totals[consumer_id] += heat
        if (circuit_id := self._circuit_of.get(consumer_id)) is not None:
            self.circuit_totals[circuit_id] += heat

    def _book(self, consumer_id: str, source_id: str, heat: float) -> None:
        """Add to the split by source."""
        self.consumed[consumer_id][source_id] += heat
        if (circuit_id := self._circuit_of.get(consumer_id)) is not None:
            self.circuit_consumed[circuit_id][source_id] += heat

    def _take(self, amount: float) -> dict[str, float]:
        """Take heat out of the account, from the sources that hold some, in proportion to it; never more than they
        hold, so no balance goes negative."""
        positive = {k: v for k, v in self.account.items() if v > EPSILON_KWH}
        total = sum(positive.values())
        if total <= 0 or amount <= 0:
            return {}
        amount = min(amount, total)
        taken = {k: amount * v / total for k, v in positive.items()}
        for source_id, value in taken.items():
            self.account[source_id] = max(self.account[source_id] - value, 0.0)
        return taken

    def _advance(self, t: float) -> float:
        """Integrate the current weights up to t (never backwards)."""
        if self._last_t is not None:
            t = max(t, self._last_t)
            if self._rates is not None:
                dt = t - self._last_t
                for consumer_id, rate in self._rates.items():
                    self._integral[consumer_id] += rate * dt
        if self._interval_start is None:
            self._interval_start = t
        self._last_t = t
        return t

    def _compute_rates(self) -> tuple[dict[str, float] | None, tuple[str, ...]]:
        rates: dict[str, float] = {}
        missing: list[str] = []
        for consumer in self._split:
            demand = self._demand(consumer, missing)
            active = self._active(consumer, missing)
            rates[consumer.id] = consumer.size.watts * demand * active
        if missing:
            return None, tuple(dict.fromkeys(missing))
        return rates, ()

    def _demand(self, consumer: ConsumerSpec, missing: list[str]) -> float:
        if consumer.demand.method is DemandMethod.ALWAYS:
            return 1.0
        return self._fraction(consumer.demand.key, missing)

    def _active(self, consumer: ConsumerSpec, missing: list[str]) -> float:
        circuit = self.plant.circuit(consumer.circuit_id)
        if circuit is None or circuit.active is ActiveMethod.ALWAYS:
            return 1.0
        return self._fraction(circuit.pump_key, missing)

    def _fraction(self, key: str | None, missing: list[str]) -> float:
        value = self._readings.get(key) if key is not None else None
        if value is None:
            missing.append(str(key))
            return 0.0
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        return min(max(float(value), 0.0), 1.0)
