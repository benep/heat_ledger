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
from homeassistant.helpers import device_registry as dr
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
from homeassistant.util.ulid import ulid_now

from . import texts
from .const import (
    CONF_CIRCUIT,
    CONF_DEMAND_ENTITY,
    CONF_DEMAND_TYPE,
    CONF_ENTITY,
    CONF_FACTOR,
    CONF_KIND,
    CONF_LOSS_KW,
    CONF_METER_ENTITY,
    CONF_PUMP_ENTITY,
    CONF_QUANTITY,
    CONF_SIZE_TYPE,
    CONF_SIZE_VALUE,
    CONF_UNIT,
    DOMAIN,
    KIND_HOT_WATER,
    KIND_RADIATOR,
    KINDS,
    QUANTITIES,
    QUANTITY_HEAT,
    QUANTITY_INPUT,
    SUBENTRY_CIRCUIT,
    SUBENTRY_CONSUMER,
    SUBENTRY_SOURCE,
)
from .engine import DemandMethod, SizeMethod
from .model import ENERGY_TO_KWH
from .texts import async_texts

# Form-only keys: not stored.
SECTION_ACCURACY = "accuracy"
CONF_CHARGING_POWER_KW = "charging_power_kw"  # stored as size nominal_power in W
CONF_USE_SUGGESTION = "use_suggestion"

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
ERROR_SOURCE_EXISTS = "source_exists"
ERROR_NOT_ENERGY = "not_energy"
ERROR_HEAT_NEEDS_ENERGY = "heat_needs_energy_unit"
ERROR_PUMP_OR_METER_REQUIRED = "pump_or_meter_required"


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
        name = state.name if state else (entry.name or entry.original_name or _device_name(hass, entry) or entity_id)
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


def _device_name(hass: HomeAssistant, entry: er.RegistryEntry) -> str | None:
    """The name of the entity's device: an entity named after its device (a thermostat, often) has no own name."""
    device = dr.async_get(hass).async_get(entry.device_id) if entry.device_id else None
    return (device.name_by_user or device.name) if device else None


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

    def meter(self, value: str | None, field: str) -> _EntityInfo | None:
        """A heat meter: a cumulative sensor in an energy unit (a water meter in L would count as kWh)."""
        info = self.entity(value, ("sensor",), field, cumulative=True)
        if info is not None and info.unit not in ENERGY_TO_KWH:
            self.errors[field] = ERROR_NOT_ENERGY
            return None
        return info

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


# --- Heat source, shared by the first setup and the source subentry ----------------------------------------------


def _source_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_NAME): TextSelector(),
            vol.Required(CONF_ENTITY): _entity("sensor"),
            vol.Required(CONF_QUANTITY, default=QUANTITY_HEAT): _select(QUANTITIES, "quantity"),
        }
    )


def _check_source(
    check: _Checker, user_input: Mapping[str, Any], taken: set[str]
) -> tuple[str, dict[str, Any], _EntityInfo] | None:
    """Title, data and entity of the source, if it is a cumulative sensor that is not yet a source of this plant.

    Without a name, the source is named after its entity.
    """
    info = check.entity(user_input.get(CONF_ENTITY), ("sensor",), CONF_ENTITY, cumulative=True)
    if info is None:
        return None
    if info.entity_id in taken:
        check.errors[CONF_ENTITY] = ERROR_SOURCE_EXISTS
        return None
    quantity = user_input.get(CONF_QUANTITY, QUANTITY_HEAT)
    data: dict[str, Any] = {CONF_ENTITY: info.entity_id, CONF_QUANTITY: quantity}
    if quantity == QUANTITY_HEAT and info.unit not in ENERGY_TO_KWH:
        # Gas in m³ or oil in L cannot be heat; read as kWh, every counter would be off by the calorific value.
        check.errors[CONF_QUANTITY] = ERROR_HEAT_NEEDS_ENERGY
        return None
    if quantity == QUANTITY_INPUT and info.unit:
        data[CONF_UNIT] = info.unit  # the factor refers to it
    title = (user_input.get(CONF_NAME) or "").strip() or info.name
    return title, data, info


def _factor_unit(info: _EntityInfo) -> str:
    """The unit the factor refers to. Energy readings are converted to kWh first, whatever their unit."""
    return "kWh" if info.unit in ENERGY_TO_KWH else (info.unit or "")


def _factor_schema() -> vol.Schema:
    return vol.Schema({vol.Required(CONF_FACTOR): _number()})


async def _energy_dashboard_suggestion(hass: HomeAssistant, taken: set[str]) -> dict[str, Any]:
    """Form values for the first gas source of the Energy dashboard that is a sensor and not yet a source here.

    A volume (m³, L) cannot be heat, so it is suggested as energy input. An energy reading can be either.
    """
    # Imported here: the energy integration is optional, and loading it costs time only when it is needed.
    from homeassistant.components.energy.data import async_get_manager

    prefs = (await async_get_manager(hass)).data or {}
    for source in prefs.get("energy_sources", []):
        statistic = source.get("stat_energy_from")
        if source.get("type") != "gas" or not statistic or statistic in taken:
            continue
        # External statistics (a utility's import, "opower:…") are no entity and cannot be a source.
        info = _entity_info(hass, statistic)
        if info is not None and info.domain == "sensor" and info.state_class != "measurement":
            quantity = QUANTITY_HEAT if info.unit in ENERGY_TO_KWH else QUANTITY_INPUT
            return {CONF_ENTITY: info.entity_id, CONF_QUANTITY: quantity}
    return {}


class _SourceSteps:
    """The steps "source" and "factor", shared by the first setup and the source subentry flow.

    The flow using it says which entities are sources already, which source is being edited, what comes after the
    factor, and what happens with the finished source (`_source_done`).
    """

    hass: HomeAssistant
    _source_title: str
    _source_data: dict[str, Any]
    _source_unit: str
    _factor_is_last_step = True

    def _taken_sources(self) -> set[str]:
        return set()

    def _edited_source(self) -> tuple[str, Mapping[str, Any]] | None:
        """Title and data of the source being edited, if any."""
        return None

    async def _source_done(self, title: str, data: dict[str, Any]) -> Any:
        raise NotImplementedError

    async def async_step_source(self, user_input: dict[str, Any] | None = None) -> Any:
        check = _Checker(self.hass)
        taken = self._taken_sources()
        if user_input is not None and (source := _check_source(check, user_input, taken)):
            self._source_title, self._source_data, info = source
            if self._source_data[CONF_QUANTITY] == QUANTITY_HEAT:
                return await self._source_done(self._source_title, self._source_data)
            self._source_unit = _factor_unit(info)
            return await self.async_step_factor()

        if user_input is not None:
            suggested: Mapping[str, Any] = user_input
        elif edited := self._edited_source():
            title, stored = edited
            suggested = {
                CONF_NAME: title,
                CONF_ENTITY: stored.get(CONF_ENTITY),
                CONF_QUANTITY: stored.get(CONF_QUANTITY, QUANTITY_HEAT),
            }
        else:
            suggested = await _energy_dashboard_suggestion(self.hass, taken)
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="source",
            data_schema=self.add_suggested_values_to_schema(_source_schema(), suggested),  # type: ignore[attr-defined]
            errors=check.errors,
            last_step=False,
        )

    async def async_step_factor(self, user_input: dict[str, Any] | None = None) -> Any:
        check = _Checker(self.hass)
        if user_input is not None:
            factor = check.positive(user_input.get(CONF_FACTOR), CONF_FACTOR)
            if factor is not None and not check.errors:
                return await self._source_done(self._source_title, {**self._source_data, CONF_FACTOR: factor})
        edited = self._edited_source()
        if user_input is not None:
            suggested: Mapping[str, Any] = user_input
        elif edited is not None and edited[1].get(CONF_QUANTITY) == QUANTITY_INPUT:
            suggested = _strip_empty({CONF_FACTOR: edited[1].get(CONF_FACTOR)})
        else:
            suggested = {}
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="factor",
            data_schema=self.add_suggested_values_to_schema(_factor_schema(), suggested),  # type: ignore[attr-defined]
            errors=check.errors,
            description_placeholders={"name": self._source_title, "unit": self._source_unit},
            last_step=self._factor_is_last_step,
        )


# --- Main entry --------------------------------------------------------------------------------------------------


# Form-only keys of the first setup.
CONF_THERMOSTATS = "thermostats"
CONF_CIRCUIT_PUMP = "circuit_pump"
CONF_HOT_WATER_PUMP = "hot_water_pump"


def _loss_schema() -> dict[vol.Marker, Any]:
    return {vol.Optional(CONF_LOSS_KW): _number("kW")}


class HeatLedgerConfigFlow(_SourceSteps, ConfigFlow, domain=DOMAIN):
    """The first setup, guided: name, heat source, pumps, thermostats. Creates the plant with its subentries.

    Everything else (more sources, circuits, sizes, heat meters) is added or refined later through the subentries.
    """

    VERSION = 1
    MINOR_VERSION = 1

    _name: str
    _source: tuple[str, dict[str, Any]]
    _pumps: tuple[_EntityInfo | None, _EntityInfo | None]
    _factor_is_last_step = False  # the pumps follow

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

    # The steps follow the heat: source, pumps, radiators. Every step but the last shows "Next".

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        check = _Checker(self.hass)
        if user_input is not None:
            self._name = check.name(user_input.get(CONF_NAME))
            if not check.errors:
                return await self.async_step_source()
        schema = vol.Schema({vol.Required(CONF_NAME): TextSelector()})
        suggested = (
            user_input
            if user_input is not None
            else {CONF_NAME: (await async_texts(self.hass))[texts.DEFAULT_PLANT_NAME]}
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=check.errors,
            last_step=False,
        )

    async def _source_done(self, title: str, data: dict[str, Any]) -> ConfigFlowResult:
        self._source = (title, data)
        return await self.async_step_pumps()

    async def async_step_pumps(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        check = _Checker(self.hass)
        if user_input is not None:
            circuit_pump = check.entity(user_input.get(CONF_CIRCUIT_PUMP), ("binary_sensor",), CONF_CIRCUIT_PUMP)
            hot_water_pump = check.entity(user_input.get(CONF_HOT_WATER_PUMP), ("binary_sensor",), CONF_HOT_WATER_PUMP)
            if not check.errors:
                self._pumps = (circuit_pump, hot_water_pump)
                return await self.async_step_thermostats()
        schema = vol.Schema(
            {
                vol.Optional(CONF_CIRCUIT_PUMP): _entity("binary_sensor"),
                vol.Optional(CONF_HOT_WATER_PUMP): _entity("binary_sensor"),
            }
        )
        return self.async_show_form(
            step_id="pumps",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=check.errors,
            last_step=False,
        )

    async def async_step_thermostats(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        check = _Checker(self.hass)
        if user_input is not None:
            thermostats = [
                info
                for entity_id in dict.fromkeys(user_input.get(CONF_THERMOSTATS) or [])
                if (info := check.entity(entity_id, ("climate",), CONF_THERMOSTATS))
            ]
            if not check.errors:
                return self.async_create_entry(
                    title=self._name,
                    data={},
                    options={},
                    subentries=self._subentries(await async_texts(self.hass), thermostats, *self._pumps),
                )
        schema = vol.Schema(
            {vol.Optional(CONF_THERMOSTATS): EntitySelector(EntitySelectorConfig(domain="climate", multiple=True))}
        )
        return self.async_show_form(
            step_id="thermostats",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=check.errors,
            last_step=True,
        )

    def _subentries(
        self,
        names: Mapping[str, str],
        thermostats: list[_EntityInfo],
        circuit_pump: _EntityInfo | None,
        hot_water_pump: _EntityInfo | None,
    ) -> list[dict[str, Any]]:
        """The subentries of the first setup. A circuit only with a pump: without one it changes nothing."""
        title, data = self._source
        subentries: list[dict[str, Any]] = [_subentry(SUBENTRY_SOURCE, title, data)]
        circuit_id = None
        if circuit_pump is not None:
            # Home Assistant takes a given subentry ID as it is, so the radiators can refer to their circuit.
            circuit_id = ulid_now()
            circuit = _subentry(SUBENTRY_CIRCUIT, names[texts.CIRCUIT], {CONF_PUMP_ENTITY: circuit_pump.entity_id})
            subentries.append(circuit | {"subentry_id": circuit_id})
        for climate in thermostats:
            # The heating demand in % if the thermostat has one, as the radiator flow suggests it.
            demand = _percent_suggestion(self.hass, climate) or climate
            radiator = _radiator_data(circuit_id, demand.entity_id, _demand_method(demand))
            subentries.append(_subentry(SUBENTRY_CONSUMER, climate.name, radiator))
        if hot_water_pump is not None:
            hot_water = _hot_water_data(hot_water_pump.entity_id)
            subentries.append(_subentry(SUBENTRY_CONSUMER, names[texts.DEFAULT_HOT_WATER_NAME], hot_water))
        return subentries


def _subentry(subentry_type: str, title: str, data: Mapping[str, Any]) -> dict[str, Any]:
    return {"subentry_type": subentry_type, "title": title, "data": dict(data), "unique_id": None}


# --- Consumer data, shared by the first setup and the consumer subentry ----------------------------------------


def _demand_method(demand: _EntityInfo) -> DemandMethod:
    """How a demand entity is read: a thermostat by hvac_action, a binary sensor on/off, a sensor in %."""
    if demand.domain == "climate":
        return DemandMethod.HVAC_ACTION
    if demand.domain == "binary_sensor":
        return DemandMethod.ON_OFF
    return DemandMethod.PERCENT


def _radiator_data(
    circuit_id: str | None,
    demand_entity: str | None,
    demand_method: DemandMethod,
    size: SizeMethod = SizeMethod.DEFAULT,
    size_value: float | None = None,
    meter_entity: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {CONF_KIND: KIND_RADIATOR}
    if circuit_id is not None:
        data[CONF_CIRCUIT] = circuit_id
    if demand_entity is None:
        data[CONF_DEMAND_TYPE] = str(DemandMethod.ALWAYS)
    else:
        data[CONF_DEMAND_ENTITY] = demand_entity
        data[CONF_DEMAND_TYPE] = str(demand_method)
    data[CONF_SIZE_TYPE] = str(size)
    if size is not SizeMethod.DEFAULT and size_value is not None:
        data[CONF_SIZE_VALUE] = size_value
    if meter_entity is not None:
        data[CONF_METER_ENTITY] = meter_entity
    return data


def _hot_water_data(
    pump_entity: str | None, power_kw: float | None = None, meter_entity: str | None = None
) -> dict[str, Any]:
    """Without a pump only with a heat meter: the meter measures, the pump would only weigh a share of the split."""
    data: dict[str, Any] = {CONF_KIND: KIND_HOT_WATER}
    if pump_entity is None:
        data[CONF_DEMAND_TYPE] = str(DemandMethod.ALWAYS)
    else:
        data[CONF_DEMAND_ENTITY] = pump_entity
        data[CONF_DEMAND_TYPE] = str(DemandMethod.ON_OFF)
    data[CONF_SIZE_TYPE] = str(SizeMethod.DEFAULT if power_kw is None else SizeMethod.NOMINAL_POWER)
    if power_kw is not None:
        data[CONF_SIZE_VALUE] = power_kw * 1000
    if meter_entity is not None:
        data[CONF_METER_ENTITY] = meter_entity
    return data


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


class SourceSubentryFlow(_SourceSteps, _SubentryFlow):
    """A heat source: a cumulative reading of heat, or of energy input with a factor to heat (second step)."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self.async_step_source(user_input)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self.async_step_source(user_input)

    def _taken_sources(self) -> set[str]:
        return {d.get(CONF_ENTITY) for _, _, d in self._subentries(SUBENTRY_SOURCE)}

    def _edited_source(self) -> tuple[str, Mapping[str, Any]] | None:
        return (self._get_reconfigure_subentry().title, self._stored()) if self._reconfiguring else None

    async def _source_done(self, title: str, data: dict[str, Any]) -> SubentryFlowResult:
        return self._finish(title, data)


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
            circuit = user_input.get(CONF_CIRCUIT)
            demand = check.entity(user_input.get(CONF_DEMAND_ENTITY), DEMAND_DOMAINS, CONF_DEMAND_ENTITY)
            # Named after its thermostat by default, as in the first setup.
            name = (user_input.get(CONF_NAME) or "").strip() or (demand.name if demand is not None else "")
            if not name:
                check.errors[CONF_NAME] = ERROR_NAME_REQUIRED
            method = DemandMethod.ALWAYS if demand is None else _demand_method(demand)
            if demand is not None and method is DemandMethod.PERCENT:
                if demand.unit != PERCENTAGE:
                    check.errors[CONF_DEMAND_ENTITY] = ERROR_NOT_PERCENT
                method = DemandMethod(more.get(CONF_DEMAND_TYPE) or DemandMethod.PERCENT)
            size_type = SizeMethod(more.get(CONF_SIZE_TYPE) or SizeMethod.DEFAULT)
            size = None
            if size_type is not SizeMethod.DEFAULT:
                if more.get(CONF_SIZE_VALUE) is None:
                    check.errors[SECTION_ACCURACY] = ERROR_SIZE_VALUE_REQUIRED
                else:
                    size = check.positive(more[CONF_SIZE_VALUE], SECTION_ACCURACY)
            meter = check.meter(more.get(CONF_METER_ENTITY), SECTION_ACCURACY)
            data = _radiator_data(
                circuit if circuit in {c[0] for c in circuits} else None,
                demand.entity_id if demand is not None else None,
                method,
                size_type,
                size,
                meter.entity_id if meter is not None else None,
            )
            if not check.errors:
                if demand is not None and demand.domain == "climate":
                    suggestion = _percent_suggestion(self.hass, demand)
                    if suggestion is not None:
                        self._pending = (name, data, demand, suggestion)
                        return await self.async_step_demand_suggestion()
                return self._finish(name, data)

        fields: dict[vol.Marker, Any] = {
            vol.Optional(CONF_DEMAND_ENTITY): _demand_selector(),
            vol.Optional(CONF_NAME): TextSelector(),
        }
        if circuits:
            options = [SelectOptionDict(value=sid, label=title) for sid, title, _ in circuits]
            fields[vol.Optional(CONF_CIRCUIT)] = SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
            )
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
            meter = check.meter(more.get(CONF_METER_ENTITY), SECTION_ACCURACY)
            if pump is None and meter is None and not check.errors:
                check.errors[CONF_DEMAND_ENTITY] = ERROR_PUMP_OR_METER_REQUIRED
            if not check.errors:
                data = _hot_water_data(
                    pump.entity_id if pump is not None else None,
                    power_kw,
                    meter.entity_id if meter is not None else None,
                )
                return self._finish(name, data)

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): TextSelector(),
                vol.Optional(CONF_DEMAND_ENTITY): _entity("binary_sensor"),
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
        elif user_input is None:
            user_input = {CONF_NAME: (await async_texts(self.hass))[texts.DEFAULT_HOT_WATER_NAME]}
        return self.async_show_form(
            step_id="hot_water",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=check.errors,
        )
