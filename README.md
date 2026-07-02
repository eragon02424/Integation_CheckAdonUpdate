# Addon Update Checker

HACS Custom Integration für Home Assistant.

Überwacht automatisch alle Dockerfiles in einem GitHub Account auf veraltete externe Abhängigkeiten.

## Setup

1. Integration über HACS installieren
2. In HA: Einstellungen → Integrationen → Addon Update Checker hinzufügen
3. GitHub Username eingeben (z.B. `eragon02424`)
4. Scan-Intervall wählen (Standard: 24h, zum Testen: 1min)

## Funktionsweise

- Scannt stündlich alle Repos des GitHub Accounts nach Dockerfiles
- Erkennt externe GitHub Release Links im Dockerfile
- Vergleicht aktuell referenzierte Version mit neuester upstream Version
- Erster Fund: Baseline speichern, keine Warnung
- Neuere Version upstream: HA Warnung + Log-Eintrag
- Dockerfile/Repo entfernt: automatisch aus Überwachung entfernt

## Sensoren

Pro erkanntem Dockerfile:
- `sensor.auc_<repo>_<addon>_installed` — aktuell referenzierte Version
- `sensor.auc_<repo>_<addon>_latest` — neueste upstream Version
