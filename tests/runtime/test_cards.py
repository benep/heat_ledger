"""The gas device cards: served and loaded with the frontend, skipped without it."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.heat_ledger.cards import CARDS_PATH, CARDS_URL, WS_CONSUMERS, async_register_cards, ws_consumers

from .common import entity_id, setup


async def test_registers_module_with_frontend(hass: HomeAssistant) -> None:
    hass.config.components.add("frontend")
    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    with patch("custom_components.heat_ledger.cards.add_extra_js_url") as add_url:
        await async_register_cards(hass)

    (paths,) = hass.http.async_register_static_paths.call_args.args
    assert [(p.url_path, p.path, p.cache_headers) for p in paths] == [(CARDS_URL, str(CARDS_PATH), False)]
    url = add_url.call_args.args[1]
    assert url.startswith(f"{CARDS_URL}?v=") and len(url) == len(CARDS_URL) + 11
    assert WS_CONSUMERS in hass.data[websocket_api.DOMAIN]


async def test_skipped_without_frontend(hass: HomeAssistant) -> None:
    with patch("custom_components.heat_ledger.cards.add_extra_js_url") as add_url:
        await async_register_cards(hass)
    add_url.assert_not_called()


def test_module_defines_both_cards() -> None:
    source = CARDS_PATH.read_text(encoding="utf-8")
    for card in ("heat-ledger-gas-devices-detail-graph", "heat-ledger-gas-devices-graph"):
        assert f'type: "{card}"' in source


async def test_consumers_for_the_cards(hass: HomeAssistant, entry: MockConfigEntry, inputs, hass_ws_client) -> None:
    """Room heating and hot water with their counters, without circuits, sources and disabled counters."""
    assert await async_setup_component(hass, "websocket_api", {})
    websocket_api.async_register_command(hass, ws_consumers)
    await setup(hass, entry)
    er.async_get(hass).async_update_entity(entity_id(hass, "bath_heat"), disabled_by=er.RegistryEntryDisabler.USER)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "heat_ledger/consumers"})
    response = await client.receive_json()

    assert response["success"]
    assert response["result"] == [
        {"entity_id": entity_id(hass, "living_heat"), "name": "Living room"},
        {"entity_id": entity_id(hass, "hot_water_heat"), "name": "Hot water"},
    ]


async def test_no_consumers_without_a_loaded_plant(hass: HomeAssistant, hass_ws_client) -> None:
    assert await async_setup_component(hass, "websocket_api", {})
    websocket_api.async_register_command(hass, ws_consumers)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "heat_ledger/consumers"})

    assert (await client.receive_json())["result"] == []
