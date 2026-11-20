"""The gas device cards: served and loaded with the frontend, skipped without it."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.heat_ledger.cards import CARDS_PATH, CARDS_URL, async_register_cards


async def test_registers_module_with_frontend(hass: HomeAssistant) -> None:
    hass.config.components.add("frontend")
    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    with patch("custom_components.heat_ledger.cards.add_extra_js_url") as add_url:
        await async_register_cards(hass)

    (paths,) = hass.http.async_register_static_paths.call_args.args
    assert [(p.url_path, p.path, p.cache_headers) for p in paths] == [(CARDS_URL, str(CARDS_PATH), False)]
    url = add_url.call_args.args[1]
    assert url.startswith(f"{CARDS_URL}?v=") and len(url) == len(CARDS_URL) + 11


async def test_skipped_without_frontend(hass: HomeAssistant) -> None:
    with patch("custom_components.heat_ledger.cards.add_extra_js_url") as add_url:
        await async_register_cards(hass)
    add_url.assert_not_called()


def test_module_defines_both_cards() -> None:
    source = CARDS_PATH.read_text(encoding="utf-8")
    for card in ("heat-ledger-gas-devices-detail-graph", "heat-ledger-gas-devices-graph"):
        assert f'type: "{card}"' in source
