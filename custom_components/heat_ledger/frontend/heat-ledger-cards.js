// Device cards for the gas tab, which Home Assistant only has on the electricity tab so far:
//
//   custom:heat-ledger-gas-devices-detail-graph   like "Individual devices detail usage" (energy-devices-detail-graph)
//   custom:heat-ledger-gas-devices-graph          like "Individual devices total usage" (energy-devices-graph)
//
// Both extend the electricity cards and hand them the gas devices of the Energy dashboard
// (device_consumption_gas) instead of the electricity devices. The energy collection loads their statistics anyway,
// for the gas Sankey. If several devices share a name, their area is shown instead, as the Sankey groups by area.
// The electricity sources are kept from the base cards, which would otherwise compare the gas devices with them.
//
// If a gas source has costs, both cards get a switch between kWh and costs. It applies to all of these cards and
// is remembered in the browser. The costs of a device are its heat times the price the gas sources had in the
// same hour or day, see priceByPeriod.
//
// The base cards only know kWh. They keep calculating as if the costs were kWh, and these cards rewrite the unit
// in what they show: the axis, the legend, the tooltip and the total.
//
// The cards rely on internal names of the frontend (_data, _getStatistics, _processStatistics, _createOptions,
// _legendData, _chartData, _chartType, the height of ha-chart-base) and on its tooltips being lit templates, so
// check them after a frontend update.

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

// The unit the electricity cards write behind every number
const ENERGY_UNIT = "kWh";
// A number with that unit behind it, as the electricity cards write an amount of energy
const ENERGY_AMOUNT = /([\d.,'’  ]*\d)\s*kWh/;
// The locales the frontend formats numbers with, by the number format of the user profile; any other format
// follows the language.
const NUMBER_LOCALES = {
  comma_decimal: ["en-US", "en"],
  decimal_comma: ["de", "es", "it"],
  space_comma: ["fr", "sv", "cs"],
  quote_decimal: ["de-CH"],
  none: "en-US",
  system: undefined,
};
const currencyFormats = new Map();
const COST_LABEL = "ui.panel.lovelace.cards.energy.energy_sources_table.cost";
// The least height of the chart with the donut, in px. Not a height the base card can choose (28 px per device
// plus 50), so the two can be told apart.
const DONUT_HEIGHT = 450;
const STORAGE_KEY = "heat-ledger-cards-unit";

const TOGGLE_STYLE = `
  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .heat-ledger-unit {
    display: flex;
    flex: none;
    margin-left: auto;
    border: 1px solid var(--divider-color);
    border-radius: 999px;
    overflow: hidden;
    font-family: var(--ha-font-family-body, inherit);
    font-size: var(--ha-font-size-s, 12px);
    font-weight: var(--ha-font-weight-medium, 500);
    line-height: 1;
    letter-spacing: normal;
  }
  .heat-ledger-unit[hidden] {
    display: none;
  }
  .content > .heat-ledger-unit {
    width: fit-content;
    margin-bottom: 8px;
  }
  .heat-ledger-unit button {
    all: unset;
    box-sizing: border-box;
    min-width: 44px;
    padding: 8px 12px;
    text-align: center;
    color: var(--secondary-text-color);
    cursor: pointer;
  }
  .heat-ledger-unit button[aria-pressed="true"] {
    background: var(--primary-color);
    color: var(--text-primary-color, #fff);
  }
  .heat-ledger-unit button:focus-visible {
    outline: 2px solid var(--primary-text-color);
    outline-offset: -3px;
  }
`;

const cards = new Set();
const rewrittenOptions = new WeakSet();
let showCosts = false;
try {
  showCosts = localStorage.getItem(STORAGE_KEY) === "cost";
} catch {
  // No storage, for example in a private window: the choice then lasts until the page is reloaded.
}

function setShowCosts(value) {
  if (value === showCosts) return;
  showCosts = value;
  try {
    localStorage.setItem(STORAGE_KEY, value ? "cost" : "energy");
  } catch {
    // See above
  }
  for (const card of cards) card._unitChanged();
}

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
  // Only the gas sources: from the electricity sources the base cards would work out an "untracked consumption",
  // the electricity of the house less the heat of the gas devices.
  const sources = (data.prefs.energy_sources || []).filter((source) => source.type === "gas");
  return { ...data, prefs: { ...data.prefs, device_consumption: named, energy_sources: sources } };
}

// The gas sources that have a price, each with the statistics of its energy and of its costs. fixedPrice is set
// if the price is a number entered in the Energy dashboard, the same for every unit, and not an entity or a cost
// statistic of its own.
function gasSourcesWithCosts(data) {
  return (data.prefs.energy_sources || [])
    .filter((source) => source.type === "gas")
    .map((source) => ({
      energy: source.stat_energy_from,
      cost: source.stat_cost || data.info?.cost_sensors?.[source.stat_energy_from],
      fixedPrice:
        !source.stat_cost && !source.entity_energy_price && typeof source.number_energy_price === "number"
          ? source.number_energy_price
          : undefined,
    }))
    .filter((source) => source.cost || source.fixedPrice !== undefined);
}

function increases(points) {
  const sum = new Map();
  for (const point of points || []) {
    if (point.change > 0) sum.set(point.start, (sum.get(point.start) || 0) + point.change);
  }
  return sum;
}

// The price a source with a fixed price had in the statistics shown: the median of its costs per unit, so the
// price it had back then and not the one entered today. The median, because single periods are off: Home
// Assistant counts no costs for energy that arrived while it was not running, and a corrected statistic changes
// only one of the two. Without any costs, as before a price was entered, it is the price entered today.
function typicalPrice(energy, cost, entered) {
  const prices = [];
  for (const [start, amount] of energy) {
    if (cost.has(start)) prices.push(cost.get(start) / amount);
  }
  prices.sort((a, b) => a - b);
  return prices.length ? prices[prices.length >> 1] : entered;
}

// The price per period of the statistics: the costs of the sources divided by their energy. A source with a
// fixed price costs its energy times that price in every period; for a price that changes, the costs are those
// Home Assistant counted. A period in which the sources delivered nothing takes the price before it, because heat
// that waited in a boiler or buffer tank was paid for when it was produced; periods before the first delivery
// take the first price.
function priceByPeriod(sources, stats) {
  const energy = new Map();
  const cost = new Map();
  const add = (sum, start, amount) => sum.set(start, (sum.get(start) || 0) + amount);
  for (const source of sources) {
    const sourceEnergy = increases(stats[source.energy]);
    const sourceCost = increases(stats[source.cost]);
    const fixed =
      source.fixedPrice === undefined ? undefined : typicalPrice(sourceEnergy, sourceCost, source.fixedPrice);
    for (const [start, amount] of sourceEnergy) {
      add(energy, start, amount);
      add(cost, start, fixed === undefined ? sourceCost.get(start) || 0 : amount * fixed);
    }
  }
  const starts = [...energy.keys()].sort((a, b) => a - b);
  const prices = starts.map((start) => (cost.get(start) || 0) / energy.get(start));
  return (start) => {
    if (!starts.length) return 0;
    let low = 0;
    let high = starts.length - 1;
    while (low < high) {
      const middle = (low + high + 1) >> 1;
      if (starts[middle] <= start) low = middle;
      else high = middle - 1;
    }
    return prices[low];
  };
}

function withCosts(data, sources) {
  const convert = (stats) => {
    if (!stats) return stats;
    const price = priceByPeriod(sources, stats);
    const converted = { ...stats };
    for (const device of data.prefs.device_consumption) {
      const points = stats[device.stat_consumption];
      if (!points) continue;
      converted[device.stat_consumption] = points.map((point) =>
        point.change ? { ...point, change: point.change * price(point.start) } : point,
      );
    }
    return converted;
  };
  return { ...data, stats: convert(data.stats), statsCompare: convert(data.statsCompare) };
}

// How Home Assistant writes an amount of money for this user: the symbol of the currency, and what stands in
// front of and behind the number ("€" in front in English, " €" behind in German). The locale follows the number
// format of the user profile, as in the frontend.
function currencyFormat(hass) {
  const currency = hass?.config?.currency;
  const numberFormat = hass?.locale?.number_format;
  const key = `${currency}|${numberFormat}|${hass?.locale?.language}`;
  if (!currencyFormats.has(key)) {
    let format = { symbol: currency || "¤", before: "", after: ` ${currency || "¤"}` };
    try {
      const locale = numberFormat in NUMBER_LOCALES ? NUMBER_LOCALES[numberFormat] : hass.locale.language;
      const parts = new Intl.NumberFormat(locale, { style: "currency", currency }).formatToParts(1);
      const first = parts.findIndex((part) => part.type === "integer");
      const last = parts.findLastIndex((part) => part.type === "integer" || part.type === "fraction");
      const text = (list) => list.map((part) => part.value).join("");
      format = {
        symbol: parts.find((part) => part.type === "currency").value,
        before: text(parts.slice(0, first)),
        after: text(parts.slice(last + 1)),
      };
    } catch {
      // An unknown currency or locale: the code of the currency behind the number
    }
    currencyFormats.set(key, format);
  }
  return currencyFormats.get(key);
}

// A lit template as the frontend builds it for a tooltip, with every text in it rewritten
function mapTemplateTexts(value, rewrite) {
  if (typeof value === "string") return rewrite(value);
  if (Array.isArray(value)) return value.map((item) => mapTemplateTexts(item, rewrite));
  if (value && Array.isArray(value.strings) && Array.isArray(value.values)) {
    return { ...value, values: value.values.map((item) => mapTemplateTexts(item, rewrite)) };
  }
  return value;
}

function createToggle() {
  const toggle = document.createElement("div");
  toggle.className = "heat-ledger-unit";
  toggle.setAttribute("role", "group");
  const style = document.createElement("style");
  style.textContent = TOGGLE_STYLE;
  toggle.append(style);
  for (const costs of [false, true]) {
    const button = document.createElement("button");
    button.type = "button";
    button.addEventListener("click", () => setShowCosts(costs));
    toggle.append(button);
  }
  return toggle;
}

// What both cards share: the data they show, the unit behind the numbers, and the switch between the two units.
// The switch is put into the rendered card by hand, in its header or, without a title, above the chart: this
// module has no access to the templates of the frontend.
function withUnitSwitch(Base) {
  return class extends Base {
    constructor() {
      super();
      const createOptions = this._createOptions;
      this._createOptions = (...args) => this._optionsInUnit(createOptions(...args));
    }

    connectedCallback() {
      super.connectedCallback();
      cards.add(this);
    }

    disconnectedCallback() {
      super.disconnectedCallback();
      cards.delete(this);
    }

    _mapData(data) {
      if (!data?.prefs) return data;
      const mapped = withGasDevices(this.hass, data);
      const sources = gasSourcesWithCosts(mapped);
      // A source with the price 0 counts into the price of the heat, but alone it gives nothing to show.
      this._canShowCosts = sources.some((source) => source.fixedPrice !== 0);
      this._showsCosts = showCosts && this._canShowCosts;
      return this._showsCosts ? withCosts(mapped, sources) : mapped;
    }

    _unit() {
      return this._showsCosts ? currencyFormat(this.hass).symbol : ENERGY_UNIT;
    }

    // A number the base card formatted with its unit, in the unit this card shows
    _inUnit(text) {
      if (!this._showsCosts || typeof text !== "string") return text;
      const { before, after } = currencyFormat(this.hass);
      // Costs with a single decimal get a second one: "16.4" becomes "16.40"
      return text.replace(
        ENERGY_AMOUNT,
        (_match, number) => before + (/[.,]\d$/.test(number) ? `${number}0` : number) + after,
      );
    }

    // The chart options of the base card, with the unit of this card on the axis and in the tooltip. The base
    // card keeps the options until its data changes, so they are rewritten in place, once.
    _optionsInUnit(options) {
      if (!this._showsCosts || rewrittenOptions.has(options)) return options;
      rewrittenOptions.add(options);
      for (const axis of ["xAxis", "yAxis"]) {
        if (options[axis]?.name === ENERGY_UNIT) options[axis] = { ...options[axis], name: this._unit() };
      }
      const formatter = options.tooltip?.formatter;
      if (typeof formatter === "function") {
        options.tooltip = {
          ...options.tooltip,
          formatter: (params) => mapTemplateTexts(formatter(params), (text) => this._inUnit(text)),
        };
      }
      return options;
    }

    updated(changed) {
      super.updated(changed);
      const header = this.shadowRoot?.querySelector(".card-header");
      const parent = header || this.shadowRoot?.querySelector(".content");
      if (!parent) return;
      if (!this._toggle) this._toggle = createToggle();
      const toggle = this._toggle;
      const [energy, cost] = toggle.querySelectorAll("button");
      const { symbol } = currencyFormat(this.hass);
      if (energy.textContent !== ENERGY_UNIT) energy.textContent = ENERGY_UNIT;
      if (cost.textContent !== symbol) cost.textContent = symbol;
      energy.setAttribute("aria-pressed", String(!this._showsCosts));
      cost.setAttribute("aria-pressed", String(!!this._showsCosts));
      toggle.hidden = !this._canShowCosts;
      if (toggle.parentNode !== parent) {
        // In the header after the title, so before the button for the chart type of the card in total
        const title = header?.querySelector("span");
        if (title) title.after(toggle);
        else if (header) header.append(toggle);
        else parent.prepend(toggle);
      }
    }
  };
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
    class GasDevicesDetailGraph extends withUnitSwitch(Base) {
      static getStubConfig = stubConfig(detail.type);

      get _data() {
        return super._data;
      }

      set _data(value) {
        this._energyData = value;
        super._data = this._mapData(value);
      }

      _processStatistics() {
        super._processStatistics();
        if (this._showsCosts && this._legendData) {
          this._legendData = this._legendData.map((item) => ({ ...item, value: this._inUnit(item.value) }));
        }
      }

      _unitChanged() {
        if (this._energyData) this._data = this._energyData;
      }
    }
    customElements.define(detail.type, GasDevicesDetailGraph);
  }
  if (!customElements.get(devices.type)) {
    const Base = await loadBase(devices.base, devices.baseTag);
    class GasDevicesGraph extends withUnitSwitch(Base) {
      static getStubConfig = stubConfig(devices.type);

      // The base card calls this with new data of the Energy dashboard, and again with the data it already shows
      // when a device is hidden or the chart type changes.
      _getStatistics(data) {
        if (data !== this._shownData) this._energyData = data;
        const mapped = this._mapData(this._energyData);
        this._shownData = mapped;
        this._data = mapped;
        const done = super._getStatistics(mapped);
        // The base card has set the chart and the legend by now; it only waits for the update to render.
        if (this._showsCosts) {
          const costLabel = this._i18n?.localize?.(COST_LABEL);
          this._legendData = this._legendData.map((item) => ({ ...item, value: this._inUnit(item.value) }));
          this._chartData = this._chartData.map((series, index) => ({
            ...series,
            name: index === 0 && costLabel ? costLabel : series.name,
            label:
              typeof series.label?.formatter === "string"
                ? { ...series.label, formatter: this._inUnit(series.label.formatter) }
                : series.label,
          }));
        }
        return done;
      }

      _unitChanged() {
        if (this._energyData) this._getStatistics(this._energyData);
      }

      // The base card makes its chart as high as the bars need, 28 px per device, and the donut fills that
      // height: large with the many devices of an electricity tab, tiny with a few rooms. The donut gets a
      // minimum height here. The base card sets the height again only when its own value changes, so the height
      // found on the chart is either its value or the one set here.
      updated(changed) {
        super.updated(changed);
        const chart = this.shadowRoot?.querySelector("ha-chart-base");
        if (!chart) return;
        if (chart.height !== this._donutHeight) this._barsHeight = chart.height;
        const enlarge = this._chartType === "pie" && parseFloat(this._barsHeight) < DONUT_HEIGHT;
        this._donutHeight = enlarge ? `${DONUT_HEIGHT}px` : undefined;
        const height = enlarge ? this._donutHeight : this._barsHeight;
        if (chart.height !== height) chart.height = height;
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
