"""Diagnostics download: everything needed to reproduce a split, for bug reports.

Contains the plant as configured, the stored state of the ledger and the current state of every input entity. It
holds no credentials; entity IDs and names (rooms, devices) are included, as in the diagnostics of most integrations.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from . import HeatLedgerConfigEntry
from .const import DOMAIN
from .engine import consumer_basis


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: HeatLedgerConfigEntry) -> dict[str, Any]:
    integration = await async_get_integration(hass, DOMAIN)
    result: dict[str, Any] = {
        "versions": {"heat_ledger": str(integration.version), "home_assistant": HA_VERSION},
        "language": hass.config.language,
        "data": dict(entry.data),
        "options": dict(entry.options),
        "subentries": [
            {"id": s.subentry_id, "type": s.subentry_type, "title": s.title, "data": dict(s.data)}
            for s in entry.subentries.values()
        ],
    }
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:  # not loaded, e.g. after a setup error
        result["loaded"] = False
        return result

    ledger = runtime.ledger
    result["loaded"] = True
    result["inputs"] = {}
    for entity_id, kind in sorted(runtime.kinds.items()):
        state = hass.states.get(entity_id)
        result["inputs"][entity_id] = {
            "read_as": str(kind),
            "state": state.state if state else None,
            "unit": state.attributes.get("unit_of_measurement") if state else None,
            "hvac_action": state.attributes.get("hvac_action") if state else None,
            "last_changed": state.last_changed.isoformat() if state else None,
        }
        if state and state.domain == "climate":
            # Without hvac_action, whether it heats is told by these (model._wants_heat).
            for attribute in ("current_temperature", "temperature", "target_temp_low"):
                result["inputs"][entity_id][attribute] = state.attributes.get(attribute)
    result["consumers"] = {
        c.id: {
            "total_kwh": ledger.consumer_total(c.id),
            "level": str((basis := consumer_basis(runtime.plant, c)).level),
            "basis": basis.methods,
            "next_improvement": basis.next_improvement,
        }
        for c in runtime.plant.consumers
    }
    result["circuits"] = {c.id: {"total_kwh": ledger.circuit_total(c.id)} for c in runtime.plant.circuits}
    result["account_kwh"] = ledger.account_total
    result["blocked_by"] = list(ledger.blocked_by)
    result["ledger"] = ledger.to_dict()
    result["backfill"] = runtime.backfill
    return result
