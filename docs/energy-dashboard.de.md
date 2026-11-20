# Energie-Dashboard

Die Zähler von Heat Ledger gehören in den Reiter **Gas** des Energie-Dashboards. Dort zeigt Home Assistant sie als
einzelne Geräte: welcher Raum wie viel Wärme verbraucht hat, pro Tag, Woche oder Monat.

!!! info "Home Assistant 2026.12 oder neuer"

    Einzelne Geräte im Gas-Reiter gibt es seit Home Assistant 2026.12. Der Reiter heißt *Gas*, auch wenn deine Wärme
    von Öl, Holz oder einer Wärmepumpe kommt; das spielt keine Rolle.

## So sieht es aus

![Der Gas-Reiter: Wärme pro Tag und ihr Weg von der Wärmequelle über die Stockwerke zu den Räumen und dem Warmwasser](assets/screenshots/energy-dashboard.png){ .screenshot }

Home Assistant gruppiert die Räume selbst nach **Stockwerk** und **Bereich**. Heat Ledger legt jede Raumheizung in
den Bereich ihres Thermostats; gib deinen Bereichen ein Stockwerk (**Einstellungen → Bereiche, Labels & Zonen**),
dann folgt die Gruppierung.

## Einrichten

Gehe zu **Einstellungen → Dashboards → Energie** und öffne den Reiter **Gas**.

!!! warning "Die Gas-Quelle muss in kWh zählen"

    Home Assistant lässt nur Gas-Quellen und einzelne Gasgeräte zusammen zu, die dieselbe Art von Einheit zählen:
    entweder alle Volumen (m³) oder alle Energie (kWh). Die Zähler der Räume zählen Wärme in kWh, also muss auch die
    **Gas-Quelle in kWh zählen**. Zählt dein Gaszähler in m³, nimm ihn hier nicht als Gas-Quelle, sondern den Sensor
    **Erzeugte Wärme** von Heat Ledger. Das ist dein Zähler mal dem Faktor, in kWh. Den Preis trägst du dann pro kWh
    ein.

![Der Gas-Bereich der Energie-Einstellungen, mit der Quelle und den einzelnen Geräten](assets/screenshots/energy-gas-settings.png){ .screenshot }

<div class="steps" markdown>

1. **Die Quelle.** Klicke unter **Gasverbrauch** auf **Gaszähler hinzufügen** und wähle deine Wärmequelle in kWh:
   den Zähler, den du Heat Ledger gegeben hast, wenn er Wärme in kWh zählt, sonst den Sensor **Erzeugte Wärme** von
   Heat Ledger. Einen Gaszähler in m³, der schon drinsteht, entferne (sein Verlauf bleibt). Einen Preis kannst du
   hier auch eintragen.
2. **Die Räume.** Klicke unter **Einzelne Gasgeräte** auf **Gerät hinzufügen** und wähle den Sensor **Wärme** einer
   Raumheizung. Wiederhole das für jeden Raum.
3. **Das Warmwasser.** Trage seinen Sensor **Wärme** genauso ein.

</div>

!!! tip "Anzeigename"

    Das Dashboard zeigt den Namen, den du im Feld **Anzeigename** einträgst. Ohne ihn zeigt es den Namen des Sensors,
    zum Beispiel *Wohnzimmer Wärme*.

**Nicht erfasst** ist, was deine Quelle über die Raumheizungen und das Warmwasser hinaus gezählt hat: die festen
Verluste, falls du welche eingetragen hast, und die Wärme aus der Zeit, bevor du Heat Ledger eingerichtet hast.

## Mit Heizkreis

Hast du Heat Ledger eine Heizkreispumpe gegeben, gibt es auch einen Zähler für den **Heizkreis**: alle seine Räume
zusammen.

- **Ein Heizkreis:** lass ihn weg. Trage die Räume direkt ein wie oben; das Dashboard gruppiert sie dann nach
  Stockwerk.
- **Mehrere Heizkreise** (Heizkörper und Fußbodenheizung, oder ein Kreis pro Stockwerk): trage jeden Heizkreis als
  Gerät ein und jeden seiner Räume mit dem Heizkreis als **vorgelagertes Gerät**. Die Räume stehen dann unter ihrem
  Heizkreis.

## Wärmepumpe oder Heizstab

Sie tauchen zweimal auf: im Strom-Reiter als Strom, im Gas-Reiter als Wärme. Doppelt gezählt wird nichts, die Reiter
werden nicht addiert. Setze den Preis der Wärmequelle im Gas-Reiter auf **0**, sonst stehen die Kosten doppelt in der
Übersicht.

## Diagramme pro Raum

Der Strom-Reiter hat Balkendiagramme pro Gerät, der Gas-Reiter noch nicht. Zwei Karten **Statistikdiagramm** auf
einem beliebigen Dashboard zeigen dasselbe. Ersetze die Entitäten durch deine eigenen Sensoren **Wärme**. Trage die
Räume und das Warmwasser ein, nicht die Heizkreise, sonst zählen die gestapelten Balken jeden Raum doppelt.

Wärme pro Raum und Tag, gestapelt:

```yaml
type: statistics-graph
title: Wärme pro Tag
chart_type: bar-stack
period: day
days_to_show: 7
stat_types:
  - change
entities:
  - entity: sensor.wohnzimmer_warme
    name: Wohnzimmer
  - entity: sensor.schlafzimmer_warme
    name: Schlafzimmer
  - entity: sensor.bad_warme
    name: Bad
  - entity: sensor.warmwasser_warme
    name: Warmwasser
```

Räume nebeneinander, pro Monat:

```yaml
type: statistics-graph
title: Räume im Vergleich
chart_type: bar
period: month
days_to_show: 30
stat_types:
  - change
entities:
  - entity: sensor.wohnzimmer_warme
    name: Wohnzimmer
  - entity: sensor.schlafzimmer_warme
    name: Schlafzimmer
  - entity: sensor.bad_warme
    name: Bad
  - entity: sensor.warmwasser_warme
    name: Warmwasser
```

Für andere Zeiträume ändere `period` und `days_to_show`, zum Beispiel `period: week` mit `days_to_show: 90`.

## Heat Ledger entfernen

Löschst du Heat Ledger oder eine seiner Raumheizungen, verschwinden ihre Sensoren, ihr Verlauf bleibt. Das
Energie-Dashboard behält die Einträge und meldet sie als fehlend: entferne sie dort von Hand.
