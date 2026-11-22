# Heat Ledger

**Where does the heat of your heating go?** Your gas meter, your oil consumption or your heat pump tells Home
Assistant how much heat the whole house used. Heat Ledger splits that heat between your rooms and your hot water,
so you can see it per room, right in the Energy dashboard.

![Without Heat Ledger: one number for the whole house. With Heat Ledger: a counter per room and for hot water.](assets/diagrams/overview.svg){ .diagram }

## What you get

- **A counter per room**, in kWh, for every radiator or underfloor heating with a thermostat.
- **A counter for hot water**, if your boiler also heats a hot water tank.
- **A picture in the Energy dashboard**: which room used how much, per day, week or month.

![The gas tab of the Energy dashboard with the heat split between rooms](assets/screenshots/energy-sankey.png){ .screenshot }

## What you need

<div class="grid cards" markdown>

- :material-home-assistant: **Home Assistant 2026.10 or newer**

    The rooms appear as devices in the Energy dashboard from **2026.12** on. Before that, a
    [heat dashboard](energy-dashboard.md#a-heat-dashboard) shows them.

- :material-counter: **A meter for your heating**

    Anything in Home Assistant that counts what your heating uses or makes, and only goes up: a gas meter, the oil
    consumption, a heat meter or the heat a heat pump reports.

- :material-thermostat: **Thermostats in Home Assistant**

    One per room or radiator, for example from Tado, Netatmo, Homematic or Zigbee radiator valves.

</div>

Optional, but it makes the split much better: a sensor that tells when the **heating pump** runs, and one that
tells when the **hot water** is heated. [Setup](setup.md#step-3-pumps) explains how to get them.

## Does it fit your home?

Two things decide whether Heat Ledger gives you useful numbers. You can check both in a minute, before you install
anything.

**Does your heating have a meter of its own in Home Assistant?** Heat Ledger only splits what this meter counts.

- :material-check: **Fits:** a house or a flat with its own heating (gas boiler, oil boiler, heat pump, district
  heating with a heat meter) whose consumption or heat arrives in Home Assistant.
- :material-close: **Does not fit:** a flat on a central heating for the whole building, of which you only know the
  yearly bill. Without a meter there is nothing to split.
- No meter in Home Assistant yet? [Questions](faq.md#getting-started) lists ways to get one.

**Do your thermostats report whether they are heating right now?** Open **Developer tools → States** and look for one
of your thermostats (`climate.…`). If its attributes show `hvac_action` with a value like `heating` or `idle`, it
fits.

- If `hvac_action` is missing, check whether the thermostat has a sensor of its own in % (heating power, heating
  demand or valve position). Choose that one instead of the thermostat, see [More accurate](more-accurate.md#demand-in).
- If it has neither, the room counts as heating for as long as the thermostat is switched on. The heat is then split
  almost only by the size of the radiators, and you do not see which room really heats a lot. Heat Ledger is hardly
  worth it then.

!!! info "An estimate, not a measurement"

    Heat Ledger does not measure the heat of each room: there is no meter on your radiators. It estimates the split
    from what Home Assistant already knows: how much heat your heating made, and when each thermostat asked for it.
    The total is always right; the split between the rooms is a good estimate, and gets better with every detail
    you add. [How it works](how-it-works.md) shows how.

!!! success "Safe to try"

    Heat Ledger only reads. It does not switch anything, does not change any setting of your heating or your Energy
    dashboard, and does not go online.

## Next steps

1. [**Install**](installation.md) Heat Ledger from the Marketplace of Home Assistant (5 minutes).
2. [**Set it up**](setup.md): four short steps, in the direction the heat flows.
3. [**Add the rooms to the Energy dashboard**](energy-dashboard.md).
