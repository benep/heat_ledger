"""Serve the gas device cards (frontend/heat-ledger-cards.js) and load them on every page of the frontend."""

from __future__ import annotations

import hashlib
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

CARDS_PATH = Path(__file__).parent / "frontend" / "heat-ledger-cards.js"
CARDS_URL = "/heat_ledger/heat-ledger-cards.js"


async def async_register_cards(hass: HomeAssistant) -> None:
    """Register the module once; without a frontend (tests, headless installs) there is nothing to do."""
    if "frontend" not in hass.config.components:
        return
    digest = await hass.async_add_executor_job(_digest)
    await hass.http.async_register_static_paths([StaticPathConfig(CARDS_URL, str(CARDS_PATH), False)])
    # The digest makes browsers load a changed module after an update instead of a cached one.
    add_extra_js_url(hass, f"{CARDS_URL}?v={digest}")


def _digest() -> str:
    return hashlib.sha256(CARDS_PATH.read_bytes()).hexdigest()[:8]
