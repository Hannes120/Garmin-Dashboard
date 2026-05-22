#!/usr/bin/env python3
"""
Garmin Daily Dashboard — Hannes Raschke
Täglich automatisch ausgeführt von GitHub Actions.
Zieht alle Garmin-Daten, schickt sie an Claude API,
generiert ein HTML-Dashboard und sendet es per E-Mail.
"""

import os
import json
import smtplib
import datetime
import traceback
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from garminconnect import Garmin
import anthropic

# ── CREDENTIALS — kommen aus GitHub Secrets ───────────────────────────────────
GARMIN_EMAIL      = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD   = os.environ["GARMIN_PASSWORD"]
CLAUDE_API_KEY    = os.environ["CLAUDE_API_KEY"]
GMAIL_USER        = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD= os.environ["GMAIL_APP_PASSWORD"]
GMAIL_TO          = os.environ["GMAIL_TO"]


# ─────────────────────────────────────────────────────────────────────────────
def get_garmin_data():
    """Verbindet mit Garmin und zieht alle relevanten Daten der letzten 30 Tage."""

    client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    client.login()

    today     = datetime.date.today()
    dates_30  = [(today - datetime.timedelta(days=i)).isoformat() for i in range(30)]
    dates_14  = dates_30[:14]
    dates_7   = dates_30[:7]

    data = {"generated_at": today.isoformat()}

    # Aktivitäten
    try:
        data["activities"] = client.get_activities(0, 30)
        print(f"   Aktivitäten: {len(data['activities'])}")
    except Exception as e:
        print(f"   Aktivitäten Fehler: {e}")
        data["activities"] = []

    # Schlaf
    sleep_list = []
    for date in dates_14:
        try:
            s = client.get_sleep_data(date)
            if s and s.get("dailySleepDTO"):
                sleep_list.append(s["dailySleepDTO"])
        except:
            pass
    data["sleep"] = sleep_list
    print(f"   Schlafdaten: {len(sleep_list)} Nächte")

    # HRV Status
    hrv_list = []
    for date in dates_7:
        try:
            h = client.get_hrv_data(date)
            if h:
                hrv_list.append({"date": date, "data": h})
        except:
            pass
    data["hrv"] = hrv_list
    print(f"   HRV: {len(hrv_list)} Tage")

    # Training Readiness
    readiness_list = []
    for date in dates_7:
        try:
            r = client.get_training_readiness(date)
            if r:
                readiness_list.append({"date": date, "data": r})
        except:
            pass
    data["readiness"] = readiness_list

    # Training Load (ATL/CTL/ACWR)
    try:
        data["training_load"] = client.get_training_load()
    except Exception as e:
        print(f"   Training Load Fehler: {e}")
        data["training_load"] = {}

    # Daily Stats — heutige Übersicht (Body Battery, Stress, Steps)
    try:
        data["today_stats"] = client.get_stats(today.isoformat())
    except Exception as e:
        print(f"   Daily Stats Fehler: {e}")
        data["today_stats"] = {}

    # User Profile + VO2max
    try:
        data["profile"] = client.get_user_profile()
    except:
        data["profile"] = {}

    # Body Battery letzte 7 Tage
    try:
        data["body_battery"] = client.get_body_battery(
            dates_7[-1], dates_7[0]
        )
    except Exception as e:
        print(f"   Body Battery Fehler: {e}")
        data["body_battery"] = []

    return data


# ─────────────────────────────────────────────────────────────────────────────
def generate_dashboard(garmin_data: dict) -> str:
    """Schickt Garmin-Daten an Claude und bekommt ein fertiges HTML-Dashboard."""

    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    today_str = datetime.date.today().strftime("%A, %d. %B %Y")
    today_date = datetime.date.today()

    # ── EVENTS — hier einfach neue Rennen/Events eintragen ───────────────────
    # Format: ("Name", "YYYY-MM-DD", "Ort", "Distanz/Format", "Notizen")
    events = [
        ("IfA Nonstop Triathlon Bamberg",
         "2026-06-07",
         "Ebinger See, Rattelsdorf",
         "Sprint: 750m Swim / 21km Bike / 5km Run",
         "Erster Triathlon, Massenstart 13:30 Uhr"),
        # Weitere Events einfach hier eintragen:
        # ("Name", "2026-08-01", "Ort", "Format", "Notiz"),
    ]

    events_info = ""
    for name, ev_date_str, loc, dist, note in events:
        ev_date = datetime.date.fromisoformat(ev_date_str)
        days_left = (ev_date - today_date).days
        if days_left >= 0:
            if days_left <= 6:    phase = "RACE WEEK 🔴"
            elif days_left <= 13: phase = "Taper 🟡"
            elif days_left <= 29: phase = "Race-Sharpening 🟡"
            elif days_left <= 59: phase = "Spezifische Vorbereitung 🟢"
            else:                 phase = "Basisaufbau 🟢"
            events_info += (
                f"\n• {name} | {ev_date_str} | NOCH {days_left} TAGE"
                f" | Phase: {phase}"
                f"\n  Ort: {loc} | Strecke: {dist}"
                f"\n  Notiz: {note}\n"
            )

    # Daten auf 60k Zeichen kürzen (API-Limit)
    data_str = json.dumps(garmin_data, indent=2, default=str)
    if len(data_str) > 60000:
        data_str = data_str[:60000] + "\n... [gekürzt]"

    prompt = f"""Du bist ein daten-getriebener Ausdauer-Coach und Sports Scientist.
Analysiere diese Garmin-Daten von Hannes Raschke:
- 20 Jahre, 182cm, 81.8kg
- Garmin Forerunner 965
- HFmax: 196 bpm | Z2: 118–137 bpm | Z5 VO2max: >176 bpm
- Aktuelle VO2max (Garmin): 47 | Biometrisch: 50.6
- Kern-Problem: 79% der Laufzeit in Zone 4, 0% in Zone 2
- FTP Real: 216W (Garmin-Anzeige 363W ist falsch)
- Schwimm-Warnung: wenn letzte Schwimmeinheit >7 Tage her → rote Warnung

KOMMENDE EVENTS:
{events_info}

GARMIN DATEN ({today_str}):
{data_str}

Erstelle ein vollständiges, standalone HTML-Dashboard. WICHTIG:
- Kein externes CSS, kein externes JavaScript, kein CDN
- Alles inline im HTML
- Mobil-optimiert (Handy-Bildschirm)
- Dunkel: Hintergrund #080c08, Grün #4cff7c, Amber #ffb84c, Rot #ff6060, Text #ddeedd
- Schrift: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif

Das Dashboard muss EXAKT in dieser Reihenfolge aufgebaut sein:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## SEKTION 1 — HEADER: Datum + Name
Klein, dezent. Nur "Guten Morgen Hannes — {today_str}"
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## SEKTION 2 — AKTUELLE METRIKEN (ganz oben, prominent)
Ein großes KPI-Raster mit ALLEN heutigen Werten auf einen Blick.
Zeige jeden Wert als große Zahl + Label + Ampelfarbe (Grün/Amber/Rot):

- Sleep Score letzte Nacht (Zahl + Bewertung)
- HRV heute morgen in ms (Zahl + Trend-Pfeil vs. Wochenschnitt)
- Training Readiness Score (Zahl + Level wie PRIME/HIGH/MODERATE/LOW)
- Body Battery aktuell (Zahl von 100)
- Ruheherzfrequenz heute (bpm)
- VO2max Laufen / Biometrisch (47 / 50.6 — erkläre kurz den Unterschied in 1 Satz darunter)
- ACWR (Zahl + Ampel: <0.8 = zu wenig, 0.8–1.3 = optimal, >1.3 = Achtung)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## SEKTION 3 — TAGES-EMPFEHLUNG
Erst NACHDEM die Metriken gezeigt wurden, kommt die Empfehlung.
Groß und klar: GRÜN / ORANGE / ROT
Begründung: "HRV X ms (Y% über Baseline), Sleep Z, ACWR W → deshalb..."
Konkrete Session: Typ, Distanz/Dauer, exakte HR-Zonen in bpm.
Beispiel: "4×4 min Intervalle @ 167–177 bpm · WU 2km Z2 · CD 1.5km"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## SEKTION 4 — TRAININGSPLAN NÄCHSTE 5 TAGE
Basierend auf aktuellem ACWR, HRV-Trend und Bamberg-Countdown:
Zeige für jeden der nächsten 5 Tage (heute bis +4 Tage):
- Wochentag + Datum
- Geplante Session (Typ, Distanz, HR-Zone)
- Intensitätslevel: HART / MITTEL / LEICHT / RUHE
- Kurze Begründung (1 Satz)

Berücksichtige dabei:
- Nach harten Sessions mindestens 1 Ruhe/Leicht-Tag
- Swim-Sessions wegen Bamberg priorisieren wenn >7 Tage keine Schwimmeinheit
- Bamberg-Countdown: wenn <14 Tage bis 7. Juni → Tapering einleiten

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## SEKTION 5 — SCHLAF LETZTE 7 NÄCHTE
Kompakte Tabelle: Datum | Score | Dauer | REM | Deep | SpO2-Min
Schlechteste Nacht rot markieren, beste grün.

## SEKTION 6 — LETZTE 7 TAGE TRAINING
Jede Session: Datum | Typ | Distanz | Dauer | Avg HR | Note

## SEKTION 7 — TRAINING LOAD
ATL | CTL | ACWR mit Ampel + 1 Satz Interpretation

## SEKTION 8 — EVENTS & VORBEREITUNG
Zeige alle kommenden Events als Karten, sortiert nach Datum.
Für jedes Event:

┌─────────────────────────────────────────┐
│  🏁 [Event-Name]                        │
│  Datum · Ort · Distanz/Format           │
│  ── X TAGE ──  (große Zahl, Ampelfarbe) │
│                                         │
│  Vorbereitungsphase:                    │
│  > 60 Tage  → "Basisaufbau"             │
│  30–60 Tage → "Spezifische Vorbereitung"│
│  14–30 Tage → "Race-Sharpening"         │
│  7–14 Tage  → "Taper beginnt"           │
│  < 7 Tage   → "Race Week"               │
│                                         │
│  Disziplin-Status (Ampel):              │
│  🏊 Schwimmen — letzte Session + Note   │
│  🚴 Radfahren — letzte Session + Note   │
│  🏃 Laufen   — letzte Session + Note    │
│                                         │
│  FOKUS DIESE WOCHE:                     │
│  Konkret was jetzt wichtigste ist       │
│  basierend auf verbleibenden Tagen      │
│  und letzten Sessions pro Disziplin     │
└─────────────────────────────────────────┘

BEKANNTE EVENTS — berechne Tage ab heute ({today_str}):

EVENT 1:
- Name: IfA Nonstop Triathlon Bamberg
- Datum: 7. Juni 2026
- Ort: Ebinger See, Rattelsdorf bei Bamberg
- Format: Sprint-Triathlon
- Strecke: 750m Schwimmen → 21km Radfahren → 5km Laufen
- Besonderheit: Erster Triathlon von Hannes, Massenstart 13:30 Uhr

Wenn <14 Tage bis Bamberg: zeige detaillierten Taper-Plan.
Wenn <7 Tage: zeige Renntag-Checkliste.

WICHTIGE VORBEREITUNGSREGELN für Bamberg (aus Hannes' Datenprofil):
- Schwimmen: Kritisch — wenn >7 Tage keine Schwimmeinheit → rote Warnung + sofortiger Handlungsbedarf
- Brick-Trainings (Rad+Lauf direkt hintereinander): mind. 2 vor dem Rennen
- Neopren: Muss im Freiwasser getestet werden
- ACWR Renntag: sollte zwischen 0.8–1.0 liegen → Taper entsprechend planen

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DESIGN-REGELN:
- Sektion 2 (Metriken) und Sektion 3 (Empfehlung) sind die wichtigsten — groß und klar
- Sektionen 5–8 sind Detailinfo — kompakter, weniger Platz
- Mobil-first: alles auf 375px Breite lesbar
- Kein scroll nötig für Sektionen 1–3 (above the fold auf dem Handy)

Antworte AUSSCHLIESSLICH mit dem vollständigen HTML. Kein Text davor oder danach.
Kein ```html Wrapper. Fang direkt mit <!DOCTYPE html> an."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}]
    )

    html = message.content[0].text.strip()
    # Falls Claude doch einen Code-Block gesendet hat, entfernen
    if html.startswith("```"):
        html = html.split("\n", 1)[1]
        if html.endswith("```"):
            html = html.rsplit("```", 1)[0]
    return html


# ─────────────────────────────────────────────────────────────────────────────
def send_email(html_content: str, date_str: str):
    """Sendet das Dashboard als HTML-E-Mail via Gmail SMTP."""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🏃 Garmin Dashboard — {date_str}"
    msg["From"]    = GMAIL_USER
    msg["To"]      = GMAIL_TO

    # Plain-Text Fallback
    plain = MIMEText(
        f"Dein Garmin Dashboard für {date_str}.\n"
        "Öffne diese E-Mail in einem HTML-fähigen Client.",
        "plain", "utf-8"
    )
    html_part = MIMEText(html_content, "html", "utf-8")

    msg.attach(plain)
    msg.attach(html_part)

    with smtplib.SMTP("smtp.mail.me.com", 587) as server:
        server.ehlo()
        server.starttls()          # iCloud braucht STARTTLS
        server.ehlo()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, GMAIL_TO, msg.as_string())


# ─────────────────────────────────────────────────────────────────────────────
def main():
    today_str = datetime.date.today().strftime("%d. %m. %Y")
    print(f"\n{'='*50}")
    print(f"  Garmin Daily Dashboard — {today_str}")
    print(f"{'='*50}\n")

    try:
        print("SCHRITT 1/3 — Garmin-Daten laden...")
        garmin_data = get_garmin_data()
        print(f"   ✓ Daten geladen\n")

        print("SCHRITT 2/3 — Claude generiert Dashboard...")
        html_dashboard = generate_dashboard(garmin_data)
        print(f"   ✓ Dashboard fertig ({len(html_dashboard):,} Zeichen)\n")

        print("SCHRITT 3/3 — E-Mail senden...")
        send_email(html_dashboard, today_str)
        print(f"   ✓ E-Mail an {GMAIL_TO} gesendet\n")

        print("✅ FERTIG — bis morgen!\n")

    except Exception as e:
        print(f"\n❌ FEHLER: {e}")
        traceback.print_exc()

        # Fehler-E-Mail senden damit du weißt was passiert ist
        try:
            error_html = f"""<!DOCTYPE html>
<html><body style="background:#1a0000;color:#ff8080;font-family:sans-serif;padding:20px">
<h2>⚠️ Garmin Dashboard Fehler — {today_str}</h2>
<pre style="background:#2a0000;padding:15px;border-radius:8px;white-space:pre-wrap">{traceback.format_exc()}</pre>
</body></html>"""
            send_email(error_html, today_str)
        except:
            pass
        raise


if __name__ == "__main__":
    main()
