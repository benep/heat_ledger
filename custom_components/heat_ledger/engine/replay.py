"""Runs a ledger over recorded readings: what the counters would show, had the plant been set up before.

Pure Python like the ledger. Intervals close on the clock, every INTERVAL_S, as the runtime closes them; at every
full hour the totals are noted, as the long-term statistics of Home Assistant keep them.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ledger import INTERVAL_S, Ledger, Readings
from .spec import PlantSpec

HOUR_S = 3600.0


@dataclass(frozen=True)
class ReplayResult:
    """What a replay booked. Times are in seconds, in the epoch of the readings."""

    start: float | None  # time of the first readings; None without any
    end: float
    hours: tuple[float, ...]  # start of every hour that ended within the replay, in order
    consumers: dict[str, tuple[float, ...]]  # total of each consumer at the end of these hours
    circuits: dict[str, tuple[float, ...]]  # the same for each circuit
    consumer_totals: dict[str, float]  # at the end of the replay
    circuit_totals: dict[str, float]
    source_heat: float  # heat the sources delivered
    unassigned: float  # of it, still in the account at the end: no consumer had a weight since it arrived


class Replay:
    """Feed it readings in the order they were recorded, then finish at the time the live counters took over."""

    def __init__(self, plant: PlantSpec) -> None:
        self.ledger = Ledger(plant)
        self._start: float | None = None
        self._next_close = 0.0
        self._hours: list[float] = []
        self._consumers: dict[str, list[float]] = {c.id: [] for c in plant.consumers}
        self._circuits: dict[str, list[float]] = {c.id: [] for c in plant.circuits}

    def observe(self, t: float, readings: Readings) -> None:
        """Readings at time t. A time before the one of the readings before counts as that one."""
        if self._start is None:
            self._start = t
            self._next_close = (t // INTERVAL_S + 1) * INTERVAL_S
        self._close_until(t)
        self.ledger.observe(t, readings)

    def finish(self, t: float) -> ReplayResult:
        """Close everything up to t, the last interval as far as it got, and return what was booked."""
        ledger = self.ledger
        if self._start is not None:
            self._close_until(t)
            ledger.close_interval(t)
        return ReplayResult(
            start=self._start,
            end=t,
            hours=tuple(self._hours),
            consumers={k: tuple(v) for k, v in self._consumers.items()},
            circuits={k: tuple(v) for k, v in self._circuits.items()},
            consumer_totals=dict(ledger.totals),
            circuit_totals=dict(ledger.circuit_totals),
            source_heat=sum(ledger.source_heat.values()),
            unassigned=ledger.account_total,
        )

    def _close_until(self, t: float) -> None:
        ledger = self.ledger
        while self._next_close <= t:
            boundary = self._next_close
            ledger.close_interval(boundary)
            if boundary % HOUR_S == 0:
                self._hours.append(boundary - HOUR_S)
                for consumer_id, totals in self._consumers.items():
                    totals.append(ledger.totals[consumer_id])
                for circuit_id, totals in self._circuits.items():
                    totals.append(ledger.circuit_totals[circuit_id])
            self._next_close = boundary + INTERVAL_S
