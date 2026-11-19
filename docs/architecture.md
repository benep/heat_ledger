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
  their valves say. Its counter is the sum of its radiators, which the Energy dashboard uses as parent
  (`included_in_stat`).
- **Measured consumer:** a consumer with its own heat meter. Its readings are taken out of the account directly and
  it takes no part in the split; the others share what is left.

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

- **Conservation:** at every moment, heat of all sources = consumers + losses + account. Tested as a property.
- **Counters never decrease**, also not across restarts.
- **Unavailable input:** if any input needed for a weight is unavailable, that stretch of time counts for no one.
  Heat arriving then stays in the account and goes to the next interval with valid weights.
- **No weights at all** (all pumps off, tank charging at night): the heat stays in the account.
- **Source decreases:** treated as a meter reset; the new value becomes the baseline, nothing is booked.
- **Source unavailable:** its increase is booked when it is back, measured from the last valid value.
- **Implausible jump:** an increase of a source or heat meter above 1 MW on average since its last change (over at
  least 5 minutes) is a glitch, such as Wh reported as kWh for a moment. It becomes the new baseline, is not booked
  and is logged as a warning. Booked, it would stay in every counter for good.
- **Other meter:** a source or heat meter switched to another entity starts over from that entity's next reading;
  the difference between the two meters is not heat. `nan` and `inf` count as unavailable.
- **Unreadable stored state:** the split starts over; the counters keep their values through their restored states.
- **Fixed loss rate** (optional): taken from the account first, every interval.
- **Release:** the whole account every interval. The engine also supports a time constant that releases only
  `account × (1 − e^(−Δt/τ))` per interval, but it is not offered in the UI: it shifts heat from
  burner times to whatever runs hours later, which is less plausible, not more.
- **Downtime:** heat that arrived while Home Assistant was down shows up as one increase after the restart and is
  split by the weights after the restart. The backfill can correct this from recorded history.

## Home Assistant layer

| Module | Role |
|---|---|
| `__init__.py` | Builds the plant from the entry and its subentries, creates the runtime, forwards the platforms. |
| `model.py` | Turns entry and subentry data into the engine's specs (pure, tested without Home Assistant). |
| `runtime.py` | Listens to all input entities, converts states to readings, calls the ledger, runs the 5-minute timer, persists the state, notifies the sensors. |
| `sensor.py` | Energy counter per consumer and per circuit (`energy`, kWh, `total_increasing`), heat per converted source, account (diagnostic). Restored on start, never lower than before. |
| `config_flow.py` | Guided first setup (name, source, thermostats, pumps: creates the entry with its subentries), options (fixed losses) and subentry flows for sources, circuits and consumers to add or refine later. Required fields first, everything else in a collapsible "more accurate" section. |

Converting states to readings: `climate` → `hvac_action` is `heating` or `preheating` (anything else off);
binary sensors → `on`; percent sensors → value / 100, clamped to 0..1; energy sensors → kWh using the unit of the
entity. `unavailable` and `unknown` become `None`.

Devices: one per subentry, so the Home Assistant UI groups the sensors of a consumer, circuit or source.

Backfill (later milestone): a service that feeds hourly long-term statistics of the inputs into a fresh ledger and
imports the resulting hourly counters with `async_import_statistics`, adjusting the live statistics after them so
the sums stay continuous. The recorder continues a sum from the last short-term (5-minute) row only, never from
imported hourly rows: the adjustment must run once the sensor has its first short-term row, otherwise it changes
nothing and the live sum starts at 0 again.

## Tests

- Engine: unit tests for every rule above, property tests (Hypothesis) for conservation and monotonic counters with
  random sequences of readings, outages and restarts (`to_dict`/`from_dict` in between).
- Home Assistant layer: config flow, setup and restore with `pytest-homeassistant-custom-component`.
