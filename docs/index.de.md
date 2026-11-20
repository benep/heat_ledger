# Heat Ledger

**Wo bleibt die Wärme deiner Heizung?** Dein Gaszähler, dein Ölverbrauch oder deine Wärmepumpe sagt Home Assistant,
wie viel Wärme das ganze Haus verbraucht hat. Heat Ledger teilt diese Wärme auf deine Räume und dein Warmwasser auf,
damit du sie pro Raum siehst, direkt im Energie-Dashboard.

![Ohne Heat Ledger: eine Zahl für das ganze Haus. Mit Heat Ledger: ein Zähler pro Raum und fürs Warmwasser.](assets/diagrams/overview.svg){ .diagram }

## Was du bekommst

- **Einen Zähler pro Raum** in kWh, für jeden Heizkörper oder jede Fußbodenheizung mit Thermostat.
- **Einen Zähler fürs Warmwasser**, wenn deine Heizung auch einen Warmwasserspeicher heizt.
- **Ein Bild im Energie-Dashboard**: welcher Raum wie viel verbraucht hat, pro Tag, Woche oder Monat.

![Der Gas-Reiter des Energie-Dashboards mit der Wärme aufgeteilt auf die Räume](assets/screenshots/energy-sankey.png){ .screenshot }

## Was du brauchst

<div class="grid cards" markdown>

- :material-home-assistant: **Home Assistant 2026.10 oder neuer**

    Als Geräte im Energie-Dashboard erscheinen die Räume ab **2026.12**.

- :material-counter: **Einen Zähler deiner Heizung**

    Irgendetwas in Home Assistant, das zählt, was deine Heizung verbraucht oder erzeugt, und nur steigt: ein
    Gaszähler, der Ölverbrauch, ein Wärmemengenzähler oder die Wärme, die eine Wärmepumpe meldet.

- :material-thermostat: **Thermostate in Home Assistant**

    Eines pro Raum oder Heizkörper, zum Beispiel von Tado, Netatmo, Homematic oder Zigbee-Heizkörperthermostate.

</div>

Optional, aber es macht die Aufteilung deutlich besser: ein Sensor, der meldet, wann die **Heizungspumpe** läuft, und
einer, der meldet, wann das **Warmwasser** geheizt wird. Wie du die bekommst, steht unter
[Einrichtung](setup.md#schritt-3-pumpen).

!!! info "Eine Schätzung, keine Messung"

    Heat Ledger misst die Wärme der einzelnen Räume nicht: An deinen Heizkörpern sitzt ja kein Zähler. Es schätzt die
    Aufteilung aus dem, was Home Assistant schon weiß: wie viel Wärme deine Heizung erzeugt hat und wann welches
    Thermostat geheizt hat. Die Summe stimmt immer; die Aufteilung auf die Räume ist eine gute Schätzung und wird mit
    jeder Angabe besser. Wie, zeigt [So funktioniert es](how-it-works.md).

!!! success "Gefahrlos ausprobieren"

    Heat Ledger liest nur. Es schaltet nichts, ändert keine Einstellung deiner Heizung oder deines Energie-Dashboards
    und geht nicht ins Internet.

## Nächste Schritte

1. Heat Ledger aus dem Marketplace von Home Assistant [**installieren**](installation.md) (5 Minuten).
2. [**Einrichten**](setup.md): vier kurze Schritte, in der Richtung, in der die Wärme fließt.
3. [**Die Räume ins Energie-Dashboard eintragen**](energy-dashboard.md).
