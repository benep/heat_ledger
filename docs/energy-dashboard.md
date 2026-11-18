# Energy dashboard

Heat Ledger's counters are meant for the **gas** tab of the Energy dashboard, which accepts any energy sensor as an
individual device since Home Assistant 2026.12. The tab is called "Gas" even when the heat comes from oil, wood or
a heat pump.

## Setup

In **Settings → Dashboards → Energy → Gas**:

1. Add each heat source as a gas source (the energy entity you gave Heat Ledger as source, or its converted
   "heat" sensor). The name you enter there is the one the dashboard shows.
2. Add each circuit's heat counter as an individual device.
3. Add each radiator's heat counter as an individual device with its circuit as **upstream device**.
4. Add hot water as an individual device without upstream device.

The Sankey then shows sources → home → circuits and hot water → radiators. Like on the electricity tab, it groups
the consumers by floor and area: Heat Ledger puts each consumer's device into the area of its thermostat (or
pump, or heat meter) when it is created, so assign floors to your areas and the grouping follows. Name the radiators for what they are ("Radiator",
or "Radiator window" when a room has two), not after the room: the area already says where they are. Named after
the room, the room appears twice in the Sankey and in the entity ID (`sensor.living_room_living_room_heat`,
because Home Assistant puts the area in front). Enter the same name as display name in the Energy settings, or the
Sankey shows the sensor's name ("Radiator Heat").

The grouping applies to the top level only: radiators below a circuit stay below it, without floors. With a
single circuit, the circuit adds little; leave it out and add the radiators without upstream device, and the Sankey
shows home → floors → areas → radiators. Keep the circuits when there are several (radiators and underfloor
heating, or one circuit per floor). "Untracked" is what the sources
delivered beyond the counters: fixed losses, and heat from before Heat Ledger was set up.

A heat pump or an immersion heater shows up twice: on the electricity tab as electricity, on the gas tab as heat.
That is not double counting, the tabs are not added up. Set the price of the heat source to 0, or the costs
appear twice in the overview.

## Per-device charts

The electricity tab has bar charts per device ("Individual devices" and its detail chart); the gas tab does not
have them yet. Two statistics graph cards on any dashboard give the same view. List the radiators and hot water,
not the circuits, or the stacked bars count every radiator twice.

Heat per consumer and day, stacked:

```yaml
type: statistics-graph
title: Heat per day
chart_type: bar-stack
period: day
days_to_show: 7
stat_types:
  - change
entities:
  - entity: sensor.living_room_heat
    name: Living room
  - entity: sensor.bedroom_heat
    name: Bedroom
  - entity: sensor.bathroom_heat
    name: Bathroom
  - entity: sensor.hot_water_heat
    name: Hot water
```

Consumers side by side, per month:

```yaml
type: statistics-graph
title: Consumers compared
chart_type: bar
period: month
days_to_show: 30
stat_types:
  - change
entities:
  - entity: sensor.living_room_heat
    name: Living room
  - entity: sensor.bedroom_heat
    name: Bedroom
  - entity: sensor.bathroom_heat
    name: Bathroom
  - entity: sensor.hot_water_heat
    name: Hot water
```

Change `period` and `days_to_show` for other ranges, for example `period: week` with `days_to_show: 90`.
