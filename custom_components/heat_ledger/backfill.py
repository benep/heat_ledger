"""Backfill: the heat of the days before the plant was set up, from the recorded states of its inputs.

A second ledger replays what the recorder still has of the inputs, up to the moment the entry was created: from
then on the live counters count. The hours before are written as long-term statistics of the counters. The
statistics the recorder has compiled since are then raised by what was backfilled, so the sums continue.

- Only statistics are written. The counters themselves keep their values, and so does the live ledger.
- The replay uses the plant as it is set up now, for the whole past: a room added later is calculated as if it had
  always been there.
- The replay starts when the state of every input the split depends on is known (see _Feeder). What the sources
  delivered before is left out: it cannot be split.
- Heat still in the account at the end of the replay (it arrived while no consumer had a weight) is not handed over
  to the live ledger.
- Once per entry. Importing twice would be harmless, raising the sums twice would not.

The recorder is imported where it is used: Heat Ledger runs without it.
"""

from __future__ import annotations

import logging
import math
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .const import (
    CONF_QUANTITY,
    DOMAIN,
    QUANTITY_INPUT,
    SUBENTRY_CIRCUIT,
    SUBENTRY_CONSUMER,
    SUBENTRY_SOURCE,
    TRANSLATION_HEAT,
    TRANSLATION_SOURCE_HEAT,
)
from .engine import PlantSpec, Reading, Replay, ReplayResult
from .model import InputKind, parse_state

if TYPE_CHECKING:
    from .runtime import HeatLedgerRuntime

_LOGGER = logging.getLogger(__name__)

# The recording is read in pieces of this length, so its size does not matter.
CHUNK = timedelta(days=1)
# Two pieces overlap by this much: the query leaves out a state recorded exactly at the start or the end of its period.
OVERLAP = timedelta(milliseconds=1)
# A counter the recorder compiles no statistics for (excluded from it) is given up after this many intervals in
# which it was tried in vain: two days of running, so a downtime does not count.
PENDING_TRIES = 576
# Hours of the backfill that are not in the statistics after this many intervals of looking are taken for lost:
# Home Assistant stopped before the recorder wrote them, or the recorder dropped them.
WRITTEN_TRIES = 6
# States recorded within this time after Home Assistant started are the ones it writes when it starts.
START_GRACE = timedelta(minutes=10)
UNIT = "kWh"

# Stored with the ledger (runtime.backfill):
KEY_START = "start"  # the replay started here, as a timestamp
KEY_END = "end"  # and ran up to here: the entry was created
KEY_HOURS = "hours"  # hours written
KEY_LAST_HOUR = "last_hour"  # start of the last hour written; None if there was none to write
KEY_HEAT = "heat"  # unique ID of the counter -> kWh backfilled
KEY_PENDING = "pending"  # unique ID -> kWh its live statistics are still to be raised by
KEY_SEEN = "seen"  # unique IDs whose hours were found in the statistics
KEY_CHECKS = "checks"  # intervals in which hours were looked for and not all found
KEY_VERIFIED = "verified"  # whether it is decided which counters' hours are there
KEY_TRIES = "tries"  # intervals in which a pending counter had no statistics to raise yet
KEY_RAISED = "raised"  # how many counters' live statistics were raised
KEY_LOST = "lost"  # how many counters' hours were not in the statistics
KEY_SKIPPED = "skipped"  # why the backfill of the first setup wrote nothing (a key of the exceptions), if so

# Reasons (translation keys of the exceptions)
DONE = "backfill_done"
RUNNING = "backfill_running"
NO_RECORDER = "backfill_no_recorder"
NO_COUNTERS = "backfill_no_counters"
NO_HISTORY = "backfill_no_history"
NO_HEAT = "backfill_no_heat"
NOT_RECORDED = "backfill_not_recorded"
NOTHING_SPLIT = "backfill_nothing_split"
HAS_STATISTICS = "backfill_has_statistics"
RELOADED = "backfill_reloaded"

ISSUE_SKIPPED = "backfill_skipped"

_starts_unreadable_logged = False


class BackfillError(HomeAssistantError):
    """Why there is nothing to backfill; the message comes from the translations."""

    def __init__(self, key: str, **placeholders: str) -> None:
        super().__init__(translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders or None)
        self.key = key


@dataclass(frozen=True)
class _Target:
    """A counter to backfill."""

    unique_id: str
    entity_id: str
    kind: str  # subentry type
    spec_id: str  # ID in the plant

    def hourly(self, result: ReplayResult) -> tuple[float, ...]:
        by_kind = {
            SUBENTRY_CONSUMER: result.consumers,
            SUBENTRY_CIRCUIT: result.circuits,
            SUBENTRY_SOURCE: result.sources,
        }
        return by_kind[self.kind][self.spec_id]

    def total(self, result: ReplayResult) -> float:
        by_kind = {
            SUBENTRY_CONSUMER: result.consumer_totals,
            SUBENTRY_CIRCUIT: result.circuit_totals,
            SUBENTRY_SOURCE: result.source_totals,
        }
        return by_kind[self.kind][self.spec_id]


def issue_id(entry: ConfigEntry) -> str:
    return f"{ISSUE_SKIPPED}_{entry.entry_id}"


async def async_backfill(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Backfill the counters of a loaded entry. Returns what was written, raises BackfillError if nothing was."""
    runtime: HeatLedgerRuntime = entry.runtime_data
    if runtime.backfill is not None and KEY_SKIPPED not in runtime.backfill:
        raise BackfillError(DONE)
    if runtime.backfilling:
        raise BackfillError(RUNNING)
    if "recorder" not in hass.config.components:
        raise BackfillError(NO_RECORDER)
    runtime.backfilling = True
    try:
        summary = await _async_backfill(hass, entry, runtime)
    finally:
        runtime.backfilling = False
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry))
    return summary


async def async_backfill_at_setup(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The backfill chosen in the first setup.

    If nothing could be written, that is noted, so it is not tried again with every start, and shown as a repair
    issue: whoever ticked the box should learn about it, and can start it again with the action once the reason is
    gone. Only "there was nothing to calculate" is no issue. An unexpected error is logged, and it is tried again
    with the next start.
    """
    runtime: HeatLedgerRuntime = entry.runtime_data
    try:
        try:
            summary = await async_backfill(hass, entry)
        except BackfillError as err:
            if err.key not in (DONE, RUNNING, RELOADED):
                await _async_skipped(hass, entry, runtime, err)
            return
    except Exception:
        _LOGGER.exception("%s: calculating the past days failed, it is tried again with the next start", entry.title)
        return
    _LOGGER.info(
        "%s: calculated %s hours from %s, %s kWh of the sources, %s kWh of it not assigned",
        entry.title,
        summary[KEY_HOURS],
        summary[KEY_START],
        summary["source_heat"],
        summary["unassigned"],
    )


async def _async_skipped(
    hass: HomeAssistant, entry: ConfigEntry, runtime: HeatLedgerRuntime, err: BackfillError
) -> None:
    """Note that the backfill of the first setup wrote nothing, and say why, unless there was nothing to calculate."""
    if err.key in (NO_HISTORY, NO_HEAT):
        _LOGGER.info("%s: no past days to calculate: %s", entry.title, err)
    else:
        # The issue before the note: interrupted in between, it is tried again with the next start instead of being
        # noted without anyone learning about it.
        _LOGGER.warning("%s: the past days were not calculated: %s", entry.title, err)
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id(entry),
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SKIPPED,
            translation_placeholders={"name": entry.title, "reason": await _async_reason(hass, err)},
        )
    runtime.backfill = {KEY_END: entry.created_at.timestamp(), KEY_SKIPPED: err.key, KEY_PENDING: {}}
    await runtime.async_save()


async def _async_backfill(hass: HomeAssistant, entry: ConfigEntry, runtime: HeatLedgerRuntime) -> dict[str, Any]:
    from homeassistant.components.recorder import get_instance, statistics
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )

    instance = get_instance(hass)
    end = entry.created_at
    begin = end - timedelta(days=instance.keep_days)
    if (oldest := instance.states_manager.oldest_ts) is not None:
        begin = max(begin, dt_util.utc_from_timestamp(oldest))
    targets = _targets(hass, entry)
    if not any(target.kind == SUBENTRY_CONSUMER for target in targets):
        raise BackfillError(NO_COUNTERS)
    if begin >= end:
        raise BackfillError(NO_HISTORY)  # the recorder keeps nothing from before the setup any more

    await instance.async_block_till_done()  # states written just now, as right after the setup
    first_live_hour = end.replace(minute=0, second=0, microsecond=0)
    existing = await instance.async_add_executor_job(
        statistics.statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        first_live_hour,
        {target.entity_id for target in targets},
        "hour",
        None,
        {"sum"},
    )
    if any(existing.values()):
        # A counter already has statistics from before it existed: imported some other way, or left by a counter
        # removed earlier that had the same entity ID. Continuing a sum behind them is not for a backfill to decide.
        raise BackfillError(HAS_STATISTICS)

    feeder = await _async_replay(hass, runtime, begin, end)
    if not feeder.recorded():
        raise BackfillError(NO_HISTORY)
    result = await hass.async_add_executor_job(feeder.replay.finish, end.timestamp())
    if not _is_loaded(entry, runtime):
        raise BackfillError(RELOADED)  # nothing is written yet: it can simply be started again
    if unknown := feeder.unknown() + feeder.silent():
        raise BackfillError(NOT_RECORDED, entity=unknown[0])
    if result.start is None:
        raise BackfillError(NO_HISTORY)
    if result.source_heat <= 0:
        raise BackfillError(NO_HEAT)  # outside the heating season, for instance
    if not any(total > 0 for total in result.consumer_totals.values()):
        raise BackfillError(NOTHING_SPLIT, heat=f"{result.source_heat:.1f}")

    # Leading hours in which nothing was booked yet are left out. If everything was booked in the hour the entry was
    # created in, there is no hour to write: that hour's statistics are the live ones, raised later.
    hourly = {target.unique_id: target.hourly(result) for target in targets}
    count = len(result.hours)
    first = next((i for i in range(count) if any(totals[i] > 0 for totals in hourly.values())), count)
    hours = [dt_util.utc_from_timestamp(hour) for hour in result.hours[first:]]
    heat = {target.unique_id: target.total(result) for target in targets}
    # Noted before the hours are queued for the recorder: whatever stops Home Assistant from here on, the note says
    # what to look for. Hours that never arrive are found missing, and nothing is raised for them.
    runtime.backfill = {
        KEY_START: result.start,
        KEY_END: end.timestamp(),
        KEY_HOURS: len(hours),
        KEY_LAST_HOUR: hours[-1].timestamp() if hours else None,
        KEY_HEAT: heat,
        KEY_PENDING: {unique_id: kwh for unique_id, kwh in heat.items() if kwh > 0},
        KEY_SEEN: [],
        KEY_CHECKS: 0,
        KEY_VERIFIED: not hours,
        KEY_TRIES: 0,
        KEY_RAISED: 0,
        KEY_LOST: 0,
    }
    await runtime.async_save()
    if hours:
        for target in targets:
            metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=None,
                source="recorder",
                statistic_id=target.entity_id,
                unit_class="energy",
                unit_of_measurement=UNIT,
            )
            totals = hourly[target.unique_id][first:]
            rows = [
                StatisticData(start=hour, sum=total, state=total) for hour, total in zip(hours, totals, strict=True)
            ]
            statistics.async_import_statistics(hass, metadata, rows)
    return {
        KEY_START: dt_util.utc_from_timestamp(result.start).isoformat(),
        KEY_END: end.isoformat(),
        KEY_HOURS: len(hours),
        "source_heat": round(result.source_heat, 3),
        "unassigned": round(result.unassigned, 3),
        KEY_HEAT: {target.entity_id: round(heat[target.unique_id], 3) for target in targets},
    }


async def async_adjust_pending(hass: HomeAssistant, runtime: HeatLedgerRuntime) -> None:
    """Raise the statistics compiled since the backfill's end by what it booked. Called after every interval.

    The recorder continues a sum from its last 5-minute row only, never from imported hours. So a counter's
    statistics are raised once the recorder has compiled the first of them, and once its hours are in the statistics:
    raised without them, all the days would show as heat of the hour of the setup. Until then the counter stays
    pending, also across restarts.
    """
    backfill = runtime.backfill
    if (
        not backfill
        or not backfill.get(KEY_PENDING)
        or runtime.adjusting
        or runtime.backfilling
        or "recorder" not in hass.config.components
    ):
        return
    from homeassistant.components.recorder import get_instance, statistics

    runtime.adjusting = True
    try:
        if not backfill.get(KEY_VERIFIED) and not await _async_check_hours(hass, runtime, backfill):
            return
        instance = get_instance(hass)
        registry = er.async_get(hass)
        entry = runtime.entry
        pending: dict[str, float] = backfill[KEY_PENDING]
        seen = set(backfill.get(KEY_SEEN, ()))
        start = dt_util.utc_from_timestamp(backfill[KEY_END]).replace(minute=0, second=0, microsecond=0)
        given_up = backfill.get(KEY_TRIES, 0) >= PENDING_TRIES
        tried_in_vain = False
        for unique_id, kwh in list(pending.items()):
            if not backfill.get(KEY_VERIFIED) and unique_id not in seen:
                continue  # its hours are not there yet
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
            raise_it = False
            if entity_id is not None:  # else: removed since, nothing to raise
                live = await instance.async_add_executor_job(
                    statistics.get_last_short_term_statistics, hass, 1, entity_id, False, {"sum"}
                )
                raise_it = bool(live.get(entity_id))
                if not raise_it and not given_up:
                    tried_in_vain = True
                    continue
                if not raise_it:
                    _LOGGER.warning(
                        "%s: the recorder compiles no statistics for %s, so they are not raised by the %.1f kWh "
                        "calculated for the past days",
                        entry.title,
                        entity_id,
                        kwh,
                    )
            if not _is_loaded(entry, runtime):
                return  # reloaded meanwhile: the reloaded entry goes on, from what was saved with the unload
            if raise_it:
                instance.async_adjust_statistics(entity_id, start, kwh, UNIT)
                backfill[KEY_RAISED] = backfill.get(KEY_RAISED, 0) + 1
            del pending[unique_id]
            # Saved at once: raised twice after a crash would be worse than this write.
            await runtime.async_save()
        if tried_in_vain:
            backfill[KEY_TRIES] = backfill.get(KEY_TRIES, 0) + 1  # saved with the ledger after the interval
    finally:
        runtime.adjusting = False


async def _async_check_hours(hass: HomeAssistant, runtime: HeatLedgerRuntime, backfill: dict[str, Any]) -> bool:
    """Look for the hours of the backfill in the statistics, until all are there or WRITTEN_TRIES intervals passed.

    They are queued for the recorder after the note, and the recorder may still be busy with them, or never get to
    them: Home Assistant stopped first, or the recorder dropped them after an error. A counter whose hours are not
    there by then is not raised. If none of the counters' hours are there, the note is dropped: nothing of the
    backfill is in the statistics, and it may run again. Returns whether raising goes on.
    """
    from homeassistant.components.recorder import get_instance, statistics

    instance = get_instance(hass)
    entry = runtime.entry
    registry = er.async_get(hass)
    seen: set[str] = set(backfill.get(KEY_SEEN, ()))
    entity_ids = {
        unique_id: entity_id
        for unique_id in backfill[KEY_HEAT]
        if (entity_id := registry.async_get_entity_id("sensor", DOMAIN, unique_id)) is not None
    }
    missing = {unique_id: entity_id for unique_id, entity_id in entity_ids.items() if unique_id not in seen}
    if missing:
        at = dt_util.utc_from_timestamp(backfill[KEY_LAST_HOUR])
        written = await instance.async_add_executor_job(
            statistics.statistics_during_period,
            hass,
            at,
            at + timedelta(hours=1),
            set(missing.values()),
            "hour",
            None,
            {"sum"},
        )
        seen |= {unique_id for unique_id, entity_id in missing.items() if written.get(entity_id)}
        missing = {unique_id: entity_id for unique_id, entity_id in missing.items() if unique_id not in seen}
    if not _is_loaded(entry, runtime):
        return False
    backfill[KEY_SEEN] = sorted(seen)
    if missing:
        backfill[KEY_CHECKS] = backfill.get(KEY_CHECKS, 0) + 1
        if backfill[KEY_CHECKS] < WRITTEN_TRIES:
            await runtime.async_save()
            return True  # the counters whose hours are there go on
        if not seen:
            _LOGGER.warning("%s: the past days were not written, they can be calculated again", entry.title)
            runtime.backfill = None
            await runtime.async_save()
            return False
        for unique_id, entity_id in missing.items():
            _LOGGER.warning("%s: the past days of %s were not written", entry.title, entity_id)
            backfill[KEY_PENDING].pop(unique_id, None)
        backfill[KEY_LOST] = len(missing)
    backfill[KEY_VERIFIED] = True
    await runtime.async_save()
    return True


async def _async_reason(hass: HomeAssistant, err: BackfillError) -> str:
    """The message of an error in the language of Home Assistant (str() of it is always English)."""
    translations = await async_get_translations(hass, hass.config.language, "exceptions", {DOMAIN})
    text = translations.get(f"component.{DOMAIN}.exceptions.{err.key}.message")
    try:
        return text.format(**(err.translation_placeholders or {})) if text else str(err)
    except (KeyError, IndexError, ValueError):
        return str(err)


def _is_loaded(entry: ConfigEntry, runtime: HeatLedgerRuntime) -> bool:
    """Whether this runtime is still the one of the entry (an entry reloads when its setup changes).

    Also while the entry is being set up: the backfill chosen in the first setup starts there, and can get this far
    before the setup has returned, if the recorder answers at once.
    """
    states = (ConfigEntryState.LOADED, ConfigEntryState.SETUP_IN_PROGRESS)
    return entry.state in states and getattr(entry, "runtime_data", None) is runtime


def _targets(hass: HomeAssistant, entry: ConfigEntry) -> list[_Target]:
    """The counters to backfill: room heating, hot water, circuits, and the heat of sources that count energy input
    (the counter the Energy dashboard gets as source then). Not the disabled ones: they have no statistics."""
    registry = er.async_get(hass)
    targets = []
    for subentry in entry.subentries.values():
        kind = subentry.subentry_type
        if kind in (SUBENTRY_CONSUMER, SUBENTRY_CIRCUIT):
            key = TRANSLATION_HEAT
        elif kind == SUBENTRY_SOURCE and subentry.data.get(CONF_QUANTITY) == QUANTITY_INPUT:
            key = TRANSLATION_SOURCE_HEAT
        else:
            continue
        unique_id = f"{subentry.subentry_id}_{key}"
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id is not None and not registry.async_get(entity_id).disabled:
            targets.append(_Target(unique_id, entity_id, kind, subentry.subentry_id))
    return targets


class _Feeder:
    """Feeds recorded states to a replay, in the order recorded, from the moment the state of every input the split
    depends on (pumps and demands, the weights) is known.

    The recorder purges the last state from before the days it keeps as well, so an input that has not changed for
    long has no state at the beginning. Such an input is known from its first valid recorded state, as Home
    Assistant's history shows it, and the replay starts when the last one is known. What the sources delivered before
    is left out instead of being split with a guess, or piling up in the account to be split at once.

    Known from the beginning instead:
    - an input with a recorded state at the beginning, or one Home Assistant has not updated since before it
      (`initial`, its state now);
    - an input whose first recorded state is the one Home Assistant wrote when it started (`starts`, the starts of the
      recorder), at the first start since the beginning: it had not changed while Home Assistant was running before.
      Its state is the one it settles to within START_GRACE, not a value of the moment it came up. This is the case of
      a circuit pump that has run for weeks, and of a setup right after a restart. An input with no state at an earlier
      start did not exist or was not recorded then.

    Meters (sources, heat meters) are not waited for: a reading is only the baseline of the next one, and one missing
    at first only means its increases count later. One with no recorded state at all is reported (`silent`).
    """

    def __init__(
        self,
        plant: PlantSpec,
        kinds: Mapping[str, InputKind],
        units: Mapping[str, str | None],
        begin: float,
        initial: Mapping[str, Reading],
        starts: Sequence[float],
    ) -> None:
        self.replay = Replay(plant)
        self.started = False
        self._kinds = kinds
        self._units = units
        self._begin = begin
        self._starts = sorted(starts)
        self._fed_until = begin
        self._exact = set(initial)
        self._initial: dict[str, Reading] = dict(initial)  # known from the beginning
        for key, kind in kinds.items():
            if kind is InputKind.ENERGY:
                self._initial.setdefault(key, None)
        self._known_from: dict[str, float] = {}  # known from its first valid state
        self._settling: dict[str, tuple[float, Reading]] = {}  # written at a start: the start, the value so far
        self._recorded: set[str] = set()
        self._buffer: list[tuple[float, str, Reading]] = []

    def unknown(self) -> list[str]:
        """Inputs of the split whose state is not known (or not settled) yet."""
        return sorted(key for key in self._kinds if key not in self._initial and key not in self._known_from)

    def silent(self) -> list[str]:
        """Meters without any recorded state, unless their state now is older than the recording."""
        return sorted(
            key
            for key, kind in self._kinds.items()
            if kind is InputKind.ENERGY and key not in self._recorded and key not in self._exact
        )

    def feed(self, states: Mapping[str, list[State]], first: bool) -> None:
        """Feed the states of one piece of the recording.

        States from before the time fed up to are the states the inputs had at the very beginning, which count from
        then on, or, in a later piece, the overlap with the piece before, which was fed already.
        """
        for t, key, state in _events(states):
            if t < self._fed_until and not first:
                continue
            self._fed_until = max(t, self._fed_until)
            self._recorded.add(key)
            reading = self._read(key, state)
            if self.started:
                self.replay.observe(self._fed_until, {key: reading})
                continue
            self._settle(self._fed_until)
            self._learn(key, t, reading)
            self._buffer.append((self._fed_until, key, reading))
            self.start()

    def learn_later(self, states: Mapping[str, list[State]]) -> None:
        """States recorded after the end, of inputs that have none in the recording: they did not change in it.

        A meter only needs to be recorded at all. An input of the split takes the state Home Assistant wrote when it
        started, at the first start since the beginning; otherwise its state in the recording stays unknown.
        """
        self._settle(math.inf)
        in_recording = set(self._recorded)
        for t, key, state in _events(states):
            self._recorded.add(key)
            if key not in in_recording and key in self.unknown():
                self._learn(key, t, self._read(key, state), later=True)
        self._settle(math.inf)
        self.start()

    def recorded(self) -> bool:
        """Whether any input has a recorded state (fed so far)."""
        return bool(self._recorded)

    def settle(self) -> None:
        """The end of the recording: what was written at a start has settled."""
        self._settle(math.inf)
        self.start()

    def start(self) -> None:
        """Start the replay where every input is known, with the states they have then, and replay what follows."""
        if self.started or self.unknown() or self._settling:
            return
        start = max([self._begin, *self._known_from.values()])
        latest = dict(self._initial)
        following: list[tuple[float, str, Reading]] = []
        for t, key, reading in self._buffer:
            if t <= start:
                latest[key] = reading
            else:
                following.append((t, key, reading))
        self.replay.observe(start, latest)
        for t, key, reading in following:
            self.replay.observe(t, {key: reading})
        self._buffer.clear()
        self.started = True

    def _read(self, key: str, state: State) -> Reading:
        return parse_state(self._kinds[key], state.state, state.attributes, self._units.get(key))

    def _learn(self, key: str, t: float, reading: Reading, *, later: bool = False) -> None:
        """A state of an input of the split not known yet."""
        if key in self._initial or key in self._known_from:
            return
        if key in self._settling:
            start, _ = self._settling[key]
            if reading is not None and t <= start + START_GRACE.total_seconds():
                self._settling[key] = (start, reading)
            return
        if reading is None:
            return  # unknown until its first valid state
        if t < self._begin:
            self._initial[key] = reading  # its state at the beginning
            return
        start = self._first_start_since_begin(t)
        if start is not None:
            self._settling[key] = (start, reading)
        elif not later:
            self._known_from[key] = t

    def _settle(self, now: float) -> None:
        """Values written at a start that have had START_GRACE to settle count from the beginning."""
        for key, (start, reading) in list(self._settling.items()):
            if now > start + START_GRACE.total_seconds():
                del self._settling[key]
                self._initial[key] = reading

    def _first_start_since_begin(self, t: float) -> float | None:
        """The start of Home Assistant a state at t was written at, if it is the first start since the beginning."""
        i = bisect_right(self._starts, t) - 1
        if i < 0 or t - self._starts[i] > START_GRACE.total_seconds():
            return None
        if i > 0 and self._starts[i - 1] > self._begin:
            return None  # an earlier start since the beginning wrote no state of it: it was not there then
        return self._starts[i]


def _events(states: Mapping[str, list[State]]) -> list[tuple[float, str, State]]:
    return sorted(
        ((state.last_updated.timestamp(), key, state) for key, items in states.items() for state in items),
        key=lambda event: event[0],
    )


async def _async_replay(hass: HomeAssistant, runtime: HeatLedgerRuntime, begin: datetime, end: datetime) -> _Feeder:
    """Replay the recorded states of the plant's inputs between begin and end."""
    from homeassistant.components.recorder import get_instance, history

    instance = get_instance(hass)
    kinds, units = dict(runtime.kinds), dict(runtime.units)
    # Not updated since before the beginning: its state now is the one it had all along.
    initial = {
        key: parse_state(kind, state.state, state.attributes, units.get(key))
        for key, kind in kinds.items()
        if (state := hass.states.get(key)) is not None and state.last_updated < begin
    }
    starts = await instance.async_add_executor_job(_recorder_starts, hass, begin - START_GRACE)
    feeder = _Feeder(runtime.plant, kinds, units, begin.timestamp(), initial, starts)
    entity_ids = list(kinds)

    def states_between(start: datetime, stop: datetime | None, keys: list[str], first: bool) -> dict[str, list[State]]:
        # Every change, also of an attribute (hvac_action is one); with the states at the very beginning.
        return history.get_significant_states(hass, start, stop, keys, None, first, False, False, False)

    start = begin
    while start < end and entity_ids:
        stop = min(start + CHUNK, end)
        first = start == begin
        states = await instance.async_add_executor_job(states_between, start - OVERLAP, stop, entity_ids, first)
        await hass.async_add_executor_job(feeder.feed, states, first)
        start = stop
    await hass.async_add_executor_job(feeder.settle)
    if feeder.recorded() and (quiet := sorted(set(feeder.unknown() + feeder.silent()))):
        later = await instance.async_add_executor_job(states_between, end, None, quiet, False)
        await hass.async_add_executor_job(feeder.learn_later, later)
    return feeder


def _recorder_starts(hass: HomeAssistant, since: datetime) -> list[float]:
    """When the recorder started since then, that is, Home Assistant. Read from the recorder's own table, which no
    public function returns; if that fails, no state counts as written at a start."""
    global _starts_unreadable_logged
    try:
        from homeassistant.components.recorder.db_schema import RecorderRuns
        from homeassistant.components.recorder.models import process_timestamp
        from homeassistant.components.recorder.util import session_scope

        with session_scope(hass=hass, read_only=True) as session:
            rows = session.query(RecorderRuns.start).filter(RecorderRuns.start >= since).all()
        return sorted(process_timestamp(row.start).timestamp() for row in rows)
    except Exception:
        if not _starts_unreadable_logged:
            _starts_unreadable_logged = True
            _LOGGER.warning(
                "The starts of Home Assistant could not be read from the recorder: an input that has not changed for "
                "long counts only from its first recorded state when the past days are calculated",
                exc_info=True,
            )
        return []
