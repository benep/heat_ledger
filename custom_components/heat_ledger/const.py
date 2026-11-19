"""Constants shared by the config flow, the model and the runtime.

The keys below are the stored format of the config entry and its subentries. Changing one needs a migration.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "heat_ledger"

INTERVAL: Final = timedelta(minutes=5)
STORAGE_VERSION: Final = 1
STORAGE_SAVE_DELAY: Final = 30  # seconds; the state is also saved on shutdown

# Main entry (the plant), in options so it can change without removing the entry.
CONF_LOSS_KW: Final = "loss_kw"

# Subentry types
SUBENTRY_SOURCE: Final = "source"
SUBENTRY_CIRCUIT: Final = "circuit"
SUBENTRY_CONSUMER: Final = "consumer"

# Source subentry
CONF_ENTITY: Final = "entity_id"  # cumulative reading; energy units are converted to kWh
CONF_CONVERSION: Final = "conversion"
CONVERSION_DIRECT: Final = "direct"  # the reading is heat
CONVERSION_EFFICIENCY: Final = "efficiency"  # fuel or electricity × factor
CONVERSION_COP: Final = "cop"  # electricity × COP (fixed factor, or an entity)
CONVERSIONS: Final = (CONVERSION_DIRECT, CONVERSION_EFFICIENCY, CONVERSION_COP)
CONF_FACTOR: Final = "factor"  # kWh heat per unit of the reading (per kWh for energy readings)
CONF_FACTOR_ENTITY: Final = "factor_entity_id"  # factor from an entity (COP, calorific value × efficiency), the fixed factor applies while it is unavailable

# Circuit subentry
CONF_PUMP_ENTITY: Final = "pump_entity_id"  # binary sensor, optional: without it the circuit is always active

# Consumer subentry
CONF_KIND: Final = "kind"
KIND_RADIATOR: Final = "radiator"
KIND_HOT_WATER: Final = "hot_water"
KINDS: Final = (KIND_RADIATOR, KIND_HOT_WATER)
CONF_CIRCUIT: Final = "circuit"  # subentry ID of the circuit, radiators only
CONF_DEMAND_ENTITY: Final = "demand_entity_id"
CONF_DEMAND_TYPE: Final = "demand_type"  # engine.DemandMethod value; "always" without an entity
CONF_SIZE_TYPE: Final = "size_type"  # engine.SizeMethod value
CONF_SIZE_VALUE: Final = "size_value"  # W for nominal power, m² for area
CONF_METER_ENTITY: Final = "meter_entity_id"  # heat meter: measured, replaces the estimate

# Entity translation keys (strings.json: entity.sensor.<key>)
TRANSLATION_HEAT: Final = "heat"  # energy counter of a consumer or a circuit
TRANSLATION_SOURCE_HEAT: Final = "source_heat"  # heat of a source after conversion
TRANSLATION_ACCOUNT: Final = "account"  # diagnostic: heat not yet assigned

# State attributes of the counters
ATTR_LEVEL: Final = "level"  # engine.Level value
ATTR_BASIS: Final = "basis"  # dict factor -> method
ATTR_NEXT_IMPROVEMENT: Final = "next_improvement"  # e.g. "demand_percent", or None
ATTR_BY_SOURCE: Final = "by_source"  # dict source name -> kWh
ATTR_BLOCKED_BY: Final = "blocked_by"  # account sensor: entity IDs that stop the split right now
