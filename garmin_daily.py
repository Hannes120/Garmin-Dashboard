#!/usr/bin/env python3
"""
Garmin Daily Dashboard — Hannes Raschke
Täglich automatisch ausgeführt von GitHub Actions.

Ablauf:
  1. Garmin-Login
  2. Sleep-Polling — wartet bis Score verfügbar (bis zu 4h, deckt Aufstehen bis 9:00 ab)
  3. Alle Daten laden
  4. Kontext aufbauen (Tag, Events, Metriken)
  5. Claude → Workout-Entscheidung (VO2max-fokussiert, polarisiertes Training)
  6. Workout in Garmin Connect hochladen
  7. Claude → HTML-Dashboard
  8. E-Mail senden
"""

import os, json, smtplib, datetime, traceback, time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from garminconnect import Garmin
import anthropic

GARMIN_EMAIL       = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD    = os.environ["GARMIN_PASSWORD"]
CLAUDE_API_KEY     = os.environ["CLAUDE_API_KEY"]
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
GMAIL_TO           = os.environ["GMAIL_TO"]

WEEKDAY_DE = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag","Sonntag"]

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


# ═══════════════════════════════════════════════════════════════════
# 1. GARMIN CLIENT
# ═══════════════════════════════════════════════════════════════════
def get_garmin_client() -> Garmin:
    client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    client.login()
    print("   ✓ Garmin Login erfolgreich")
    return client


# ═══════════════════════════════════════════════════════════════════
# 2. SLEEP POLLING — wartet bis Score verfügbar (max 4h)
# ═══════════════════════════════════════════════════════════════════
def wait_for_sleep_score(client: Garmin,
                          max_wait_min: int = 240,
                          poll_interval_sec: int = 300) -> bool:
    """
    Prüft alle 5 Minuten ob Sleep Score für HEUTE da ist.
    Max 4 Stunden Wartezeit — deckt Aufstehen bis 09:00 Uhr ab
    (Skript startet 04:00 CEST + 4h = 08:00, mit Puffer bis ~09:00).
    Gibt True zurück wenn Score gefunden, False nach Timeout.
    """
    today      = datetime.date.today()
    start_time = datetime.datetime.now()
    max_delta  = datetime.timedelta(minutes=max_wait_min)
    attempt    = 0

    print(f"   Sleep Polling für {today} | Max {max_wait_min}min | Check alle {poll_interval_sec//60}min")

    while datetime.datetime.now() - start_time < max_delta:
        attempt += 1
        try:
            raw = client.get_sleep_data(today.isoformat())
            if raw and isinstance(raw, dict):
                dto    = raw.get("dailySleepDTO") or raw
                scores = dto.get("sleepScores", {}) if isinstance(dto, dict) else {}
                score  = (scores.get("overallScore")
                          or dto.get("overallSleepScore")
                          or scores.get("overall", {}).get("value"))
                if score and int(score) > 0:
                    elapsed = (datetime.datetime.now() - start_time).seconds // 60
                    print(f"   ✓ Sleep Score {score} — nach {elapsed}min (Versuch {attempt})")
                    return True
        except Exception as e:
            print(f"   Poll {attempt} Fehler: {e}")

        elapsed_min = (datetime.datetime.now() - start_time).seconds // 60
        print(f"   Versuch {attempt}: kein Score — {elapsed_min}min gewartet")
        time.sleep(poll_interval_sec)

    print(f"   ⚠️ Timeout {max_wait_min}min — weiter ohne Sleep Score")
    return False


# ═══════════════════════════════════════════════════════════════════
# 3. DATEN LADEN
# ═══════════════════════════════════════════════════════════════════
def get_garmin_data(client: Garmin) -> dict:
    today    = datetime.date.today()
    dates_14 = [(today - datetime.timedelta(days=i)).isoformat() for i in range(14)]
    dates_7  = dates_14[:7]
    data = {"pulled_at": datetime.datetime.now().isoformat(), "today": today.isoformat()}

    try:
        data["activities"] = client.get_activities(0, 60)
        print(f"   Aktivitäten: {len(data['activities'])}")
    except Exception as e:
        print(f"   Aktivitäten Fehler: {e}"); data["activities"] = []

    sleep_list = []
    for date in dates_14:
        try:
            s = client.get_sleep_data(date)
            if s and isinstance(s, dict):
                dto = s.get("dailySleepDTO") or s
                if isinstance(dto, dict) and dto.get("calendarDate"):
                    sleep_list.append(dto)
        except: pass
    data["sleep"] = sleep_list
    print(f"   Schlaf: {len(sleep_list)} Nächte")

    hrv_list = []
    for date in dates_7:
        try:
            h = client.get_hrv_data(date)
            if h: hrv_list.append({"date": date, "data": h})
        except: pass
    data["hrv"] = hrv_list

    readiness_list = []
    for date in dates_7:
        try:
            r = client.get_training_readiness(date)
            if r: readiness_list.append({"date": date, "data": r})
        except: pass
    data["readiness"] = readiness_list

    try: data["training_load"] = client.get_training_load()
    except: data["training_load"] = {}

    try: data["today_stats"] = client.get_stats(today.isoformat())
    except: data["today_stats"] = {}

    try: data["body_battery"] = client.get_body_battery(dates_7[-1], dates_7[0])
    except: data["body_battery"] = []

    try: data["profile"] = client.get_user_profile()
    except: data["profile"] = {}

    data["last_sessions"] = get_last_sessions(client, today)
    return data


def get_last_sessions(client: Garmin, today: datetime.date, days_back: int = 120) -> dict:
    """Findet letzte Session pro Disziplin — zieht 100 Aktivitäten, filtert lokal."""
    def parse_date(act):
        ts = act.get("startTimeLocal") or act.get("startTimeGMT")
        if ts is None: return None
        if isinstance(ts, str):
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
                try: return datetime.datetime.strptime(ts[:26], fmt).date()
                except: continue
            try: return datetime.datetime.fromisoformat(ts[:19]).date()
            except: return None
        if isinstance(ts, (int, float)):
            try: return datetime.datetime.fromtimestamp(ts/1000).date()
            except: return None
        return None

    def type_of(act):
        t = act.get("activityType", "")
        if isinstance(t, dict): return str(t.get("typeKey") or t.get("typeId") or "").lower()
        return str(t).lower()

    activities = []
    try: activities = client.get_activities(0, 100)
    except Exception as e: print(f"   get_activities Fehler: {e}")

    def find_last(matcher):
        candidates = [(parse_date(a), a) for a in activities if matcher(type_of(a))]
        candidates = [(d, a) for d, a in candidates if d]
        if not candidates: return None
        latest_date, latest = max(candidates, key=lambda x: x[0])
        return {
            "date": latest_date.isoformat(),
            "days_ago": (today - latest_date).days,
            "name": latest.get("activityName") or latest.get("name", ""),
            "distance_m": int(latest.get("distance", 0) or 0),
            "duration_min": round((latest.get("duration", 0) or 0) / 60, 0),
            "avg_hr": latest.get("averageHR") or latest.get("avgHr"),
        }

    sessions = {
        "last_swim": find_last(lambda t: "swim" in t),
        "last_run":  find_last(lambda t: t in ("running","treadmill_running","trail_running","track_running","virtual_run")),
        "last_bike": find_last(lambda t: "cycling" in t or "biking" in t),
    }
    for sport, info in sessions.items():
        if info: print(f"   {sport}: {info['days_ago']}d her ({info['distance_m']}m, {info['date']})")
        else: print(f"   {sport}: keine Session in {len(activities)} Aktivitäten")
    return sessions


# ═══════════════════════════════════════════════════════════════════
# 4. METRIKEN EXTRAHIEREN
# ═══════════════════════════════════════════════════════════════════
def extract_fresh_metrics(garmin_data: dict) -> dict:
    today     = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    result    = {}

    def freshness(d):
        if not d: return "unbekannt"
        if d == today.isoformat():     return "HEUTE"
        if d == yesterday.isoformat(): return "GESTERN"
        return d

    # Schlaf — FIX: or 0 statt ,0 (fängt None-Werte ab)
    sleep_entries = garmin_data.get("sleep", [])
    if sleep_entries:
        latest = max(sleep_entries, key=lambda x: x.get("calendarDate",""), default=None)
        if latest:
            scores  = latest.get("sleepScores", {})
            overall = (scores.get("overallScore") or latest.get("overallSleepScore")
                       or scores.get("overall",{}).get("value"))
            dur_s   = ((latest.get("deepSleepSeconds") or 0)
                       + (latest.get("lightSleepSeconds") or 0)
                       + (latest.get("remSleepSeconds") or 0))
            spo2    = latest.get("spo2SleepSummary", {})
            date_v  = latest.get("calendarDate","")
            entry   = {"freshness": freshness(date_v), "date": date_v}
            if overall:                     entry["score"]      = overall
            if dur_s > 0:                   entry["duration_h"] = round(dur_s/3600, 1)
            rem_s = latest.get("remSleepSeconds")
            if rem_s:  entry["rem_h"]  = round((rem_s  or 0)/3600, 1)
            deep_s = latest.get("deepSleepSeconds")
            if deep_s: entry["deep_h"] = round((deep_s or 0)/3600, 1)
            if spo2.get("averageSPO2"): entry["spo2_avg"] = spo2["averageSPO2"]
            if spo2.get("lowestSPO2"):  entry["spo2_low"] = spo2["lowestSPO2"]
            result["sleep"] = entry

    # HRV
    hrv_entries = garmin_data.get("hrv", [])
    if hrv_entries:
        latest = max(hrv_entries, key=lambda x: x.get("date",""), default=None)
        if latest:
            raw   = latest.get("data", {})
            entry = {"freshness": freshness(latest["date"]), "date": latest["date"]}
            weekly, last = None, None
            if isinstance(raw, dict):
                weekly = raw.get("weeklyAvg") or raw.get("hrvWeeklyAverage")
                last   = raw.get("lastNight") or raw.get("lastNight5MinHigh")
                if not (weekly or last):
                    s = raw.get("hrvSummary", {})
                    weekly = s.get("weeklyAvg") or s.get("weekly5MinHigh")
                    last   = s.get("lastNight") or s.get("lastNight5MinHigh")
            elif isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        weekly = item.get("weeklyAvg") or item.get("hrvWeeklyAverage")
                        last   = item.get("lastNight") or item.get("lastNight5MinHigh")
                        if weekly or last: break
            if weekly: entry["weekly_avg_ms"] = weekly
            if last:   entry["last_night_ms"] = last
            result["hrv"] = entry

    # Training Readiness
    readiness_entries = garmin_data.get("readiness", [])
    if readiness_entries:
        latest = max(readiness_entries, key=lambda x: x.get("date",""), default=None)
        if latest:
            raw   = latest.get("data", {})
            entry = {"freshness": freshness(latest["date"]), "date": latest["date"]}
            def find_r(obj):
                if isinstance(obj, dict):
                    return (obj.get("score") or obj.get("trainingReadinessScore"),
                            obj.get("level") or obj.get("trainingReadinessLevel"))
                if isinstance(obj, list):
                    for item in obj:
                        s, l = find_r(item)
                        if s: return s, l
                return None, None
            score, level = find_r(raw)
            if score: entry["score"] = score
            if level: entry["level"] = level
            result["readiness"] = entry

    # Today Stats
    ts = garmin_data.get("today_stats", {})
    if ts:
        te = {"freshness": "HEUTE"}
        if ts.get("restingHeartRate"):        te["rhr"]        = ts["restingHeartRate"]
        if ts.get("averageStressLevel"):      te["stress_avg"] = ts["averageStressLevel"]
        if ts.get("totalSteps"):              te["steps"]      = ts["totalSteps"]
        if ts.get("bodyBatteryChargedValue"): te["bb_charged"] = ts["bodyBatteryChargedValue"]
        if ts.get("bodyBatteryDrainedValue"): te["bb_drained"] = ts["bodyBatteryDrainedValue"]
        result["today_stats"] = te

    # Body Battery
    bb_data = garmin_data.get("body_battery", [])
    if isinstance(bb_data, list) and bb_data:
        try:
            last_bb = bb_data[-1]
            if isinstance(last_bb, dict):
                val = last_bb.get("bodyBatteryLevel") or last_bb.get("charged") or last_bb.get("value")
                if val: result["body_battery_now"] = val
        except: pass

    # Training Load
    tl = garmin_data.get("training_load", {})
    if tl and isinstance(tl, dict):
        le = {}
        if tl.get("acuteLoad"):                le["atl"]  = tl["acuteLoad"]
        if tl.get("chronicLoad"):               le["ctl"]  = tl["chronicLoad"]
        if tl.get("acuteChronicWorkloadRatio"): le["acwr"] = round(tl["acuteChronicWorkloadRatio"],2)
        if le: result["training_load"] = le

    # Letzte Sessions
    last_sessions = garmin_data.get("last_sessions", {})
    if last_sessions.get("last_swim"): result["last_swim"] = last_sessions["last_swim"]
    if last_sessions.get("last_run"):  result["last_run"]  = last_sessions["last_run"]
    if last_sessions.get("last_bike"): result["last_bike"] = last_sessions["last_bike"]

    return result


# ═══════════════════════════════════════════════════════════════════
# 5. TAGESKONTEXT
# ═══════════════════════════════════════════════════════════════════
def get_day_context(today_date: datetime.date) -> dict:
    work_active = today_date <= datetime.date(2026, 7, 17)
    weekday     = today_date.weekday()
    is_hol      = today_date in BAVARIAN_HOLIDAYS
    is_wkend    = weekday >= 5
    is_fri      = weekday == 4
    hol_name    = BAVARIAN_HOLIDAYS.get(today_date, "")

    upcoming_free = []
    for i in range(1, 11):
        d = today_date + datetime.timedelta(days=i)
        if d.weekday() >= 5 or d in BAVARIAN_HOLIDAYS:
            upcoming_free.append({
                "date": d.isoformat(), "weekday": WEEKDAY_DE[d.weekday()],
                "name": BAVARIAN_HOLIDAYS.get(d, "Wochenende"), "days_from_now": i
            })

    if is_wkend or is_hol:
        return {"type": "FREIER_TAG", "label": hol_name or WEEKDAY_DE[weekday],
                "training_window": "Ganztags", "double_session": True,
                "note": "Wochenende/Feiertag — ganztags Zeit, lange Sessions möglich",
                "upcoming_free_days": upcoming_free}
    elif is_fri and work_active:
        return {"type": "FREITAG", "label": "Freitag",
                "training_window": "Ab 16:30", "double_session": False,
                "note": "Freitag — langer Abend möglich",
                "upcoming_free_days": upcoming_free}
    elif work_active:
        return {"type": "ARBEITSTAG", "label": WEEKDAY_DE[weekday],
                "training_window": "Ab 16:30", "double_session": False,
                "note": "Arbeitstag — ab 16:30 trainingsfähig",
                "upcoming_free_days": upcoming_free}
    else:
        return {"type": "FREI", "label": WEEKDAY_DE[weekday],
                "training_window": "Ganztags", "double_session": True,
                "note": "Voller Trainingstag", "upcoming_free_days": upcoming_free}


# ═══════════════════════════════════════════════════════════════════
# 6. EVENTS
# ═══════════════════════════════════════════════════════════════════
def get_events_context(today_date: datetime.date) -> list:
    events_config = [
        {"name": "IfA Nonstop Triathlon Bamberg", "emoji": "🏊🚴🏃",
         "date": datetime.date(2026, 6, 7), "location": "Ebinger See, Rattelsdorf",
         "type": "sprint_triathlon", "distance": "750m / 21km / 5km",
         "goal": "Finishen + Spaß. Zielzeit ~1:32.",
         # Amateur-Taper: nur 5-7 Tage, max 25% Volumenreduktion — kein Ultra-Taper
         "taper_days": 5,
         "taper_style": "AMATEUR-TAPER: 5 Tage, Volumen -20%, Intensität halten, keine komplette Pause",
         "disciplines": ["swim","bike","run"],
         "critical_notes": [
             "Schwimmen: >7 Tage keine Session → Warnung",
             "Mind. 2 Brick-Einheiten absolviert haben",
             "Polar H9 unter Neopren für T1-Start"
         ]},
        {"name": "Halbmarathon Geburtstag 🎂", "emoji": "🏃",
         "date": datetime.date(2026, 7, 12), "location": "München (TBD)",
         "type": "halfmarathon", "distance": "21.1km",
         "goal": "ADAPTIV: Basis sub 2:00h (5:41/km). Wenn Fitness zeigt schnelleres Tempo möglich → Ziel anpassen.",
         "taper_days": 7,
         "taper_style": "AMATEUR-TAPER: 7 Tage, Volumen -25%, 2 kurze schnelle Einheiten als Sharpener",
         "disciplines": ["run"],
         "critical_notes": ["5 Wochen Laufaufbau nach Bamberg", "Ziel dynamisch nach unten anpassen"]},
        {"name": "1. triathlon.de CUP Königsbrunn", "emoji": "🏊🚴🏃",
         "date": datetime.date(2026, 9, 20), "location": "Ilsesee, Königsbrunn",
         "type": "middle_distance",
         "distance": "1.9km / 80km / 20km",
         "goal": "Finishen. Mitteldistanz als erste große Herausforderung. Ziel: unter 6h.",
         "taper_days": 10,
         "taper_style": "AMATEUR-TAPER: 10 Tage, Volumen -30%, 2 Sharpener-Einheiten",
         "disciplines": ["swim","bike","run"],
         "critical_notes": [
             "Schwimmaufbau ab Juli kritisch für 1.9km",
             "Long Rides >3h nötig ab Juli",
             "Brick-Sessions bis 3h+ ab August"
         ]},
        # Weitere Events:
        # {"name":"...", "emoji":"🏁", "date": datetime.date(2026,10,1), ...}
    ]
    result = []
    for ev in events_config:
        days_left = (ev["date"] - today_date).days
        if days_left < -2: continue
        if days_left < 0:               phase = "GERADE VORBEI"
        elif days_left == 0:            phase = "HEUTE 🔴🔴🔴"
        elif days_left <= ev["taper_days"]: phase = "TAPER 🟡"
        elif days_left <= 7:            phase = "RACE WEEK 🔴"
        elif days_left <= 14:           phase = "RACE SHARPENING 🟡"
        elif days_left <= 35:           phase = "Spezifische Vorbereitung 🟢"
        else:                           phase = "Basisaufbau 🟢"
        result.append({**ev, "days_left": days_left, "phase": phase,
                        "date_str": ev["date"].strftime("%d. %B %Y")})
    return result


# ═══════════════════════════════════════════════════════════════════
# 7. WORKOUT ENTSCHEIDUNG — VO2MAX-FOKUS (aktuelle Sportwissenschaft)
# ═══════════════════════════════════════════════════════════════════
def generate_workout_plan(metrics: dict, day_ctx: dict, events: list) -> dict:
    """
    Claude entscheidet Workout basierend auf polarisiertem Trainingsmodell
    und aktueller VO2max-Forschung (Helgerud 2007, Buchheit & Laursen 2013,
    Stöggl & Sperlich 2014 — 80/20 polarisiert > Schwellentraining).
    """
    client    = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    today_str = datetime.date.today().strftime("%A, %d. %B %Y")

    sleep     = metrics.get("sleep", {})
    hrv       = metrics.get("hrv", {})
    readiness = metrics.get("readiness", {})
    tl        = metrics.get("training_load", {})
    bb        = metrics.get("body_battery_now", "?")
    last_swim = metrics.get("last_swim")
    last_run  = metrics.get("last_run")
    last_bike = metrics.get("last_bike")

    events_str = "\n".join([
        f"- {ev['name']}: noch {ev['days_left']} Tage ({ev['phase']})"
        for ev in events
    ])

    prompt = f"""Du bist Sportwissenschaftler für Hannes Raschke (20J, Amateur-Triathlet).
Heute: {today_str} | Tagestyp: {day_ctx['type']} — {day_ctx['note']}

METRIKEN:
Sleep: {sleep.get('score','?')} ({sleep.get('freshness','')})
HRV: {hrv.get('last_night_ms','?')}ms | Wochenschnitt: {hrv.get('weekly_avg_ms','?')}ms
Readiness: {readiness.get('score','?')} [{readiness.get('level','')}]
Body Battery: {bb}
ATL/CTL/ACWR: {tl.get('atl','?')}/{tl.get('ctl','?')}/{tl.get('acwr','?')}

LETZTE SESSIONS:
Schwimmen: {f"{last_swim['days_ago']}d her, {last_swim['distance_m']}m" if last_swim else 'keine Daten'}
Laufen: {f"{last_run['days_ago']}d her, {last_run['distance_m']}m" if last_run else 'keine Daten'}
Radfahren: {f"{last_bike['days_ago']}d her, {last_bike['distance_m']}m" if last_bike else 'keine Daten'}

EVENTS:
{events_str}

HANNES PROFIL:
HFmax 196bpm | Z2 (aerob): 118-137bpm | LT: 176bpm | Z5 VO2max: >176bpm
5km PR: 26:58 (5:24/km) | VO2max Garmin: 47 | Biometrisch: 50.6
KERN-PROBLEM: 79% Laufzeit in Z4 (Junk-Zone), 0% in echtem Z2
Polar H9 Brustgurt verfügbar

SPORTWISSENSCHAFTLICHE GRUNDLAGE (aktuelle Studien):
1. POLARISIERTES TRAINING (Stöggl & Sperlich 2014, Seiler):
   - 80% der Sessions: Z1-Z2 (118-137bpm) — baut aerobes Fundament, erhöht Schlagvolumen
   - 20%: Z4-Z5 (>167bpm) — direkter VO2max-Stimulus
   - Z3 (Junk-Zone, 137-167bpm) VERMEIDEN — zu hart für Regeneration, zu leicht für VO2max

2. BESTE VO2MAX-PROTOKOLLE (Helgerud et al. 2007, Buchheit & Laursen 2013):
   - 4×4min @ 90-95% HFmax (176-186bpm) + 3min aktive Pause = GOLD-STANDARD
   - 8-10×1min @ 95%+ HFmax + 1-2min Pause = schnellere Adaptation
   - Schwellen-Radeinheiten: 3×10min @ 80-85% HFmax (157-166bpm) + Ausdauer-Z2 davor

3. AMATEUR-SPEZIFISCH:
   - Nicht jeden Tag hart — echte Erholung ermöglicht Supercompensation
   - Z2-Läufe bei 7:00-7:30/km fühlen sich zu langsam an — das ist korrekt!
   - Garmin Readiness <50 oder ACWR >1.4: NUR Z2 oder Ruhe

4. TAPER-PHILOSOPHIE FÜR AMATEURE:
   - Sprint-Triathlon: nur 5 Tage, Volumen -20%, KEINE komplette Trainings-Pause
   - Intensität halten — ein kurzer Sharpener 3-4 Tage vor dem Rennen ist ok
   - Kein wochenlanger Ultra-Taper — das ist für Profis

ENTSCHEIDE DAS HEUTIGE TRAINING. Gib GENAU dieses JSON zurück (kein Markdown, kein Text):

{{
  "decision": "TRAIN",
  "reason": "2-3 Sätze mit konkreten Metrik-Werten und sportwiss. Begründung",
  "workout": {{
    "sport": "running",
    "name": "Name max 40 Zeichen",
    "description": "Für Garmin Connect",
    "steps": [
      {{
        "phase": "warmup|interval|recovery|cooldown|active",
        "name": "Schrittname",
        "end_type": "distance|time",
        "end_value": 2000,
        "end_unit": "meter|second",
        "hr_min": 118,
        "hr_max": 137,
        "repeats": 1
      }}
    ]
  }}
}}

ODER: {{"decision": "REST", "reason": "Begründung", "workout": null}}

STEP-REGELN:
- repeats>1: interval+recovery mit gleicher Zahl bilden Wiederholungsgruppe
- distance in Metern, time in Sekunden
- warmup/cooldown/active: repeats=1

BEISPIEL 4×4min: warmup 2000m Z2 → interval 240s @176-186bpm ×4 → recovery 180s @Z1 ×4 → cooldown 1500m Z2
BEISPIEL Z2 Lauf: active 10000m @118-137bpm ×1
BEISPIEL Sharpener: warmup 1500m → interval 600s @157-166bpm ×2 → recovery 300s ×2 → cooldown 1000m

WICHTIG: Sei konkret und progressiv. Kein Vorschlagen von "leichtem Spaziergang".
Bei FREIEM TAG: längere oder doppelte Sessions möglich.
Swim sport="swimming", Bike sport="cycling"."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}]
    )
    text = message.content[0].text.strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
        if "```" in text: text = text.rsplit("```", 1)[0]
    return json.loads(text.strip())


# ═══════════════════════════════════════════════════════════════════
# 8. GARMIN WORKOUT BAUEN
# ═══════════════════════════════════════════════════════════════════
def build_garmin_workout(workout_plan: dict, today: datetime.date) -> dict:
    SPORT_TYPES = {
        "running":  {"sportTypeId": 1, "sportTypeKey": "running",      "displayOrder": 1},
        "cycling":  {"sportTypeId": 2, "sportTypeKey": "cycling",      "displayOrder": 2},
        "swimming": {"sportTypeId": 5, "sportTypeKey": "lap_swimming", "displayOrder": 5},
        "other":    {"sportTypeId": 4, "sportTypeKey": "other",        "displayOrder": 4},
    }
    STEP_TYPES = {
        "warmup":   {"stepTypeId": 1, "stepTypeKey": "warmup",   "displayOrder": 1},
        "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown", "displayOrder": 2},
        "interval": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
        "recovery": {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4},
        "active":   {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
        "rest":     {"stepTypeId": 5, "stepTypeKey": "rest",     "displayOrder": 5},
    }
    END_COND = {
        "distance": {"conditionTypeId": 3, "conditionTypeKey": "distance",  "displayOrder": 3, "displayable": True},
        "time":     {"conditionTypeId": 2, "conditionTypeKey": "time",      "displayOrder": 2, "displayable": True},
    }
    ITER_COND = {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False}
    HR_TARGET = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4}
    NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target",       "displayOrder": 1}

    sport      = workout_plan.get("sport", "running")
    sport_type = SPORT_TYPES.get(sport, SPORT_TYPES["running"])
    steps_in   = workout_plan.get("steps", [])
    counter    = {"id": 0}

    def next_id():
        counter["id"] += 1
        return counter["id"]

    def make_exec(step, order):
        phase  = step.get("phase", "active")
        et     = step.get("end_type", "distance")
        ev_val = float(step.get("end_value", 1000))
        hr_min = step.get("hr_min"); hr_max = step.get("hr_max")
        has_hr = bool(hr_min and hr_max)
        return {
            "type": "ExecutableStepDTO", "stepId": next_id(), "stepOrder": order,
            "stepType": STEP_TYPES.get(phase, STEP_TYPES["interval"]),
            "childStepId": None, "description": step.get("name",""),
            "endCondition": END_COND.get(et, END_COND["distance"]),
            "endConditionValue": ev_val, "preferredEndConditionUnit": None,
            "endConditionCompare": None,
            "targetType": HR_TARGET if has_hr else NO_TARGET,
            "targetValueOne": float(hr_min) if has_hr else None,
            "targetValueTwo": float(hr_max) if has_hr else None,
            "zoneNumber": None,
        }

    garmin_steps = []; order = 1; i = 0
    while i < len(steps_in):
        step = steps_in[i]
        rep  = step.get("repeats", 1)
        if rep > 1:
            group = []
            while i < len(steps_in) and steps_in[i].get("repeats",1) == rep:
                group.append(steps_in[i]); i += 1
            repeat_id = next_id()
            inner = [make_exec(gs, io+1) for io, gs in enumerate(group)]
            garmin_steps.append({
                "type": "RepeatGroupDTO", "stepId": repeat_id, "stepOrder": order,
                "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
                "childStepId": 1, "numberOfIterations": rep, "smartRepeat": False,
                "endCondition": ITER_COND, "endConditionValue": float(rep),
                "workoutSteps": inner, "skipLastRestStep": False,
            })
            order += 1
        else:
            garmin_steps.append(make_exec(step, order)); order += 1; i += 1

    return {
        "workoutName": workout_plan.get("name", f"Training {today.isoformat()}"),
        "description": workout_plan.get("description",""),
        "sportType": sport_type, "subSportType": None,
        "workoutSegments": [{"segmentOrder": 1, "sportType": sport_type, "workoutSteps": garmin_steps}],
    }


# ═══════════════════════════════════════════════════════════════════
# 9. WORKOUT HOCHLADEN
# ═══════════════════════════════════════════════════════════════════
def upload_workout_to_garmin(client: Garmin, workout_dict: dict, today: datetime.date) -> dict:
    def extract_id(r):
        if isinstance(r, dict): return r.get("workoutId") or r.get("id") or r.get("workout_id")
        try: return int(r)
        except: return None

    def try_schedule(wid):
        try:
            client.garth.connectapi(f"/workout-service/schedule/{wid}", method="POST", json={"date": today.isoformat()})
            print(f"   ✓ Für {today.isoformat()} geplant"); return True
        except Exception as e:
            print(f"   Scheduling Fehler: {e}"); return False

    workout_id = None; upload_method = None

    if hasattr(client, "garth"):
        try:
            result = client.garth.connectapi("/workout-service/workout", method="POST", json=workout_dict)
            workout_id = extract_id(result)
            if workout_id: upload_method = "garth.connectapi"; print(f"   ✓ Workout ID: {workout_id}")
        except Exception as e: print(f"   connectapi Fehler: {str(e)[:100]}")

    if not workout_id and hasattr(client, "garth"):
        try:
            resp = client.garth.post("connectapi", "/workout-service/workout", json=workout_dict, api=True)
            result = resp.json() if hasattr(resp, "json") else resp
            workout_id = extract_id(result)
            if workout_id: upload_method = "garth.post"; print(f"   ✓ Workout ID: {workout_id}")
        except Exception as e: print(f"   garth.post Fehler: {str(e)[:100]}")

    if not workout_id and hasattr(client, "add_workout"):
        try:
            result = client.add_workout(workout_dict)
            workout_id = extract_id(result)
            if workout_id: upload_method = "add_workout"; print(f"   ✓ Workout ID: {workout_id}")
        except Exception as e: print(f"   add_workout Fehler: {str(e)[:100]}")

    if workout_id:
        scheduled = try_schedule(workout_id)
        return {
            "success": True, "workout_id": workout_id, "scheduled": scheduled,
            "method": upload_method,
            "message": "Workout erstellt und geplant ✓" if scheduled else "Workout erstellt ✓ (Garmin Connect → Meine Workouts)",
            "garmin_link": f"https://connect.garmin.com/modern/workout/{workout_id}"
        }
    return {"success": False, "workout_id": None,
            "message": "Upload fehlgeschlagen — Session manuell starten"}


# ═══════════════════════════════════════════════════════════════════
# 10. HTML DASHBOARD
# ═══════════════════════════════════════════════════════════════════
def generate_dashboard(garmin_data: dict, metrics: dict, day_ctx: dict,
                       events: list, workout_plan: dict, workout_status: dict) -> str:
    client    = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    today     = datetime.date.today()
    today_str = today.strftime("%A, %d. %B %Y")

    last_swim     = metrics.get("last_swim", {})
    swim_days_ago = (last_swim.get("days_ago") or 999) if last_swim else 999
    swim_warning  = ""
    for ev in events:
        if "triathlon" in ev.get("type",""):
            if swim_days_ago >= 14:
                swim_warning = f"🚨 KRITISCH: {swim_days_ago} Tage kein Schwimmen! {ev['name']} in {ev['days_left']} Tagen."
            elif swim_days_ago >= 7:
                swim_warning = f"⚠️ {swim_days_ago} Tage kein Schwimmen. Dringend ins Wasser."

    if workout_plan.get("decision") == "REST":
        workout_banner = f"WORKOUT: RUHETAG\n{workout_plan.get('reason','')}"
    elif workout_status.get("success"):
        w = workout_plan.get("workout", {})
        steps_s = " → ".join(
            f"{s.get('phase')} {s.get('end_value')}{s.get('end_unit','')}"
            + (f"×{s.get('repeats')}" if s.get("repeats",1)>1 else "")
            for s in w.get("steps",[])
        )
        workout_banner = (f"WORKOUT: ✅ IN GARMIN CONNECT\nName: {w.get('name','')}\n"
                          f"Sport: {w.get('sport','')}\nStruktur: {steps_s}\n"
                          f"Begründung: {workout_plan.get('reason','')}\n"
                          f"Status: {workout_status.get('message','')}")
        if workout_status.get("garmin_link"):
            workout_banner += f"\nLink: {workout_status['garmin_link']}"
    else:
        w = workout_plan.get("workout", {}) or {}
        workout_banner = (f"WORKOUT: ⚠️ UPLOAD FEHLGESCHLAGEN\nName: {w.get('name','?')}\n"
                          f"Begründung: {workout_plan.get('reason','')}\n"
                          f"Fehler: {workout_status.get('message','')}\n"
                          f"→ Session manuell starten")

    events_str = "".join(
        f"\n{ev['emoji']} {ev['name']}: {ev['days_left']}d | {ev['phase']}\n"
        f"  {ev['distance']} | {ev['goal']}\n  Taper: {ev['taper_style']}\n"
        for ev in events
    )
    free_str  = ", ".join(f"{d['weekday']} ({d['name']}, +{d['days_from_now']}d)"
                          for d in day_ctx.get("upcoming_free_days",[])[:4]) or "keine"
    acts_str  = json.dumps(garmin_data.get("activities",[])[:10], indent=1, default=str)[:6000]
    sleep_str = json.dumps(garmin_data.get("sleep",[])[:7], indent=1, default=str)[:4000]
    metr_str  = json.dumps(metrics, indent=2, ensure_ascii=False)

    prompt = f"""Erstelle das tägliche Trainings-Dashboard für Hannes Raschke ({today_str}).
{swim_warning}

KONTEXT: {day_ctx['type']} | {day_ctx['note']} | Freie Tage: {free_str}
METRIKEN: {metr_str}
{workout_banner}
EVENTS: {events_str}
AKTIVITÄTEN (letzte 10): {acts_str}
SCHLAF (7 Nächte): {sleep_str}

ERSTELLE VOLLSTÄNDIGES STANDALONE HTML. Kein CDN, alles inline, Mobil-first 375px.
Hintergrund #080c08 | Grün #4cff7c | Amber #ffb84c | Rot #ff6060 | Text #ddeedd
Schrift: -apple-system, BlinkMacSystemFont, sans-serif

REIHENFOLGE (PFLICHT):
[1] HEADER — "Guten Morgen Hannes — {today_str}"
[2] METRIKEN — große KPIs mit Ampeln:
    Sleep | HRV ms | Readiness | Body Battery | RHR | ACWR
    Freshness-Label unter jedem Wert. Ampelregeln: Sleep≥85=grün,<70=rot | HRV≥63=grün,<50=rot
    Readiness≥80=grün | BB≥70=grün | RHR≤58=grün,≥65=rot | ACWR 0.8-1.3=grün
[3] WORKOUT STATUS — farbige Box:
    ✅ Grün: Name + HR-Zonen + "Uhr synchronisieren"
    🔴 Rest: Ruhetag + Grund
    ⚠️ Amber: Manuell starten + Session-Beschreibung
[4] TRAININGSEMPFEHLUNG — Details, HR-Zonen, Tipps
[5] NÄCHSTE 5 TAGE — polarisiertes Wochenprogramm mit Z2/Intervall-Balance
[6] EVENTS — kompakte Karten mit Disziplin-Ampeln und Countdown
[7] SCHLAF-TABELLE — 7 Nächte kompakt
[8] LETZTE 7 TRAININGS — Liste
[9] TRAINING LOAD — wenn verfügbar

INTELLIGENZ:
- ACWR>1.4: roter Banner oben
- Readiness<50: Pflichtruhe-Banner
- Race Week (<7d): Race-Week-Modus
- Beim Workout-Banner: wenn ✅ → zeige Garmin-Link als klickbaren Button

Antworte NUR mit HTML, direkt ab <!DOCTYPE html>."""

    message = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=8000,
        messages=[{"role": "user", "content": prompt}]
    )
    html = message.content[0].text.strip()
    if html.startswith("```"):
        html = "\n".join(html.split("\n")[1:])
        if "```" in html: html = html.rsplit("```",1)[0]
    return html.strip()


# ═══════════════════════════════════════════════════════════════════
# 11. E-MAIL
# ═══════════════════════════════════════════════════════════════════
def send_email(html_content: str, date_str: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🏃 {date_str} — Garmin Dashboard"
    msg["From"]    = GMAIL_USER
    msg["To"]      = GMAIL_TO
    msg.attach(MIMEText(f"Dashboard für {date_str}.", "plain","utf-8"))
    msg.attach(MIMEText(html_content, "html","utf-8"))
    with smtplib.SMTP("smtp.mail.me.com", 587) as server:
        server.ehlo(); server.starttls(); server.ehlo()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, GMAIL_TO, msg.as_string())


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════
def main():
    today     = datetime.date.today()
    today_str = today.strftime("%d. %m. %Y")
    print(f"\n{'='*54}\n  Garmin Dashboard — {today_str}\n{'='*54}\n")

    try:
        print("1/6 — Garmin Login...")
        garmin_client = get_garmin_client()

        print("1b/6 — Warte auf Sleep Score (max 4h)...")
        wait_for_sleep_score(garmin_client, max_wait_min=240, poll_interval_sec=300)

        print("2/6 — Daten laden...")
        garmin_data = get_garmin_data(garmin_client)

        print("3/6 — Kontext...")
        metrics  = extract_fresh_metrics(garmin_data)
        day_ctx  = get_day_context(today)
        events   = get_events_context(today)
        print(f"   {day_ctx['type']} | Events: {len(events)}")

        print("4/6 — Workout-Entscheidung...")
        workout_plan = generate_workout_plan(metrics, day_ctx, events)
        decision     = workout_plan.get("decision","REST")
        print(f"   {decision}: {workout_plan.get('workout',{}).get('name','-') if workout_plan.get('workout') else 'Ruhetag'}")

        workout_status = {"success": False, "message": "Ruhetag", "workout_id": None}
        if decision == "TRAIN" and workout_plan.get("workout"):
            print("5/6 — Workout hochladen...")
            garmin_workout = build_garmin_workout(workout_plan["workout"], today)
            workout_status = upload_workout_to_garmin(garmin_client, garmin_workout, today)
            print(f"   {workout_status['message']}")
        else:
            print("5/6 — Ruhetag")

        print("6/6 — Dashboard generieren...")
        html_dashboard = generate_dashboard(garmin_data, metrics, day_ctx, events, workout_plan, workout_status)
        print(f"   {len(html_dashboard):,} Zeichen")

        send_email(html_dashboard, today_str)
        print(f"\n✅ FERTIG — {GMAIL_TO}\n")

    except Exception as e:
        print(f"\n❌ FEHLER: {e}")
        traceback.print_exc()
        try:
            err_html = f"""<!DOCTYPE html><html><body style="background:#1a0000;color:#ff8080;font-family:monospace;padding:20px">
<h2>⚠️ Fehler — {today_str}</h2>
<pre style="background:#2a0000;padding:15px;border-radius:8px;white-space:pre-wrap">{traceback.format_exc()}</pre>
</body></html>"""
            send_email(err_html, today_str)
        except: pass
        raise


if __name__ == "__main__":
    main()
