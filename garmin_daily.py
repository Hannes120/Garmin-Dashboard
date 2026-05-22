#!/usr/bin/env python3
"""
Garmin Daily Dashboard — Hannes Raschke
Täglich automatisch ausgeführt von GitHub Actions um 06:15 Uhr CEST.

Architektur:
  1. get_garmin_data()       — rohe Garmin-Daten ziehen
  2. extract_fresh_metrics() — frischeste Datenpunkte isolieren + labeln
  3. get_day_context()       — Tagestyp, Trainingszeit, Feiertage
  4. get_events_context()    — Events mit Countdown + Phase
  5. generate_dashboard()    — alles an Claude → HTML
  6. send_email()            — HTML per iCloud verschicken
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

# ── CREDENTIALS ───────────────────────────────────────────────────────────────
GARMIN_EMAIL       = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD    = os.environ["GARMIN_PASSWORD"]
CLAUDE_API_KEY     = os.environ["CLAUDE_API_KEY"]
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
GMAIL_TO           = os.environ["GMAIL_TO"]


# ─────────────────────────────────────────────────────────────────────────────
# BAYERISCHE FEIERTAGE 2026
# ─────────────────────────────────────────────────────────────────────────────
BAVARIAN_HOLIDAYS = {
    datetime.date(2026, 1,  1): "Neujahr",
    datetime.date(2026, 1,  6): "Heilige Drei Könige",
    datetime.date(2026, 4,  3): "Karfreitag",
    datetime.date(2026, 4,  6): "Ostermontag",
    datetime.date(2026, 5,  1): "Tag der Arbeit",
    datetime.date(2026, 5, 14): "Christi Himmelfahrt",
    datetime.date(2026, 5, 25): "Pfingstmontag",
    datetime.date(2026, 6,  4): "Fronleichnam",
    datetime.date(2026, 8, 15): "Mariä Himmelfahrt",
    datetime.date(2026, 10, 3): "Tag der Deutschen Einheit",
    datetime.date(2026, 11, 1): "Allerheiligen",
    datetime.date(2026, 12,25): "1. Weihnachtstag",
    datetime.date(2026, 12,26): "2. Weihnachtstag",
}

WEEKDAY_DE = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag","Sonntag"]


# ─────────────────────────────────────────────────────────────────────────────
def get_garmin_data() -> dict:
    """Verbindet mit Garmin und zieht alle relevanten Rohdaten."""

    client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    client.login()

    today    = datetime.date.today()
    dates_14 = [(today - datetime.timedelta(days=i)).isoformat() for i in range(14)]
    dates_7  = dates_14[:7]

    data = {"pulled_at": datetime.datetime.now().isoformat(), "today": today.isoformat()}

    # Aktivitäten (letzte 20)
    try:
        data["activities"] = client.get_activities(0, 20)
        print(f"   Aktivitäten: {len(data['activities'])}")
    except Exception as e:
        print(f"   Aktivitäten: Fehler — {e}"); data["activities"] = []

    # Schlafdaten — wichtig: auch heute abrufen (nach Aufwachen schon verfügbar)
    sleep_list = []
    for date in dates_14:
        try:
            s = client.get_sleep_data(date)
            if s and isinstance(s, dict):
                dto = s.get("dailySleepDTO") or s
                if isinstance(dto, dict) and dto.get("calendarDate"):
                    sleep_list.append(dto)
        except:
            pass
    data["sleep"] = sleep_list
    print(f"   Schlaf: {len(sleep_list)} Nächte")

    # HRV — wichtig: heute zuerst, dann zurück
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
    print(f"   Readiness: {len(readiness_list)} Einträge")

    # Training Load (ATL/CTL/ACWR)
    try:
        data["training_load"] = client.get_training_load()
    except Exception as e:
        print(f"   Training Load: Fehler — {e}"); data["training_load"] = {}

    # Daily Stats heute (RHR, Stress, Steps, Kalorien)
    try:
        data["today_stats"] = client.get_stats(today.isoformat())
    except Exception as e:
        print(f"   Daily Stats: Fehler — {e}"); data["today_stats"] = {}

    # Body Battery letzte 7 Tage
    try:
        data["body_battery"] = client.get_body_battery(dates_7[-1], dates_7[0])
    except Exception as e:
        print(f"   Body Battery: Fehler — {e}"); data["body_battery"] = []

    # User Profile (VO2max etc.)
    try:
        data["profile"] = client.get_user_profile()
    except:
        data["profile"] = {}

    return data


# ─────────────────────────────────────────────────────────────────────────────
def extract_fresh_metrics(garmin_data: dict) -> dict:
    """
    Isoliert den aktuellsten Datenpunkt jeder Metrik.
    Jeder Wert bekommt ein 'freshness'-Label (HEUTE / GESTERN / Datum).
    Nur was wirklich vorhanden ist wird weitergegeben — kein Auffüllen mit None.
    """
    today     = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    result    = {}

    def freshness(date_str):
        if not date_str: return "unbekannt"
        if date_str == today.isoformat():     return "HEUTE"
        if date_str == yesterday.isoformat(): return "GESTERN"
        return date_str

    # ── SCHLAF ───────────────────────────────────────────────────────────────
    sleep_entries = garmin_data.get("sleep", [])
    if sleep_entries:
        latest = max(sleep_entries,
                     key=lambda x: x.get("calendarDate", ""), default=None)
        if latest:
            scores    = latest.get("sleepScores", {})
            overall   = (scores.get("overallScore")
                         or latest.get("overallSleepScore")
                         or scores.get("overall", {}).get("value"))
            dur_s     = (latest.get("deepSleepSeconds", 0)
                         + latest.get("lightSleepSeconds", 0)
                         + latest.get("remSleepSeconds", 0))
            spo2      = latest.get("spo2SleepSummary", {})
            date_val  = latest.get("calendarDate", "")
            entry     = {"freshness": freshness(date_val), "date": date_val}
            if overall:            entry["score"]       = overall
            if dur_s > 0:          entry["duration_h"]  = round(dur_s / 3600, 1)
            if latest.get("remSleepSeconds"):
                entry["rem_h"]  = round(latest["remSleepSeconds"] / 3600, 1)
            if latest.get("deepSleepSeconds"):
                entry["deep_h"] = round(latest["deepSleepSeconds"] / 3600, 1)
            if spo2.get("averageSPO2"): entry["spo2_avg"] = spo2["averageSPO2"]
            if spo2.get("lowestSPO2"):  entry["spo2_low"] = spo2["lowestSPO2"]
            result["sleep"] = entry

    # ── HRV ──────────────────────────────────────────────────────────────────
    hrv_entries = garmin_data.get("hrv", [])
    if hrv_entries:
        latest = max(hrv_entries, key=lambda x: x.get("date", ""), default=None)
        if latest:
            raw  = latest.get("data", {})
            entry = {"freshness": freshness(latest["date"]), "date": latest["date"]}

            # Garmin gibt HRV in unterschiedlichen Strukturen zurück
            def search_hrv(obj):
                if isinstance(obj, dict):
                    return (obj.get("weeklyAvg") or obj.get("hrvWeeklyAverage")
                            or obj.get("lastNight") or obj.get("lastNight5MinHigh")
                            or obj.get("hrvValue"))
                if isinstance(obj, list):
                    for item in obj:
                        v = search_hrv(item)
                        if v: return v
                return None

            weekly = None
            last   = None
            if isinstance(raw, dict):
                weekly = raw.get("weeklyAvg") or raw.get("hrvWeeklyAverage")
                last   = raw.get("lastNight") or raw.get("lastNight5MinHigh")
                if not (weekly or last):
                    summary = raw.get("hrvSummary", {})
                    weekly  = summary.get("weeklyAvg") or summary.get("weekly5MinHigh")
                    last    = summary.get("lastNight") or summary.get("lastNight5MinHigh")
            elif isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        weekly = item.get("weeklyAvg") or item.get("hrvWeeklyAverage")
                        last   = item.get("lastNight") or item.get("lastNight5MinHigh")
                        if weekly or last: break

            if weekly: entry["weekly_avg_ms"] = weekly
            if last:   entry["last_night_ms"] = last
            entry["raw_sample"] = str(raw)[:300]
            result["hrv"] = entry

    # ── TRAINING READINESS ───────────────────────────────────────────────────
    readiness_entries = garmin_data.get("readiness", [])
    if readiness_entries:
        latest = max(readiness_entries, key=lambda x: x.get("date", ""), default=None)
        if latest:
            raw   = latest.get("data", {})
            entry = {"freshness": freshness(latest["date"]), "date": latest["date"]}

            def find_readiness(obj):
                if isinstance(obj, dict):
                    score = obj.get("score") or obj.get("trainingReadinessScore")
                    level = obj.get("level") or obj.get("trainingReadinessLevel")
                    return score, level
                if isinstance(obj, list):
                    for item in obj:
                        s, l = find_readiness(item)
                        if s: return s, l
                return None, None

            score, level = find_readiness(raw)
            if score: entry["score"] = score
            if level: entry["level"] = level
            result["readiness"] = entry

    # ── TODAY STATS (RHR, BODY BATTERY, STRESS, STEPS) ──────────────────────
    ts = garmin_data.get("today_stats", {})
    if ts:
        today_entry = {"freshness": "HEUTE"}
        if ts.get("restingHeartRate"):           today_entry["rhr"]          = ts["restingHeartRate"]
        if ts.get("averageStressLevel"):         today_entry["stress_avg"]   = ts["averageStressLevel"]
        if ts.get("totalSteps"):                 today_entry["steps"]        = ts["totalSteps"]
        if ts.get("bodyBatteryChargedValue"):    today_entry["bb_charged"]   = ts["bodyBatteryChargedValue"]
        if ts.get("bodyBatteryDrainedValue"):    today_entry["bb_drained"]   = ts["bodyBatteryDrainedValue"]
        if ts.get("totalKilocalories"):          today_entry["kcal"]         = ts["totalKilocalories"]
        result["today_stats"] = today_entry

    # ── BODY BATTERY AKTUELL ─────────────────────────────────────────────────
    bb_data = garmin_data.get("body_battery", [])
    if isinstance(bb_data, list) and bb_data:
        try:
            last_bb = bb_data[-1]
            if isinstance(last_bb, dict):
                val = (last_bb.get("bodyBatteryLevel")
                       or last_bb.get("charged")
                       or last_bb.get("value"))
                if val: result["body_battery_now"] = val
        except:
            pass

    # ── TRAINING LOAD (ATL/CTL/ACWR) ────────────────────────────────────────
    tl = garmin_data.get("training_load", {})
    if tl:
        load_entry = {}
        if isinstance(tl, dict):
            atl  = tl.get("acuteLoad") or tl.get("weeklyRunningLoad")
            ctl  = tl.get("chronicLoad") or tl.get("4weekRunningLoad")
            acwr_val = tl.get("acuteChronicWorkloadRatio")
            if atl:     load_entry["atl"]  = atl
            if ctl:     load_entry["ctl"]  = ctl
            if acwr_val: load_entry["acwr"] = round(acwr_val, 2)
        elif isinstance(tl, list) and tl:
            latest_tl = max(tl, key=lambda x: x.get("calendarDate", ""), default={})
            load_entry["raw_latest"] = str(latest_tl)[:400]
        result["training_load"] = load_entry

    # ── LETZTE SCHWIMM-SESSION ───────────────────────────────────────────────
    # Garmin gibt activityType manchmal als String, manchmal als Dict zurück
    def get_type_str(a):
        t = a.get("activityType", "")
        if isinstance(t, dict):
            return str(t.get("typeKey") or t.get("typeId") or "").lower()
        return str(t).lower()

    activities = garmin_data.get("activities", [])
    swim_acts  = [a for a in activities if "swim" in get_type_str(a)]
    run_acts   = [a for a in activities if get_type_str(a) in
                  ["running","treadmill_running","trail_running","track_running"]]
    bike_acts  = [a for a in activities if "cycling" in get_type_str(a)]

    def last_activity_info(acts):
        if not acts: return None
        latest = max(acts, key=lambda x: x.get("startTimeLocal", 0), default=None)
        if not latest: return None
        ts = latest.get("startTimeLocal", 0)
        try:
            act_date = datetime.datetime.fromtimestamp(ts / 1000).date()
            days_ago = (today - act_date).days
        except:
            act_date, days_ago = None, None
        return {
            "date": act_date.isoformat() if act_date else "?",
            "days_ago": days_ago,
            "name": latest.get("name", ""),
            "distance_m": int(latest.get("distance", 0) / 100) if latest.get("distance") else 0,
            "duration_min": round(latest.get("duration", 0) / 60000, 0) if latest.get("duration") else 0,
            "avg_hr": latest.get("avgHr"),
        }

    if swim_acts:  result["last_swim"]  = last_activity_info(swim_acts)
    if run_acts:   result["last_run"]   = last_activity_info(run_acts)
    if bike_acts:  result["last_bike"]  = last_activity_info(bike_acts)

    return result


# ─────────────────────────────────────────────────────────────────────────────
def get_day_context(today_date: datetime.date) -> dict:
    """
    Berechnet: Tagestyp, verfügbares Trainingsfenster, Länge der möglichen Session.
    Hannes arbeitet bis 17. Juli 2026.
    Arbeitszeiten: 06:30 Uhr weg, ab ~16:30 Uhr trainingsfähig.
    """
    work_schedule_active = today_date <= datetime.date(2026, 7, 17)
    weekday  = today_date.weekday()   # 0=Mo, 6=So
    is_hol   = today_date in BAVARIAN_HOLIDAYS
    is_wkend = weekday >= 5
    is_fri   = weekday == 4
    holiday_name = BAVARIAN_HOLIDAYS.get(today_date, "")

    # Nächste freie Tage in den nächsten 10 Tagen
    upcoming_free = []
    for i in range(1, 11):
        d = today_date + datetime.timedelta(days=i)
        if d.weekday() >= 5 or d in BAVARIAN_HOLIDAYS:
            upcoming_free.append({
                "date": d.isoformat(),
                "weekday": WEEKDAY_DE[d.weekday()],
                "name": BAVARIAN_HOLIDAYS.get(d, "Wochenende"),
                "days_from_now": i
            })

    if is_wkend or is_hol:
        return {
            "type": "FREIER_TAG",
            "label": holiday_name or WEEKDAY_DE[weekday],
            "training_window": "Ganztags verfügbar",
            "double_session": True,
            "note": f"Ganztägig Zeit — längere Sessions und Doppeleinheiten möglich",
            "upcoming_free_days": upcoming_free,
        }
    elif is_fri and work_schedule_active:
        return {
            "type": "FREITAG",
            "label": "Freitag",
            "training_window": "Ab 16:30 Uhr, Abend offen",
            "double_session": False,
            "note": "Freitag — längeres Trainingsfenster als normale Arbeitstage",
            "upcoming_free_days": upcoming_free,
        }
    elif work_schedule_active:
        return {
            "type": "ARBEITSTAG",
            "label": WEEKDAY_DE[weekday],
            "training_window": "Ab 16:30 Uhr bis ~19:00 (max. 2.5h Fenster)",
            "double_session": False,
            "note": "Arbeitstag: Session muss kompakt sein. Kein Zeitdruck wenn Priorität gesetzt.",
            "upcoming_free_days": upcoming_free,
        }
    else:
        return {
            "type": "FREI_POST_ARBEIT",
            "label": WEEKDAY_DE[weekday],
            "training_window": "Ganztags (nach 17. Juli)",
            "double_session": True,
            "note": "Voller Trainingstag möglich",
            "upcoming_free_days": upcoming_free,
        }


# ─────────────────────────────────────────────────────────────────────────────
def get_events_context(today_date: datetime.date) -> list:
    """
    Events-Konfiguration. Neue Events einfach in der Liste eintragen.
    Gibt für jedes Event Countdown, Phase und Ziel zurück.
    """

    # ── EVENTS — hier eintragen ───────────────────────────────────────────────
    events_config = [
        {
            "name": "IfA Nonstop Triathlon Bamberg",
            "emoji": "🏊🚴🏃",
            "date": datetime.date(2026, 6, 7),
            "location": "Ebinger See, Rattelsdorf bei Bamberg",
            "type": "sprint_triathlon",
            "distance": "750m Schwimmen / 21km Radfahren / 5km Laufen",
            "goal": "Finishen + Spaß. Zielzeit ~1:32 (Swim 20min / Bike 38min / Run 30min).",
            "taper_days": 7,
            "taper_style": "LEICHT — Kein Ultra-Taper. Volumen max 40% reduzieren. Hannes soll frisch aber nicht eingerostet ankommen.",
            "disciplines": ["swim", "bike", "run"],
            "critical_notes": [
                "Schwimmen ist die vernachlässigte Disziplin — wenn >7 Tage kein Training: ROTE WARNUNG",
                "Mind. 2 Brick-Einheiten (Rad + direkt Lauf) vor dem Rennen absolviert haben",
                "Neopren im Freiwasser testen bevor Renntag",
                "ACWR am Renntag: Ziel 0.8–1.0",
            ],
        },
        {
            "name": "Halbmarathon (Geburtstag 🎂)",
            "emoji": "🏃",
            "date": datetime.date(2026, 7, 12),
            "location": "München (konkrete Veranstaltung TBD)",
            "type": "halfmarathon",
            "distance": "21.1km",
            "goal": "ADAPTIV: Basis-Ziel sub 2:00h (5:41/km). ABER: wenn VO2max-Trend + aktuelle Pace-Daten eine schnellere Zeit realistisch machen, Ziel dynamisch nach unten anpassen. Kein hartes Kratzen an der 2h-Grenze.",
            "goal_time_seconds": 7200,
            "goal_pace_per_km": "5:41/km",
            "taper_days": 10,
            "taper_style": "NORMAL — 10 Tage Taper, Volumen 30-40% reduzieren, Intensität halten.",
            "disciplines": ["run"],
            "critical_notes": [
                "Geburtstag von Hannes — soll Spaß machen",
                "Nach Bamberg (7. Juni) hat er 5 Wochen gezielten HM-Aufbau",
                "Wenn 5km-PR-Pace eine sub-1:50 HM impliziert → Ziel auf 1:50 anpassen",
            ],
        },
        # Weitere Events hier eintragen:
        # {
        #     "name": "Name",
        #     "emoji": "🏁",
        #     "date": datetime.date(2026, 9, 15),
        #     "location": "Ort",
        #     "type": "typ",
        #     "distance": "Strecke",
        #     "goal": "Ziel",
        #     "taper_days": 7,
        #     "taper_style": "...",
        #     "disciplines": ["run"],
        #     "critical_notes": [],
        # },
    ]

    result = []
    for ev in events_config:
        days_left = (ev["date"] - today_date).days
        if days_left < -2:
            continue  # Überspringe Events die >2 Tage zurückliegen

        if days_left < 0:               phase = "GERADE VORBEI"
        elif days_left == 0:            phase = "HEUTE 🔴🔴🔴"
        elif days_left <= 6:            phase = "RACE WEEK 🔴"
        elif days_left <= ev["taper_days"]: phase = "TAPER PHASE 🟡"
        elif days_left <= 14:           phase = "RACE SHARPENING 🟡"
        elif days_left <= 35:           phase = "Spezifische Vorbereitung 🟢"
        else:                           phase = "Basisaufbau 🟢"

        result.append({**ev, "days_left": days_left, "phase": phase,
                       "date_str": ev["date"].strftime("%d. %B %Y")})
    return result


# ─────────────────────────────────────────────────────────────────────────────
def generate_dashboard(garmin_data: dict) -> str:
    """
    Baut alle Kontextinformationen auf, schickt sie an Claude,
    bekommt fertiges HTML-Dashboard zurück.
    """
    client     = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    today      = datetime.date.today()
    today_str  = today.strftime("%A, %d. %B %Y")

    # ── Kontext aufbauen ─────────────────────────────────────────────────────
    metrics    = extract_fresh_metrics(garmin_data)
    day_ctx    = get_day_context(today)
    events     = get_events_context(today)

    # ── Schwimm-Warnung berechnen ─────────────────────────────────────────────
    last_swim  = metrics.get("last_swim", {})
    swim_days_ago = (last_swim.get("days_ago") or 999) if last_swim else 999
    swim_warning = ""
    if swim_days_ago >= 14:
        swim_warning = f"🚨 KRITISCH: Letztes Schwimmen vor {swim_days_ago} Tagen! Bamberg in {next((e['days_left'] for e in events if 'triathlon' in e.get('type','')), '?')} Tagen."
    elif swim_days_ago >= 7:
        swim_warning = f"⚠️ WARNUNG: Letztes Schwimmen vor {swim_days_ago} Tagen. Dringend wieder ins Wasser."

    # ── Metriken als lesbarer String ─────────────────────────────────────────
    metrics_str = json.dumps(metrics, indent=2, ensure_ascii=False)

    # ── Events als lesbarer String ────────────────────────────────────────────
    events_str = ""
    for ev in events:
        events_str += f"""
╔══ {ev['emoji']} {ev['name']} ══
║  Datum: {ev['date_str']} | Noch: {ev['days_left']} Tage | Phase: {ev['phase']}
║  Ort: {ev['location']}
║  Strecke: {ev['distance']}
║  Ziel: {ev['goal']}
║  Taper: {ev['taper_style']}
║  Wichtige Hinweise:
"""
        for note in ev.get("critical_notes", []):
            events_str += f"║  • {note}\n"
        events_str += "╚══\n"

    # ── Freie Tage als String ─────────────────────────────────────────────────
    free_days = day_ctx.get("upcoming_free_days", [])
    free_days_str = ", ".join(
        f"{d['weekday']} {d['date']} ({d['name']}, in {d['days_from_now']}d)"
        for d in free_days[:5]
    ) or "Keine in den nächsten 10 Tagen"

    # ── Aktivitäten (letzte 10) für Überblick ─────────────────────────────────
    acts = garmin_data.get("activities", [])[:10]
    acts_str = json.dumps(acts, indent=1, default=str, ensure_ascii=False)
    if len(acts_str) > 8000:
        acts_str = acts_str[:8000] + "..."

    # ── Sleep (letzte 7) für Tabellenansicht ─────────────────────────────────
    sleep_str = json.dumps(garmin_data.get("sleep", [])[:7], indent=1,
                           default=str, ensure_ascii=False)
    if len(sleep_str) > 5000:
        sleep_str = sleep_str[:5000] + "..."

    # ── Prompt ───────────────────────────────────────────────────────────────
    prompt = f"""Du bist der persönliche Ausdauer-Coach und Sports Scientist von Hannes Raschke.
Jetzt generierst du sein tägliches Dashboard für {today_str}.

═══ HANNES' PROFIL ═══
- 20 Jahre, 182cm, 81.8kg | Garmin Forerunner 965
- HFmax: 196 bpm | Z2: 118–137 bpm | Schwelle: 176 bpm | Z5 VO2max: >176 bpm
- VO2max Laufen: 47 | Biometrisch: 50.6 (Lücke = fehlende Z2-Basis)
- Kern-Problem: 79% der Laufzeit in Z4, 0% in Z2 → muss geändert werden
- FTP Real: 216W (Garmin zeigt 363W — ignorieren)
- 5km PR: 26:58 (5:24/km) vom 15. April 2026
{swim_warning}

═══ HEUTIGER TAG ═══
Typ: {day_ctx['type']} ({day_ctx['label']})
Trainingsfenster: {day_ctx['training_window']}
Doppeleinheit möglich: {'Ja' if day_ctx['double_session'] else 'Nein'}
Hinweis: {day_ctx['note']}
Nächste freie Tage: {free_days_str}

═══ FRISCHESTE METRIKEN (mit Datum) ═══
{metrics_str}

═══ LETZTE 10 AKTIVITÄTEN ═══
{acts_str}

═══ SCHLAF LETZTE 7 NÄCHTE (Rohdaten) ═══
{sleep_str}

═══ KOMMENDE EVENTS ═══
{events_str}

═══ DEINE AUFGABE ═══

Erstelle ein vollständiges, standalone HTML-Dashboard. Das Dashboard soll sich INTELLIGENT an den Tag anpassen — nicht jede Sektion immer zeigen, sondern nur was heute wirklich relevant ist.

PFLICHT-DESIGN:
- Kein externes CSS/JS/CDN — alles inline
- Mobil-first: optimiert für 375–430px Breite (iPhone)
- Dunkel: Hintergrund #080c08 | Grün #4cff7c | Amber #ffb84c | Rot #ff6060 | Text #ddeedd | Cards #0f140f
- Schrift: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif
- Sections mit max-width: 480px; margin: 0 auto; padding: 16px

STRUKTUR — IN DIESER REIHENFOLGE:

─── HEADER ───
"Guten Morgen, Hannes" + heutiges Datum + Wochentag
Klein, dezent, eine Zeile.
Falls es ein Feiertag oder Wochenende ist: kurzen freundlichen Hinweis.

─── METRIKEN (immer, ganz oben) ───
Alle verfügbaren Tageswerte als Grid. Für jede Metrik:
• Große Zahl + Einheit
• Label darunter
• Ampelfarbe: Grün / Amber / Rot basierend auf Schwellenwerten
• Datum/Freshness-Label (z.B. "heute nacht" / "gestern")

Zeige NUR was Daten hat — keine leeren Felder.
Empfohlene Metriken falls vorhanden:
Sleep Score | HRV (letzte Nacht ms + Wochenschnitt) | Training Readiness Score + Level
Body Battery | Ruheherzfrequenz | ACWR

Ampel-Schwellen:
Sleep: ≥85=grün, 70-84=amber, <70=rot
HRV: ≥63ms=grün, 50-62=amber, <50=rot (Basis Hannes: 57-61ms)
Readiness: ≥80=grün, 60-79=amber, <60=rot
Body Battery: ≥70=grün, 40-69=amber, <40=rot
RHR: ≤58=grün, 59-64=amber, ≥65=rot
ACWR: 0.8-1.3=grün, <0.8=amber, >1.3=rot

─── TAGES-EMPFEHLUNG (immer) ───
Groß + farbig + konkret. NICHT generisch.
Format:
[🟢/🟡/🔴] [HEUTE: Session-Titel]
Begründung in 2 Sätzen mit echten Zahlenwerten aus den Metriken.
Konkrete Session:
  • Typ + Distanz/Dauer
  • HR-Zonen in bpm
  • 
  • Falls FREIER TAG: darf länger sein, Doppeleinheit falls sinnvoll

─── NÄCHSTE 5 TAGE ───
Für jeden Tag:
Wochentag Datum | [FREI/ARBEIT/FEIERTAG] | Geplante Session | Intensität
Berücksichtige:
• Freie Tage (Wochenende/Feiertag) → längere/härtere Sessions möglich
• Arbeitstage → max 75 min ab 16:30
• Nach hartem Tag: Erholung einplanen
• Event-Phasen: wenn Taper → Volumen reduzieren
• Schwimmen priorisieren wenn Bamberg <35 Tage weg und >5 Tage kein Schwimmen

─── EVENTS (immer, kompakt) ───
Für jedes Event eine Karte:
[Emoji + Name] — [X TAGE] — [Phase]
Disziplin-Ampeln (basierend auf days_ago der letzten Session):
🏊 Swim: <5 Tage=grün, 5-10=amber, >10=rot
🚴 Bike: <7 Tage=grün, 7-14=amber, >14=rot
🏃 Run:  <4 Tage=grün, 4-7=amber, >7=rot
Fokus dieser Woche: 1-2 Sätze was jetzt am wichtigsten ist.
HM-Ziel adaptiv: wenn aktuelle Pace-Daten eine schnellere Zeit implizieren, zeige angepasstes Ziel.

─── SCHLAF LETZTE 7 NÄCHTE (kompakt) ───
Kompakte Tabelle: Datum | Score | Dauer | REM | SpO2-Min
Beste Nacht grün, schlechteste rot markieren.
Nur zeigen wenn ≥3 Nächte vorhanden.

─── LETZTE 7 TRAININGS (kompakt) ───
Kompakte Liste: Datum | Typ | Distanz | Dauer | Ø HR
Nur wenn vorhanden.

─── TRAINING LOAD (nur wenn ACWR-Daten vorhanden) ───
ATL | CTL | ACWR — Ampel + 1 Satz Interpretation.

INTELLIGENZ-REGELN:
• Wenn ACWR > 1.4: Erholungswarnung prominent ganz oben zeigen
• Wenn Readiness < 50: Ruhepflicht-Banner
• Wenn <7 Tage bis Event: Race-Week-Banner ersetze die normale Empfehlung
• Wenn eine Disziplin rot ist und <14 Tage bis Rennen: Warnung in Event-Karte
• Wenn HM-Pace-Hochrechnung (aus 5km PR 26:58) schneller als sub-2h: adaptiertes Ziel zeigen

STIL-REGELN:
• Cards: border-radius: 12px; padding: 16px; margin: 10px 0; background: #0f140f; border: 1px solid #1c261c
• Große KPI-Zahlen: font-size: 28-32px; font-weight: 700
• Section-Titel: font-size: 11px; text-transform: uppercase; letter-spacing: 0.12em; color: #3a5a3a
• Ampel-Farben auch als kleine Punkte (●) vor Werten

Antworte AUSSCHLIESSLICH mit vollständigem HTML. Direkt mit <!DOCTYPE html> beginnen."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}]
    )

    html = message.content[0].text.strip()
    if html.startswith("```"):
        html = html.split("\n", 1)[1]
        if html.endswith("```"):
            html = html.rsplit("```", 1)[0]
    return html.strip()


# ─────────────────────────────────────────────────────────────────────────────
def send_email(html_content: str, date_str: str):
    """Sendet das Dashboard als HTML-E-Mail via iCloud SMTP."""

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = f"🏃 {date_str} — Garmin Dashboard"
    msg["From"]    = GMAIL_USER
    msg["To"]      = GMAIL_TO

    plain = MIMEText(
        f"Dein Garmin Dashboard für {date_str}. "
        "Öffne diese E-Mail in einem HTML-fähigen Client.",
        "plain", "utf-8"
    )
    msg.attach(plain)
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    with smtplib.SMTP("smtp.mail.me.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, GMAIL_TO, msg.as_string())


# ─────────────────────────────────────────────────────────────────────────────
def main():
    today_str = datetime.date.today().strftime("%d. %m. %Y")
    print(f"\n{'='*52}")
    print(f"  Garmin Daily Dashboard — {today_str}")
    print(f"{'='*52}\n")

    try:
        print("1/3 — Garmin-Daten laden...")
        garmin_data = get_garmin_data()
        print(f"   ✓ Daten geladen\n")

        print("2/3 — Claude generiert Dashboard...")
        html_dashboard = generate_dashboard(garmin_data)
        print(f"   ✓ Dashboard fertig ({len(html_dashboard):,} Zeichen)\n")

        print("3/3 — E-Mail senden...")
        send_email(html_dashboard, today_str)
        print(f"   ✓ Gesendet an {GMAIL_TO}\n")

        print("✅ FERTIG — bis morgen!\n")

    except Exception as e:
        print(f"\n❌ FEHLER: {e}")
        traceback.print_exc()
        try:
            error_html = f"""<!DOCTYPE html>
<html><body style="background:#1a0000;color:#ff8080;font-family:monospace;padding:20px">
<h2>⚠️ Dashboard Fehler — {today_str}</h2>
<pre style="background:#2a0000;padding:15px;border-radius:8px;white-space:pre-wrap">{traceback.format_exc()}</pre>
</body></html>"""
            send_email(error_html, today_str)
        except:
            pass
        raise


if __name__ == "__main__":
    main()
