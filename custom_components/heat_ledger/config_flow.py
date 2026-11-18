"""Config flow: the plant entry, its options and the subentries for sources, circuits and consumers.

Every form asks for the minimum first; everything that makes the estimate more accurate sits in a collapsed
section. Section fields are stored flat in the subentry data with the keys from const.py. Reconfiguring a subentry
keeps its ID, so its counters continue.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import probatio as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_UNIT_OF_MEASUREMENT, CONF_NAME, PERCENTAGE
from homeassistant.core import HomeAssistant, callback, split_entity_id
from homeassistant.data_entry_flow import section
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_CIRCUIT,
    CONF_CONVERSION,
    CONF_DEMAND_ENTITY,
    CONF_DEMAND_TYPE,
    CONF_ENTITY,
    CONF_FACTOR,
    CONF_FACTOR_ENTITY,
    CONF_KIND,
    CONF_LOSS_KW,
    CONF_METER_ENTITY,
    CONF_PUMP_ENTITY,
    CONF_SIZE_TYPE,
    CONF_SIZE_VALUE,
    CONVERSION_COP,
    CONVERSION_DIRECT,
    CONVERSION_EFFICIENCY,
    CONVERSIONS,
    DOMAIN,
    KIND_HOT_WATER,
    KIND_RADIATOR,
    KINDS,
    SUBENTRY_CIRCUIT,
    SUBENTRY_CONSUMER,
    SUBENTRY_SOURCE,
)
from .engine import DemandMethod, SizeMethod

# Form-only keys: not stored.
SECTION_ACCURACY = "accuracy"
CONF_CHARGING_POWER_KW = "charging_power_kw"  # stored as size nominal_power in W
CONF_USE_SUGGESTION = "use_suggestion"

DEFAULT_FACTORS: Mapping[str, float] = {
    CONVERSION_DIRECT: 1.0,
    CONVERSION_EFFICIENCY: 0.9,
    CONVERSION_COP: 3.0,
}
SIZE_TYPES = (SizeMethod.DEFAULT, SizeMethod.AREA, SizeMethod.NOMINAL_POWER)
SENSOR_DEMAND_TYPES = (DemandMethod.PERCENT, DemandMethod.VALVE)
DEMAND_DOMAINS = ("climate", "sensor", "binary_sensor")
# Percent sensors on a thermostat that are not a heating demand.
NOT_DEMAND_DEVICE_CLASSES = {"humidity", "moisture", "battery", "signal_strength"}

ERROR_ENTITY_NOT_FOUND = "entity_not_found"
ERROR_WRONG_DOMAIN = "wrong_domain"
ERROR_NOT_CUMULATIVE = "not_cumulative"
ERROR_NOT_PERCENT = "not_percent"
ERROR_NOT_POSITIVE = "not_positive"
ERROR_NAME_REQUIRED = "name_required"
ERROR_SIZE_VALUE_REQUIRED = "size_value_required"
ERROR_FACTOR_ENTITY_COP_ONLY = "factor_entity_cop_only"
ERROR_SOURCE_EXISTS = "source_exists"


# --- Selectors ---------------------------------------------------------------------------------------------------


def _entity(domain: str | list[str]) -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain=domain))


def _number(unit: str | None = None) -> NumberSelector:
    config = NumberSelectorConfig(mode=NumberSelectorMode.BOX, step="any")
    if unit:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


def _select(options: tuple[str, ...], translation_key: str) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[str(o) for o in options], translation_key=translation_key, mode=SelectSelectorMode.DROPDOWN
        )
    )


def _demand_selector() -> EntitySelector:
    return EntitySelector(
        EntitySelectorConfig(
            filter=[
                {"domain": "climate"},
                {"domain": "binary_sensor"},
                {"domain": "sensor", "unit_of_measurement": PERCENTAGE},
            ]
        )
    )


def _accuracy(fields: dict[vol.Marker, Any]) -> section:
    return section(vol.Schema(fields), {"collapsed": True})


# --- Entity checks -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _EntityInfo:
    entity_id: str
    domain: str
    name: str
    unit: str | None
    state_class: str | None
    device_class: str | None
    device_id: str | None


def _entity_info(hass: HomeAssistant, entity_id_or_uuid: str) -> _EntityInfo | None:
    """What the flow needs to know about an entity, from its state or else from the registry."""
    registry = er.async_get(hass)
    entity_id = er.async_resolve_entity_id(registry, entity_id_or_uuid)
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    entry = registry.async_get(entity_id)
    if state is None and entry is None:
        return None
    attributes: Mapping[str, Any] = state.attributes if state else {}
    if entry is not None:
        unit = attributes.get(ATTR_UNIT_OF_MEASUREMENT, entry.unit_of_measurement)
        state_class = attributes.get("state_class", (entry.capabilities or {}).get("state_class"))
        device_class = attributes.get(ATTR_DEVICE_CLASS, entry.device_class or entry.original_device_class)
        name = state.name if state else (entry.name or entry.original_name or entity_id)
    else:
        assert state is not None
        unit = attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        state_class = attributes.get("state_class")
        device_class = attributes.get(ATTR_DEVICE_CLASS)
        name = state.name
    return _EntityInfo(
        entity_id=entity_id,
        domain=split_entity_id(entity_id)[0],
        name=name,
        unit=unit,
        state_class=state_class,
        device_class=device_class,
        device_id=entry.device_id if entry else None,
    )


class _Checker:
    """Collects form errors. Fields inside a section report their error on the section, as core does."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.errors: dict[str, str] = {}

    def name(self, value: str | None, field: str = CONF_NAME) -> str:
        name = (value or "").strip()
        if not name:
            self.errors[field] = ERROR_NAME_REQUIRED
        return name

    def entity(
        self, value: str | None, domains: tuple[str, ...], field: str, *, cumulative: bool = False
    ) -> _EntityInfo | None:
        if not value:
            return None
        info = _entity_info(self.hass, value)
        if info is None:
            self.errors[field] = ERROR_ENTITY_NOT_FOUND
        elif info.domain not in domains:
            self.errors[field] = ERROR_WRONG_DOMAIN
        elif cumulative and info.state_class == "measurement":
            self.errors[field] = ERROR_NOT_CUMULATIVE
        else:
            return info
        return None

    def positive(self, value: float | None, field: str) -> float | None:
        if value is None:
            return None
        if value <= 0:
            self.errors[field] = ERROR_NOT_POSITIVE
            return None
        return float(value)


def _percent_suggestion(hass: HomeAssistant, climate: _EntityInfo) -> _EntityInfo | None:
    """A sensor on the thermostat's device that reports its heating demand in %, if there is one.

    It is a better demand than hvac_action (heating yes/no). Humidity, battery and the like are skipped; with
    several candidates the first by entity ID wins.
    """
    if climate.device_id is None:
        return None
    registry = er.async_get(hass)
    for entry in sorted(er.async_entries_for_device(registry, climate.device_id), key=lambda e: e.entity_id):
        if entry.domain != "sensor" or entry.entity_id == climate.entity_id:
            continue
        info = _entity_info(hass, entry.entity_id)
        if (
            info is not None
            and info.unit == PERCENTAGE
            and info.state_class == "measurement"
            and info.device_class not in NOT_DEMAND_DEVICE_CLASSES
        ):
            return info
    return None


def _strip_empty(data: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v not in (None, "")}


# --- Main entry --------------------------------------------------------------------------------------------------


def _loss_schema() -> dict[vol.Marker, Any]:
    return {vol.Optional(CONF_LOSS_KW): _number("kW")}


class HeatLedgerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Creates the plant. Sources, circuits and consumers are added as subentries."""

    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HeatLedgerOptionsFlow:
        return HeatLedgerOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {
            SUBENTRY_SOURCE: SourceSubentryFlow,
            SUBENTRY_CIRCUIT: CircuitSubentryFlow,
            SUBENTRY_CONSUMER: ConsumerSubentryFlow,
        }

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        check = _Checker(self.hass)
        if user_input is not None:
            name = check.name(user_input.get(CONF_NAME))
            loss = check.positive(user_input.get(CONF_LOSS_KW), CONF_LOSS_KW)
            if not check.errors:
                options = {CONF_LOSS_KW: loss} if loss is not None else {}
                return self.async_create_entry(title=name, data={}, options=options)
        schema = vol.Schema({vol.Required(CONF_NAME): TextSelector(), **_loss_schema()})
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=check.errors,
        )


class HeatLedgerOptionsFlow(OptionsFlow):
    """Plant options. Changing them reloads the entry through the runtime's update listener."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        check = _Checker(self.hass)
        if user_input is not None:
            loss = check.positive(user_input.get(CONF_LOSS_KW), CONF_LOSS_KW)
            if not check.errors:
                options = {k: v for k, v in self.config_entry.options.items() if k != CONF_LOSS_KW}
                if loss is not None:
                    options[CONF_LOSS_KW] = loss
                return self.async_create_entry(data=options)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(_loss_schema()), user_input if user_input is not None else self.config_entry.options
            ),
            errors=check.errors,
        )


# --- Subentries --------------------------------------------------------------------------------------------------


class _SubentryFlow(ConfigSubentryFlow):
    """Shared by all subentry flows: the same steps create a subentry or update it in place."""

    @property
    def _reconfiguring(self) -> bool:
        return self.source == SOURCE_RECONFIGURE

    def _stored(self) -> Mapping[str, Any]:
        return self._get_reconfigure_subentry().data if self._reconfiguring else {}

    def _finish(self, title: str, data: dict[str, Any]) -> SubentryFlowResult:
        if self._reconfiguring:
            # Same subentry ID: the counters continue. The runtime reloads through its update listener.
            return self.async_update_and_abort(
                self._get_entry(), self._get_reconfigure_subentry(), title=title, data=data
            )
        return self.async_create_entry(title=title, data=data)

    def _subentries(self, subentry_type: str) -> list[tuple[str, str, Mapping[str, Any]]]:
        own = self.context.get("subentry_id") if self._reconfiguring else None
        return [
            (s.subentry_id, s.title, s.data)
            for s in self._get_entry().subentries.values()
            if s.subentry_type == subentry_type and s.subentry_id != own
        ]


class SourceSubentryFlow(_SubentryFlow):
    """A heat source: a cumulative reading, converted to heat by a factor."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self.async_step_source(user_input)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self.async_step_source(user_input)

    async def async_step_source(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        check = _Checker(self.hass)
        stored = self._stored()
        if user_input is not None:
            more = user_input.get(SECTION_ACCURACY) or {}
            conversion = user_input.get(CONF_CONVERSION, CONVERSION_DIRECT)
            info = check.entity(user_input.get(CONF_ENTITY), ("sensor",), CONF_ENTITY, cumulative=True)
            if info and any(d.get(CONF_ENTITY) == info.entity_id for _, _, d in self._subentries(SUBENTRY_SOURCE)):
                check.errors[CONF_ENTITY] = ERROR_SOURCE_EXISTS
            factor = check.positive(more.get(CONF_FACTOR), SECTION_ACCURACY)
            if (
                stored
                and conversion != stored.get(CONF_CONVERSION)
                and factor is not None
                and factor == stored.get(CONF_FACTOR)
            ):
                # The conversion changed, the factor was left as it was: it belongs to the old conversion.
                factor = None
            factor_entity = None
            if more.get(CONF_FACTOR_ENTITY):
                if conversion != CONVERSION_COP:
                    check.errors[SECTION_ACCURACY] = ERROR_FACTOR_ENTITY_COP_ONLY
                elif factor_info := check.entity(more[CONF_FACTOR_ENTITY], ("sensor",), SECTION_ACCURACY):
                    factor_entity = factor_info.entity_id
            if info and not check.errors:
                data: dict[str, Any] = {CONF_ENTITY: info.entity_id, CONF_CONVERSION: conversion}
                if conversion != CONVERSION_DIRECT:
                    data[CONF_FACTOR] = factor if factor is not None else DEFAULT_FACTORS[conversion]
                if factor_entity:
                    data[CONF_FACTOR_ENTITY] = factor_entity
                return self._finish(info.name, data)

        schema = vol.Schema(
            {
                vol.Required(CONF_ENTITY): _entity("sensor"),
                vol.Required(CONF_CONVERSION, default=CONVERSION_DIRECT): _select(CONVERSIONS, "conversion"),
                vol.Required(SECTION_ACCURACY): _accuracy(
                    {
                        vol.Optional(CONF_FACTOR): _number(),
                        vol.Optional(CONF_FACTOR_ENTITY): _entity("sensor"),
                    }
                ),
            }
        )
        if user_input is None:
            suggested: Mapping[str, Any] = {
                CONF_ENTITY: stored.get(CONF_ENTITY),
                CONF_CONVERSION: stored.get(CONF_CONVERSION, CONVERSION_DIRECT),
                SECTION_ACCURACY: _strip_empty(
                    {CONF_FACTOR: stored.get(CONF_FACTOR), CONF_FACTOR_ENTITY: stored.get(CONF_FACTOR_ENTITY)}
                ),
            }
            suggested = _strip_empty(suggested)
        else:
            suggested = user_input
        return self.async_show_form(
            step_id="source",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=check.errors,
        )


class CircuitSubentryFlow(_SubentryFlow):
    """A heating circuit: radiators behind one pump."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self.async_step_circuit(user_input)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self.async_step_circuit(user_input)

    async def async_step_circuit(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        check = _Checker(self.hass)
        if user_input is not None:
            name = check.name(user_input.get(CONF_NAME))
            pump = check.entity(user_input.get(CONF_PUMP_ENTITY), ("binary_sensor",), CONF_PUMP_ENTITY)
            if not check.errors:
                return self._finish(name, {CONF_PUMP_ENTITY: pump.entity_id} if pump else {})

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): TextSelector(),
                vol.Optional(CONF_PUMP_ENTITY): _entity("binary_sensor"),
            }
        )
        if user_input is None and self._reconfiguring:
            subentry = self._get_reconfigure_subentry()
            user_input = _strip_empty({CONF_NAME: subentry.title, **subentry.data})
        return self.async_show_form(
            step_id="circuit",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=check.errors,
        )


class ConsumerSubentryFlow(_SubentryFlow):
    """A radiator or the domestic hot water."""

    _pending: tuple[str, dict[str, Any], _EntityInfo, _EntityInfo] | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return self.async_show_menu(step_id="user", menu_options=list(KINDS))

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if self._stored().get(CONF_KIND) == KIND_HOT_WATER:
            return await self.async_step_hot_water()
        return await self.async_step_radiator()

    # Radiator

    async def async_step_radiator(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        check = _Checker(self.hass)
        circuits = self._subentries(SUBENTRY_CIRCUIT)
        if user_input is not None:
            more = user_input.get(SECTION_ACCURACY) or {}
            name = check.name(user_input.get(CONF_NAME))
            data: dict[str, Any] = {CONF_KIND: KIND_RADIATOR}
            if (circuit := user_input.get(CONF_CIRCUIT)) and circuit in {c[0] for c in circuits}:
                data[CONF_CIRCUIT] = circuit
            demand = check.entity(user_input.get(CONF_DEMAND_ENTITY), DEMAND_DOMAINS, CONF_DEMAND_ENTITY)
            if demand is None:
                data[CONF_DEMAND_TYPE] = str(DemandMethod.ALWAYS)
            else:
                data[CONF_DEMAND_ENTITY] = demand.entity_id
                if demand.domain == "climate":
                    data[CONF_DEMAND_TYPE] = str(DemandMethod.HVAC_ACTION)
                elif demand.domain == "binary_sensor":
                    data[CONF_DEMAND_TYPE] = str(DemandMethod.ON_OFF)
                elif demand.unit != PERCENTAGE:
                    check.errors[CONF_DEMAND_ENTITY] = ERROR_NOT_PERCENT
                else:
                    data[CONF_DEMAND_TYPE] = str(more.get(CONF_DEMAND_TYPE) or DemandMethod.PERCENT)
            size_type = SizeMethod(more.get(CONF_SIZE_TYPE) or SizeMethod.DEFAULT)
            data[CONF_SIZE_TYPE] = str(size_type)
            if size_type is not SizeMethod.DEFAULT:
                if more.get(CONF_SIZE_VALUE) is None:
                    check.errors[SECTION_ACCURACY] = ERROR_SIZE_VALUE_REQUIRED
                elif (size := check.positive(more[CONF_SIZE_VALUE], SECTION_ACCURACY)) is not None:
                    data[CONF_SIZE_VALUE] = size
            if meter := check.entity(more.get(CONF_METER_ENTITY), ("sensor",), SECTION_ACCURACY, cumulative=True):
                data[CONF_METER_ENTITY] = meter.entity_id
            if not check.errors:
                if demand is not None and demand.domain == "climate":
                    suggestion = _percent_suggestion(self.hass, demand)
                    if suggestion is not None:
                        self._pending = (name, data, demand, suggestion)
                        return await self.async_step_demand_suggestion()
                return self._finish(name, data)

        fields: dict[vol.Marker, Any] = {vol.Required(CONF_NAME): TextSelector()}
        if circuits:
            options = [SelectOptionDict(value=sid, label=title) for sid, title, _ in circuits]
            fields[vol.Optional(CONF_CIRCUIT)] = SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
            )
        fields[vol.Optional(CONF_DEMAND_ENTITY)] = _demand_selector()
        fields[vol.Required(SECTION_ACCURACY)] = _accuracy(
            {
                vol.Optional(CONF_DEMAND_TYPE, default=str(DemandMethod.PERCENT)): _select(
                    SENSOR_DEMAND_TYPES, "demand_type"
                ),
                vol.Optional(CONF_SIZE_TYPE, default=str(SizeMethod.DEFAULT)): _select(SIZE_TYPES, "size_type"),
                vol.Optional(CONF_SIZE_VALUE): _number(),
                vol.Optional(CONF_METER_ENTITY): _entity("sensor"),
            }
        )
        if user_input is None and self._reconfiguring:
            user_input = self._radiator_form_values(circuits)
        return self.async_show_form(
            step_id="radiator",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(fields), user_input),
            errors=check.errors,
        )

    def _radiator_form_values(self, circuits: list[tuple[str, str, Mapping[str, Any]]]) -> dict[str, Any]:
        subentry = self._get_reconfigure_subentry()
        data = subentry.data
        demand_type = data.get(CONF_DEMAND_TYPE)
        return _strip_empty(
            {
                CONF_NAME: subentry.title,
                CONF_CIRCUIT: data.get(CONF_CIRCUIT) if data.get(CONF_CIRCUIT) in {c[0] for c in circuits} else None,
                CONF_DEMAND_ENTITY: data.get(CONF_DEMAND_ENTITY),
                SECTION_ACCURACY: _strip_empty(
                    {
                        CONF_DEMAND_TYPE: demand_type if demand_type in SENSOR_DEMAND_TYPES else None,
                        CONF_SIZE_TYPE: data.get(CONF_SIZE_TYPE),
                        CONF_SIZE_VALUE: data.get(CONF_SIZE_VALUE),
                        CONF_METER_ENTITY: data.get(CONF_METER_ENTITY),
                    }
                ),
            }
        )

    async def async_step_demand_suggestion(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """The thermostat has a heating demand sensor in %: offer it instead of hvac_action."""
        assert self._pending is not None
        name, data, climate, suggestion = self._pending
        if user_input is not None:
            if user_input.get(CONF_USE_SUGGESTION, True):
                data = data | {
                    CONF_DEMAND_ENTITY: suggestion.entity_id,
                    CONF_DEMAND_TYPE: str(DemandMethod.PERCENT),
                }
            return self._finish(name, data)
        return self.async_show_form(
            step_id="demand_suggestion",
            data_schema=vol.Schema({vol.Required(CONF_USE_SUGGESTION, default=True): BooleanSelector()}),
            description_placeholders={"climate": climate.name, "sensor": suggestion.name},
        )

    # Hot water

    async def async_step_hot_water(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        check = _Checker(self.hass)
        if user_input is not None:
            more = user_input.get(SECTION_ACCURACY) or {}
            name = check.name(user_input.get(CONF_NAME))
            pump = check.entity(user_input.get(CONF_DEMAND_ENTITY), ("binary_sensor",), CONF_DEMAND_ENTITY)
            power_kw = check.positive(more.get(CONF_CHARGING_POWER_KW), SECTION_ACCURACY)
            meter = check.entity(more.get(CONF_METER_ENTITY), ("sensor",), SECTION_ACCURACY, cumulative=True)
            if pump and not check.errors:
                data: dict[str, Any] = {
                    CONF_KIND: KIND_HOT_WATER,
                    CONF_DEMAND_ENTITY: pump.entity_id,
                    CONF_DEMAND_TYPE: str(DemandMethod.ON_OFF),
                    CONF_SIZE_TYPE: str(SizeMethod.DEFAULT if power_kw is None else SizeMethod.NOMINAL_POWER),
                }
                if power_kw is not None:
                    data[CONF_SIZE_VALUE] = power_kw * 1000
                if meter:
                    data[CONF_METER_ENTITY] = meter.entity_id
                return self._finish(name, data)

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): TextSelector(),
                vol.Required(CONF_DEMAND_ENTITY): _entity("binary_sensor"),
                vol.Required(SECTION_ACCURACY): _accuracy(
                    {
                        vol.Optional(CONF_CHARGING_POWER_KW): _number("kW"),
                        vol.Optional(CONF_METER_ENTITY): _entity("sensor"),
                    }
                ),
            }
        )
        if user_input is None and self._reconfiguring:
            subentry = self._get_reconfigure_subentry()
            stored = subentry.data
            power_w = stored.get(CONF_SIZE_VALUE) if stored.get(CONF_SIZE_TYPE) == SizeMethod.NOMINAL_POWER else None
            user_input = _strip_empty(
                {
                    CONF_NAME: subentry.title,
                    CONF_DEMAND_ENTITY: stored.get(CONF_DEMAND_ENTITY),
                    SECTION_ACCURACY: _strip_empty(
                        {
                            CONF_CHARGING_POWER_KW: power_w / 1000 if power_w else None,
                            CONF_METER_ENTITY: stored.get(CONF_METER_ENTITY),
                        }
                    ),
                }
            )
        return self.async_show_form(
            step_id="hot_water",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=check.errors,
        )
