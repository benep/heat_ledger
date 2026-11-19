"""Static description of a heating plant: what the ledger needs to know, independent of Home Assistant.

Inputs are referred to by key (in Home Assistant: the entity ID). Readings for these keys are passed to the
ledger at runtime, already converted to plain values (see ledger.Readings).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

DEFAULT_RADIATOR_POWER_W = 1000.0
DEFAULT_HOT_WATER_POWER_W = 10000.0
DEFAULT_SPECIFIC_POWER_W_PER_M2 = 60.0


class SizeMethod(StrEnum):
    """How the size of a consumer is known, rough to precise."""

    DEFAULT = "default"
    AREA = "area"
    CALCULATED = "calculated"  # nominal power from type and dimensions
    NOMINAL_POWER = "nominal_power"


class DemandMethod(StrEnum):
    """Where the demand of a consumer comes from, rough to precise."""

    ALWAYS = "always"
    HVAC_ACTION = "hvac_action"  # reading: bool, heating or not
    ON_OFF = "on_off"  # reading: bool, e.g. an actuator or the hot water pump
    PERCENT = "percent"  # reading: 0..1, heating demand of a controller
    VALVE = "valve"  # reading: 0..1, valve position


class ActiveMethod(StrEnum):
    """Whether a circuit carries heat, rough to precise."""

    ALWAYS = "always"
    PUMP = "pump"  # reading: bool


@dataclass(frozen=True)
class Size:
    method: SizeMethod = SizeMethod.DEFAULT
    value: float | None = None  # W for nominal power and calculated, m² for area, W default for DEFAULT

    @property
    def watts(self) -> float:
        if self.value is None:
            return DEFAULT_RADIATOR_POWER_W
        if self.method is SizeMethod.DEFAULT:
            return self.value
        if self.method is SizeMethod.AREA:
            return self.value * DEFAULT_SPECIFIC_POWER_W_PER_M2
        return self.value


@dataclass(frozen=True)
class Demand:
    method: DemandMethod = DemandMethod.ALWAYS
    key: str | None = None


@dataclass(frozen=True)
class SourceSpec:
    """A cumulative energy reading; its increase times the factor is heat."""

    id: str
    key: str
    factor: float = 1.0  # kWh heat per unit; 1.0 when the reading already is heat
    unit: str | None = None  # unit the reading is taken in; with the key, what identifies the meter


@dataclass(frozen=True)
class CircuitSpec:
    id: str
    active: ActiveMethod = ActiveMethod.ALWAYS
    pump_key: str | None = None


@dataclass(frozen=True)
class ConsumerSpec:
    id: str
    circuit_id: str | None = None
    size: Size = field(default_factory=Size)
    demand: Demand = field(default_factory=Demand)
    meter_key: str | None = None  # cumulative heat in kWh: measured, takes no part in the split
    hot_water: bool = False  # size is a charging power, floor area makes no sense


@dataclass(frozen=True)
class PlantSpec:
    sources: tuple[SourceSpec, ...]
    circuits: tuple[CircuitSpec, ...] = ()
    consumers: tuple[ConsumerSpec, ...] = ()
    loss_kw: float = 0.0  # fixed loss rate, taken from the account every interval
    release_tau_s: float = 0.0  # 0: release the whole account every interval

    def __post_init__(self) -> None:
        ids = [s.id for s in self.sources] + [c.id for c in self.circuits] + [c.id for c in self.consumers]
        if len(ids) != len(set(ids)):
            raise ValueError("Source, circuit and consumer IDs must be unique")
        circuits = {c.id for c in self.circuits}
        for consumer in self.consumers:
            if consumer.circuit_id is not None and consumer.circuit_id not in circuits:
                raise ValueError(f"Consumer {consumer.id} refers to unknown circuit {consumer.circuit_id}")
            if consumer.demand.method is not DemandMethod.ALWAYS and consumer.demand.key is None:
                raise ValueError(f"Consumer {consumer.id} needs a key for demand method {consumer.demand.method}")
        for circuit in self.circuits:
            if circuit.active is ActiveMethod.PUMP and circuit.pump_key is None:
                raise ValueError(f"Circuit {circuit.id} needs a pump key")

    def circuit(self, circuit_id: str | None) -> CircuitSpec | None:
        return next((c for c in self.circuits if c.id == circuit_id), None)
