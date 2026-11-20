"""Texts the integration writes into Home Assistant itself: device models, default names, and the labels in the heat
by source attribute.

Device registry fields, subentry titles and attribute values are not translated by Home Assistant, so they are taken
from the "device" section of the translations, in the language of Home Assistant (English for keys a language lacks).
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN

# Keys in the "device" section of strings.json.
PLANT = "plant"  # model of the plant's device
SOURCE = "source"
CIRCUIT = "circuit"  # also the default name of the circuit the first setup creates
RADIATOR = "radiator"
HOT_WATER = "hot_water"
DEFAULT_PLANT_NAME = "default_plant_name"
DEFAULT_HOT_WATER_NAME = "default_hot_water_name"
REMOVED_SOURCE = "removed_source"  # where a source removed since shows in the heat by source
UNATTRIBUTED = "unattributed"  # heat a meter measured that no source has delivered (yet, or written off)


async def async_texts(hass: HomeAssistant) -> dict[str, str]:
    """Key → text, for the keys above."""
    translations = await async_get_translations(hass, hass.config.language, "device", {DOMAIN})
    prefix, suffix = f"component.{DOMAIN}.device.", ".name"
    return {
        key[len(prefix) : -len(suffix)]: text
        for key, text in translations.items()
        if key.startswith(prefix) and key.endswith(suffix)
    }
