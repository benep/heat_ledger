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

# No heating plant this integration is meant for delivers more on average. A larger increase is a glitch (an
# integration briefly reporting Wh as kWh, a meter swapped for one with a higher reading): booked, it would stay in
# every counter for good.
MAX_PLAUSIBLE_KW = 1000.0
# The average is taken over at least this long, so two readings close together cannot trip the limit.
MIN_SPAN_S = 300.0
# After a drop, nothing is booked until the reading is back at its old value (a glitch: only what lies above counts)
# or it stayed below for this long (a meter that was really reset or replaced: counted from its lowest value). A
# template that adds up two meters and briefly misses one of them recovers in steps; booking those steps would
# count the recovered part twice.
RESET_AFTER_S = 24 * 3600.0


class _Counter:
    """Follows a cumulative reading and returns its increases.

    - Drop: held, see RESET_AFTER_S. The heat of the held time is booked when the hold ends, not lost.
    - Jump: an increase above `limit_per_hour` (in units of the reading) since the last change is a glitch, such as
      Wh reported as kWh. It is reported in `rejected` instead of being returned. If the reading comes back to where
      it was before the jump, it continues from there; if not, the jump was a meter swapped for one with a higher
      reading, and the new value is the baseline.
    """

    __slots__ = (
        "before_drop",
        "before_jump",
        "changed_t",
        "drop_t",
        "key",
        "last",
        "limit_per_hour",
        "low",
        "rejected",
    )

    def __init__(self, key: str, limit_per_hour: float) -> None:
        self.key = key
        self.limit_per_hour = limit_per_hour
        self.last: float | None = None
        self.changed_t: float | None = None  # time of the last booked change, for the limit
        self.before_drop: float | None = None  # holding: the value before the drop
        self.drop_t: float | None = None
        self.low: float | None = None  # lowest value while holding
        self.before_jump: float | None = None  # the value before a rejected jump
        self.rejected: float | None = None

    def update(self, value: Reading, t: float) -> float:
        if value is None or isinstance(value, bool):
            return 0.0
        value = float(value)
        if self.last is None:
            self.last, self.changed_t = value, t
            return 0.0
        if self.before_drop is not None:
            return self._holding(value, t)
        if value == self.last:
            return 0.0
        if value < self.last:
            if self.before_jump is not None and value >= self.before_jump:
                # Back from a rejected jump: go on from where it was before.
                base, self.before_jump = self.before_jump, None
                return self._book(value, base, t)
            self.before_drop = self.before_jump if self.before_jump is not None else self.last
            self.before_jump = None
            self.low, self.drop_t, self.last = value, t, value
            return 0.0
        return self._book(value, self.last, t)

    def _holding(self, value: float, t: float) -> float:
        assert self.before_drop is not None
        if self.low is None or self.drop_t is None:  # restored from a state without them
            self.low, self.drop_t = self.last if self.last is not None else value, t
        if value >= self.before_drop:
            base = self.before_drop  # a glitch: only the part above the old value counts
        elif t - self.drop_t >= RESET_AFTER_S:
            base = min(self.low, value)  # a real reset: count from its lowest value
        else:
            self.low, self.last = min(self.low, value), value
            return 0.0
        self.before_drop = self.low = self.drop_t = None
        return self._book(value, base, t)

    def _book(self, value: float, base: float, t: float) -> float:
        span = max(t - self.changed_t, MIN_SPAN_S) if self.changed_t is not None else MIN_SPAN_S
        increase = max(value - base, 0.0)
        if increase > self.limit_per_hour * span / 3600:
            self.rejected = increase
            self.before_jump = base if self.before_jump is None else self.before_jump
            self.last = value
            return 0.0
        # A normal increase after a rejected jump: the jump was a meter with a higher reading, not a glitch.
        self.last, self.changed_t, self.before_jump = value, t, None
        return increase

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "last": self.last,
            "changed_t": self.changed_t,
            "before_drop": self.before_drop,
            "drop_t": self.drop_t,
            "low": self.low,
            "before_jump": self.before_jump,
        }

    def restore(self, stored: Any, last_t: float | None) -> None:
        """From to_dict, or from the older list [last, before_drop(, key)] that held the drop without a time."""
        if isinstance(stored, Mapping):
            for name in ("last", "changed_t", "before_drop", "drop_t", "low", "before_jump"):
                value = stored.get(name)
                setattr(self, name, float(value) if value is not None else None)
            return
        last, before_drop = stored[0], stored[1]
        self.last = float(last) if last is not None else None
        self.changed_t = last_t
        if before_drop is not None and self.last is not None:
            self.before_drop, self.low, self.drop_t = float(before_drop), self.last, last_t

    @staticmethod
    def stored_key(stored: Any) -> str | None:
        if isinstance(stored, Mapping):
            return stored.get("key")
        return stored[2] if len(stored) > 2 else None


class Ledger:
    def __init__(self, plant: PlantSpec) -> None:
        self.plant = plant
        source_ids = [s.id for s in plant.sources]
        self.account: dict[str, float] = dict.fromkeys(source_ids, 0.0)
        self.source_heat: dict[str, float] = dict.fromkeys(source_ids, 0.0)
        self.losses: dict[str, float] = dict.fromkeys(source_ids, 0.0)
        self.consumed: dict[str, dict[str, float]] = {c.id: dict.fromkeys(source_ids, 0.0) for c in plant.consumers}
        # A circuit counts what its consumers received while they belonged to it: moving one to another circuit
        # leaves its history with the old circuit, and the new one does not jump by it.
        self.circuit_consumed: dict[str, dict[str, float]] = {
            c.id: dict.fromkeys(source_ids, 0.0) for c in plant.circuits
        }
        self._circuit_of = {c.id: c.circuit_id for c in plant.consumers if c.circuit_id in self.circuit_consumed}
        self._sources = {
            s.id: _Counter(s.key, MAX_PLAUSIBLE_KW / s.factor if s.factor > 0 else math.inf) for s in plant.sources
        }
        self._meters = {
            c.id: _Counter(c.meter_key, MAX_PLAUSIBLE_KW) for c in plant.consumers if c.meter_key is not None
        }
        self.rejected: list[tuple[str, float]] = []  # (entity, increase in its unit): implausible jumps, not booked
        self._split = [c for c in plant.consumers if c.meter_key is None]
        self._readings: dict[str, Reading] = {}
        self._rates: dict[str, float] | None = None
        self._blocked: tuple[str, ...] = ()
        self._integral: dict[str, float] = {c.id: 0.0 for c in self._split}
        self._last_t: float | None = None
        self._interval_start: float | None = None

    # --- Input -------------------------------------------------------------------------------------------------

    def observe(self, t: float, readings: Readings) -> None:
        """New readings at time t. Readings not given keep their previous value."""
        t = self._advance(t)
        self._readings.update(readings)
        for source in self.plant.sources:
            increase = self._update(self._sources[source.id], t)
            if increase > 0:
                heat = increase * source.factor
                self.account[source.id] += heat
                self.source_heat[source.id] += heat
        for consumer in self.plant.consumers:
            if consumer.meter_key is not None:
                heat = self._update(self._meters[consumer.id], t)
                if heat > 0:
                    for source_id, amount in self._take(heat).items():
                        self._book(consumer.id, source_id, amount)
        self._rates, self._blocked = self._compute_rates()

    def rename_input(self, old: str, new: str) -> None:
        """An input entity got a new ID: its meter counts on under it, as the same meter (not another one)."""
        for counter in (*self._sources.values(), *self._meters.values()):
            if counter.key == old:
                counter.key = new
        if old in self._readings:
            self._readings[new] = self._readings.pop(old)

    def _book(self, consumer_id: str, source_id: str, heat: float) -> None:
        self.consumed[consumer_id][source_id] += heat
        if (circuit_id := self._circuit_of.get(consumer_id)) is not None:
            self.circuit_consumed[circuit_id][source_id] += heat

    def _update(self, counter: _Counter, t: float) -> float:
        increase = counter.update(self._readings.get(counter.key), t)
        if counter.rejected is not None:
            self.rejected.append((counter.key, counter.rejected))
            counter.rejected = None
        return increase

    def close_interval(self, t: float) -> dict[str, float]:
        """Release the account by the weights integrated since the last close. Returns kWh per consumer."""
        t = self._advance(t)
        start = self._interval_start if self._interval_start is not None else t
        duration = t - start
        self._interval_start = t

        if self.plant.loss_kw > 0 and duration > 0:
            loss = min(self.plant.loss_kw * duration / 3600, max(self.account_total, 0.0))
            for source_id, amount in self._take(loss).items():
                self.losses[source_id] += amount

        released = dict.fromkeys(self._integral, 0.0)
        total_weight = sum(self._integral.values())
        available = self.account_total
        if total_weight > 0 and available > 0:
            tau = self.plant.release_tau_s
            amount = available if tau <= 0 else available * (1 - math.exp(-duration / tau))
            taken = self._take(amount)
            for consumer_id, weight in self._integral.items():
                share = weight / total_weight
                for source_id, heat in taken.items():
                    self._book(consumer_id, source_id, heat * share)
                released[consumer_id] = amount * share
        self._integral = dict.fromkeys(self._integral, 0.0)
        return released

    # --- Results -----------------------------------------------------------------------------------------------

    @property
    def account_total(self) -> float:
        return sum(self.account.values())

    @property
    def blocked_by(self) -> tuple[str, ...]:
        """Input keys that are unavailable and stop the split right now."""
        return self._blocked

    def consumer_total(self, consumer_id: str) -> float:
        return sum(self.consumed[consumer_id].values())

    def circuit_total(self, circuit_id: str) -> float:
        return sum(self.circuit_consumed[circuit_id].values())

    # --- State -------------------------------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "account": dict(self.account),
            "source_heat": dict(self.source_heat),
            "losses": dict(self.losses),
            "consumed": {k: dict(v) for k, v in self.consumed.items()},
            "circuit_consumed": {k: dict(v) for k, v in self.circuit_consumed.items()},
            "sources": {k: c.to_dict() for k, c in self._sources.items()},
            "meters": {k: c.to_dict() for k, c in self._meters.items()},
            "integral": dict(self._integral),
            "last_t": self._last_t,
            "interval_start": self._interval_start,
        }

    @classmethod
    def from_dict(cls, plant: PlantSpec, data: Mapping[str, Any]) -> Ledger:
        """Restore a ledger. Sources, consumers or circuits added since start at zero; removed ones are dropped.

        The time between the stored state and the first new reading counts for no consumer: the weights during a
        downtime are unknown. Heat that arrived meanwhile is split by the weights after the restart.

        A source or meter that now reads another entity starts over: its next reading becomes the baseline, so the
        difference between the old and the new meter is not booked. Raises on data that is not a stored ledger.
        """
        ledger = cls(plant)
        if data.get("version") != STATE_VERSION:
            return ledger
        for name in ("account", "source_heat", "losses"):
            target = getattr(ledger, name)
            target.update({k: float(v) for k, v in data.get(name, {}).items() if k in target})
        for consumer_id, by_source in data.get("consumed", {}).items():
            if consumer_id in ledger.consumed:
                target = ledger.consumed[consumer_id]
                target.update({k: float(v) for k, v in by_source.items() if k in target})
        if "circuit_consumed" in data:
            for circuit_id, by_source in data["circuit_consumed"].items():
                if circuit_id in ledger.circuit_consumed:
                    target = ledger.circuit_consumed[circuit_id]
                    target.update({k: float(v) for k, v in by_source.items() if k in target})
        else:
            # Stored before circuits had their own counters: they were the sum of their consumers, start from that.
            for consumer_id, circuit_id in ledger._circuit_of.items():
                for source_id, heat in ledger.consumed[consumer_id].items():
                    ledger.circuit_consumed[circuit_id][source_id] += heat
        last_t = data.get("last_t")
        for name, counters in (("sources", ledger._sources), ("meters", ledger._meters)):
            for counter_id, stored in data.get(name, {}).items():
                counter = counters.get(counter_id)
                # Stored without the entity (an older state): assume it is the same entity.
                stored_key = _Counter.stored_key(stored)
                if counter is None or (stored_key is not None and stored_key != counter.key):
                    continue
                counter.restore(stored, last_t)
        ledger._integral.update({k: float(v) for k, v in data.get("integral", {}).items() if k in ledger._integral})
        ledger._last_t = last_t
        ledger._interval_start = data.get("interval_start")
        return ledger

    # --- Internals ---------------------------------------------------------------------------------------------

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

    def _take(self, amount: float) -> dict[str, float]:
        """Take heat out of the account, from each source in proportion to what it holds."""
        positive = {k: v for k, v in self.account.items() if v > 0}
        total = sum(positive.values())
        if total > 0:
            taken = {k: amount * v / total for k, v in positive.items()}
        else:
            taken = {k: amount / len(self.account) for k in self.account}
        for source_id, value in taken.items():
            self.account[source_id] -= value
        return taken
