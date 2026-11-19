"""Energy counters per consumer and circuit, heat per converted source, and the account."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HeatLedgerConfigEntry
from .const import (
    ATTR_BASIS,
    ATTR_BLOCKED_BY,
    ATTR_BY_SOURCE,
    ATTR_LEVEL,
    ATTR_NEXT_IMPROVEMENT,
    CONF_DEMAND_ENTITY,
    CONF_ENTITY,
    CONF_KIND,
    CONF_METER_ENTITY,
    CONF_PUMP_ENTITY,
    CONF_QUANTITY,
    DOMAIN,
    KIND_HOT_WATER,
    QUANTITY_INPUT,
    SUBENTRY_CIRCUIT,
    SUBENTRY_CONSUMER,
    SUBENTRY_SOURCE,
    TRANSLATION_ACCOUNT,
    TRANSLATION_HEAT,
    TRANSLATION_SOURCE_HEAT,
)
from .engine import Basis, Level, consumer_basis
from .engine.basis import ACTIVE_ORDER, DEMAND_ORDER, SIZE_ORDER
from .model import localized
from .runtime import HeatLedgerRuntime

# Restored values below the ledger by less than this are rounding, not a lost state.
RESTORE_TOLERANCE_KWH = 1e-6

LEVEL_ORDER = [Level.ESTIMATED, Level.CALCULATED, Level.MEASURED]
METHOD_ORDER = {"size": SIZE_ORDER, "demand": DEMAND_ORDER, "circuit": ACTIVE_ORDER}


async def async_setup_entry(
    hass: HomeAssistant, entry: HeatLedgerConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    runtime = entry.runtime_data
    titles = {s.subentry_id: s.title for s in entry.subentries.values() if s.subentry_type == SUBENTRY_SOURCE}
    # Every device is suggested the area of its input (thermostat, pump, meter), so Home Assistant's dialog after
    # the setup comes prefilled. The plant goes where its first source is or, if that has no area (a template),
    # where the pumps are: usually the boiler room. A source without an area of its own goes there too.
    subentries = list(entry.subentries.values())
    areas = {s.subentry_id: _input_area(hass, s) for s in subentries}
    by_type = [SUBENTRY_SOURCE, SUBENTRY_CIRCUIT, SUBENTRY_CONSUMER]
    plant_area = next(
        (
            areas[s.subentry_id]
            for kind in by_type
            for s in subentries
            if s.subentry_type == kind
            and areas[s.subentry_id]
            and (kind != SUBENTRY_CONSUMER or s.data.get(CONF_KIND) == KIND_HOT_WATER)
        ),
        None,
    )
    language = hass.config.language
    for subentry in subentries:
        area = areas[subentry.subentry_id]
        entity: SensorEntity | None = None
        if subentry.subentry_type == SUBENTRY_CONSUMER:
            model = DEVICE_HOT_WATER if subentry.data.get(CONF_KIND) == KIND_HOT_WATER else DEVICE_RADIATOR
            entity = ConsumerHeatSensor(runtime, subentry, titles, _device(subentry, area, model, language))
        elif subentry.subentry_type == SUBENTRY_CIRCUIT:
            entity = CircuitHeatSensor(runtime, subentry, titles, _device(subentry, area, DEVICE_CIRCUIT, language))
        elif subentry.subentry_type == SUBENTRY_SOURCE and subentry.data.get(CONF_QUANTITY) == QUANTITY_INPUT:
            # A source that counts heat needs no sensor of its own: its input entity already is the heat.
            device = _device(subentry, area or plant_area, DEVICE_SOURCE, language)
            entity = SourceHeatSensor(runtime, subentry, device)
        if entity is not None:
            async_add_entities([entity], config_subentry_id=subentry.subentry_id)

    plant = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        suggested_area=plant_area,
        model=localized(language, DEVICE_PLANT),
        manufacturer=MANUFACTURER,
        entry_type=DeviceEntryType.SERVICE,
    )
    async_add_entities([AccountSensor(runtime, entry, plant)])


class _LedgerSensor(SensorEntity):
    """Updated by the runtime after every interval."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(self, runtime: HeatLedgerRuntime) -> None:
        self.runtime = runtime

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.runtime.async_add_listener(self.async_write_ha_state))


# What kind of device it is, shown next to the icon wherever Home Assistant lists devices (the model), with the
# integration below it (the manufacturer). Device registry fields are not translated, so the language is chosen
# when the device is set up; it follows a change of the language with the next start.
MANUFACTURER = "Heat Ledger"
DEVICE_PLANT = {"en": "Heating system", "de": "Heizungsanlage"}
DEVICE_SOURCE = {"en": "Heat source", "de": "Wärmequelle"}
DEVICE_CIRCUIT = {"en": "Heating circuit", "de": "Heizkreis"}
DEVICE_RADIATOR = {"en": "Room heating", "de": "Raumheizung"}  # radiators, underfloor heating
DEVICE_HOT_WATER = {"en": "Hot water heating", "de": "Warmwasserbereitung"}


def _device(subentry: ConfigSubentry, area: str | None, model: Mapping[str, str], language: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, subentry.subentry_id)},
        name=subentry.title,
        suggested_area=area,
        model=localized(language, model),
        manufacturer=MANUFACTURER,
        entry_type=DeviceEntryType.SERVICE,
    )


def _input_area(hass: HomeAssistant, subentry: ConfigSubentry) -> str | None:
    """Name of the area of the subentry's input entity (or of its device), if it has one.

    Only used when the device is created; moving it to another area later is up to the user.
    """
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    areas = ar.async_get(hass)
    for key in (CONF_DEMAND_ENTITY, CONF_METER_ENTITY, CONF_PUMP_ENTITY, CONF_ENTITY):
        entity_id = subentry.data.get(key)
        entry = entities.async_get(entity_id) if entity_id else None
        if entry is None:
            continue
        area_id = entry.area_id
        if area_id is None and entry.device_id and (device := devices.async_get(entry.device_id)):
            area_id = device.area_id
        if area_id and (area := areas.async_get_area(area_id)):
            return area.name
    return None


class _CounterSensor(_LedgerSensor, RestoreSensor):
    """A heat counter for the Energy dashboard: never lower than the value it had before a restart.

    The ledger normally restores the same value from its own store. If that store was lost or is older than the
    restored state, the difference is kept as an offset, so the counter continues from where it was.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, runtime: HeatLedgerRuntime, subentry: ConfigSubentry, key: str, device: DeviceInfo) -> None:
        super().__init__(runtime)
        self._id = subentry.subentry_id
        self._offset = 0.0
        self._attr_unique_id = f"{subentry.subentry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = device

    def _total(self) -> float:
        raise NotImplementedError

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is None or last.native_unit_of_measurement != UnitOfEnergy.KILO_WATT_HOUR:
            return
        try:
            restored = float(last.native_value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if restored > self._total() + RESTORE_TOLERANCE_KWH:
            self._offset = restored - self._total()

    @property
    def native_value(self) -> float:
        return self._total() + self._offset


class _SplitSensor(_CounterSensor):
    """Counter of a consumer or a circuit, with how it is computed and where its heat came from."""

    def __init__(
        self,
        runtime: HeatLedgerRuntime,
        subentry: ConfigSubentry,
        titles: Mapping[str, str],
        device: DeviceInfo,
    ) -> None:
        super().__init__(runtime, subentry, TRANSLATION_HEAT, device)
        self._titles = titles

    def _basis(self) -> Basis:
        raise NotImplementedError

    def _by_source(self) -> dict[str, float]:
        raise NotImplementedError

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        basis = self._basis()
        by_source: dict[str, float] = {}
        for source_id, kwh in self._by_source().items():
            title = self._titles.get(source_id, source_id)
            by_source[title] = by_source.get(title, 0.0) + kwh
        return {
            ATTR_LEVEL: str(basis.level),
            ATTR_BASIS: basis.methods,
            ATTR_NEXT_IMPROVEMENT: basis.next_improvement,
            ATTR_BY_SOURCE: {title: round(kwh, 3) for title, kwh in by_source.items()},
        }


class ConsumerHeatSensor(_SplitSensor):
    """In the room of its thermostat: the Energy dashboard groups consumers by floor and area."""

    def _total(self) -> float:
        return self.runtime.ledger.consumer_total(self._id)

    def _basis(self) -> Basis:
        plant = self.runtime.plant
        consumer = next(c for c in plant.consumers if c.id == self._id)
        return consumer_basis(plant, consumer)

    def _by_source(self) -> dict[str, float]:
        return dict(self.runtime.ledger.consumed[self._id])


class CircuitHeatSensor(_SplitSensor):
    """The sum of its radiators; the parent of their counters in the Energy dashboard."""

    def _total(self) -> float:
        return self.runtime.ledger.circuit_total(self._id)

    def _basis(self) -> Basis:
        plant = self.runtime.plant
        return circuit_basis([consumer_basis(plant, c) for c in plant.consumers if c.circuit_id == self._id])

    def _by_source(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for consumer in self.runtime.plant.consumers:
            if consumer.circuit_id == self._id:
                for source_id, kwh in self.runtime.ledger.consumed[consumer.id].items():
                    totals[source_id] = totals.get(source_id, 0.0) + kwh
        return totals


def circuit_basis(radiators: list[Basis]) -> Basis:
    """A sum is as good as its roughest part: the lowest level and the roughest method per factor.

    The next improvement is the one most of its radiators would profit from.
    """
    if not radiators:
        return Basis(Level.ESTIMATED, {}, None)
    level = min((b.level for b in radiators), key=LEVEL_ORDER.index)
    split = [b for b in radiators if b.level is not Level.MEASURED]
    if not split:
        methods = {"heat": "meter"}
    else:
        methods = {}
        for factor, order in METHOD_ORDER.items():
            used = [b.methods[factor] for b in split if factor in b.methods]
            if used:
                methods[factor] = min(used, key=lambda m, order=order: _rank(order, m))
        if len(split) < len(radiators):
            methods["heat"] = "split, meter"
    hints = Counter(b.next_improvement for b in radiators if b.next_improvement)
    improvement = hints.most_common(1)[0][0] if hints else None
    return Basis(level, methods, improvement)


def _rank(order: list, method: str) -> int:
    """Position in the order; on/off ranks with hvac_action, as in engine.basis."""
    values = [str(m) for m in order]
    if method == "on_off":
        method = "hvac_action"
    return values.index(method) if method in values else 0


class SourceHeatSensor(_CounterSensor):
    """Heat of a source that counts energy input: its reading times the factor."""

    def __init__(self, runtime: HeatLedgerRuntime, subentry: ConfigSubentry, device: DeviceInfo) -> None:
        super().__init__(runtime, subentry, TRANSLATION_SOURCE_HEAT, device)

    def _total(self) -> float:
        return self.runtime.ledger.source_heat[self._id]


class AccountSensor(_LedgerSensor):
    """Heat that has entered but is not assigned yet. Negative when a heat meter took more than the sources gave."""

    _attr_device_class = SensorDeviceClass.ENERGY_STORAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = TRANSLATION_ACCOUNT

    def __init__(self, runtime: HeatLedgerRuntime, entry: HeatLedgerConfigEntry, device: DeviceInfo) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{entry.entry_id}_{TRANSLATION_ACCOUNT}"
        self._attr_device_info = device

    @property
    def native_value(self) -> float:
        return self.runtime.ledger.account_total

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {ATTR_BLOCKED_BY: list(self.runtime.ledger.blocked_by)}
