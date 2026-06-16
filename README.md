# Garmin Dashboard — Robust Setup & Deployment

## 📋 Überblick

Diese überarbeitete Version garantiert:
- ✓ **Konsistente Schlafdaten** (robuster Login, mehrfache Nacht-Suche)
- ✓ **Body Battery** funktioniert zuverlässig (multi-Format Parser)
- ✓ **Trainings werden IMMER ausgegeben** (Fallback-Coach, kein hartes Abbrechen)
- ✓ **Idempotenz** (genau 1 Mail pro Tag, zuverlässiger Token-Cache)

---

## 🚀 Schritt 1: Token generieren (lokal)

Das Wichtigste: Einmaliger Login mit MFA, der Token wird dann im CI wiederverwendet.

```bash
# 1. Script lokal herunterladen und ausführen
python3 generate_garmin_token.py

# 2. Du wirst aufgefordert:
#    📧 Garmin Email: deine.email@example.com
#    🔑 Garmin Passwort: [versteckte Eingabe]
#    (Falls MFA aktiv: sieh in deine Garmin-App oder Email)

# 3. Script gibt einen LANGEN Base64-String aus → kopieren!
```

---

## 🔑 Schritt 2: Secret in GitHub speichern

1. Gehe zu: **https://github.com/Hannes120/Garmin-Dashboard/settings/secrets/actions**
2. Klick auf **"New repository secret"**
3. Name: `GARMIN_TOKEN_BASE64`
4. Value: [den Base64-String aus Schritt 1 einfügen]
5. **"Add secret"**

Optional (falls kein Token verfügbar, als Fallback):
- `GARMIN_EMAIL` → deine Garmin-Email
- `GARMIN_PASSWORD` → dein Garmin-Passwort

---

## 📧 Schritt 3: Email-Secrets

Diese sind nötig, damit die Mail rausgeht:

| Secret | Wert |
|--------|------|
| `GMAIL_USER` | deine iCloud/Gmail-Adresse (Sender) |
| `GMAIL_APP_PASSWORD` | App-spezifisches Passwort (nicht dein normales PW!) |
| `GMAIL_TO` | Empfänger-Email (kann gleich sein) |
| `CLAUDE_API_KEY` | dein Anthropic API Key |

### Email-Setup (iCloud/Gmail):

**iCloud (empfohlen):**
- Gehe zu https://appleid.apple.com → "App-spezifische Passwörter"
- Erzeuge eines für "Mail" → copy-paste als `GMAIL_APP_PASSWORD`
- `SMTP_HOST`: `smtp.mail.me.com` (wird in Workflow schon gesetzt)

**Gmail:**
- Ähnlich: https://myaccount.google.com → "Sicherheit" → "App-Passwörter"

---

## 💾 Schritt 4: Code ins Repo pushen

```bash
# 1. Dateien ersetzen/hinzufügen:
garmin_daily.py              # → ins Repo-Root
requirements.txt             # → ins Repo-Root (bestehende ersetzen)
.github/workflows/Dashboard.yml  # → ggf. alte löschen, diese neu

# 2. Committen & pushen
git add garmin_daily.py requirements.txt .github/workflows/Dashboard.yml
git commit -m "refactor: robust garmin dashboard with fallback coach"
git push
```

---

## 🧪 Schritt 5: Testen (einmalig)

1. Gehe zu **GitHub → Actions → "Training Dashboard"**
2. **"Run workflow"** klicken
3. `force_send`: `true` (damit wird dir die Mail auch heute noch gesendet, selbst wenn schon eine raus ist)
4. **"Run workflow"**
5. Warten, dann in den Logs schauen nach:

```
✓ Garmin login OK (Session aus Token-Store fortgesetzt)
✓ Schlafdaten gefunden für Nacht 2026-06-15 (Score=78, 7.5h)
✓ Body Battery: 65
✓ Claude: heutiges Workout...
✓ Claude: Wochenplan...
✓ Email an deine.email@example.com gesendet
✓ Workout zu Garmin Connect hochgeladen
```

Falls Error: Logs kopieren, dann bei mir nachfragen.

---

## 📅 Automatische Läufe

Der Workflow läuft ab sofort täglich zu drei Zeiten (UTC):
- **04:30 UTC** = 06:30 CEST (Frühaufsteher)
- **06:30 UTC** = 08:30 CEST (Standard)
- **09:00 UTC** = 11:00 CEST (Langschläfer-Backup)

Idempotent: jeder Tag gibt es **max. 1 Mail**, egal wie oft der Workflow läuft.

---

## 🆘 Troubleshooting

### "Schlafdaten noch nicht gesynct"
→ Normal, wenn die Watch noch nicht zu Garmin Connect hochgeladen hat. Der Workflow wartet nicht (braucht nicht mehr), plane einfach konservativ.

### "Body Battery null"
→ Sollte nicht mehr vorkommen. Wenn doch: in Logs nach "body_battery fehlgeschlagen" suchen → poste die Exception.

### "Kein Workout ausgegeben"
→ Gibt es nicht mehr. Der Fallback-Coach übernimmt (polarisierte Standardwoche nach Wochentag + Recovery-Ampel).

### "Email kommt nicht an"
→ Logs prüfen nach "Email fehlgeschlagen". Typisch:
- GMAIL_APP_PASSWORD ist falsch (muss App-spezifisch sein, nicht das normale PW)
- SMTP_HOST für dein Provider (iCloud: `smtp.mail.me.com`, Gmail: `smtp.gmail.com`)

---

## 📊 Dashboard-Output

Jede Mail enthält:

**Status-Kacheln:**
- Body Battery / Training Readiness / HRV / Ruhepuls / Schlaf / Schlaf-Score / CTL / TSB

**Heutiges Training:**
- Sportart (Schwimmen / Rad / Laufen / Kraft / Ruhe)
- Dauer & Intensität (Easy / Moderate / Hard)
- Struktur (Warm-up / Main / Cool-down)
- Begründung (warum dieser Plan?)

**Wochenplan:**
- Mo–So mit Sportart, Fokus, Dauer, Intensität
- Wochenthema (z. B. "Base-Phase: Grundlagen")

**Zonenverteilung (7 Tage):**
- Z1–Z5 Prozente
- Prüfung gegen 80/20-Regel (Polarisierung)

**Wettkampf-Countdown:**
- Nächste Races mit Tagen bis dahin

---

## 🔄 Updates & Anpassungen

### Rennkalender erweitern

In `garmin_daily.py`, ca. Zeile 70:
```python
RACE_CALENDAR = [
    {"name": "Halbmarathon Geburtstag", "date": "2026-07-12", "type": "run", "goal": "Sub 2:00 h"},
    {"name": "Königsbrunn Middle Distance", "date": "2026-09-20", "type": "triathlon", "goal": "Sub 6:00 h (1.9k/80k/20k)"},
    # Neue Races hier hinzufügen:
    {"name": "Ironman Frankfurt", "date": "2026-10-25", "type": "triathlon", "goal": "Sub 10:00 h"},
]
```

### Tageszeiten für Workflow anpassen

In `.github/workflows/Dashboard.yml`:
```yaml
- cron: "30 4 * * *"   # 06:30 CEST  (TIMES SIND UTC!)
- cron: "30 6 * * *"   # 08:30 CEST
- cron: "0 9 * * *"    # 11:00 CEST
```
Zeitzone: UTC. CEST = UTC+2.

---

## ✅ Checkliste

- [ ] Token generieren (`generate_garmin_token.py` lokal ausführen)
- [ ] `GARMIN_TOKEN_BASE64` in GitHub Secrets speichern
- [ ] Email-Secrets speichern (`GMAIL_USER`, `GMAIL_APP_PASSWORD`, `GMAIL_TO`, `CLAUDE_API_KEY`)
- [ ] Code pushen (3 Dateien)
- [ ] Einmaltest: "Run workflow" mit `force_send=true`
- [ ] Logs prüfen: "login OK", "Schlaf", "Email gesendet"
- [ ] Fertig! 🎉

---

## 📞 Support

Falls etwas nicht läuft: Action-Logs posten (anonym deine Secrets!) → ich debugge.

Viel Erfolg mit dem robusten Dashboard! 🏋️‍♂️
