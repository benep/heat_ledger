"""Feeds the ledger from Home Assistant: input states, the 5-minute timer and the stored state."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_utc_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, INTERVAL, STORAGE_SAVE_DELAY, STORAGE_VERSION
from .engine import Ledger, PlantSpec, Reading
from .engine.ledger import MAX_PLAUSIBLE_KW
from .model import InputKind, input_kinds, parse_state

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
        minutes = int(INTERVAL.total_seconds() // 60)
        self._unsubscribe.append(
            async_track_utc_time_change(self.hass, self._on_interval, minute=f"/{minutes}", second=0)
        )
        self._unsubscribe.append(self.hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, self._on_stop))

    async def async_stop(self) -> None:
        """Stop listening and save the state, up to now."""
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
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
        return parse_state(self.kinds[key], state.state, state.attributes)

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
                "Ignored a jump of %s by %.1f (more than %.0f kW on average): taken as a glitch or a replaced meter",
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
