# Questions

## Getting started

??? question "Does Heat Ledger control my heating?"

    No. It only reads entities that are already in Home Assistant and creates sensors of its own. It does not switch
    anything, does not change settings and does not go online.

??? question "I just set it up. Why are all counters at 0?"

    The counters start when you set up Heat Ledger and grow from then on. The first heat appears within a few minutes
    after your meter counts something and a thermostat asks for heat. Earlier heat, before the setup, is not split
    up; the Energy dashboard shows it as *untracked*.

??? question "My heating has no meter in Home Assistant."

    Heat Ledger needs one number that counts what your heating uses or makes. Ideas:

    - The integration of your boiler or heat pump often has one (gas consumption, energy, heat energy).
    - A gas meter can be read with an optical reader or a pulse sensor.
    - For an oil burner, the run time of the burner multiplied by its output works as well (factor: the output in kW).

## The numbers

??? question "How accurate is it?"

    The total is exactly what your meter counted. The split between the rooms is an estimate: good per day and week,
    rougher per hour. With the pumps, the demand in % and the size of the radiators it gets much better.
    [How it works](how-it-works.md#how-accurate-is-it) shows why.

??? question "A room gets no heat, although it is warm there."

    Check the room heating on the integration page:

    - Is its thermostat **available**? If any input is unavailable, Heat Ledger shares out nothing for that time. The
      sensor **Unassigned heat** of the heating system shows in its attribute **Blocked by** which input is missing.
    - Does its thermostat ask for heat? A room that is warmed by its neighbours, or by the sun, gets nothing.
    - Is it in the right **heating circuit**? While that circuit's pump stands still, its rooms get nothing.

??? question "The sensor Unassigned heat goes up and down. Is that a problem?"

    No. It is the heat that waits to be shared out, for example while the boiler warms up and the pumps stand still.
    It goes to the rooms as soon as they take heat. [How it works](how-it-works.md#why-the-rooms-get-the-heat-later)
    shows an example.

??? question "There is a spike in my statistics."

    If your meter jumps for a moment (a template that reports 0 during a restart, a wrong reading), Heat Ledger counts
    it like Home Assistant does: as heat. The spike then appears in the source and in the rooms. Correct it the same
    way as for any other sensor: **Developer tools → Statistics**, find the sensor, click the ramp icon
    :material-chart-timeline-variant: and adjust the value at that hour.

    If your meter is a **template**, prevent it: give the template an `availability` template, so it is *unavailable*
    instead of 0 while its inputs are missing.

??? question "A heat pump shows up twice in the Energy dashboard."

    That is right: on the electricity tab as electricity, on the gas tab as heat. The tabs are not added up. Set the
    price of the heat source on the gas tab to 0, otherwise the costs appear twice.

## Changing things

??? question "I moved a radiator to another heating circuit."

    Its own counter continues. Its history stays with the old circuit; the new circuit counts it from now on, without
    a jump.

??? question "I replaced my boiler with a heat pump."

    Add the heat pump with **Add heat source**, then delete the old source. The counters of the rooms continue; the
    heat they got from the old boiler stays in them and shows as *Removed source* in the attribute **By source**.

??? question "How do I remove Heat Ledger?"

    **Settings → Devices & services → Heat Ledger**, the three dots, **Delete**. Its sensors disappear, their history
    stays. Remove the entries from the Energy dashboard by hand, it keeps them and reports them as missing. Then remove
    Heat Ledger in the Marketplace: open it there and choose **Uninstall**.

## Help

??? question "Something is wrong. How do I report it?"

    Open an issue on [GitHub](https://github.com/benep/heat_ledger/issues). Please attach the **diagnostics**: on the
    integration page, the three dots, **Download diagnostics**. It contains your setup, the state of the split and the
    current state of the entities Heat Ledger reads (your meter, thermostats and pumps), nothing else.
