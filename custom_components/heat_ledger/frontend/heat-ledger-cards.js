// Device cards for the gas tab, which Home Assistant only has on the electricity tab so far:
//
//   custom:heat-ledger-gas-devices-detail-graph   like "Individual devices detail usage" (energy-devices-detail-graph)
//   custom:heat-ledger-gas-devices-graph          like "Individual devices total usage" (energy-devices-graph)
//
// Both extend the electricity cards and hand them the gas devices of the Energy dashboard
// (device_consumption_gas) instead of the electricity devices. The energy collection loads their statistics anyway,
// for the gas Sankey. If several devices share a name, their area is shown instead, as the Sankey groups by area.
// They rely on internal names of the frontend (_data, _getStatistics), so check them after a frontend update.

const CARDS = [
  {
    type: "heat-ledger-gas-devices-detail-graph",
    base: "energy-devices-detail-graph",
    baseTag: "hui-energy-devices-detail-graph-card",
    name: "Heat Ledger: gas devices over time",
    description: "Heat per room and hot water over time, like the device chart of the electricity tab.",
  },
  {
    type: "heat-ledger-gas-devices-graph",
    base: "energy-devices-graph",
    baseTag: "hui-energy-devices-graph-card",
    name: "Heat Ledger: gas devices in total",
    description: "Heat per room and hot water in total, as bars or a donut.",
  },
];

function withGasDevices(hass, data) {
  if (!data?.prefs) return data;
  const devices = data.prefs.device_consumption_gas || [];
  const count = {};
  for (const d of devices) count[d.name] = (count[d.name] || 0) + 1;
  const areaName = (entityId) => {
    const entity = hass?.entities?.[entityId];
    const areaId = entity?.area_id || hass?.devices?.[entity?.device_id]?.area_id;
    return hass?.areas?.[areaId]?.name;
  };
  const named = devices.map((d) =>
    d.name && count[d.name] > 1 ? { ...d, name: areaName(d.stat_consumption) || d.name } : d,
  );
  return { ...data, prefs: { ...data.prefs, device_consumption: named } };
}

async function loadBase(type, tag) {
  // The frontend sets loadCardHelpers while it starts; this module may run before that.
  while (!window.loadCardHelpers) await new Promise((resolve) => setTimeout(resolve, 100));
  const helpers = await window.loadCardHelpers();
  helpers.createCardElement({ type });
  await customElements.whenDefined(tag);
  return customElements.get(tag);
}

function stubConfig(type) {
  return () => ({ type: `custom:${type}` });
}

async function define() {
  const [detail, devices] = CARDS;
  if (!customElements.get(detail.type)) {
    const Base = await loadBase(detail.base, detail.baseTag);
    class GasDevicesDetailGraph extends Base {
      static getStubConfig = stubConfig(detail.type);

      get _data() {
        return super._data;
      }

      set _data(value) {
        super._data = withGasDevices(this.hass, value);
      }
    }
    customElements.define(detail.type, GasDevicesDetailGraph);
  }
  if (!customElements.get(devices.type)) {
    const Base = await loadBase(devices.base, devices.baseTag);
    class GasDevicesGraph extends Base {
      static getStubConfig = stubConfig(devices.type);

      _getStatistics(data) {
        const mapped = withGasDevices(this.hass, data);
        this._data = mapped;
        return super._getStatistics(mapped);
      }
    }
    customElements.define(devices.type, GasDevicesGraph);
  }
}

window.customCards = window.customCards || [];
for (const card of CARDS) {
  if (!window.customCards.some((c) => c.type === card.type)) {
    window.customCards.push({ type: card.type, name: card.name, description: card.description });
  }
}

define().catch((err) => console.error("Heat Ledger: could not define the gas device cards", err));
