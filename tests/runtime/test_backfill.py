"""The backfill: the hours before the plant was set up, from the recorded states, and the live statistics raised."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
    statistics_during_period,
)

from custom_components.heat_ledger import backfill
from custom_components.heat_ledger.backfill import (
    KEY_HEAT,
    KEY_PENDING,
    KEY_SKIPPED,
    BackfillError,
    async_adjust_pending,
    async_backfill,
    async_backfill_at_setup,
)
from custom_components.heat_ledger.const import CONF_BACKFILL, CONF_METER_ENTITY, DOMAIN
from custom_components.heat_ledger.diagnostics import async_get_config_entry_diagnostics
from custom_components.heat_ledger.runtime import HeatLedgerRuntime

from .common import BOILER, HEAT_PUMP, HOT_WATER_PUMP, LIVING_DEMAND, PUMP, SUBENTRIES, entity_id, next_interval, setup

DAY = datetime(2026, 1, 5, tzinfo=UTC)
SETUP = DAY.replace(hour=10, second=30)


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute, second=second)


@pytest.fixture
def mock_recorder_before_hass(async_test_recorder: Any) -> None:
    """The recorder's database has to be prepared before Home Assistant is; tests asking for recorder_mock get it."""


@pytest.fixture(autouse=True)
def start_time(freezer: FrozenDateTimeFactory) -> None:
    """Home Assistant starts hours before the plant is set up, so there is a past to record."""
    freezer.move_to(at(6))


async def record(hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: datetime, states: dict[str, Any]) -> None:
    freezer.move_to(when)
    for key, value in states.items():
        attributes = {"unit_of_measurement": "kWh"} if key in (BOILER, HEAT_PUMP) else {}
        hass.states.async_set(key, str(value), attributes)
    await hass.async_block_till_done()


async def record_past(hass: HomeAssistant, freezer: FrozenDateTimeFactory, *, pump: str | None = "on") -> None:
    """Four hours of heating before the setup at 10:00:30.

    Living room 2000 W at 50 %, bath 10 m² (600 W) always on, hot water pump off: living 5/8, bath 3/8 of everything.
    Hour 6: 2 kWh of the boiler. Hour 7: 1 kWh of electricity for the heat pump, 3 kWh of heat. Hour 8: nothing.
    Hour 9: 1.6 kWh. Hour 10, before the setup: 0.8 kWh.
    """
    first = {BOILER: 100.0, HEAT_PUMP: 50.0, LIVING_DEMAND: 50, HOT_WATER_PUMP: "off"}
    await record(hass, freezer, at(6, 0, 10), first | ({PUMP: pump} if pump else {}))
    await record(hass, freezer, at(6, 20), {BOILER: 102.0})
    await record(hass, freezer, at(7, 30), {HEAT_PUMP: 51.0})
    await record(hass, freezer, at(9, 40), {BOILER: 103.6})
    await record(hass, freezer, at(10, 0, 20), {BOILER: 104.4})
    freezer.move_to(SETUP)
    await async_wait_recording_done(hass)


async def background(hass: HomeAssistant) -> None:
    """Wait for what the entries run in the background: the backfill of the setup, raising pending statistics.

    Home Assistant's own wait does not cover the background tasks of a config entry.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        if tasks := list(entry._background_tasks):
            await asyncio.gather(*tasks)
    await hass.async_block_till_done()


async def tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Close the next interval and let what it starts in the background finish (raising pending statistics)."""
    await next_interval(hass, freezer)
    await background(hass)


def new_entry(hass: HomeAssistant, **data: Any) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Heating", data=data, options={}, subentries_data=SUBENTRIES)
    entry.add_to_hass(hass)
    return entry


async def sums(hass: HomeAssistant, unique_id: str, period: str = "hour") -> dict[datetime, float]:
    statistic_id = entity_id(hass, unique_id)
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, at(0), None, {statistic_id}, period, None, {"sum"}
    )
    return {datetime.fromtimestamp(row["start"], UTC): row["sum"] for row in rows.get(statistic_id, [])}


async def test_hours_before_the_setup_are_written(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await record_past(hass, freezer)
    entry = new_entry(hass)
    await setup(hass, entry)

    summary = await async_backfill(hass, entry)
    await async_wait_recording_done(hass)

    assert summary["start"] == at(6, 0, 10).isoformat()
    assert summary["end"] == SETUP.isoformat()
    assert summary["hours"] == 4
    assert summary["source_heat"] == pytest.approx(7.4)
    assert summary["unassigned"] == pytest.approx(0.0)
    assert summary[KEY_HEAT] == {
        entity_id(hass, "ground_floor_heat"): pytest.approx(7.4),
        entity_id(hass, "living_heat"): pytest.approx(4.625),
        entity_id(hass, "bath_heat"): pytest.approx(2.775),
        entity_id(hass, "hot_water_heat"): 0.0,
        entity_id(hass, "heat_pump_source_heat"): pytest.approx(3.0),
    }
    # The hour of the setup is not written: its statistics are the live ones.
    assert await sums(hass, "living_heat") == {
        at(6): pytest.approx(1.25),
        at(7): pytest.approx(3.125),
        at(8): pytest.approx(3.125),
        at(9): pytest.approx(4.125),
    }
    assert await sums(hass, "ground_floor_heat") == {
        at(6): pytest.approx(2.0),
        at(7): pytest.approx(5.0),
        at(8): pytest.approx(5.0),
        at(9): pytest.approx(6.6),
    }
    assert list((await sums(hass, "hot_water_heat")).values()) == [0.0, 0.0, 0.0, 0.0]
    # Neither the counters nor the live ledger are touched.
    assert float(hass.states.get(entity_id(hass, "living_heat")).state) == 0.0
    assert entry.runtime_data.ledger.account_total == 0.0
    # What nothing was booked for has nothing to raise.
    assert entry.runtime_data.backfill[KEY_PENDING] == {
        "ground_floor_heat": pytest.approx(7.4),
        "living_heat": pytest.approx(4.625),
        "bath_heat": pytest.approx(2.775),
        "heat_pump_source_heat": pytest.approx(3.0),
    }


async def test_live_statistics_are_raised_once_they_exist(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await record_past(hass, freezer)
    entry = new_entry(hass)
    await setup(hass, entry)
    runtime = entry.runtime_data
    await async_backfill(hass, entry)

    # Live: 1.6 kWh more before the first interval closes at 10:05, 0.8 kWh before the second at 10:10.
    await record(hass, freezer, at(10, 2), {BOILER: 106.0})
    await tick(hass, freezer)
    await async_wait_recording_done(hass)
    assert runtime.backfill[KEY_PENDING]  # nothing compiled yet: nothing to raise
    await record(hass, freezer, at(10, 7), {BOILER: 106.8})
    await tick(hass, freezer)
    await async_wait_recording_done(hass)
    for start in (at(10), at(10, 5), at(10, 10)):
        do_adhoc_statistics(hass, start=start)
    await async_wait_recording_done(hass)
    live = await sums(hass, "living_heat", "5minute")
    assert live == {at(10): 0.0, at(10, 5): pytest.approx(1.0), at(10, 10): pytest.approx(1.5)}

    await async_adjust_pending(hass, runtime)
    await async_wait_recording_done(hass)

    assert runtime.backfill[KEY_PENDING] == {}
    assert await sums(hass, "living_heat", "5minute") == {
        at(10): pytest.approx(4.625),
        at(10, 5): pytest.approx(5.625),
        at(10, 10): pytest.approx(6.125),
    }
    assert (await sums(hass, "ground_floor_heat", "5minute"))[at(10, 10)] == pytest.approx(7.4 + 2.4)
    # The hours written before are not raised, so the sum continues from them.
    assert (await sums(hass, "living_heat"))[at(9)] == pytest.approx(4.125)

    # Raising again changes nothing, and what the recorder compiles next continues from the raised sum.
    await async_adjust_pending(hass, runtime)
    await record(hass, freezer, at(10, 17), {BOILER: 108.4})
    await tick(hass, freezer)
    await async_wait_recording_done(hass)
    for start in (at(10, 15), at(10, 20)):
        do_adhoc_statistics(hass, start=start)
    await async_wait_recording_done(hass)
    assert (await sums(hass, "living_heat", "5minute"))[at(10, 20)] == pytest.approx(7.125)


async def test_pending_is_raised_after_an_interval_and_survives_a_reload(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await record_past(hass, freezer)
    entry = new_entry(hass)
    await setup(hass, entry)
    await async_backfill(hass, entry)
    await record(hass, freezer, at(10, 2), {BOILER: 106.0})
    await tick(hass, freezer)
    await async_wait_recording_done(hass)
    do_adhoc_statistics(hass, start=at(10, 5))
    await async_wait_recording_done(hass)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    assert runtime.backfill[KEY_PENDING]["living_heat"] == pytest.approx(4.625)

    await tick(hass, freezer)  # raises in the background
    await async_wait_recording_done(hass)

    assert runtime.backfill[KEY_PENDING] == {}
    assert (await sums(hass, "living_heat", "5minute"))[at(10, 5)] == pytest.approx(5.625)


async def test_only_once(recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await record_past(hass, freezer)
    entry = new_entry(hass)
    await setup(hass, entry)
    await async_backfill(hass, entry)

    with pytest.raises(BackfillError) as error:
        await async_backfill(hass, entry)
    assert error.value.key == "backfill_done"

    # Also after a reload: it is stored with the ledger.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(BackfillError) as error:
        await async_backfill(hass, entry)
    assert error.value.key == "backfill_done"


async def test_nothing_recorded_before_the_setup(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(SETUP)
    entry = new_entry(hass)
    await setup(hass, entry)

    with pytest.raises(BackfillError) as error:
        await async_backfill(hass, entry)

    assert error.value.key == "backfill_no_history"
    assert entry.runtime_data.backfill is None  # nothing written: it may be tried again


async def test_an_input_missing_in_the_recording(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await record_past(hass, freezer, pump=None)  # the circuit pump was not recorded
    entry = new_entry(hass)
    await setup(hass, entry)

    with pytest.raises(BackfillError) as error:
        await async_backfill(hass, entry)

    assert error.value.key == "backfill_not_recorded"
    assert error.value.translation_placeholders == {"entity": PUMP}
    assert await sums(hass, "living_heat") == {}
    assert entry.runtime_data.backfill is None


async def import_hour(hass: HomeAssistant, statistic_id: str, start: datetime, total: float) -> None:
    """A statistic of a counter from some other origin: an earlier import, or a removed counter of the same name."""
    metadata = {
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "name": None,
        "source": "recorder",
        "statistic_id": statistic_id,
        "unit_class": "energy",
        "unit_of_measurement": "kWh",
    }
    async_import_statistics(hass, metadata, [{"start": start, "sum": total, "state": total}])
    await async_wait_recording_done(hass)


@pytest.mark.parametrize(
    "hour", [at(8), datetime(2025, 11, 1, tzinfo=UTC)], ids=["in the days recorded", "long before"]
)
async def test_counters_with_older_statistics_are_left_alone(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory, hour: datetime
) -> None:
    await record_past(hass, freezer)
    entry = new_entry(hass)
    await setup(hass, entry)
    await import_hour(hass, entity_id(hass, "bath_heat"), hour, 500.0)

    with pytest.raises(BackfillError) as error:
        await async_backfill(hass, entry)

    assert error.value.key == "backfill_has_statistics"
    assert await sums(hass, "living_heat") == {}
    assert await sums(hass, "bath_heat") == ({at(8): 500.0} if hour == at(8) else {})


async def test_without_a_recorder(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(SETUP)
    entry = new_entry(hass)
    await setup(hass, entry)

    with pytest.raises(BackfillError) as error:
        await async_backfill(hass, entry)

    assert error.value.key == "backfill_no_recorder"


async def test_chosen_in_the_first_setup(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await record_past(hass, freezer)
    entry = new_entry(hass, **{CONF_BACKFILL: True})

    await setup(hass, entry)
    await background(hass)
    await async_wait_recording_done(hass)

    assert entry.runtime_data.backfill[KEY_HEAT]["living_heat"] == pytest.approx(4.625)
    assert (await sums(hass, "living_heat"))[at(9)] == pytest.approx(4.125)


async def test_chosen_in_the_first_setup_without_a_past(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(SETUP)
    entry = new_entry(hass, **{CONF_BACKFILL: True})

    await setup(hass, entry)
    await background(hass)

    # Noted, so it is not tried again with every start. The action may still be tried, and says why not.
    assert entry.runtime_data.backfill == {
        "end": SETUP.timestamp(),
        KEY_SKIPPED: "backfill_no_history",
        KEY_PENDING: {},
    }
    with pytest.raises(BackfillError) as error:
        await async_backfill(hass, entry)
    assert error.value.key == "backfill_no_history"


async def test_not_chosen_nothing_happens(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await record_past(hass, freezer)
    entry = new_entry(hass)

    await setup(hass, entry)
    await background(hass)
    await async_wait_recording_done(hass)

    assert entry.runtime_data.backfill is None
    assert await sums(hass, "living_heat") == {}


async def test_an_unexpected_error_is_tried_again_with_the_next_start(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    await record_past(hass, freezer)
    entry = new_entry(hass)
    await setup(hass, entry)

    async def broken(*_: Any) -> None:
        raise RuntimeError("database gone")

    monkeypatch.setattr("custom_components.heat_ledger.backfill._async_replay", broken)
    await async_backfill_at_setup(hass, entry)

    assert entry.runtime_data.backfill is None
    assert entry.runtime_data.backfilling is False


async def test_action(recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await record_past(hass, freezer)
    entry = new_entry(hass)
    await setup(hass, entry)

    response = await hass.services.async_call(
        DOMAIN, "backfill", {"config_entry_id": entry.entry_id}, blocking=True, return_response=True
    )

    assert response["hours"] == 4
    assert response[KEY_HEAT][entity_id(hass, "living_heat")] == pytest.approx(4.625)

    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call(DOMAIN, "backfill", {"config_entry_id": "missing"}, blocking=True)
    assert error.value.translation_key == "entry_not_found"

    assert await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call(DOMAIN, "backfill", {"config_entry_id": entry.entry_id}, blocking=True)
    assert error.value.translation_key == "entry_not_loaded"


async def test_long_recordings_are_read_in_pieces(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Three days, read day by day: nothing is lost or counted twice where two pieces meet."""
    first = {BOILER: 100.0, HEAT_PUMP: 50.0, LIVING_DEMAND: 50, HOT_WATER_PUMP: "off", PUMP: "on"}
    await record(hass, freezer, at(6, 0, 10), first)
    value = 100.0
    for hours in range(1, 73):
        value += 1.0
        await record(hass, freezer, at(6) + timedelta(hours=hours, minutes=10), {BOILER: value})
    setup_time = at(6) + timedelta(hours=73)
    # A reading exactly where two pieces meet (the pieces end 24 and 48 hours before the setup).
    freezer.move_to(setup_time)
    await async_wait_recording_done(hass)
    entry = new_entry(hass)
    await setup(hass, entry)

    summary = await async_backfill(hass, entry)

    assert summary["source_heat"] == pytest.approx(72.0)
    assert summary["hours"] == 72  # hour 6 of the first day has nothing booked yet and is left out
    assert sum(summary[KEY_HEAT][entity_id(hass, key)] for key in ("living_heat", "bath_heat")) == pytest.approx(72.0)


async def test_while_the_entry_is_still_being_set_up(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The backfill of the first setup can be through before the setup has returned, if the recorder answers at once."""
    await record_past(hass, freezer)
    entry = new_entry(hass)
    await setup(hass, entry)
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)

    summary = await async_backfill(hass, entry)

    entry.mock_state(hass, ConfigEntryState.LOADED)  # so it is unloaded when the test ends
    assert summary["hours"] == 4
    assert entry.runtime_data.backfill[KEY_PENDING]


@pytest.mark.parametrize("change", ["unloading", "reloaded"])
async def test_changed_while_it_was_calculated(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """Unloaded or reloaded during the replay: nothing is written, so it can be started again."""
    await record_past(hass, freezer)
    entry = new_entry(hass)
    await setup(hass, entry)
    runtime = entry.runtime_data
    replay = backfill._async_replay

    async def replay_then_change(*args: Any) -> Any:
        result = await replay(*args)
        if change == "unloading":
            entry.mock_state(hass, ConfigEntryState.UNLOAD_IN_PROGRESS)
        else:
            entry.runtime_data = HeatLedgerRuntime(hass, entry, runtime.plant)
        return result

    monkeypatch.setattr(backfill, "_async_replay", replay_then_change)
    with pytest.raises(BackfillError) as error:
        await async_backfill(hass, entry)
    await async_wait_recording_done(hass)

    assert error.value.key == "backfill_reloaded"
    assert runtime.backfill is None
    assert await sums(hass, "living_heat") == {}
    entry.runtime_data = runtime
    entry.mock_state(hass, ConfigEntryState.LOADED)
    monkeypatch.undo()
    assert (await async_backfill(hass, entry))["hours"] == 4


# --- From the review of the backfill -------------------------------------------------------------------------------


async def record_pump_late(hass: HomeAssistant, freezer: FrozenDateTimeFactory, before: list[str]) -> None:
    """The past of record_past, but the circuit pump has its first state only at 9:50 or 9:51."""
    first = {BOILER: 100.0, HEAT_PUMP: 50.0, LIVING_DEMAND: 50, HOT_WATER_PUMP: "off"}
    await record(hass, freezer, at(6, 0, 10), first)
    await record(hass, freezer, at(6, 20), {BOILER: 102.0})
    await record(hass, freezer, at(7, 30), {HEAT_PUMP: 51.0})
    await record(hass, freezer, at(9, 40), {BOILER: 103.6})
    for state in before:
        await record(hass, freezer, at(9, 50), {PUMP: state})
    await record(hass, freezer, at(9, 51), {PUMP: "on"})
    await record(hass, freezer, at(10, 0, 20), {BOILER: 104.4})
    freezer.move_to(SETUP)
    await async_wait_recording_done(hass)


def started_at(monkeypatch: pytest.MonkeyPatch, *starts: datetime) -> None:
    """When Home Assistant started, as the recorder's table of runs would have it."""
    monkeypatch.setattr(backfill, "_recorder_starts", lambda *_: [start.timestamp() for start in starts])


@pytest.mark.parametrize("before", [[], ["unavailable", "unknown"]], ids=["state", "unavailable at first"])
async def test_an_input_written_again_at_a_start_counts_from_the_beginning(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    before: list[str],
) -> None:
    """The pump has run since before the recording: the recorder purged its state, Home Assistant wrote it again when
    it started shortly before the setup. It had not changed before, so its state counts from the beginning, and the
    days before are split as they were, instead of the replay starting minutes before the setup."""
    started_at(monkeypatch, at(9, 50))
    await record_pump_late(hass, freezer, before)
    entry = new_entry(hass)
    await setup(hass, entry)

    summary = await async_backfill(hass, entry)
    await async_wait_recording_done(hass)

    assert summary["start"] == at(6, 0, 10).isoformat()
    assert summary["source_heat"] == pytest.approx(7.4)
    assert summary[KEY_HEAT][entity_id(hass, "living_heat")] == pytest.approx(4.625)
    assert await sums(hass, "living_heat") == {
        at(6): pytest.approx(1.25),
        at(7): pytest.approx(3.125),
        at(8): pytest.approx(3.125),
        at(9): pytest.approx(4.125),
    }


@pytest.mark.parametrize("start", [None, at(9, 30)], ids=["no start", "a start long before"])
async def test_an_input_that_changed_counts_from_then(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    start: datetime | None,
) -> None:
    """The first state of the pump is a change: its state before is unknown, so the replay starts there, and the
    heat before is left out instead of being split with a guess."""
    started_at(monkeypatch, *([start] if start else []))
    await record_pump_late(hass, freezer, [])
    entry = new_entry(hass)
    await setup(hass, entry)

    summary = await async_backfill(hass, entry)

    assert summary["start"] == at(9, 51).isoformat()
    assert summary["source_heat"] == pytest.approx(0.8)  # from 103.6 at 9:40 to 104.4 at 10:00:20
    assert summary[KEY_HEAT][entity_id(hass, "living_heat")] == pytest.approx(0.5)
    assert summary["hours"] == 0  # all of it in the hour of the setup, raised with the live statistics


@pytest.mark.parametrize(("start", "key"), [(True, None), (False, "backfill_not_recorded")], ids=["start", "change"])
async def test_an_input_with_no_state_in_the_days_and_a_restart_after(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    start: bool,
    key: str | None,
) -> None:
    """The pump did not change in the days before the setup; its state then is purged. The action runs after Home
    Assistant restarted: the state it wrote again then is the one the pump had in those days. Without a start, its
    first state after is a change, and its state before stays unknown."""
    started_at(monkeypatch, *([at(10, 30)] if start else []))
    await record_past(hass, freezer, pump=None)
    entry = new_entry(hass)
    await setup(hass, entry)
    await record(hass, freezer, at(10, 30, 5), {PUMP: "on"})
    await async_wait_recording_done(hass)

    if key:
        with pytest.raises(BackfillError) as error:
            await async_backfill(hass, entry)
        assert error.value.key == key
        return
    summary = await async_backfill(hass, entry)
    assert summary["source_heat"] == pytest.approx(7.4)
    assert summary[KEY_HEAT][entity_id(hass, "living_heat")] == pytest.approx(4.625)


async def test_the_starts_of_home_assistant_are_read_from_the_recorder(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    starts = await get_instance(hass).async_add_executor_job(backfill._recorder_starts, hass, at(0))

    assert starts
    assert starts[-1] <= at(6).timestamp() + 60


async def test_an_input_without_any_valid_state(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await record_past(hass, freezer, pump="unavailable")
    entry = new_entry(hass)
    await setup(hass, entry)

    with pytest.raises(BackfillError) as error:
        await async_backfill(hass, entry)

    assert error.value.key == "backfill_not_recorded"
    assert error.value.translation_placeholders == {"entity": PUMP}


@pytest.mark.parametrize("recorder_config", [{"exclude": {"entities": [PUMP]}}])
async def test_an_input_unchanged_since_before_the_recording_counts_with_its_state_now(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The pump has run since before the recording begins: it has no state in it, and its state now held all along."""
    hass.states.async_set(PUMP, "on", timestamp=at(5).timestamp())
    await record_past(hass, freezer, pump=None)
    entry = new_entry(hass)
    await setup(hass, entry)

    summary = await async_backfill(hass, entry)

    assert summary["source_heat"] == pytest.approx(7.4)
    assert summary[KEY_HEAT][entity_id(hass, "living_heat")] == pytest.approx(4.625)


async def test_a_heat_meter_does_not_hide_that_nothing_can_be_split(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Hot water with a heat meter counts without the split. With the circuit pump missing in the recording the rooms
    would stay at 0 for all the days, and the backfill would count as done."""
    meter = "sensor.hot_water_meter"
    subentries = [
        s | {"data": s["data"] | {CONF_METER_ENTITY: meter}} if s["subentry_id"] == "hot_water" else s
        for s in SUBENTRIES
    ]
    await record(hass, freezer, at(6, 0, 5), {meter: 10.0})
    hass.states.async_set(meter, "10.0", {"unit_of_measurement": "kWh"})
    await record_past(hass, freezer, pump=None)
    hass.states.async_set(meter, "11.0", {"unit_of_measurement": "kWh"})
    await async_wait_recording_done(hass)
    entry = MockConfigEntry(domain=DOMAIN, title="Heating", options={}, subentries_data=subentries)
    entry.add_to_hass(hass)
    await setup(hass, entry)

    with pytest.raises(BackfillError) as error:
        await async_backfill(hass, entry)

    assert error.value.key == "backfill_not_recorded"
    assert error.value.translation_placeholders == {"entity": PUMP}
    assert await sums(hass, "hot_water_heat") == {}
    assert entry.runtime_data.backfill is None


async def test_no_room_heated_in_the_days_recorded(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await record_past(hass, freezer, pump="off")
    entry = new_entry(hass)
    await setup(hass, entry)

    with pytest.raises(BackfillError) as error:
        await async_backfill(hass, entry)

    assert error.value.key == "backfill_nothing_split"
    assert error.value.translation_placeholders == {"heat": "7.4"}
    assert entry.runtime_data.backfill is None


async def test_heat_of_a_source_that_counts_energy_input(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Its heat counter is what the Energy dashboard gets as source: without it, the days calculated would show the
    rooms' heat and no source."""
    await record_past(hass, freezer)
    entry = new_entry(hass)
    await setup(hass, entry)
    await async_backfill(hass, entry)
    await async_wait_recording_done(hass)

    assert await sums(hass, "heat_pump_source_heat") == {
        at(7): pytest.approx(3.0),
        at(8): pytest.approx(3.0),
        at(9): pytest.approx(3.0),
    } | {at(6): 0.0}

    await tick(hass, freezer)
    await async_wait_recording_done(hass)
    do_adhoc_statistics(hass, start=at(10, 5))
    await async_wait_recording_done(hass)
    await async_adjust_pending(hass, entry.runtime_data)
    await async_wait_recording_done(hass)

    assert (await sums(hass, "heat_pump_source_heat", "5minute"))[at(10, 5)] == pytest.approx(3.0)


async def test_reason_of_the_first_setup_is_shown_and_it_can_be_started_again(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Ticked in the setup, nothing written: a repair issue says why, and the action runs once the reason is gone."""
    await record_past(hass, freezer)
    await import_hour(hass, "sensor.bath_heat", at(8), 500.0)  # left by a counter removed earlier
    entry = new_entry(hass, **{CONF_BACKFILL: True})

    await setup(hass, entry)
    await background(hass)

    runtime = entry.runtime_data
    assert entity_id(hass, "bath_heat") == "sensor.bath_heat"
    assert runtime.backfill[KEY_SKIPPED] == "backfill_has_statistics"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, backfill.issue_id(entry))
    assert issue.translation_key == "backfill_skipped"
    assert issue.translation_placeholders["name"] == "Heating"
    assert "already have statistics" in issue.translation_placeholders["reason"]
    assert "the past days were not calculated" in caplog.text

    # Tried again with the reason still there: the reason, not "already calculated".
    with pytest.raises(BackfillError) as error:
        await async_backfill(hass, entry)
    assert error.value.key == "backfill_has_statistics"

    get_instance(hass).async_clear_statistics(["sensor.bath_heat"])
    await async_wait_recording_done(hass)
    summary = await async_backfill(hass, entry)

    assert summary["hours"] == 4
    assert KEY_SKIPPED not in runtime.backfill
    assert ir.async_get(hass).async_get_issue(DOMAIN, backfill.issue_id(entry)) is None


async def test_nothing_recorded_yet_is_no_issue(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(SETUP)
    entry = new_entry(hass, **{CONF_BACKFILL: True})

    await setup(hass, entry)
    await background(hass)

    assert entry.runtime_data.backfill[KEY_SKIPPED] == "backfill_no_history"
    assert ir.async_get(hass).async_get_issue(DOMAIN, backfill.issue_id(entry)) is None


async def compile_live(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Some live heat, and the first statistics the recorder compiles of it."""
    await record(hass, freezer, at(10, 2), {BOILER: 106.0})
    await tick(hass, freezer)
    await async_wait_recording_done(hass)
    for start in (at(10), at(10, 5)):
        do_adhoc_statistics(hass, start=start)
    await async_wait_recording_done(hass)


async def backfill_with_imports(
    hass: HomeAssistant, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch, write: Any
) -> None:
    """The backfill, with what the recorder gets of the imports replaced by `write`: as if Home Assistant stopped before
    the recorder wrote them (and started again), or the recorder dropped them."""
    with monkeypatch.context() as patched:
        patched.setattr("homeassistant.components.recorder.statistics.async_import_statistics", write)
        await async_backfill(hass, entry)
    await async_wait_recording_done(hass)


async def test_hours_that_were_never_written_raise_nothing(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raising the live statistics without the hours would show all the days as heat of the hour of the setup. None
    of them arrives, so after looking for them long enough nothing of the backfill is in the statistics: it may run
    again. The note was saved before the hours were queued, so this also holds after a crash in between."""
    monkeypatch.setattr(backfill, "WRITTEN_TRIES", 2)
    await record_past(hass, freezer)
    entry = new_entry(hass)
    await setup(hass, entry)
    runtime = entry.runtime_data
    await backfill_with_imports(hass, entry, monkeypatch, lambda *_: None)
    assert runtime.backfill[KEY_PENDING]

    await compile_live(hass, freezer)  # the interval looks once
    assert runtime.backfill["checks"] == 1
    await async_adjust_pending(hass, runtime)
    await async_wait_recording_done(hass)

    assert runtime.backfill is None
    assert (await sums(hass, "living_heat", "5minute"))[at(10, 5)] == pytest.approx(1.0)  # not raised
    assert (await async_backfill(hass, entry))["hours"] == 4


async def test_hours_still_being_written_are_waited_for(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorder has not got to the hours yet (busy, or a retry): nothing is raised and nothing taken for lost; once
    they are there, they are raised."""
    await record_past(hass, freezer)
    entry = new_entry(hass)
    await setup(hass, entry)
    runtime = entry.runtime_data
    queued: list[tuple[Any, ...]] = []
    await backfill_with_imports(hass, entry, monkeypatch, lambda *args: queued.append(args))

    await compile_live(hass, freezer)
    await async_adjust_pending(hass, runtime)
    await async_wait_recording_done(hass)

    assert runtime.backfill["checks"] == 2
    assert runtime.backfill[KEY_PENDING]["living_heat"] == pytest.approx(4.625)
    assert (await sums(hass, "living_heat", "5minute"))[at(10, 5)] == pytest.approx(1.0)  # not raised yet

    for args in queued:
        async_import_statistics(*args)
    await async_wait_recording_done(hass)
    await async_adjust_pending(hass, runtime)
    await async_wait_recording_done(hass)

    assert runtime.backfill["verified"]
    assert runtime.backfill[KEY_PENDING] == {}
    assert runtime.backfill["lost"] == 0
    assert (await sums(hass, "living_heat", "5minute"))[at(10, 5)] == pytest.approx(5.625)


@pytest.mark.parametrize("written", ["bath_heat", "hot_water_heat"], ids=["a counter with heat", "one without"])
async def test_hours_of_some_counters_that_were_not_written(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    written: str,
) -> None:
    """Only the counters whose hours are there are raised. The backfill stays done: its other hours are in the
    statistics, so a second one would only find them (also hours of a counter with nothing to raise)."""
    monkeypatch.setattr(backfill, "WRITTEN_TRIES", 2)
    await record_past(hass, freezer)
    entry = new_entry(hass)
    await setup(hass, entry)
    runtime = entry.runtime_data
    kept = entity_id(hass, written)

    def only_one(hass: HomeAssistant, metadata: dict[str, Any], rows: Any) -> None:
        if metadata["statistic_id"] == kept:
            async_import_statistics(hass, metadata, rows)

    await backfill_with_imports(hass, entry, monkeypatch, only_one)
    await compile_live(hass, freezer)
    await async_adjust_pending(hass, runtime)
    await async_wait_recording_done(hass)

    assert runtime.backfill[KEY_PENDING] == {}
    assert runtime.backfill["lost"] == 4  # of the five counters, heat of the heat pump included
    assert (await sums(hass, "living_heat", "5minute"))[at(10, 5)] == pytest.approx(1.0)  # not raised
    bath = 2.775 + 0.6 if written == "bath_heat" else 0.6
    assert (await sums(hass, "bath_heat", "5minute"))[at(10, 5)] == pytest.approx(bath)
    with pytest.raises(BackfillError) as error:
        await async_backfill(hass, entry)
    assert error.value.key == "backfill_done"


async def test_nothing_is_raised_by_an_interval_while_the_backfill_runs(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """An interval between the note and the recorder writing the hours would find them missing: it waits."""
    await record_past(hass, freezer)
    entry = new_entry(hass)
    await setup(hass, entry)
    runtime = entry.runtime_data
    runtime.backfill = {"end": SETUP.timestamp(), "last_hour": at(9).timestamp(), KEY_HEAT: {"living_heat": 1.0}}
    runtime.backfill[KEY_PENDING] = {"living_heat": 1.0}
    runtime.backfilling = True

    await async_adjust_pending(hass, runtime)

    assert runtime.backfill[KEY_PENDING] == {"living_heat": 1.0}
    assert not runtime.backfill.get("verified")
    runtime.backfilling = False


async def test_disabled_counters_are_left_out(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await record_past(hass, freezer)
    entry = new_entry(hass)
    await setup(hass, entry)
    circuit = entity_id(hass, "ground_floor_heat")
    er.async_get(hass).async_update_entity(circuit, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()

    summary = await async_backfill(hass, entry)
    await async_wait_recording_done(hass)

    assert circuit not in summary[KEY_HEAT]
    assert "ground_floor_heat" not in entry.runtime_data.backfill[KEY_PENDING]
    assert await sums(hass, "ground_floor_heat") == {}
    assert (await sums(hass, "living_heat"))[at(9)] == pytest.approx(4.125)


async def test_a_counter_without_statistics_is_given_up(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A counter excluded from the recorder never gets statistics to raise: it does not stay pending for good. But
    it is given up only after intervals of running in vain, not after a downtime: the recorder compiles what it
    missed only once Home Assistant has started."""
    await record_past(hass, freezer)
    entry = new_entry(hass)
    await setup(hass, entry)
    runtime = entry.runtime_data
    await async_backfill(hass, entry)
    await async_wait_recording_done(hass)
    pending = dict(runtime.backfill[KEY_PENDING])
    assert runtime.backfill["tries"] == 0
    monkeypatch.setattr(backfill, "PENDING_TRIES", 2)

    freezer.move_to(SETUP + timedelta(days=3))  # three days off
    await async_adjust_pending(hass, runtime)
    await async_adjust_pending(hass, runtime)
    assert runtime.backfill[KEY_PENDING] == pending
    assert "compiles no statistics" not in caplog.text

    await async_adjust_pending(hass, runtime)

    assert runtime.backfill[KEY_PENDING] == {}
    assert runtime.backfill[KEY_HEAT]["living_heat"] == pytest.approx(4.625)  # still done
    assert f"the recorder compiles no statistics for {entity_id(hass, 'living_heat')}" in caplog.text


async def test_action_is_for_administrators(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_read_only_user: MockUser
) -> None:
    await record_past(hass, freezer)
    entry = new_entry(hass)
    await setup(hass, entry)

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "backfill",
            {"config_entry_id": entry.entry_id},
            blocking=True,
            context=Context(user_id=hass_read_only_user.id),
        )

    assert entry.runtime_data.backfill is None


async def test_diagnostics_show_the_backfill(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await record_past(hass, freezer)
    entry = new_entry(hass, **{CONF_BACKFILL: True})
    await setup(hass, entry)
    await background(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["data"] == {CONF_BACKFILL: True}
    assert diagnostics["backfill"]["hours"] == 4
    assert diagnostics["backfill"][KEY_PENDING]["living_heat"] == pytest.approx(4.625)
    json.dumps(diagnostics)


async def test_reason_of_the_issue_in_the_language_of_home_assistant(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    hass.config.language = "de"
    await record_past(hass, freezer, pump=None)
    entry = new_entry(hass, **{CONF_BACKFILL: True})

    await setup(hass, entry)
    await background(hass)

    issue = ir.async_get(hass).async_get_issue(DOMAIN, backfill.issue_id(entry))
    assert issue.translation_placeholders["reason"].startswith(
        f"Home Assistant hat keinen aufgezeichneten Zustand von {PUMP}"
    )


async def test_set_up_longer_ago_than_the_recorder_keeps(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The action, long after the setup: everything from before it is purged."""
    await record_past(hass, freezer)
    entry = new_entry(hass)
    object.__setattr__(entry, "created_at", at(5))  # before the oldest state recorded
    await setup(hass, entry)

    with pytest.raises(BackfillError) as error:
        await async_backfill(hass, entry)

    assert error.value.key == "backfill_no_history"


async def test_outside_the_heating_season_there_is_nothing_to_calculate(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The sources delivered nothing: no repair issue, there was nothing to calculate."""
    first = {BOILER: 100.0, HEAT_PUMP: 50.0, LIVING_DEMAND: 0, HOT_WATER_PUMP: "off"}
    await record(hass, freezer, at(6, 0, 10), first)
    await record(hass, freezer, at(6, 0, 20), {PUMP: "off"})
    freezer.move_to(SETUP)
    await async_wait_recording_done(hass)
    entry = new_entry(hass, **{CONF_BACKFILL: True})

    await setup(hass, entry)
    await background(hass)

    assert entry.runtime_data.backfill[KEY_SKIPPED] == "backfill_no_heat"
    assert ir.async_get(hass).async_get_issue(DOMAIN, backfill.issue_id(entry)) is None


async def test_an_input_missing_at_an_earlier_start_counts_from_its_first_state(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Home Assistant started at 7:00 and at 9:50, the pump has its first state only at the second start: at the first
    it did not exist or was not recorded, so its state at 9:50 says nothing about the time before."""
    started_at(monkeypatch, at(7), at(9, 50))
    await record_pump_late(hass, freezer, [])
    entry = new_entry(hass)
    await setup(hass, entry)

    summary = await async_backfill(hass, entry)

    assert summary["start"] == at(9, 51).isoformat()
    assert summary["source_heat"] == pytest.approx(0.8)


async def test_the_value_an_input_settles_to_after_a_start_counts(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Home Assistant starts, a template may show a default for a moment (the pump 'off' while its plug is not
    up yet). The value it settles to within START_GRACE is its state before the start, not that moment's."""
    started_at(monkeypatch, at(9, 50))
    first = {BOILER: 100.0, HEAT_PUMP: 50.0, LIVING_DEMAND: 50, HOT_WATER_PUMP: "off"}
    await record(hass, freezer, at(6, 0, 10), first)
    await record(hass, freezer, at(6, 20), {BOILER: 102.0})
    await record(hass, freezer, at(9, 40), {BOILER: 103.6})
    await record(hass, freezer, at(9, 50, 5), {PUMP: "off"})
    await record(hass, freezer, at(9, 50, 40), {PUMP: "on"})
    await record(hass, freezer, at(10, 0, 20), {BOILER: 104.4})
    freezer.move_to(SETUP)
    await async_wait_recording_done(hass)
    entry = new_entry(hass)
    await setup(hass, entry)

    summary = await async_backfill(hass, entry)
    await async_wait_recording_done(hass)

    assert summary["start"] == at(6, 0, 10).isoformat()
    # Pump on from the beginning: the 2 kWh of 6:20 go to the rooms, not into the account to wait.
    assert (await sums(hass, "living_heat"))[at(6)] == pytest.approx(1.25)
    # 4.4 kWh in all, 5/8 of it the living room. The 35 seconds it showed "off" changed nothing: no heat came then.
    assert summary[KEY_HEAT][entity_id(hass, "living_heat")] == pytest.approx(2.75)


async def test_a_meter_does_not_hold_back_the_beginning(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The heat pump's meter has its first state only at 9:40 (its last change was purged, it delivered nothing): the
    split does not need it before, so the replay starts with the others, and the meter counts from its first state."""
    first = {BOILER: 100.0, LIVING_DEMAND: 50, HOT_WATER_PUMP: "off", PUMP: "on"}
    await record(hass, freezer, at(6, 0, 10), first)
    await record(hass, freezer, at(6, 20), {BOILER: 102.0})
    await record(hass, freezer, at(9, 40), {BOILER: 103.6, HEAT_PUMP: 51.0})
    await record(hass, freezer, at(10, 0, 20), {BOILER: 104.4, HEAT_PUMP: 51.5})
    freezer.move_to(SETUP)
    await async_wait_recording_done(hass)
    entry = new_entry(hass)
    await setup(hass, entry)

    summary = await async_backfill(hass, entry)

    assert summary["start"] == at(6, 0, 10).isoformat()
    assert summary["source_heat"] == pytest.approx(4.4 + 1.5)  # the boiler, and the heat pump from its first state
    assert summary[KEY_HEAT][entity_id(hass, "heat_pump_source_heat")] == pytest.approx(1.5)


@pytest.mark.parametrize("after", [True, False], ids=["recorded after the setup", "never recorded"])
async def test_a_meter_without_states_in_the_days(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory, after: bool
) -> None:
    """A meter with no state in the days before the setup did not change in them, if it is recorded at all."""
    first = {BOILER: 100.0, LIVING_DEMAND: 50, HOT_WATER_PUMP: "off", PUMP: "on"}
    await record(hass, freezer, at(6, 0, 10), first)
    await record(hass, freezer, at(6, 20), {BOILER: 102.0})
    freezer.move_to(SETUP)
    await async_wait_recording_done(hass)
    entry = new_entry(hass)
    await setup(hass, entry)
    if after:
        await record(hass, freezer, at(10, 20), {HEAT_PUMP: 52.0})
        await async_wait_recording_done(hass)

    if not after:
        with pytest.raises(BackfillError) as error:
            await async_backfill(hass, entry)
        assert error.value.key == "backfill_not_recorded"
        assert error.value.translation_placeholders == {"entity": HEAT_PUMP}
        return
    summary = await async_backfill(hass, entry)
    assert summary["source_heat"] == pytest.approx(2.0)


async def test_the_starts_of_home_assistant_at_their_time(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    """The recorder started with the test, at 6:00 UTC: read back at that time, not shifted by a time zone."""
    await hass.config.async_set_time_zone("Europe/Berlin")
    starts = await get_instance(hass).async_add_executor_job(backfill._recorder_starts, hass, at(0))

    assert abs(starts[-1] - at(6).timestamp()) < 5
