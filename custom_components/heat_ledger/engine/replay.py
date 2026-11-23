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
    sources: dict[str, tuple[float, ...]]  # and the heat each source had delivered by then
    consumer_totals: dict[str, float]  # at the end of the replay
    circuit_totals: dict[str, float]
    source_totals: dict[str, float]
    unassigned: float  # of the sources' heat, still in the account at the end: no consumer had a weight since

    @property
    def source_heat(self) -> float:
        """Heat all sources delivered."""
        return sum(self.source_totals.values())


class Replay:
    """Feed it readings in the order they were recorded, then finish at the time the live counters took over."""

    def __init__(self, plant: PlantSpec) -> None:
        self.ledger = Ledger(plant)
        self._start: float | None = None
        self._next_close = 0.0
        self._hours: list[float] = []
        self._consumers: dict[str, list[float]] = {c.id: [] for c in plant.consumers}
        self._circuits: dict[str, list[float]] = {c.id: [] for c in plant.circuits}
        self._sources: dict[str, list[float]] = {s.id: [] for s in plant.sources}

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
            sources={k: tuple(v) for k, v in self._sources.items()},
            consumer_totals=dict(ledger.totals),
            circuit_totals=dict(ledger.circuit_totals),
            source_totals={source_id: ledger.source_heat[source_id] for source_id in self._sources},
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
                for source_id, totals in self._sources.items():
                    totals.append(ledger.source_heat[source_id])
            self._next_close = boundary + INTERVAL_S
