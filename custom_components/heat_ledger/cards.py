"""Serve the gas device cards (frontend/heat-ledger-cards.js), load them on every page of the frontend and tell them
the counters of the consumers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, SUBENTRY_CONSUMER, TRANSLATION_HEAT

CARDS_PATH = Path(__file__).parent / "frontend" / "heat-ledger-cards.js"
CARDS_URL = "/heat_ledger/heat-ledger-cards.js"
WS_CONSUMERS = f"{DOMAIN}/consumers"


async def async_register_cards(hass: HomeAssistant) -> None:
    """Register the module once; without a frontend (tests, headless installs) there is nothing to do."""
    if "frontend" not in hass.config.components:
        return
    digest = await hass.async_add_executor_job(_digest)
    await hass.http.async_register_static_paths([StaticPathConfig(CARDS_URL, str(CARDS_PATH), False)])
    # The digest makes browsers load a changed module after an update instead of a cached one.
    add_extra_js_url(hass, f"{CARDS_URL}?v={digest}")
    websocket_api.async_register_command(hass, ws_consumers)


def _digest() -> str:
    return hashlib.sha256(CARDS_PATH.read_bytes()).hexdigest()[:8]


@websocket_api.websocket_command({vol.Required("type"): WS_CONSUMERS})
@callback
def ws_consumers(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The heat counters of all room heating and hot water, with their names.

    The cards show them while the Energy dashboard has no gas devices: before Home Assistant 2026.12, which brought
    them, or before the user has entered any. Circuits are left out, their heat is that of their room heating.
    """
    registry = er.async_get(hass)
    consumers = []
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        for subentry in entry.subentries.values():
            if subentry.subentry_type != SUBENTRY_CONSUMER:
                continue
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{subentry.subentry_id}_{TRANSLATION_HEAT}")
            if entity_id is None or registry.async_get(entity_id).disabled:
                continue
            consumers.append({"entity_id": entity_id, "name": subentry.title})
    connection.send_result(msg["id"], consumers)
