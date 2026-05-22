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

    # Daten auf 60k Zeichen kürzen (API-Limit)
    data_str = json.dumps(garmin_data, indent=2, default=str)
    if len(data_str) > 60000:
        data_str = data_str[:60000] + "\n... [gekürzt]"

    prompt = f"""Du bist ein daten-getriebener Ausdauer-Coach und Sports Scientist.
Analysiere diese Garmin-Daten von Hannes Raschke:
- 20 Jahre, 182cm, 81.8kg
- Sprint-Triathlon: IfA Nonstop Bamberg, 7. Juni 2026 (750m Swim / 21km Bike / 5km Run)
- Garmin Forerunner 965
- HFmax: 196 bpm | Z2: 118–137 bpm | Z5 VO2max: >176 bpm
- Aktuelle VO2max (Garmin): 47 | Biometrisch: 50.6
- Kern-Problem: 79% der Laufzeit in Zone 4, 0% in Zone 2
- FTP Real: 216W (Garmin-Anzeige 363W ist falsch)

GARMIN DATEN ({today_str}):
{data_str}

Erstelle ein vollständiges, standalone HTML-Dashboard. WICHTIG:
- Kein externes CSS, kein externes JavaScript, kein CDN
- Alles inline im HTML
- Mobil-optimiert (Handy-Bildschirm)
- Dunkel: Hintergrund #080c08, Grün #4cff7c, Amber #ffb84c, Rot #ff6060, Text #ddeedd
- Schrift: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif

Das Dashboard muss genau diese Sektionen enthalten:

## 1. TAGES-EMPFEHLUNG — ganz oben, groß, prominent
Basierend auf heutiger HRV, Sleep Score letzte Nacht, ACWR und Body Battery:
- GRÜN: Konkrete Session mit HR-Zonen und Distanz/Dauer
- ORANGE: Leichte Session — was genau
- ROT: Ruhetag — warum genau
Zahl + Begründung in 2–3 Sätzen aus den echten Datenwerten.

## 2. HEUTE AUF EINEN BLICK — KPI-Raster
VO2max | RHR | HRV heute | Sleep Score letzte Nacht | Body Battery | Training Readiness Score | ACWR

## 3. TRAINING LOAD — ATL / CTL / ACWR
Aktuelle Werte + Ampel (Grün/Orange/Rot) + 1 Satz Interpretation

## 4. LETZTE 7 TAGE — Trainingsübersicht
Jede Session: Datum, Typ, Distanz, Dauer, Avg HR, VO2max falls vorhanden

## 5. SCHLAF LETZTE 7 NÄCHTE
Tabelle: Datum | Score | Dauer | REM | Deep | SpO2-Min

## 6. BAMBERG COUNTDOWN
Verbleibende Tage bis 7. Juni 2026.
Status-Ampel für Swim / Bike / Run basierend auf letzten Sessions.
Nächste geplante Session pro Disziplin.

## 7. VO2MAX TRACKER
Letzter bekannter Wert + Trend der letzten Wochen aus den Daten.
Leading-Indikator: Pace @ 130bpm wenn messbar.

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
