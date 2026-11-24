# Questions

## Getting started

??? question "Does Heat Ledger control my heating?"

    No. It only reads entities that are already in Home Assistant and creates sensors of its own. It does not switch
    anything, does not change settings and does not go online.

??? question "I just set it up. Why are all counters at 0?"

    The counters start when you set up Heat Ledger and grow from then on. The first heat appears within a few minutes
    after your meter counts something and a thermostat asks for heat.

    Earlier heat, before the setup, is only split up if you ticked **Calculate the past days as well** in the last
    step of the setup; otherwise the Energy dashboard shows it as *untracked*. You can still do it afterwards, once:
    go to **Settings → Devices & services → Heat Ledger**, click the gear :material-cog: of your heating system (the
    topmost row) and choose **Calculate the past**. It covers the days before the setup that Home Assistant still has
    recorded, 10 days by default; so do it soon after the setup, every day later there is one day less. Complete your
    heating system first (pumps, pump power, sizes): it calculates with the settings it has then. Tick **Also
    estimate the time before, very roughly** there to go back as far as Home Assistant has hourly statistics of your
    heat source ([what that means](setup.md#step-4-room-heating)).

??? question "Can I calculate the past again, with better settings?"

    Not for the same heating system: the past is calculated once. Home Assistant keeps the states it is calculated
    from only for 10 days, so calculating again later could only lose what was counted exactly since.

    You can start over instead. Everything is then calculated anew, and what was counted before is gone:

    1. Delete the heating system: **Settings → Devices & services → Heat Ledger**, the three dots of your heating
       system, **Delete**.
    2. Delete its statistics: **Settings → Tools → Statistics**. Its sensors (*Heat* of every room and so on) show
       *There is no state available for this entity*. Select them all and choose **Delete selected statistics**.
    3. Set it up again, with all the settings, and calculate the past.

??? question "My heating has no meter in Home Assistant."

    Heat Ledger needs one number that counts what your heating uses or makes: the integration of your boiler or heat
    pump, a reader on the gas meter, or the run time of an oil burner. [Setup](setup.md#no-meter-yet) shows the three
    ways.

## The numbers

??? question "How accurate is it?"

    The total is exactly what your meter counted. The split between the rooms is an estimate: good per day and week,
    rougher per hour. With the pumps, the demand in % and the size of the radiators it gets much better.
    [How it works](how-it-works.md#how-accurate-is-it) shows why.

??? question "My heating is not hydraulically balanced. Is the split still right?"

    The total is, the split between the rooms less so. Heat Ledger assumes that every radiator gives off its nominal
    power when its valve is open. That is true in a balanced system: every radiator then gets as much water as it
    needs.

    Without balancing, the radiators near the pump get too much water and the distant ones too little:

    - A **poorly supplied** radiator only gets lukewarm. Its thermostat therefore heats for longer, and Heat Ledger
      gives it **too much** heat.
    - A **well supplied** radiator warms the room with its valve barely open. Heat Ledger gives it **too little**.

    The error is always the same for each room. Comparing a room with itself (this week against the last) is still
    right; only the comparison between the rooms is shifted.

    What you can do:

    - If a radiator stays lukewarm even when fully open, enter a smaller **nominal power** (or floor area) for it.
    - Hydraulic balancing fixes the cause.
    - A heat meter measures instead of estimating, see [More accurate](more-accurate.md#heat-meter).

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
