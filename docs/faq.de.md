# Häufige Fragen

## Erste Schritte

??? question "Steuert Heat Ledger meine Heizung?"

    Nein. Es liest nur Entitäten, die schon in Home Assistant sind, und legt eigene Sensoren an. Es schaltet nichts,
    ändert keine Einstellungen und geht nicht ins Internet.

??? question "Ich habe es gerade eingerichtet. Warum stehen alle Zähler auf 0?"

    Die Zähler beginnen, wenn du Heat Ledger einrichtest, und wachsen ab dann. Die erste Wärme erscheint wenige Minuten
    nachdem dein Zähler etwas gezählt hat und ein Thermostat heizen will.

    Frühere Wärme, vor der Einrichtung, wird nur aufgeteilt, wenn du im letzten Schritt der Einrichtung **Vergangene
    Tage nachrechnen** angekreuzt hast; sonst zeigt das Energie-Dashboard sie als *nicht erfasst*. Du kannst es
    einmalig nachholen: Gehe zu **Einstellungen → Geräte & Dienste → Heat Ledger**, klicke auf das Zahnrad
    :material-cog: deiner Heizungsanlage (die oberste Zeile) und wähle **Vergangene Zeit berechnen**. Es umfasst die
    Tage vor der Einrichtung, die Home Assistant noch aufgezeichnet hat, in der Voreinstellung 10 Tage; mach es also
    bald nach der Einrichtung, jeder Tag später ist ein Tag weniger. Vervollständige deine Heizungsanlage vorher
    (Pumpen, Leistung der Pumpen, Größen): Gerechnet wird mit den Einstellungen, die sie dann hat. Setzt du dort den
    Haken **Auch die Zeit davor schätzen, sehr grob**, geht es so weit zurück, wie Home Assistant Stundenstatistiken
    deiner Wärmequelle hat ([was das heißt](setup.md#schritt-4-raumheizung)).

??? question "Kann ich die vergangene Zeit mit besseren Einstellungen noch einmal berechnen?"

    Nicht für dieselbe Heizungsanlage: Die vergangene Zeit wird einmal berechnet. Die Zustände, aus denen sie
    berechnet wird, hebt Home Assistant nur 10 Tage auf; später neu zu rechnen könnte also nur verlieren, was seitdem
    genau gezählt wurde.

    Du kannst aber von vorn anfangen. Dann wird alles neu berechnet, und was vorher gezählt wurde, ist weg:

    1. Lösche die Heizungsanlage: **Einstellungen → Geräte & Dienste → Heat Ledger**, die drei Punkte deiner
       Heizungsanlage, **Löschen**.
    2. Lösche ihre Statistiken: **Einstellungen → Werkzeuge → Statistik**. Ihre Sensoren (*Wärme* jedes Raums und
       so weiter) zeigen *Für diese Entität ist kein Zustand verfügbar.* Wähle sie alle aus und dann **Ausgewählte
       Statistiken löschen**.
    3. Richte sie neu ein, mit allen Einstellungen, und berechne die vergangene Zeit.

??? question "Meine Heizung hat keinen Zähler in Home Assistant."

    Heat Ledger braucht eine Zahl, die zählt, was deine Heizung verbraucht oder erzeugt: die Integration deines
    Kessels oder deiner Wärmepumpe, einen Lesekopf am Gaszähler oder bei einer Ölheizung
    [Fuel Ledger](https://benep.github.io/fuel_ledger/de/).
    [Einrichtung](setup.md#noch-kein-zahler) zeigt die drei Wege.

## Die Zahlen

??? question "Wie genau ist das?"

    Die Summe ist genau das, was dein Zähler gezählt hat. Die Aufteilung auf die Räume ist eine Schätzung: gut pro Tag
    und Woche, gröber pro Stunde. Mit den Pumpen, der Anforderung in % und der Größe der Heizkörper wird sie deutlich
    besser. Warum, zeigt [So funktioniert es](how-it-works.md#wie-genau-ist-das).

??? question "Meine Heizung ist nicht hydraulisch abgeglichen. Stimmt die Aufteilung trotzdem?"

    Die Summe ja, die Aufteilung zwischen den Räumen weniger gut. Heat Ledger nimmt an, dass jeder Heizkörper bei
    offenem Ventil seine Nennleistung abgibt. Das stimmt, wenn die Anlage abgeglichen ist: Dann bekommt jeder
    Heizkörper so viel Wasser, wie er braucht.

    Ohne Abgleich bekommen die Heizkörper nahe der Pumpe zu viel Wasser und die entfernten zu wenig:

    - Ein **schlecht versorgter** Heizkörper wird nur lauwarm. Sein Thermostat heizt deshalb länger, und Heat Ledger
      schreibt ihm **zu viel** Wärme zu.
    - Ein **gut versorgter** Heizkörper wärmt den Raum schon mit wenig geöffnetem Ventil. Heat Ledger schreibt ihm
      **zu wenig** zu.

    Der Fehler ist für jeden Raum immer derselbe. Der Vergleich eines Raums mit sich selbst (diese Woche gegen die
    letzte) stimmt also trotzdem; nur der Vergleich der Räume untereinander ist verschoben.

    Was du tun kannst:

    - Bleibt ein Heizkörper auch voll aufgedreht nur lauwarm, trage bei ihm eine kleinere **Nennleistung** (oder
      Raumfläche) ein.
    - Ein hydraulischer Abgleich behebt die Ursache.
    - Ein Wärmemengenzähler misst, statt zu schätzen, siehe [Genauer machen](more-accurate.md#warmemengenzahler).

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
    anderen Sensor: **Einstellungen → Werkzeuge → Statistik** (in älteren Versionen **Entwicklerwerkzeuge**), den
    Sensor suchen, auf das Rampen-Symbol :material-chart-timeline-variant: klicken und den Wert zu dieser Stunde
    anpassen.

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
