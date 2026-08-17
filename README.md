# Abholkarte – fertige PWA für Kleinanzeigen-Abholungen

Die App sammelt einzelne Kleinanzeigen und komplette Suchen auf einer Karte, prüft gespeicherte Anzeigen auf Verfügbarkeit und optimiert Abholrouten.

## Funktionen
- Einzelnen Kleinanzeigen-Link automatisch importieren
- Manuelles Hinzufügen als Fallback
- Gespeicherte Kleinanzeigen-Suchlinks anlegen und synchronisieren
- Bis zu 100 Treffer pro Suche pro Synchronisation
- Doppelte Anzeigen werden anhand der Inserat-ID zusammengeführt
- Karte mit OpenStreetMap/Leaflet
- Automatische Standortauflösung aus Ort/PLZ
- Anzeigen auswählen und Rundtour oder Start→Ziel-Route optimieren
- Kilometer und Fahrzeit anzeigen
- Veraltete/gelöschte Anzeigen per Statusprüfung ausgrauen
- SQLite-Datenbank
- Optionaler PIN-Schutz
- PWA: auf iPhone in Safari „Teilen → Zum Home-Bildschirm“
- Docker-/Render-Deployment vorbereitet

## Wichtig: Kleinanzeigen-Daten
Für zuverlässigen automatischen Import nutzt die App die API von `kleinanzeigen-agent.de`. Laut deren aktueller Dokumentation benötigt die REST-API einen API-Key und arbeitet credit-basiert. Der Key bleibt ausschließlich auf dem Server.

1. Konto/API-Key bei Kleinanzeigen Agent anlegen.
2. `.env.example` nach `.env` kopieren.
3. `KLAZ_API_KEY=...` eintragen.
4. Optional `APP_PIN=...` setzen.

Ohne API-Key funktioniert die App weiterhin mit manuellen Pins und Routing. Ein direkter HTML-Abruf ist als experimenteller Fallback vorhanden und standardmäßig deaktiviert (`ENABLE_DIRECT_FETCH=false`), weil sich die Webseite jederzeit ändern kann.

## Lokal starten
```bash
cp .env.example .env
docker compose up -d --build
```
Dann `http://localhost:8080` öffnen.

Alternativ ohne Docker:
```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## iPhone
Die App muss für eine echte PWA-Funktion über HTTPS erreichbar sein. Danach in Safari öffnen → Teilen → „Zum Home-Bildschirm“.

## Hinweise zu Karten-/Routingdiensten
Die App nutzt OpenStreetMap-Kacheln, Nominatim für einzelne Geocoding-Anfragen und den öffentlichen OSRM-Demo-Router. Nominatim wird serverseitig gecacht und auf maximal etwa eine Anfrage pro Sekunde gedrosselt. Für größere oder gewerbliche Nutzung sollten eigene/kommerzielle Geocoding- und Routingdienste konfiguriert werden.


## iPhone/GitHub Upload
Diese Ausgabe ist absichtlich komplett flach aufgebaut. Alle Dateien können gemeinsam ins Hauptverzeichnis des GitHub-Repositories hochgeladen werden.
