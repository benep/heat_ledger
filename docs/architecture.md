# Architecture

Heat Ledger estimates how the heat of one or more sources is split between radiators and domestic hot water, and
exposes the result as energy counters for the Energy dashboard. It never controls anything and never measures on
its own: every input is an existing entity.

## Model

```
Sources ──► Account (per source, kWh) ──► Consumers
            released every 5 min          radiators (grouped into circuits), hot water
            by the weights of the         "not tracked" is computed by Home Assistant:
            interval                      sources minus top-level consumers
```

- **Source:** an entity with a cumulative reading of heat (heat meter, heat output of a heat pump), or of energy
  input (gas, oil, electricity) with a fixed factor to heat. Its increase is heat that enters the account. Turning
  fuel or electricity into heat precisely is left to the integrations that know the boiler or heat pump; the factor
  is the fallback when none does.
- **Account:** heat that has entered but is not yet assigned. It is kept per source, so every consumer later knows
  where its heat came from (costs per radiator are a planned extension, the bookkeeping is there from the start).
  Without a storage tank it is empty almost all the time.
- **Consumer:** something that takes heat. Its weight at any moment is

  ```
  weight [W] = size [W] × demand [0..1] × circuit active [0 or 1]
  ```

  Hot water is a consumer like a radiator: size = charging power (default 10 kW), demand = hot water pump
  running, no circuit. Its share depends mostly on when the pump runs, little on the charging power.
- **Circuit:** a group of radiators behind one pump. Inactive (pump off) means no heat for any of them, whatever
  their valves say. Its counter is what its radiators received while they belonged to it, which the Energy
  dashboard uses as parent (`included_in_stat`). A radiator moved to another circuit leaves its history with the
  old one; the new one counts it from then on, without a jump. Rewriting the history instead (as if it had always
  been in the new circuit) would need a rewrite of its statistics, which the backfill does not do yet.
- **Measured consumer:** a consumer with its own heat meter. Its readings are taken out of the account directly (or as
  soon as a source holds heat) and it takes no part in the split; the others share what is left.

## Levels

Every factor of the weight has a list of methods, from rough to precise. Configuring an input selects the best
method it allows; nothing else changes. This is the extension point: new levels are new methods, not new structure.

| Factor | Methods (rough → precise) |
|---|---|
| size | default (typical radiator) → floor area → nominal power from type and dimensions → nominal power entered |
| demand | always on → `hvac_action` heating → heating demand % → valve position % (on/off for actuators) |
| circuit active | always → pump binary sensor |
| consumer heat | weight split → heat meter (measured) |

Each result sensor has three attributes: `level` (`measured`, `calculated`, `estimated`), `basis` (the method per
factor) and `next_improvement` (the input that would improve it most). The method is chosen from the
configuration, not at runtime: an input that becomes unavailable does not silently fall back to a rougher method.

Planned methods that fit this structure without changes: heat cost allocators (weight from counter increments
instead of a rate), flow and return temperatures at the radiator (absolute power, level `calculated`), source
conversion from ΔT × flow, storage tank sensors (account replaced by a measured heat balance).

## The ledger (pure Python)

`custom_components/heat_ledger/engine/` has no Home Assistant imports and never reads the clock: every call gets
the time. The same code runs live, in tests and in the backfill of past statistics.

- `observe(t, readings)`: the current value of every input (`None` = unavailable). Integrates the weights since the
  previous call (piecewise constant), adds source increases to the account, books meter readings.
- `close_interval(t)`: releases the account by the weights integrated in the interval. Called every 5 minutes,
  aligned to the clock like the short-term statistics of the recorder; finer would not be visible anywhere.
- `to_dict()` / `from_dict()`: the complete state, stored with Home Assistant's `Store` after every interval and
  on shutdown.

Rules:

- **Conservation:** at every moment, heat of all sources = what consumers received by source + losses + account.
  Balances never go negative: heat is only taken from sources that hold some, up to what they hold. Tested as a
  property, also across restarts and with a source removed. (A balance an earlier version overdrew is kept, so the
  next deliveries fill it instead of being split again.)
- **Counters never decrease**, also not across restarts. Totals are kept apart from the split by source, so they
  only ever grow, also in floating point.
- **Unavailable input:** if any input needed for a weight is unavailable, that stretch of time counts for no one.
  Heat arriving then stays in the account and goes to the next interval with valid weights.
- **No weights at all** (all pumps off, tank charging at night): the heat stays in the account.
- **Source and heat meter readings** follow the rules Home Assistant applies to the statistics of a
  `total_increasing` sensor, so the counters agree with the source in the Energy dashboard. An increase counts in
  full. A drop below 90 % of the reading before is a reset (a meter of the day at midnight, a new meter): the new
  reading counts in full. A smaller drop is a dip, paid off by the next increases before they count (Home Assistant
  shows it as a negative bar instead; the sums agree, the days do not). Negative readings are skipped.
- **No plausibility check:** like Home Assistant, Heat Ledger does not judge whether an increase is plausible. A
  spike of the source, or a template reporting 0 for a moment, ends up in the source's statistics and in those of the
  consumers, and is corrected there by hand (Developer tools, Statistics). A template source avoids it with an
  `availability` template. A spike while no consumer has a weight (pumps off at night) waits in the account and
  reaches the consumers' statistics only when they heat again, possibly hours later.
- **Source unavailable:** its increase is booked when it is back, measured from the last valid value. Heat that
  arrived while Home Assistant was down counts the same way.
- **Heat meter before its source:** what a meter measures beyond what the sources hold counts for its consumer at
  once and waits ("Not attributed" in `by_source`) until a source delivers; it is then charged to the sources
  that hold heat at that moment, per consumer the oldest part first, and to the circuit the consumer belonged to when
  it was measured. Removed before that, its heat is still taken out of the account, so it is not split among the
  others. It waits for 48 hours at most (a source reporting once a day still catches up): what no delivery covered
  by then is written off and stays "Not attributed" for good. So a heat meter that jumped takes the deliveries of
  two days at most, not of months; for those two days the other consumers get nothing.
- **Removed source:** stays in the books (what it delivered, what it holds, what consumers received from it), so
  no total goes down and what it still holds is released as before; it shows as "Removed source" in `by_source`.
- **Other meter:** a source or heat meter switched to another entity, or read in another unit, starts over from
  its next reading; the difference between the two meters is not heat. `nan` and `inf` count as unavailable.
- **Units:** heat (sources that count heat, heat meters) must be in an energy unit and is read in kWh. Energy input
  keeps the unit it was set up with, because its factor refers to it: another unit of the same kind (L instead of
  m³) is converted, another kind (kWh instead of m³) books nothing and is logged once, until the unit fits again
  or the source is set up again.
- **Renamed input:** a new entity ID of an input is followed: the subentries take it and its meter counts on as
  the same meter, so no heat is lost meanwhile.
- **Unreadable stored state:** the split starts over; the counters keep their values through their restored states.
- **Fixed loss rate** (optional): taken from the account first, every interval.
- **Release:** the whole account every interval. The engine also supports a time constant that releases only
  `account × (1 − e^(−Δt/τ))` per interval, but it is not offered in the UI: it shifts heat from
  burner times to whatever runs hours later, which is less plausible, not more.
- **Downtime:** heat that arrived while Home Assistant was down shows up as one increase after the restart and is
  split by the weights after the restart.

## Home Assistant layer

| Module | Role |
|---|---|
| `__init__.py` | Builds the plant from the entry and its subentries, creates the runtime, forwards the platforms. |
| `model.py` | Turns entry and subentry data into the engine's specs (pure, tested without Home Assistant). |
| `runtime.py` | Listens to all input entities, converts states to readings, calls the ledger, runs the 5-minute timer, persists the state, notifies the sensors. |
| `sensor.py` | Energy counter per consumer and per circuit (`energy`, kWh, `total_increasing`), heat per converted source, account (diagnostic). Restored on start, never lower than before. |
| `config_flow.py` | Guided first setup (name, source, thermostats, pumps: creates the entry with its subentries), options (fixed losses) and subentry flows for sources, circuits and consumers to add or refine later. Required fields first, everything else in a collapsible "more accurate" section. |
| `backfill.py` | The days before the setup, from the recorder: replays the inputs, imports the hours as statistics and raises the live ones; if chosen, estimates older periods from the hourly statistics (see Backfill). |

Converting states to readings: `climate` → `hvac_action` is `heating` or `preheating` (anything else off; a
thermostat that does not report `hvac_action` counts as heating in `heat`, `heat_cool` or `auto`);
binary sensors → `on`; percent sensors → value / 100, clamped to 0..1; energy sensors → kWh using the unit of the
entity. `unavailable` and `unknown` become `None`.

Devices: one per subentry, so the Home Assistant UI groups the sensors of a consumer, circuit or source.

## Backfill

`backfill.py` calculates the days before the plant was set up, once per entry: chosen in the first setup, or later in
the plant's options (**Calculate the past**, which checks first whether it can run, then runs it as a background task
of the entry and waits on it with a progress dialog) or with the action `heat_ledger.backfill` (administrators only).
Calculating again would only replace exactly counted days by recalculated or estimated ones; starting over means a
new entry, after the statistics of the old one were deleted.

- **Recalculation:** a second ledger (`engine/replay.py`) is fed the recorded states of the inputs, read from the
  recorder day by day, up to the moment the entry was created; from then on the live counters count. It closes its
  intervals on the clock like the runtime and notes the totals at every full hour. It reaches as far back as the
  recorder keeps states (`purge_keep_days`, 10 days by default).
- **The state of every input at the beginning:** the recorder purges the last state from before the days it keeps
  as well, so an input that has not changed for long has no state at the beginning. An input of the split (pump,
  demand) is known from its first valid recorded state, as Home Assistant's history shows it, and the recalculation
  starts when every one is known; the heat delivered before is left out instead of being split with a guess. Two
  exceptions count from the beginning: an input Home Assistant has not updated since before it (its state now), and
  one whose first recorded state is the one Home Assistant wrote when it started, at the first start since the
  beginning (within ten minutes of a start of the recorder, read from its table of runs; with the value it settled
  to in those minutes): it had not changed while Home Assistant was running before. An input with no state at an
  earlier start did not exist or was not recorded then. The second exception also applies to a state written at a
  start after the end, for an input with none in the recording. Meters (sources, heat meters) are not waited for:
  their first reading is the baseline of the next. An input that stays unknown, or a meter with no recorded state at
  all, stops the recalculation: then nothing is written.
- **Statistics:** the hours before the entry was created are imported as long-term statistics of the counters
  (`async_import_statistics`): room heating, hot water, circuits, and the heat of sources that count energy input,
  which is what the Energy dashboard gets as source. The hour the entry was created in is not written: its
  statistics are the live ones.
- **Raising the live statistics:** the recorder continues a sum from its last short-term (5-minute) row only, never
  from imported hours. So the statistics compiled since are raised by what was backfilled
  (`async_adjust_statistics`), which is only possible once the recorder has compiled the first of them. Until then
  the counter stays pending; it is tried again after every interval, also after a restart, and noted with the stored
  ledger. A counter is raised only once its hours are in the statistics: raised without them, all the days would
  show as heat of the hour of the setup. The note is saved before the hours are queued, so every interruption leaves
  a note to go by. Hours not there after six intervals of looking are taken for lost (Home Assistant stopped before
  the recorder wrote them, or the recorder dropped them): that counter is not raised, and if no counter's hours are
  there, the backfill may run again. A counter the recorder compiles no statistics for is given up after two days of
  running in vain.
- **Only statistics are written.** The counters keep their values and the live ledger is untouched. Heat still in
  the replay's account at the end is not handed over.
- **The plant as it is now:** the whole past is calculated with the current setup. A room added later is not
  backfilled on its own.
- **Nothing is written** if a counter already has statistics from before the entry was created, if an input has no
  valid reading in the recording, if the sources delivered nothing or no consumer received anything, or if the
  entry is reloaded during the recalculation. Chosen in the first setup, the reason is shown as a repair issue
  (unless there was simply nothing to calculate), and the options can try again.

Not built yet: rewriting the history of a single consumer.

### Estimate of older periods

Chosen as well (a box of its own in the setup, `estimate` in the action), the time before the recorded states is
estimated from the hourly long-term statistics, back to the first hour every meter has them (`engine/estimate.py`,
whose docstring has the details). Demands in percent are hourly means. A pump with a power sensor (`history_key`)
ran the share of the hour its mean shows above standby, of its usual running power above standby; an hour without a
value counts with its mean share in the recorded days, and a power that never shows the pump running is left out
and named in the summary. Everything else without statistics (on/off, `hvac_action`, pumps) counts as always on, so
every consumer's weight is calibrated on the hours calculated from the states, until the hourly calculation splits
them like the exact one did. Its hours are written in front of the others
in the same run; if it is not possible, the recorded days are written all the same and the reason is noted.

## Tests

- Engine: unit tests for every rule above, property tests (Hypothesis) for conservation and monotonic counters with
  random sequences of readings, outages and restarts (`to_dict`/`from_dict` in between).
- Home Assistant layer: config flow, setup and restore with `pytest-homeassistant-custom-component`.
