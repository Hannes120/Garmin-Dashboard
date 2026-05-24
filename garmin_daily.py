#!/usr/bin/env python3
"""
Garmin Daily Dashboard — Hannes Raschke
Täglich automatisch ausgeführt von GitHub Actions um 06:15 Uhr CEST.

Ablauf:
  1. Garmin-Login (Client wird für Lesen + Schreiben wiederverwendet)
  2. Alle Daten ziehen
  3. Kontext aufbauen (Tag, Events, frische Metriken)
  4. Claude → Workout-Entscheidung (strukturiertes JSON)
  5. Workout in Garmin Connect hochladen + für heute planen
  6. Claude → HTML-Dashboard (mit Workout-Bestätigung)
  7. Dashboard per iCloud E-Mail senden
"""

import os
import json
import smtplib
import datetime
import traceback
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from garminconnect import Garmin
import time
import anthropic

# ── CREDENTIALS ───────────────────────────────────────────────────────────────
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


# ═════════════════════════════════════════════════════════════════════════════
# 1. GARMIN CLIENT
# ═════════════════════════════════════════════════════════════════════════════
def get_garmin_client() -> Garmin:
    """Login einmalig — Client wird für Lesen UND Schreiben (Workout-Upload) genutzt."""
    client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    client.login()
    print("   ✓ Garmin Login erfolgreich")
    return client


# ═════════════════════════════════════════════════════════════════════════════
# 2. DATEN LADEN
# ═════════════════════════════════════════════════════════════════════════════
def get_garmin_data(client: Garmin) -> dict:
    """Zieht alle relevanten Rohdaten der letzten 14 Tage."""
    today   = datetime.date.today()
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

    # Letzte Session pro Disziplin — schaut 120 Tage zurück (findet auch alte Schwimmeinheiten)
    data["last_sessions"] = get_last_sessions(client, today, days_back=120)

    return data


def get_last_sessions(client: Garmin, today: datetime.date, days_back: int = 120) -> dict:
    """
    Findet die letzte Session pro Disziplin.
    Robuster Ansatz: zieht einmal bis zu 100 Aktivitäten (deckt ~3-4 Monate ab)
    und filtert LOKAL nach Sportart. Zuverlässiger als API-seitige Filter,
    die je nach Library-Version unterschiedlich funktionieren.
    """
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
        if isinstance(t, dict):
            return str(t.get("typeKey") or t.get("typeId") or "").lower()
        return str(t).lower()

    # Einmal viele Aktivitäten ziehen
    activities = []
    try:
        activities = client.get_activities(0, 100)
    except Exception as e:
        print(f"   get_activities(100) Fehler: {e}")
        activities = []

    # Lokal nach Disziplin gruppieren
    def find_last(matcher):
        candidates = []
        for a in activities:
            tk = type_of(a)
            if matcher(tk):
                d = parse_date(a)
                if d: candidates.append((d, a))
        if not candidates: return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        latest_date, latest = candidates[0]
        return {
            "date":         latest_date.isoformat(),
            "days_ago":     (today - latest_date).days,
            "name":         latest.get("activityName") or latest.get("name", ""),
            "distance_m":   int(latest.get("distance", 0) or 0),
            "duration_min": round((latest.get("duration", 0) or 0) / 60, 0),
            "avg_hr":       latest.get("averageHR") or latest.get("avgHr"),
        }

    sessions = {
        "last_swim": find_last(lambda t: "swim" in t),
        "last_run":  find_last(lambda t: t in ("running","treadmill_running",
                                               "trail_running","track_running","virtual_run")),
        "last_bike": find_last(lambda t: "cycling" in t or "biking" in t),
    }
    for sport, info in sessions.items():
        if info:
            print(f"   {sport}: {info['days_ago']} Tage her ({info['distance_m']}m, {info['date']})")
        else:
            print(f"   {sport}: nichts in {len(activities)} Aktivitäten gefunden")
    return sessions


# ═════════════════════════════════════════════════════════════════════════════
# 3. KONTEXT AUFBAUEN
# ═════════════════════════════════════════════════════════════════════════════
def extract_fresh_metrics(garmin_data: dict) -> dict:
    """Isoliert den aktuellsten Datenpunkt jeder Metrik mit Datums-Label."""
    today     = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    result    = {}

    def freshness(d):
        if not d: return "unbekannt"
        if d == today.isoformat():     return "HEUTE"
        if d == yesterday.isoformat(): return "GESTERN"
        return d

    # Schlaf
    sleep_entries = garmin_data.get("sleep", [])
    if sleep_entries:
        latest = max(sleep_entries, key=lambda x: x.get("calendarDate",""), default=None)
        if latest:
            scores  = latest.get("sleepScores", {})
            overall = (scores.get("overallScore") or latest.get("overallSleepScore")
                       or scores.get("overall",{}).get("value"))
            dur_s   = (latest.get("deepSleepSeconds",0) + latest.get("lightSleepSeconds",0)
                       + latest.get("remSleepSeconds",0))
            spo2    = latest.get("spo2SleepSummary", {})
            date_v  = latest.get("calendarDate","")
            entry   = {"freshness": freshness(date_v), "date": date_v}
            if overall:                     entry["score"]      = overall
            if dur_s > 0:                   entry["duration_h"] = round(dur_s/3600, 1)
            if latest.get("remSleepSeconds"):  entry["rem_h"]  = round(latest["remSleepSeconds"]/3600,1)
            if latest.get("deepSleepSeconds"): entry["deep_h"] = round(latest["deepSleepSeconds"]/3600,1)
            if spo2.get("averageSPO2"):     entry["spo2_avg"]   = spo2["averageSPO2"]
            if spo2.get("lowestSPO2"):      entry["spo2_low"]   = spo2["lowestSPO2"]
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
                    return obj.get("score") or obj.get("trainingReadinessScore"), \
                           obj.get("level") or obj.get("trainingReadinessLevel")
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

    # Body Battery aktuell
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
        if tl.get("acuteLoad"):                   le["atl"]  = tl["acuteLoad"]
        if tl.get("chronicLoad"):                  le["ctl"]  = tl["chronicLoad"]
        if tl.get("acuteChronicWorkloadRatio"):    le["acwr"] = round(tl["acuteChronicWorkloadRatio"],2)
        if le: result["training_load"] = le

    # Letzte Session pro Disziplin — date-based Suche (zuverlässig, 120 Tage zurück)
    last_sessions = garmin_data.get("last_sessions", {})
    if last_sessions.get("last_swim"): result["last_swim"] = last_sessions["last_swim"]
    if last_sessions.get("last_run"):  result["last_run"]  = last_sessions["last_run"]
    if last_sessions.get("last_bike"): result["last_bike"] = last_sessions["last_bike"]

    return result


def get_day_context(today_date: datetime.date) -> dict:
    """Tagestyp und Trainingskontext."""
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
                "date": d.isoformat(),
                "weekday": WEEKDAY_DE[d.weekday()],
                "name": BAVARIAN_HOLIDAYS.get(d, "Wochenende"),
                "days_from_now": i
            })

    if is_wkend or is_hol:
        return {"type": "FREIER_TAG", "label": hol_name or WEEKDAY_DE[weekday],
                "training_window": "Ganztags", "double_session": True,
                "note": "Wochenende/Feiertag — ganztags Zeit, längere Sessions möglich",
                "upcoming_free_days": upcoming_free}
    elif is_fri and work_active:
        return {"type": "FREITAG", "label": "Freitag",
                "training_window": "Ab 16:30 Uhr, Abend offen", "double_session": False,
                "note": "Freitag — längeres Abend-Trainingsfenster",
                "upcoming_free_days": upcoming_free}
    elif work_active:
        return {"type": "ARBEITSTAG", "label": WEEKDAY_DE[weekday],
                "training_window": "Ab 16:30 Uhr", "double_session": False,
                "note": "Arbeitstag — ab 16:30 Uhr trainingsfähig",
                "upcoming_free_days": upcoming_free}
    else:
        return {"type": "FREI", "label": WEEKDAY_DE[weekday],
                "training_window": "Ganztags", "double_session": True,
                "note": "Voller Trainingstag", "upcoming_free_days": upcoming_free}


def get_events_context(today_date: datetime.date) -> list:
    """Events-Konfiguration — neue Events hier eintragen."""
    events_config = [
        {"name": "IfA Nonstop Triathlon Bamberg", "emoji": "🏊🚴🏃",
         "date": datetime.date(2026, 6, 7), "location": "Ebinger See, Rattelsdorf",
         "type": "sprint_triathlon", "distance": "750m / 21km / 5km",
         "goal": "Finishen + Spaß. Zielzeit ~1:32.", "taper_days": 7,
         "taper_style": "LEICHT — kein Ultra-Taper, max 40% Volumenreduktion",
         "disciplines": ["swim","bike","run"],
         "critical_notes": ["Schwimmen: >7 Tage keine Session → ROTE WARNUNG",
                            "Mind. 2 Brick-Einheiten vor Renntag",
                            "Neopren im Freiwasser testen (Decathlon Surfanzug 4/3mm)"]},
        {"name": "Halbmarathon Geburtstag 🎂", "emoji": "🏃",
         "date": datetime.date(2026, 7, 12), "location": "München (TBD)",
         "type": "halfmarathon", "distance": "21.1km",
         "goal": "ADAPTIV: Basis sub 2:00h (5:41/km). Wenn Fitness besser → Ziel nach unten anpassen.",
         "taper_days": 10, "taper_style": "NORMAL — 10 Tage, 30-40% Volumenreduktion",
         "disciplines": ["run"],
         "critical_notes": ["5 Wochen HM-Aufbau nach Bamberg", "Ziel dynamisch anpassen"]},
        # Weitere Events:
        # {"name":"...", "emoji":"🏁", "date": datetime.date(2026,9,1), ...}
    ]
    result = []
    for ev in events_config:
        days_left = (ev["date"] - today_date).days
        if days_left < -2: continue
        if days_left < 0:               phase = "GERADE VORBEI"
        elif days_left == 0:            phase = "HEUTE 🔴🔴🔴"
        elif days_left <= 6:            phase = "RACE WEEK 🔴"
        elif days_left <= ev["taper_days"]: phase = "TAPER 🟡"
        elif days_left <= 14:           phase = "RACE SHARPENING 🟡"
        elif days_left <= 35:           phase = "Spezifische Vorbereitung 🟢"
        else:                           phase = "Basisaufbau 🟢"
        result.append({**ev, "days_left": days_left, "phase": phase,
                        "date_str": ev["date"].strftime("%d. %B %Y")})
    return result


# ═════════════════════════════════════════════════════════════════════════════
# 4. WORKOUT ENTSCHEIDUNG (Claude Call 1 — klein, schnell)
# ═════════════════════════════════════════════════════════════════════════════
def generate_workout_plan(metrics: dict, day_ctx: dict, events: list) -> dict:
    """
    Claude entscheidet was heute trainiert wird und gibt strukturiertes JSON zurück.
    Dieses JSON wird direkt in build_garmin_workout() verwendet.
    """
    client     = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    today_str  = datetime.date.today().strftime("%A, %d. %B %Y")

    sleep     = metrics.get("sleep", {})
    hrv       = metrics.get("hrv", {})
    readiness = metrics.get("readiness", {})
    tl        = metrics.get("training_load", {})
    bb        = metrics.get("body_battery_now","unbekannt")

    last_swim = metrics.get("last_swim")
    last_run  = metrics.get("last_run")
    last_bike = metrics.get("last_bike")

    events_str = "\n".join([
        f"- {ev['name']}: noch {ev['days_left']} Tage ({ev['phase']})"
        for ev in events
    ])

    prompt = f"""Du bist Sports Scientist für Hannes Raschke (20J, Triathlet).
Heute: {today_str} | Tagestyp: {day_ctx['type']} — {day_ctx['note']}

AKTUELLE METRIKEN:
Sleep Score: {sleep.get('score','?')} ({sleep.get('freshness','')})
HRV letzte Nacht: {hrv.get('last_night_ms','?')} ms | Wochenschnitt: {hrv.get('weekly_avg_ms','?')} ms ({hrv.get('freshness','')})
Training Readiness: {readiness.get('score','?')} [{readiness.get('level','')}] ({readiness.get('freshness','')})
Body Battery: {bb}
ATL/CTL/ACWR: {tl.get('atl','?')} / {tl.get('ctl','?')} / {tl.get('acwr','?')}

LETZTE SESSIONS:
Schwimmen: {f"{last_swim['days_ago']} Tage her ({last_swim['distance_m']}m)" if last_swim else 'keine Daten'}
Laufen: {f"{last_run['days_ago']} Tage her ({last_run['distance_m']}m)" if last_run else 'keine Daten'}
Radfahren: {f"{last_bike['days_ago']} Tage her ({last_bike['distance_m']}m)" if last_bike else 'keine Daten'}

EVENTS:
{events_str}

HANNES' PROFIL:
HFmax 196 bpm | Z2: 118-137 bpm | Schwelle: 176 bpm | Z5: >176 bpm
PROBLEM: 79% Laufzeit in Z4, 0% in Z2 — Fokus auf echte Z2-Läufe und VO2max-Intervalle
ACWR: <0.8=zu wenig, 0.8-1.3=optimal, >1.3=Vorsicht

Entscheide jetzt das heutige Training. Antworte AUSSCHLIESSLICH mit diesem JSON-Format
(kein Markdown, kein Text davor/danach):

{{
  "decision": "TRAIN",
  "reason": "2-3 Sätze mit echten Werten aus den Metriken",
  "workout": {{
    "sport": "running",
    "name": "Workout-Name max 40 Zeichen",
    "description": "Kurzbeschreibung für Garmin Connect",
    "steps": [
      {{
        "phase": "warmup",
        "name": "Warm-Up Z2",
        "end_type": "distance",
        "end_value": 2000,
        "end_unit": "meter",
        "hr_min": 118,
        "hr_max": 137,
        "repeats": 1
      }}
    ]
  }}
}}

ODER bei Ruhetag:
{{"decision": "REST", "reason": "Begründung", "workout": null}}

STEP-REGELN:
- phase-Werte: "warmup", "interval", "recovery", "cooldown", "active"
- end_type: "distance" (end_value in Metern) oder "time" (end_value in Sekunden)
- repeats: für Intervalle+Erholung gleiche Zahl setzen (z.B. beide repeats=4)
- Wiederholungsgruppen: interval-Schritt + recovery-Schritt mit gleichen repeats
- warmup/cooldown/active: repeats=1

BEISPIELE:
Z2 Lauf 10km: [{{"phase":"active","name":"Z2 Lauf","end_type":"distance","end_value":10000,"end_unit":"meter","hr_min":118,"hr_max":137,"repeats":1}}]

4x4min Intervalle: [
  {{"phase":"warmup","name":"Warm-Up","end_type":"distance","end_value":2000,"end_unit":"meter","hr_min":118,"hr_max":137,"repeats":1}},
  {{"phase":"interval","name":"VO2max Intervall","end_type":"time","end_value":240,"end_unit":"second","hr_min":167,"hr_max":177,"repeats":4}},
  {{"phase":"recovery","name":"Trabpause","end_type":"time","end_value":180,"end_unit":"second","hr_min":100,"hr_max":118,"repeats":4}},
  {{"phase":"cooldown","name":"Cool-Down","end_type":"distance","end_value":1500,"end_unit":"meter","hr_min":118,"hr_max":137,"repeats":1}}
]

8x1min: repeats=8, end_value=60 für interval, end_value=90 für recovery

Swim sport="swimming", Bike sport="cycling"
Bei Brick: wähle den Haupt-Sport (meist cycling), beschreibe Run im description-Feld"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}]
    )
    text = message.content[0].text.strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
        if "```" in text:
            text = text.rsplit("```", 1)[0]
    return json.loads(text.strip())


# ═════════════════════════════════════════════════════════════════════════════
# 5. WORKOUT IN GARMIN-FORMAT KONVERTIEREN
# ═════════════════════════════════════════════════════════════════════════════
def build_garmin_workout(workout_plan: dict, today: datetime.date) -> dict:
    """
    Konvertiert das vereinfachte Step-Format in Garmins natives API-Format.
    Enthält ALLE Pflichtfelder: stepId, displayOrder, displayable, type.
    Unterstützt einfache Schritte + Wiederholungsgruppen.
    """
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
        "distance": {"conditionTypeId": 3, "conditionTypeKey": "distance",
                     "displayOrder": 3, "displayable": True},
        "time":     {"conditionTypeId": 2, "conditionTypeKey": "time",
                     "displayOrder": 2, "displayable": True},
        "lap":      {"conditionTypeId": 1, "conditionTypeKey": "lap.button",
                     "displayOrder": 1, "displayable": True},
    }
    ITER_COND = {"conditionTypeId": 7, "conditionTypeKey": "iterations",
                 "displayOrder": 7, "displayable": False}
    HR_TARGET = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone",
                 "displayOrder": 4}
    NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target",
                 "displayOrder": 1}

    sport      = workout_plan.get("sport", "running")
    sport_type = SPORT_TYPES.get(sport, SPORT_TYPES["running"])
    steps_in   = workout_plan.get("steps", [])

    # Globaler stepId-Zähler über ALLE Schritte (auch verschachtelte)
    counter = {"id": 0}
    def next_id():
        counter["id"] += 1
        return counter["id"]

    def make_exec_step(step, order):
        phase  = step.get("phase", "active")
        et     = step.get("end_type", "distance")
        ev     = float(step.get("end_value", 1000))
        hr_min = step.get("hr_min")
        hr_max = step.get("hr_max")
        has_hr = bool(hr_min and hr_max)
        return {
            "type":              "ExecutableStepDTO",
            "stepId":            next_id(),
            "stepOrder":         order,
            "stepType":          STEP_TYPES.get(phase, STEP_TYPES["interval"]),
            "childStepId":       None,
            "description":       step.get("name", ""),
            "endCondition":      END_COND.get(et, END_COND["distance"]),
            "endConditionValue": ev,
            "preferredEndConditionUnit": None,
            "endConditionCompare": None,
            "targetType":        HR_TARGET if has_hr else NO_TARGET,
            "targetValueOne":    float(hr_min) if has_hr else None,
            "targetValueTwo":    float(hr_max) if has_hr else None,
            "zoneNumber":        None,
        }

    garmin_steps = []
    order = 1
    i = 0
    while i < len(steps_in):
        step = steps_in[i]
        rep  = step.get("repeats", 1)

        if rep > 1:
            # Sammle aufeinanderfolgende Schritte mit gleicher repeats-Zahl
            group_steps = []
            while i < len(steps_in) and steps_in[i].get("repeats", 1) == rep:
                group_steps.append(steps_in[i])
                i += 1

            repeat_id = next_id()
            inner = []
            inner_order = 1
            for gs in group_steps:
                inner.append(make_exec_step(gs, inner_order))
                inner_order += 1

            garmin_steps.append({
                "type":               "RepeatGroupDTO",
                "stepId":             repeat_id,
                "stepOrder":          order,
                "stepType":           {"stepTypeId": 6, "stepTypeKey": "repeat",
                                       "displayOrder": 6},
                "childStepId":        1,
                "numberOfIterations": rep,
                "smartRepeat":        False,
                "endCondition":       ITER_COND,
                "endConditionValue":  float(rep),
                "workoutSteps":       inner,
                "skipLastRestStep":   False,
            })
            order += 1
        else:
            garmin_steps.append(make_exec_step(step, order))
            order += 1
            i += 1

    return {
        "workoutName":  workout_plan.get("name", f"Training {today.isoformat()}"),
        "description":  workout_plan.get("description", ""),
        "sportType":    sport_type,
        "subSportType": None,
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType":    sport_type,
            "workoutSteps": garmin_steps,
        }],
    }


# ═════════════════════════════════════════════════════════════════════════════
# 6. WORKOUT IN GARMIN CONNECT HOCHLADEN
# ═════════════════════════════════════════════════════════════════════════════
def upload_workout_to_garmin(client: Garmin, workout_dict: dict,
                             today: datetime.date) -> dict:
    """
    Lädt Workout über Garmins workout-service hoch + plant es für heute.
    Nutzt garth.connectapi() — die korrekte Methode des internen HTTP-Clients.
    Mehrere Fallback-Strategien für maximale Robustheit.
    """
    def extract_id(result):
        if isinstance(result, dict):
            return (result.get("workoutId") or result.get("id")
                    or result.get("workout_id"))
        if isinstance(result, (int, str)):
            try: return int(result)
            except: return None
        return None

    def try_schedule(workout_id):
        """Plant Workout für heute. Mehrere Methoden."""
        date_str = today.isoformat()
        # garth connectapi
        try:
            client.garth.connectapi(
                f"/workout-service/schedule/{workout_id}",
                method="POST",
                json={"date": date_str}
            )
            print(f"   ✓ Für heute ({date_str}) geplant")
            return True
        except Exception as e:
            print(f"   Scheduling via connectapi fehlgeschlagen: {e}")
        return False

    workout_id = None
    upload_method = None

    # ── METHODE 1: garth.connectapi (korrekte primäre Methode) ───────────────
    if hasattr(client, "garth"):
        try:
            result = client.garth.connectapi(
                "/workout-service/workout",
                method="POST",
                json=workout_dict
            )
            workout_id = extract_id(result)
            if workout_id:
                upload_method = "garth.connectapi"
                print(f"   ✓ Upload via connectapi — Workout ID: {workout_id}")
        except Exception as e:
            print(f"   connectapi fehlgeschlagen: {str(e)[:120]}")

    # ── METHODE 2: garth.post mit voller URL ─────────────────────────────────
    if not workout_id and hasattr(client, "garth"):
        try:
            resp = client.garth.post(
                "connectapi",
                "/workout-service/workout",
                json=workout_dict,
                api=True
            )
            result = resp.json() if hasattr(resp, "json") else resp
            workout_id = extract_id(result)
            if workout_id:
                upload_method = "garth.post"
                print(f"   ✓ Upload via garth.post — Workout ID: {workout_id}")
        except Exception as e:
            print(f"   garth.post fehlgeschlagen: {str(e)[:120]}")

    # ── METHODE 3: add_workout (falls Library-Version es hat) ────────────────
    if not workout_id and hasattr(client, "add_workout"):
        try:
            result = client.add_workout(workout_dict)
            workout_id = extract_id(result)
            if workout_id:
                upload_method = "add_workout"
                print(f"   ✓ Upload via add_workout — Workout ID: {workout_id}")
        except Exception as e:
            print(f"   add_workout fehlgeschlagen: {str(e)[:120]}")

    # ── Ergebnis auswerten ────────────────────────────────────────────────────
    if workout_id:
        scheduled = try_schedule(workout_id)
        msg = ("Workout erstellt und für heute geplant ✓" if scheduled
               else "Workout erstellt ✓ (Garmin Connect → Trainings → Meine Workouts)")
        return {
            "success": True,
            "workout_id": workout_id,
            "scheduled": scheduled,
            "method": upload_method,
            "message": msg,
            "garmin_link": f"https://connect.garmin.com/modern/workout/{workout_id}"
        }
    else:
        return {
            "success": False,
            "workout_id": None,
            "message": "Upload über alle Methoden fehlgeschlagen — Session manuell auf der Uhr starten"
        }


# ═════════════════════════════════════════════════════════════════════════════
# 7. HTML DASHBOARD GENERIEREN (Claude Call 2 — Haupt-Dashboard)
# ═════════════════════════════════════════════════════════════════════════════
def generate_dashboard(garmin_data: dict, metrics: dict, day_ctx: dict,
                       events: list, workout_plan: dict, workout_status: dict) -> str:
    """
    Generiert das vollständige HTML-Dashboard mit eingebetteter Workout-Bestätigung.
    """
    client    = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    today     = datetime.date.today()
    today_str = today.strftime("%A, %d. %B %Y")

    # Swim-Warnung
    last_swim     = metrics.get("last_swim", {})
    swim_days_ago = (last_swim.get("days_ago") or 999) if last_swim else 999
    swim_warning  = ""
    for ev in events:
        if "triathlon" in ev.get("type",""):
            days_to_race = ev["days_left"]
            if swim_days_ago >= 14:
                swim_warning = f"🚨 KRITISCH: {swim_days_ago} Tage kein Schwimmen! Bamberg in {days_to_race} Tagen."
            elif swim_days_ago >= 7:
                swim_warning = f"⚠️ {swim_days_ago} Tage kein Schwimmen. Dringend ins Wasser."

    # Workout-Status für Dashboard
    if workout_plan.get("decision") == "REST":
        workout_banner = f"""
WORKOUT HEUTE: RUHETAG
Begründung: {workout_plan.get('reason','')}
Garmin: Kein Workout hochgeladen."""
    elif workout_status.get("success"):
        w = workout_plan.get("workout", {})
        steps_summary = " → ".join(
            f"{s.get('phase')} {s.get('end_value')}{s.get('end_unit','')}"
            + (f"x{s.get('repeats')}" if s.get("repeats",1)>1 else "")
            for s in w.get("steps",[])
        )
        workout_banner = f"""
WORKOUT HEUTE: ✅ IN GARMIN CONNECT HOCHGELADEN
Name: {w.get('name','')}
Sport: {w.get('sport','')}
Struktur: {steps_summary}
Begründung: {workout_plan.get('reason','')}
Status: {workout_status.get('message','')}"""
        if workout_status.get("garmin_link"):
            workout_banner += f"\nLink: {workout_status['garmin_link']}"
    else:
        w = workout_plan.get("workout", {}) or {}
        workout_banner = f"""
WORKOUT HEUTE: ⚠️ UPLOAD FEHLGESCHLAGEN
Name: {w.get('name','unbekannt')}
Begründung: {workout_plan.get('reason','')}
Fehler: {workout_status.get('message','')}
Manuelle Alternative: Session wie unten beschrieben selbst starten."""

    # Events string
    events_str = ""
    for ev in events:
        events_str += (f"\n{ev['emoji']} {ev['name']}: noch {ev['days_left']} Tage"
                       f" | Phase: {ev['phase']}"
                       f"\n  Strecke: {ev['distance']} | Ziel: {ev['goal']}"
                       f"\n  Taper: {ev['taper_style']}\n")

    # Freie Tage
    free_str = ", ".join(
        f"{d['weekday']} ({d['name']}, in {d['days_from_now']}d)"
        for d in day_ctx.get("upcoming_free_days",[])[:4]
    ) or "keine in 10 Tagen"

    # Daten
    acts_str  = json.dumps(garmin_data.get("activities",[])[:10], indent=1, default=str)[:6000]
    sleep_str = json.dumps(garmin_data.get("sleep",[])[:7], indent=1, default=str)[:4000]
    metr_str  = json.dumps(metrics, indent=2, ensure_ascii=False)

    prompt = f"""Du bist der persönliche Coach von Hannes Raschke.
Erstelle sein tägliches HTML-Dashboard für {today_str}.
{swim_warning}

TAGESKONTEXT:
Typ: {day_ctx['type']} | {day_ctx['note']}
Nächste freie Tage: {free_str}

FRISCHE METRIKEN:
{metr_str}

{workout_banner}

EVENTS:
{events_str}

LETZTE 10 AKTIVITÄTEN:
{acts_str}

SCHLAF LETZTE 7 NÄCHTE:
{sleep_str}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ERSTELLE EIN VOLLSTÄNDIGES STANDALONE HTML-DASHBOARD.
Kein externes CSS/JS/CDN. Alles inline. Mobil-first (375px).
Hintergrund #080c08 | Grün #4cff7c | Amber #ffb84c | Rot #ff6060 | Text #ddeedd
Schrift: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif

PFLICHT-REIHENFOLGE:

[1] HEADER — "Guten Morgen Hannes — {today_str}" (dezent, eine Zeile)

[2] METRIKEN — alle verfügbaren Tageswerte, große Zahlen, Ampelfarben:
Sleep Score | HRV letzte Nacht ms | Training Readiness | Body Battery | RHR | ACWR
Ampeln: Sleep≥85=grün,70-84=amber,<70=rot | HRV≥63=grün,50-62=amber,<50=rot
Readiness≥80=grün,60-79=amber | BB≥70=grün,40-69=amber | RHR≤58=grün,>65=rot
ACWR 0.8-1.3=grün | Freshness-Label unter jedem Wert ("heute nacht","gestern" etc.)

[3] WORKOUT GARMIN STATUS — prominent, farbige Box:
Falls Upload erfolgreich (✅): Grüne Box — Workout-Name + Sport + HR-Zonen + Kurzstruktur
  + "Workout ist in Garmin Connect — sync Uhr um es zu starten"
Falls REST (🔴): Rote Box — "Ruhetag" + Begründung
Falls Fehler (⚠️): Amber Box — Session-Beschreibung + Hinweis manuell starten

[4] TAGES-EMPFEHLUNG — ergänzende Details zur Session:
Konkrete HR-Zonen in bpm, Struktur, Tipps für die heutige Session

[5] NÄCHSTE 5 TAGE — Trainingsvorschau:
Für jeden Tag: Wochentag | Tagestyp | Geplante Session | Intensität
Freie Tage dürfen längere/härtere Sessions haben

[6] EVENTS — kompakte Karten:
Name | Tage | Phase | Disziplin-Ampeln (🏊🚴🏃 je nach days_ago)
Fokus: was ist jetzt wichtigste Aufgabe für dieses Event

[7] SCHLAF LETZTE 7 NÄCHTE — kompakte Tabelle

[8] LETZTE 7 TRAININGS — kompakte Liste

[9] TRAINING LOAD — ATL/CTL/ACWR wenn vorhanden

Wenn ACWR>1.4: Warnung oben als roter Banner.
Wenn Readiness<50: Ruhe-Banner über allem anderen.
Wenn Race Week (<7d): Race-Week-Banner statt normale Empfehlung.

Antworte AUSSCHLIESSLICH mit vollständigem HTML. Direkt <!DOCTYPE html>."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}]
    )
    html = message.content[0].text.strip()
    if html.startswith("```"):
        html = "\n".join(html.split("\n")[1:])
        if "```" in html:
            html = html.rsplit("```",1)[0]
    return html.strip()


# ═════════════════════════════════════════════════════════════════════════════
# 8. E-MAIL SENDEN
# ═════════════════════════════════════════════════════════════════════════════
def send_email(html_content: str, date_str: str):
    msg            = MIMEMultipart("alternative")
    msg["Subject"] = f"🏃 {date_str} — Garmin Dashboard"
    msg["From"]    = GMAIL_USER
    msg["To"]      = GMAIL_TO
    msg.attach(MIMEText(
        f"Dein Garmin Dashboard für {date_str}. HTML-fähigen Client öffnen.",
        "plain","utf-8"))
    msg.attach(MIMEText(html_content, "html","utf-8"))
    with smtplib.SMTP("smtp.mail.me.com", 587) as server:
        server.ehlo(); server.starttls(); server.ehlo()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, GMAIL_TO, msg.as_string())



# ═════════════════════════════════════════════════════════════════════════════
# SMART SLEEP POLLING — wartet bis heutiger Sleep Score verfügbar
# ═════════════════════════════════════════════════════════════════════════════
def wait_for_sleep_score(client: Garmin, max_wait_min: int = 90,
                          poll_interval_sec: int = 180) -> bool:
    """
    Prüft alle 3 Minuten ob der Sleep Score für HEUTE verfügbar ist.
    Gibt True zurück sobald Score da ist, False nach Timeout.
    Der Score wird von Garmin erst nach dem Aufwachen + Handy-Sync finalisiert.
    """
    today      = datetime.date.today()
    start_time = datetime.datetime.now()
    max_delta  = datetime.timedelta(minutes=max_wait_min)
    attempt    = 0

    print(f"   Smart Sleep Polling — warte auf Score für {today}")
    print(f"   Max Wartezeit: {max_wait_min} min | Intervall: {poll_interval_sec//60} min")

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
                    print(f"   ✓ Sleep Score {score} gefunden (nach {elapsed} min, Versuch {attempt})")
                    return True
        except Exception as e:
            print(f"   Poll-Fehler (Versuch {attempt}): {e}")

        elapsed_min = (datetime.datetime.now() - start_time).seconds // 60
        remaining   = max_wait_min - elapsed_min
        print(f"   Versuch {attempt}: kein Score — {elapsed_min}min gewartet,"
              f" noch {remaining}min | nächster Check in {poll_interval_sec//60}min")
        time.sleep(poll_interval_sec)

    print(f"   ⚠️ Timeout nach {max_wait_min} min — weiter ohne heutigen Sleep Score")
    return False

# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════
def main():
    today     = datetime.date.today()
    today_str = today.strftime("%d. %m. %Y")
    print(f"\n{'='*54}")
    print(f"  Garmin Daily Dashboard + Workout — {today_str}")
    print(f"{'='*54}\n")

    try:
        # ── 1. Login (einmalig, für Lesen + Schreiben) ────────────────────────
        print("1/6 — Garmin Login...")
        garmin_client = get_garmin_client()

        # ── 1b. Warten bis heutiger Sleep Score verfügbar ────────────────────
        print("1b/6 — Warte auf heutigen Sleep Score...")
        wait_for_sleep_score(garmin_client, max_wait_min=90, poll_interval_sec=180)

        # ── 2. Daten laden ────────────────────────────────────────────────────
        print("2/6 — Daten laden...")
        garmin_data = get_garmin_data(garmin_client)

        # ── 3. Kontext ────────────────────────────────────────────────────────
        print("3/6 — Kontext aufbauen...")
        metrics  = extract_fresh_metrics(garmin_data)
        day_ctx  = get_day_context(today)
        events   = get_events_context(today)
        print(f"   Tagestyp: {day_ctx['type']} | Events: {len(events)}")

        # ── 4. Workout-Entscheidung ───────────────────────────────────────────
        print("4/6 — Claude entscheidet Workout...")
        workout_plan = generate_workout_plan(metrics, day_ctx, events)
        decision     = workout_plan.get("decision","REST")
        print(f"   Entscheidung: {decision}")
        if decision == "TRAIN" and workout_plan.get("workout"):
            print(f"   Workout: {workout_plan['workout'].get('name','?')}")

        # ── 5. Workout in Garmin hochladen ────────────────────────────────────
        workout_status = {"success": False, "message": "Kein Training heute", "workout_id": None}
        if decision == "TRAIN" and workout_plan.get("workout"):
            print("5/6 — Workout in Garmin Connect hochladen...")
            garmin_workout = build_garmin_workout(workout_plan["workout"], today)
            workout_status = upload_workout_to_garmin(garmin_client, garmin_workout, today)
            print(f"   Status: {workout_status['message']}")
        else:
            print("5/6 — Ruhetag, kein Workout-Upload")

        # ── 6. Dashboard generieren ───────────────────────────────────────────
        print("6/6 — Claude generiert Dashboard...")
        html_dashboard = generate_dashboard(
            garmin_data, metrics, day_ctx, events, workout_plan, workout_status
        )
        print(f"   Dashboard: {len(html_dashboard):,} Zeichen")

        # ── E-Mail ────────────────────────────────────────────────────────────
        send_email(html_dashboard, today_str)
        print(f"\n✅ FERTIG — E-Mail an {GMAIL_TO} gesendet\n")

    except Exception as e:
        print(f"\n❌ FEHLER: {e}")
        traceback.print_exc()
        try:
            err_html = f"""<!DOCTYPE html><html><body
              style="background:#1a0000;color:#ff8080;font-family:monospace;padding:20px">
              <h2>⚠️ Dashboard Fehler — {today_str}</h2>
              <pre style="background:#2a0000;padding:15px;border-radius:8px;
              white-space:pre-wrap">{traceback.format_exc()}</pre></body></html>"""
            send_email(err_html, today_str)
        except: pass
        raise


if __name__ == "__main__":
    main()
