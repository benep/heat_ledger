# Häufige Fragen

## Erste Schritte

??? question "Steuert Heat Ledger meine Heizung?"

    Nein. Es liest nur Entitäten, die schon in Home Assistant sind, und legt eigene Sensoren an. Es schaltet nichts,
    ändert keine Einstellungen und geht nicht ins Internet.

??? question "Ich habe es gerade eingerichtet. Warum stehen alle Zähler auf 0?"

    Die Zähler beginnen, wenn du Heat Ledger einrichtest, und wachsen ab dann. Die erste Wärme erscheint wenige Minuten
    nachdem dein Zähler etwas gezählt hat und ein Thermostat heizen will. Frühere Wärme, vor der Einrichtung, wird
    nicht aufgeteilt; das Energie-Dashboard zeigt sie als *nicht erfasst*.

??? question "Meine Heizung hat keinen Zähler in Home Assistant."

    Heat Ledger braucht eine Zahl, die zählt, was deine Heizung verbraucht oder erzeugt. Ideen:

    - Die Integration deines Kessels oder deiner Wärmepumpe hat oft einen (Gasverbrauch, Energie, Wärmemenge).
    - Einen Gaszähler kannst du mit einem optischen Lesekopf oder einem Impulssensor auslesen.
    - Bei einem Ölbrenner geht auch die Laufzeit des Brenners mal seine Leistung (Faktor: die Leistung in kW).

## Die Zahlen

??? question "Wie genau ist das?"

    Die Summe ist genau das, was dein Zähler gezählt hat. Die Aufteilung auf die Räume ist eine Schätzung: gut pro Tag
    und Woche, gröber pro Stunde. Mit den Pumpen, der Anforderung in % und der Größe der Heizkörper wird sie deutlich
    besser. Warum, zeigt [So funktioniert es](how-it-works.md#wie-genau-ist-das).

??? question "Ein Raum bekommt keine Wärme, obwohl es dort warm ist."

    Sieh dir die Raumheizung auf der Seite der Integration an:

    - Ist ihr Thermostat **verfügbar**? Fehlt irgendein Eingang, verteilt Heat Ledger für diese Zeit nichts. Der Sensor
      **Nicht verteilte Wärme** der Heizungsanlage zeigt in seinem Attribut **Blockiert durch**, welcher Eingang fehlt.
    - Will ihr Thermostat Wärme? Ein Raum, den die Nachbarräume oder die Sonne mitheizen, bekommt nichts.
    - Steckt sie im richtigen **Heizkreis**? Solange dessen Pumpe steht, bekommen seine Räume nichts.

??? question "Der Sensor Nicht verteilte Wärme steigt und fällt. Ist das ein Problem?"

    Nein. Das ist die Wärme, die noch auf die Verteilung wartet, zum Beispiel während der Kessel aufheizt und die Pumpen
    stehen. Sie geht an die Räume, sobald diese Wärme abnehmen. Ein Beispiel zeigt
    [So funktioniert es](how-it-works.md#warum-die-raume-die-warme-spater-bekommen).

??? question "In meiner Statistik ist eine Spitze."

    Springt dein Zähler kurz (ein Template, das beim Neustart 0 meldet, ein falscher Wert), zählt Heat Ledger das wie
    Home Assistant: als Wärme. Die Spitze steht dann in der Quelle und in den Räumen. Korrigiere sie wie bei jedem
    anderen Sensor: **Entwicklerwerkzeuge → Statistiken**, den Sensor suchen, auf das Rampen-Symbol
    :material-chart-timeline-variant: klicken und den Wert zu dieser Stunde anpassen.

    Ist dein Zähler ein **Template**, verhindere es: Gib dem Template ein `availability`-Template, damit es *nicht
    verfügbar* ist statt 0, solange seine Eingänge fehlen.

??? question "Eine Wärmepumpe taucht im Energie-Dashboard zweimal auf."

    Das stimmt so: im Strom-Reiter als Strom, im Gas-Reiter als Wärme. Die Reiter werden nicht addiert. Setze den Preis
    der Wärmequelle im Gas-Reiter auf 0, sonst stehen die Kosten doppelt drin.

## Änderungen

??? question "Ich habe einen Heizkörper in einen anderen Heizkreis verschoben."

    Sein eigener Zähler läuft weiter. Sein Verlauf bleibt beim alten Heizkreis; der neue Heizkreis zählt ihn ab jetzt,
    ohne Sprung.

??? question "Ich habe meinen Kessel durch eine Wärmepumpe ersetzt."

    Trage die Wärmepumpe mit **Wärmequelle hinzufügen** ein und lösche dann die alte Quelle. Die Zähler der Räume
    laufen weiter; die Wärme, die sie vom alten Kessel bekommen haben, bleibt darin und erscheint im Attribut **Nach
    Quelle** als *Entfernte Quelle*.

??? question "Wie entferne ich Heat Ledger?"

    **Einstellungen → Geräte & Dienste → Heat Ledger**, die drei Punkte, **Löschen**. Die Sensoren verschwinden, ihr
    Verlauf bleibt. Entferne die Einträge im Energie-Dashboard von Hand, es behält sie und meldet sie als fehlend. Dann
    deinstalliere Heat Ledger im Marketplace: dort öffnen und **Deinstallieren** wählen.

## Hilfe

??? question "Etwas stimmt nicht. Wie melde ich das?"

    Eröffne ein Issue auf [GitHub](https://github.com/benep/heat_ledger/issues). Hänge bitte die **Diagnose** an: auf
    der Seite der Integration die drei Punkte, **Diagnose herunterladen**. Sie enthält deine Einrichtung, den Stand der
    Aufteilung und den aktuellen Zustand der Entitäten, die Heat Ledger liest (Zähler, Thermostate, Pumpen), sonst
    nichts.
