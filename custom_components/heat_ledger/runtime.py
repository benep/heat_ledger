"""Feeds the ledger from Home Assistant: input states, the 5-minute timer and the stored state."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.entity_registry import EventEntityRegistryUpdatedData
from homeassistant.helpers.event import (
    async_track_entity_registry_updated_event,
    async_track_state_change_event,
    async_track_utc_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEMAND_ENTITY,
    CONF_ENTITY,
    CONF_METER_ENTITY,
    CONF_PUMP_ENTITY,
    DOMAIN,
    INTERVAL,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
)
from .engine import Ledger, PlantSpec, Reading
from .engine.ledger import MAX_PLAUSIBLE_KW
from .model import InputKind, Subentry, input_kinds, parse_state, reference_units

_LOGGER = logging.getLogger(__name__)

STORE_KEY_LEDGER = "ledger"


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}"


class HeatLedgerRuntime:
    """Owns the ledger of one config entry while it is loaded."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, plant: PlantSpec) -> None:
        self.hass = hass
        self.entry = entry
        self.plant = plant
        self.kinds: dict[str, InputKind] = input_kinds(plant)
        self.units = reference_units(
            Subentry(s.subentry_id, s.subentry_type, s.title, s.data) for s in entry.subentries.values()
        )
        self._unit_warned: set[str] = set()
        self.ledger = Ledger(plant)
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, store_key(entry.entry_id))
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubscribe: list[CALLBACK_TYPE] = []

    # --- Lifecycle ---------------------------------------------------------------------------------------------

    async def async_start(self) -> None:
        """Restore the stored state, take the current readings and start listening."""
        stored = await self._store.async_load()
        if stored and isinstance(stored.get(STORE_KEY_LEDGER), dict):
            try:
                self.ledger = Ledger.from_dict(self.plant, stored[STORE_KEY_LEDGER])
            except (KeyError, TypeError, ValueError, AttributeError, IndexError):
                # The counters keep their values through their restored states; only the split starts over.
                _LOGGER.warning("Stored state of %s is unreadable, starting over", self.entry.title, exc_info=True)
        self.ledger.observe(self._now(), {key: self._read(key, self.hass.states.get(key)) for key in self.kinds})
        self._log_rejected()

        if self.kinds:
            self._unsubscribe.append(async_track_state_change_event(self.hass, list(self.kinds), self._on_state))
            self._unsubscribe.append(
                async_track_entity_registry_updated_event(self.hass, list(self.kinds), self._on_registry)
            )
        minutes = int(INTERVAL.total_seconds() // 60)
        self._unsubscribe.append(
            async_track_utc_time_change(self.hass, self._on_interval, minute=f"/{minutes}", second=0)
        )
        self._unsubscribe.append(self.hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, self._on_stop))

    @callback
    def cancel(self) -> None:
        """Stop listening. Also called when the setup fails after the start, so no listener is left behind."""
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()

    async def async_stop(self) -> None:
        """Stop listening and save the state, up to now."""
        self.cancel()
        await self.async_save()

    async def async_save(self) -> None:
        self.ledger.observe(self._now(), {})
        await self._store.async_save(self._data())

    # --- Sensors -----------------------------------------------------------------------------------------------

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> Callable[[], None]:
        """Call update after every interval. Returns the function that removes the listener."""
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    # --- Internals ---------------------------------------------------------------------------------------------

    def _now(self) -> float:
        return dt_util.utcnow().timestamp()

    def _read(self, key: str, state: State | None) -> Reading:
        if state is None:
            return None
        kind = self.kinds[key]
        reading = parse_state(kind, state.state, state.attributes, self.units.get(key))
        if reading is None and kind is InputKind.ENERGY and key not in self._unit_warned:
            unit = state.attributes.get("unit_of_measurement")
            if (
                parse_state(kind, state.state, state.attributes) is not None
            ):  # a number, but in a unit that does not fit
                self._unit_warned.add(key)
                _LOGGER.warning(
                    "%s is now in %s instead of %s: nothing is booked from it until its unit fits again or the "
                    "source is set up again with the new unit",
                    key,
                    unit,
                    self.units.get(key),
                )
        return reading

    def _data(self) -> dict[str, Any]:
        return {STORE_KEY_LEDGER: self.ledger.to_dict()}

    @callback
    def _on_state(self, event: Event[EventStateChangedData]) -> None:
        key = event.data["entity_id"]
        self.ledger.observe(self._now(), {key: self._read(key, event.data["new_state"])})
        self._log_rejected()

    def _log_rejected(self) -> None:
        for entity_id, increase in self.ledger.rejected:
            _LOGGER.warning(
                "Ignored a jump of %s by %.1f (a unit mixed up, or more than %.0f kW on average): taken as a glitch "
                "or a replaced meter, not as heat",
                entity_id,
                increase,
                MAX_PLAUSIBLE_KW,
            )
        self.ledger.rejected.clear()

    @callback
    def _on_interval(self, _now: Any) -> None:
        self.ledger.close_interval(self._now())
        for update in list(self._listeners):
            update()
        self._store.async_delay_save(self._data, STORAGE_SAVE_DELAY)

    async def _on_stop(self, _event: Event) -> None:
        await self.async_save()

    @callback
    def _on_registry(self, event: Event[EventEntityRegistryUpdatedData]) -> None:
        data = event.data
        if data["action"] == "update" and (old := data.get("old_entity_id")) and old != data["entity_id"]:
            self.hass.async_create_task(self._async_renamed(old, data["entity_id"]), eager_start=True)

    async def _async_renamed(self, old: str, new: str) -> None:
        """An input got a new entity ID: follow it, so the plant keeps reading it and no heat is lost meanwhile.

        The ledger takes the new ID first and is saved, then the subentries are updated, which reloads the entry:
        the reloaded ledger finds its meters under the new IDs and counts on.
        """
        self.ledger.rename_input(old, new)
        await self.async_save()
        entry = self.entry
        for subentry in list(entry.subentries.values()):
            data = dict(subentry.data)
            changed = False
            for key in (CONF_ENTITY, CONF_DEMAND_ENTITY, CONF_PUMP_ENTITY, CONF_METER_ENTITY):
                if data.get(key) == old:
                    data[key] = new
                    changed = True
            if changed:
                self.hass.config_entries.async_update_subentry(entry, subentry, data=data)
        _LOGGER.info("%s was renamed to %s: %s follows it", old, new, entry.title)
