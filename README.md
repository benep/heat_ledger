# Heat Ledger

<img src="https://raw.githubusercontent.com/benep/heat_ledger/main/custom_components/heat_ledger/brand/icon@2x.png" alt="Heat Ledger logo" width="112" align="right">

**See where the heat of your heating goes: room by room, right in the Energy dashboard of Home Assistant.**

**Deutsch?** Die ganze Anleitung gibt es [auf Deutsch](https://benep.github.io/heat_ledger/de/), und auch die
Integration selbst ist übersetzt.

[![Release](https://img.shields.io/github/v/release/benep/heat_ledger?include_prereleases&label=release)](https://github.com/benep/heat_ledger/releases)
[![Home Assistant 2026.10 or newer](https://img.shields.io/badge/Home%20Assistant-2026.10%2B-41BDF5?logo=homeassistant&logoColor=white)](#does-it-fit-your-home)
[![Help pages in English and German](https://img.shields.io/badge/help-English%20%7C%20Deutsch-orange)](https://benep.github.io/heat_ledger/)
[![Tests](https://github.com/benep/heat_ledger/actions/workflows/tests.yml/badge.svg)](https://github.com/benep/heat_ledger/actions/workflows/tests.yml)

Your gas meter, your oil consumption or your heat pump tells Home Assistant how much heat the whole house used. Heat
Ledger splits that heat between your rooms and your hot water and gives each of them a counter in kWh. No new
hardware: it works with the meter and the thermostats you already have.

![The gas tab of the Energy dashboard: the heat flows from the boiler through the floors to every room and the hot water](https://raw.githubusercontent.com/benep/heat_ledger/main/docs/assets/screenshots/energy-sankey.png)
*The gas flow in the Energy dashboard, from Home Assistant 2026.12 on. Before that, the heat dashboard below shows
the rooms.*

Step-by-step help, with pictures: in [English](https://benep.github.io/heat_ledger/) and in
[German (Deutsch)](https://benep.github.io/heat_ledger/de/).

## What you get

- **A counter per room**, in kWh, for every radiator or underfloor heating with a thermostat.
- **A counter for hot water**, if your heating also heats a hot water tank.
- **The whole picture in the Energy dashboard**: which room used how much, per day, week or month, grouped by floor.
- **Two extra cards** for a heat dashboard of your own: your rooms over time and compared.

All of it on one page: a heat dashboard of your own that works like the Energy dashboard, only extended. Same date
picker, same sources and rooms from your energy settings, plus the room charts the gas tab does not have yet. New
rooms show up by themselves. The [help pages](https://benep.github.io/heat_ledger/energy-dashboard/#a-heat-dashboard)
have it ready to copy.

![A heat dashboard: heat per day, totals, the rooms per day and compared, and the heat flow](https://raw.githubusercontent.com/benep/heat_ledger/main/docs/assets/screenshots/heat-dashboard.png)

## Does it fit your home?

Heat Ledger needs two things that are already in your Home Assistant. Check them before you install anything:

| | Fits | Does not fit |
|---|---|---|
| **A meter for your heating** | Your own gas boiler, oil boiler, heat pump or district heating, with a counter in Home Assistant that only goes up: gas, oil, a heat meter, or the heat a heat pump reports. | A flat on a central heating for the whole building, of which you only know the yearly bill. Without a meter there is nothing to split. |
| **Thermostats, one per room or radiator** | They report whether they are heating right now (`hvac_action`), or a heating demand or valve position in %. Many do, for example Tado, Netatmo, Homematic or Zigbee radiator valves. Others (Fritz, for one) count as heating while the room is colder than set, which is rougher. | They report neither, nor the room temperature. The heat is then split almost only by the size of the radiators. |

Not sure? [Check both in a minute](https://benep.github.io/heat_ledger/#does-it-fit-your-home).

You also need **Home Assistant 2026.10** or newer. The rooms appear as individual devices in the Energy dashboard
from **2026.12** on; before that, a heat dashboard built from the cards of Heat Ledger shows them.

## How it works

![The new heat of the meter is shared out between the rooms by weight](https://raw.githubusercontent.com/benep/heat_ledger/main/docs/assets/diagrams/split.svg)

Every five minutes, Heat Ledger looks at how much new heat your meter counted and shares it out between the rooms
that were heating. The share of each room follows its **size** (how much heat its radiator can give off) times its
**demand** (how much heat its thermostat asks for). If you tell it when the pumps run, it also knows when heat really
flows to the rooms and when it goes to the hot water. Heat that no room takes yet waits until one does.

More, with pictures: [How it works](https://benep.github.io/heat_ledger/how-it-works/).

## An estimate, and honest about it

There is no meter on your radiators, so Heat Ledger estimates. What you can rely on:

- **The total is exactly what your meter counted.** Only the split between the rooms is estimated.
- **Good per day and per week, rougher per hour**, because heat sometimes waits in the boiler or a buffer tank.
- **Every counter says how it is calculated**: *estimated* or *measured*, and which detail would improve it most.
- **It gets better with every detail you add**: the pumps, the demand in %, the size of each radiator, a real heat
  meter where you have one. None of them is required, and no counter starts over when you add one.

What each detail brings: [More accurate](https://benep.github.io/heat_ledger/more-accurate/).

## Safe to try

Heat Ledger only reads. It does not switch anything, does not change any setting of your heating or your Energy
dashboard, and does not go online. If you remove it, only its own sensors disappear.

## Install

1. [Add Heat Ledger to the Marketplace](https://my.home-assistant.io/redirect/marketplace_repository/?owner=benep&repository=heat_ledger&category=integration)
   of Home Assistant. By hand: open **Marketplace**, choose **Add from link**, enter
   `https://github.com/benep/heat_ledger` and choose the type **Integration**.
2. Open **Heat Ledger** there, click **Install** and restart Home Assistant.
3. Add the integration:

   [![Add Heat Ledger to Home Assistant](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=heat_ledger)

Adding from a link needs a free GitHub account, until Heat Ledger is in the catalog of the Marketplace. Every step
with its details: [Installation](https://benep.github.io/heat_ledger/installation/).

## Set up

The setup asks four short questions, in the direction the heat flows:

1. **Name**: what your heating system is called.
2. **Heat source**: the meter of your heating. If it counts gas, oil or electricity instead of heat, you add a
   factor; the dialog lists typical values.
3. **Pumps** (optional): when the circuit pump runs and when the hot water is heated.
4. **Thermostats**: one per room. Each becomes a room heating with its own counter.

Then add the rooms to the gas tab of the Energy dashboard. The counters start at 0 and grow from then on.

Every step with a screenshot: [Setup](https://benep.github.io/heat_ledger/setup/) and
[Energy dashboard](https://benep.github.io/heat_ledger/energy-dashboard/).

## Good to know

Heat Ledger is in **beta**.

- **The counters start with the setup.** If you want, the setup also works out the days before, as far as Home
  Assistant still has them recorded (10 days by default), and estimates the time before that very roughly from the
  hourly statistics.
- **The tab is called *Gas***, even if your heat comes from oil, wood or a heat pump. That does not matter.
- **The gas tab needs a source in kWh.** If your gas meter counts m³, Heat Ledger gives you a sensor in kWh to use
  as the source instead. [Energy dashboard](https://benep.github.io/heat_ledger/energy-dashboard/#set-it-up) explains
  the swap.
- **Your heating is different?** Underfloor heating, a heat pump, district heating, several heat sources: all of
  them are meant to work, and reports from such systems help most.

## Help and feedback

- [Questions](https://benep.github.io/heat_ledger/faq/) answers the common ones: how accurate it is, why a room
  gets nothing, what to do about a spike.
- Something is wrong, or the numbers look odd? [Open an issue](https://github.com/benep/heat_ledger/issues) and
  attach the **diagnostics**: on the integration page, the three dots, **Download diagnostics**. It contains your
  setup and the current state of the entities Heat Ledger reads, nothing else.

## For developers

[Architecture](docs/architecture.md) and [development](docs/development.md). Heat Ledger is released under the
[MIT license](LICENSE).
