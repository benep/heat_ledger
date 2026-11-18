# Heat Ledger

A Home Assistant custom integration that shows where your heating energy goes: it splits the heat of one or more
sources (boiler, heat pump, immersion heater, wood stove) into energy counters per radiator and for domestic hot
water, ready to use as individual devices in the Energy dashboard.

It estimates from entities you already have (source energy, thermostat demand, pump states) and gets more accurate
with every optional input, such as radiator ratings, flow and return temperatures or heat cost allocators.

Status: in development, not released yet.

How to set up the Energy dashboard with it, and two cards for per-device charts that the gas tab does not have
yet: [docs/energy-dashboard.md](docs/energy-dashboard.md).

Sister project: Fuel Ledger, bookkeeping for heating fuel.
