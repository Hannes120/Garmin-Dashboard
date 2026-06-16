#!/usr/bin/env python3
"""
Garmin Training Dashboard — robuste Fassung.

Designziele dieser Version:
  * Garmin-Login persistiert zuverlässig (Token via Secret ODER Cache), kein MFA
    bei jedem Lauf, kein "halb eingeloggter" Zustand mehr.
  * Datenabruf ist tolerant: Schlaf/Body Battery/HRV werden über mehrere Tage
    und mehrere Antwort-Layouts hinweg gesucht. Fehlt etwas, läuft der Rest
    trotzdem weiter.
  * Es wird IMMER ein Training ausgegeben. Wenn Claude nicht antwortet, greift
    ein regelbasierter Fallback-Coach. Das Skript bricht nie still ab.
  * Idempotenz: pro Tag wird genau eine Mail verschickt (per State-Datei/Cache),
    mit FORCE_SEND-Override fürs Testen.
"""

import os
import re
import sys
import json
import time
import base64
import logging
import smtplib
import datetime
import statistics
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from anthropic import Anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── Timezone ──────────────────────────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("DASHBOARD_TZ", "Europe/Berlin"))
except Exception:  # pragma: no cover
    TZ = None
    log.warning("zoneinfo unavailable — falling back to system (UTC) time")


def today_local() -> datetime.date:
    if TZ is not None:
        return datetime.datetime.now(TZ).date()
    return datetime.date.today()


# ─── Konstanten ────────────────────────────────────────────────────────────────

RACE_CALENDAR = [
    {"name": "Halbmarathon Geburtstag", "date": "2026-07-12", "type": "run",
     "goal": "Sub 2:00 h"},
    {"name": "Königsbrunn Middle Distance", "date": "2026-09-20", "type": "triathlon",
     "goal": "Sub 6:00 h (1.9k/80k/20k)"},
]

POLARIZED_TARGET = 0.80
ANTHROPIC_MODEL  = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")


# ─── Garmin: Auth ──────────────────────────────────────────────────────────────

def _materialize_token_from_secret(token_store: str) -> bool:
    """
    Falls GARMIN_TOKEN_BASE64 gesetzt ist, den dort hinterlegten garth-Token-Dump
    in token_store entpacken. Das ist der zuverlässigste Weg: einmal lokal
    eingeloggt, Token als Repo-Secret hinterlegt → nie wieder MFA im CI.

    Erzeugen (einmalig, lokal):
        import garth; garth.login("EMAIL","PW")  # ggf. MFA
        print(garth.client.dumps())              # base64-String → als Secret speichern
    """
    blob = os.environ.get("GARMIN_TOKEN_BASE64", "").strip()
    if not blob:
        return False
    try:
        import garth
        os.makedirs(token_store, exist_ok=True)
        # garth.client.loads akzeptiert den dumps()-String direkt.
        garth.client.loads(blob)
        garth.client.dump(token_store)
        log.info("Garmin token aus GARMIN_TOKEN_BASE64 materialisiert")
        return True
    except Exception as e:
        log.warning(f"GARMIN_TOKEN_BASE64 konnte nicht geladen werden: {e}")
        return False


def garmin_connect():
    import garminconnect

    token_store = os.environ.get("GARM_TOKENS_DIR", "/tmp/garmin_tokens")
    os.makedirs(token_store, exist_ok=True)

    email    = os.environ.get("GARMIN_EMAIL", "")
    password = os.environ.get("GARMIN_PASSWORD", "")

    # Bevorzugt: Token aus Secret (umgeht MFA komplett).
    _materialize_token_from_secret(token_store)

    client = garminconnect.Garmin(
        email, password, is_cn=False,
        prompt_mfa=lambda: os.environ.get("GARMIN_MFA", ""),
    )

    # 1) Vorhandene Session aus token_store fortsetzen (kein Re-Login/MFA).
    try:
        client.login(token_store)
        log.info("Garmin login OK (Session aus Token-Store fortgesetzt)")
        _persist_tokens(client, token_store)
        return client
    except Exception as e:
        log.warning(f"Token-Login fehlgeschlagen, versuche Voll-Login: {e}")

    # 2) Voll-Login mit Passwort + Backoff (nur nötig, wenn Token fehlt/abgelaufen).
    if not (email and password):
        raise RuntimeError(
            "Kein gültiger Token und keine GARMIN_EMAIL/GARMIN_PASSWORD gesetzt."
        )
    for attempt in range(3):
        try:
            client.login()  # frischer Login
            log.info("Garmin login OK (Voll-Login)")
            _persist_tokens(client, token_store)
            return client
        except Exception as e:
            log.warning(f"Login-Versuch {attempt + 1} fehlgeschlagen: {e}")
            if attempt < 2:
                time.sleep(10 * (attempt + 1))
    raise RuntimeError("Garmin login nach 3 Versuchen fehlgeschlagen")


def _persist_tokens(client, token_store: str) -> None:
    """Aktuelle (ggf. erneuerte) Tokens zurückschreiben, damit der CI-Cache sie sichert."""
    try:
        client.garth.dump(token_store)
    except Exception as e:
        log.debug(f"Token-Dump nicht möglich (unkritisch): {e}")


# ─── Garmin: Daten ─────────────────────────────────────────────────────────────

def safe_get(fn, default=None, label="api"):
    for attempt in range(3):
        try:
            return fn()
        except Exception as e:
            log.warning(f"{label} fehlgeschlagen (Versuch {attempt + 1}): {e}")
            time.sleep(4)
    log.error(f"{label}: gebe Default zurück nach 3 Fehlversuchen")
    return default


def _num(x):
    return x if isinstance(x, (int, float)) else None


def _extract_bb_level(body_battery):
    """
    Letzten Body-Battery-*Stand* (0–100) aus den diversen Antwortformaten ziehen.
    Format-Varianten, die hier abgedeckt werden:
      * Liste von Tages-Dicts mit 'bodyBatteryValuesArray' aus Tupeln
        [timestamp, status, level, ...]  → level an Index 2
      * Tupel [timestamp, level]          → level an Index 1
      * Felder bodyBatteryMostRecentValue / level / charged
    """
    if not isinstance(body_battery, list) or not body_battery:
        return None

    best_level = None
    for entry in body_battery:
        if not isinstance(entry, dict):
            continue
        arr = (entry.get("bodyBatteryValuesArray")
               or entry.get("bodyBatteryValuesMap")
               or [])
        for v in arr:
            if not isinstance(v, (list, tuple)) or len(v) < 2:
                continue
            # level steht je nach Version an Index 2 (mit status) oder 1.
            cand = None
            if len(v) >= 3 and isinstance(v[2], (int, float)):
                cand = v[2]
            elif isinstance(v[1], (int, float)):
                cand = v[1]
            if cand is not None and 0 <= cand <= 100:
                best_level = cand  # letzter gültiger Wert gewinnt (chronologisch)

    if best_level is not None:
        return best_level

    for key in ("bodyBatteryMostRecentValue", "level", "bodyBattery", "charged"):
        for entry in body_battery:
            if isinstance(entry, dict) and isinstance(entry.get(key), (int, float)):
                return entry[key]
    return None


def _extract_sleep(sleep_raw):
    """Schlaf-Kernwerte tolerant aus verschiedenen DTO-Layouts ziehen."""
    if not isinstance(sleep_raw, dict):
        return None
    daily = (sleep_raw.get("dailySleepDTO")
             or sleep_raw.get("sleepDTO")
             or sleep_raw.get("daily_sleep")
             or {})
    if not isinstance(daily, dict):
        daily = {}

    score = None
    scores = daily.get("sleepScores") or {}
    if isinstance(scores, dict):
        overall = scores.get("overall") or {}
        if isinstance(overall, dict):
            score = overall.get("value")
    score = score or daily.get("sleepScore") or daily.get("overallScore")

    duration_s = daily.get("sleepTimeSeconds") or sleep_raw.get("sleepTimeSeconds") or 0
    rem_s   = daily.get("remSleepSeconds")   or sleep_raw.get("remSleepSeconds")   or 0
    deep_s  = daily.get("deepSleepSeconds")  or sleep_raw.get("deepSleepSeconds")  or 0
    light_s = daily.get("lightSleepSeconds") or sleep_raw.get("lightSleepSeconds") or 0

    if not (score or duration_s):
        return None  # nichts Brauchbares

    return {
        "score": score,
        "duration_h": round((duration_s or 0) / 3600, 2),
        "rem_min": round((rem_s or 0) / 60),
        "deep_min": round((deep_s or 0) / 60),
        "light_min": round((light_s or 0) / 60),
    }


def fetch_sleep(client, today):
    """Schlaf der letzten Nacht suchen — über mehrere Tage hinweg, robust."""
    # Nächte rückwärts probieren: gestern, vorgestern, heute (frühe Läufe).
    for delta in (1, 2, 0, 3):
        d = (today - datetime.timedelta(days=delta)).isoformat()
        raw = safe_get(lambda: client.get_sleep_data(d), {}, f"sleep({d})") or {}
        parsed = _extract_sleep(raw)
        if parsed:
            parsed["night_of"] = d
            parsed["synced"] = True
            log.info(f"Schlafdaten gefunden für Nacht {d} (Score={parsed['score']}, "
                     f"{parsed['duration_h']}h)")
            return parsed
    log.warning("Keine Schlafdaten in den letzten 3 Nächten gefunden")
    return {"score": None, "duration_h": 0, "rem_min": 0, "deep_min": 0,
            "light_min": 0, "night_of": None, "synced": False}


def fetch_body_battery(client, today):
    """Body Battery über kurzen Zeitraum holen (heute + gestern), letzten Wert nehmen."""
    start = (today - datetime.timedelta(days=1)).isoformat()
    end   = today.isoformat()
    bb = safe_get(lambda: client.get_body_battery(start, end), None, "body_battery")
    level = _extract_bb_level(bb)
    if level is None:
        # Fallback: Single-Day-Aufruf.
        bb1 = safe_get(lambda: client.get_body_battery(end), None, "body_battery(1d)")
        level = _extract_bb_level(bb1)
    return level


def fetch_training_readiness(client, today):
    """Training Readiness Score (0–100) — sehr guter Erholungs-Indikator, wenn vorhanden."""
    tr = safe_get(lambda: client.get_training_readiness(today.isoformat()),
                  None, "training_readiness")
    if isinstance(tr, list) and tr:
        tr = tr[0]
    if isinstance(tr, dict):
        return tr.get("score") or tr.get("trainingReadinessScore")
    return None


def fetch_all_data(client):
    today = today_local()
    two_weeks_ago = today - datetime.timedelta(days=14)

    # ── Schlaf ──
    sleep = fetch_sleep(client, today)

    # ── Body Battery / HRV / Readiness ──
    bb_now = fetch_body_battery(client, today)
    readiness = fetch_training_readiness(client, today)

    hrv_raw = safe_get(lambda: client.get_hrv_data(today.isoformat()), {}, "hrv")
    hrv_summary = hrv_raw.get("hrvSummary", {}) if isinstance(hrv_raw, dict) else {}
    hrv_value = (hrv_summary.get("lastNightAvg")
                 or hrv_summary.get("lastNight")
                 or hrv_summary.get("weeklyAvg"))

    # ── Stats heute ──
    stats_raw = safe_get(lambda: client.get_stats(today.isoformat()), {}, "stats") or {}
    rhr      = stats_raw.get("restingHeartRate")
    stress   = stats_raw.get("averageStressLevel")
    steps    = stats_raw.get("totalSteps")
    calories = stats_raw.get("totalKilocalories")
    vo2max   = stats_raw.get("vo2MaxValue") or stats_raw.get("maxMetValue")

    if vo2max is None:
        mm = safe_get(lambda: client.get_max_metrics(today.isoformat()), None, "max_metrics")
        if isinstance(mm, list) and mm and isinstance(mm[0], dict):
            generic = mm[0].get("generic") or {}
            vo2max = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")

    # ── Aktivitäten 14 Tage ──
    activities_raw = safe_get(
        lambda: client.get_activities_by_date(two_weeks_ago.isoformat(), today.isoformat()),
        [], "activities") or []

    activities = []
    for a in activities_raw[:40]:
        if not isinstance(a, dict):
            continue
        at = a.get("activityType")
        act_type = (at.get("typeKey") if isinstance(at, dict) else at) or "unknown"
        act_type = str(act_type).lower()

        duration_min = (a.get("duration") or a.get("movingDuration") or 0) / 60
        distance_km  = (a.get("distance") or 0) / 1000
        tss          = a.get("trainingStressScore") or a.get("tss")
        avg_hr       = a.get("averageHR") or a.get("averageHeartRate")
        max_hr       = a.get("maxHR") or a.get("maxHeartRate")

        hr_zones = {}
        for z in (a.get("heartRateZones") or a.get("hrZones") or a.get("zones") or []):
            if not isinstance(z, dict):
                continue
            zn   = z.get("zoneNumber") or z.get("zone")
            secs = z.get("secsInZone") or z.get("seconds") or z.get("duration") or 0
            if zn:
                hr_zones[f"z{zn}"] = round(secs / 60, 1)

        act_date = str(a.get("startTimeLocal") or a.get("beginTimestamp")
                       or a.get("startTimeGMT") or "")[:10]

        activities.append({
            "date": act_date, "type": act_type,
            "duration_min": round(duration_min, 1),
            "distance_km": round(distance_km, 2),
            "tss": tss, "avg_hr": avg_hr, "max_hr": max_hr,
            "hr_zones": hr_zones, "name": a.get("activityName", ""),
        })

    activities.sort(key=lambda a: a["date"])  # chronologisch

    # ── Zonen (7 Tage) ──
    week_ago = (today - datetime.timedelta(days=7)).isoformat()
    recent_acts = [a for a in activities if a["date"] >= week_ago]
    zone_totals = {"z1": 0, "z2": 0, "z3": 0, "z4": 0, "z5": 0}
    for a in recent_acts:
        for z, mins in a["hr_zones"].items():
            if z in zone_totals:
                zone_totals[z] += mins
    total = sum(zone_totals.values())
    if total > 0:
        zone_pct = {z: round(m / total * 100, 1) for z, m in zone_totals.items()}
        easy_pct = round(zone_pct.get("z1", 0) + zone_pct.get("z2", 0), 1)
    else:
        zone_pct, easy_pct = {}, None

    # ── Load (ATL/CTL/TSB aus TSS) ──
    tss_values = [a["tss"] for a in activities if a["tss"] is not None]
    atl = round(statistics.mean(tss_values[-7:]),  1) if len(tss_values) >= 3 else None
    ctl = round(statistics.mean(tss_values[-42:]), 1) if len(tss_values) >= 7 else None
    tsb = round(ctl - atl, 1) if (atl is not None and ctl is not None) else None

    return {
        "date": today.isoformat(),
        "sleep": {**sleep},
        "body": {
            "battery": bb_now, "hrv": hrv_value, "rhr": rhr, "stress": stress,
            "steps": steps, "calories": calories, "vo2max": vo2max,
            "readiness": readiness,
        },
        "load": {"atl": atl, "ctl": ctl, "tsb": tsb},
        "zones_7d": {"totals_min": zone_totals, "pct": zone_pct, "easy_pct": easy_pct},
        "activities_14d": activities,
        "recent_count": len(recent_acts),
    }


def recent_activities(data, n=5):
    return list(reversed(data["activities_14d"]))[:n]


# ─── Race-Phase ────────────────────────────────────────────────────────────────

def race_context():
    today = today_local()
    upcoming = []
    for r in RACE_CALENDAR:
        days_out = (datetime.date.fromisoformat(r["date"]) - today).days
        if days_out >= -3:
            upcoming.append({**r, "days_out": days_out})

    if not upcoming:
        return {"phase": "Off-season", "next_race": None, "days_out": None,
                "all_upcoming": []}

    upcoming.sort(key=lambda x: x["days_out"])
    nxt = upcoming[0]
    d = nxt["days_out"]
    if   d <= 7:  phase = "Race Week"
    elif d <= 21: phase = "Taper"
    elif d <= 56: phase = "Build"
    else:         phase = "Base"
    return {"phase": phase, "next_race": nxt, "days_out": d, "all_upcoming": upcoming}


# ─── Erholungs-Ampel (steuert Coach + Fallback) ────────────────────────────────

def recovery_state(data):
    """
    Liefert 'good' | 'ok' | 'low' aus den vorhandenen Signalen.
    Robust gegen fehlende Werte: nur vorhandene Signale zählen.
    """
    score = 0
    n = 0
    bb = data["body"].get("battery")
    rd = data["body"].get("readiness")
    sl = data["sleep"].get("duration_h")
    sc = data["sleep"].get("score")
    tsb = data["load"].get("tsb")

    def add(val, low, high):
        nonlocal score, n
        if val is None:
            return
        n += 1
        score += 0 if val < low else (2 if val >= high else 1)

    add(bb, 40, 70)
    add(rd, 40, 70)
    add(sl, 6.0, 7.5)
    add(sc, 60, 80)
    if tsb is not None:
        n += 1
        score += 0 if tsb < -20 else (2 if tsb > -5 else 1)

    if n == 0:
        return "ok"  # keine Daten → neutral
    ratio = score / (2 * n)
    return "low" if ratio < 0.4 else ("good" if ratio >= 0.7 else "ok")


# ─── Claude ────────────────────────────────────────────────────────────────────

def call_claude(client, system_prompt, user_prompt, max_tokens=1200):
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model=ANTHROPIC_MODEL, max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            if resp.content and getattr(resp.content[0], "text", None):
                return resp.content[0].text
            log.warning("Claude lieferte leeren Inhalt")
        except Exception as e:
            log.warning(f"Claude-Aufruf {attempt + 1} fehlgeschlagen: {e}")
            time.sleep(5)
    return None


def extract_json(raw):
    if not raw:
        return None
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception as e:
        log.error(f"JSON-Parse-Fehler: {e}\nRaw: {str(raw)[:300]}")
        return None


def _data_quality_note(data):
    missing = []
    if not data["sleep"].get("synced"): missing.append("Schlaf")
    if data["body"].get("battery") is None: missing.append("Body Battery")
    if data["body"].get("hrv") is None: missing.append("HRV")
    if missing:
        return ("Hinweis: Folgende Werte fehlen heute (noch nicht gesynct) — "
                f"plane konservativ/neutral: {', '.join(missing)}.")
    return "Alle Kernwerte vorhanden."


def build_todays_workout(claude_client, data, race_ctx):
    system = """Du bist ein erfahrener Triathlon-Coach.
Antworte NUR mit einem JSON-Objekt — keine Prosa, keine Markdown-Backticks.
Schema:
{
  "primary": {
    "sport": "swim|bike|run|strength|rest",
    "title": "kurzer Titel",
    "duration_min": <int>,
    "intensity": "easy|moderate|hard",
    "zones": "<z.B. Z1-Z2>",
    "structure": "<Warm-up / Hauptteil / Cool-down in 2-3 Sätzen>",
    "rationale": "<1 Satz Begründung>"
  },
  "optional": { ... } | null,
  "day_verdict": "<easy|moderate|hard>",
  "recovery_note": "<1 Satz falls Schlaf/BB schlecht, sonst leer>"
}"""

    rec = recovery_state(data)
    user = f"""Athletendaten heute ({data['date']}):

Erholungs-Ampel (vorberechnet): {rec.upper()}
{_data_quality_note(data)}

Schlaf: Score={data['sleep']['score']}, Dauer={data['sleep']['duration_h']}h, Tief={data['sleep']['deep_min']}min, REM={data['sleep']['rem_min']}min
Body Battery: {data['body']['battery']}/100
Training Readiness: {data['body']['readiness']}
HRV: {data['body']['hrv']} ms
Ruhepuls: {data['body']['rhr']} bpm
Stress gestern: {data['body']['stress']}

Trainingsload: ATL={data['load']['atl']}, CTL={data['load']['ctl']}, TSB={data['load']['tsb']}
Zonenverteilung letzte 7 Tage: {json.dumps(data['zones_7d']['pct'])}
Easyanteil: {data['zones_7d']['easy_pct']}% (Ziel: ≥80%)

Letzte 5 Aktivitäten:
{json.dumps(recent_activities(data, 5), indent=2, ensure_ascii=False)}

Race-Phase: {race_ctx['phase']}
Nächstes Rennen: {race_ctx['next_race']['name'] if race_ctx['next_race'] else 'keins'} in {race_ctx['days_out']} Tagen
Ziel: {race_ctx['next_race']['goal'] if race_ctx['next_race'] else '-'}
Wochentag: {today_local().strftime('%A')}

Regeln:
- Es MUSS immer ein primary-Workout ausgegeben werden (auch bei rest: sport="rest", duration_min=0).
- Polarisiert: 80% Z1-Z2, max 20% Z3-Z5.
- Wenn Ampel=LOW (oder TSB<-20 oder BB<40 oder Schlaf<6h) → nur Easy oder Rest.
- Race Week → nur kurze Aktivierung, kein Stress.
- Wenn Easy-Anteil < 70% → primary MUSS Z1-Z2 sein.
- Material: Chainrings 52-36T, Shimano Dura-Ace Di2."""

    return extract_json(call_claude(claude_client, system, user, max_tokens=1000))


def build_week_plan(claude_client, data, race_ctx):
    system = """Du bist ein erfahrener Triathlon-Coach.
Antworte NUR mit einem JSON-Objekt — keine Prosa, keine Markdown-Backticks.
Schema:
{
  "week_theme": "<1 Satz Wochenmotto>",
  "days": {
    "Mo": {"sport":"...","focus":"...","duration_min":<int>,"intensity":"easy|moderate|hard"},
    "Di": {...}, "Mi": {...}, "Do": {...}, "Fr": {...}, "Sa": {...}, "So": {...}
  },
  "weekly_load_note": "<1 Satz>",
  "key_session": "<Tag der Haupteinheit>"
}"""

    overview = [{
        "date": a["date"], "type": a["type"], "duration_min": a["duration_min"],
        "intensity_approx": "hard" if a["avg_hr"] and a["avg_hr"] > 160 else "easy",
    } for a in data["activities_14d"]]

    user = f"""Erstelle einen Wochenplan (Heute ist {today_local().strftime('%A')}, {data['date']}).

Phase: {race_ctx['phase']}
Nächstes Rennen: {race_ctx['next_race']['name'] if race_ctx['next_race'] else 'keins'} in {race_ctx['days_out']} Tagen (Ziel: {race_ctx['next_race']['goal'] if race_ctx['next_race'] else '-'})
Trainingsload: ATL={data['load']['atl']}, CTL={data['load']['ctl']}, TSB={data['load']['tsb']}
Zonenverteilung 7 Tage: {json.dumps(data['zones_7d']['pct'])}
Aktivitäten 14 Tage: {json.dumps(overview, indent=2, ensure_ascii=False)}

Regeln:
- Jeder der 7 Tage MUSS gefüllt sein (Ruhetag als sport="rest").
- Triathlet: Schwimmen + Rad + Lauf, 80/20 Polarisierung.
- Max 2 harte Einheiten/Woche, 1 Ruhetag, lange Einheit am Wochenende.
- Bei TSB<-15 Volumen reduzieren. Taper: Frequenz halten, Volumen -30-40%."""

    return extract_json(call_claude(claude_client, system, user, max_tokens=1200))


# ─── Regelbasierter Fallback (falls Claude ausfällt) ───────────────────────────

def fallback_workout(data, race_ctx):
    """Garantiert ein sinnvolles Training, auch ohne Claude."""
    rec = recovery_state(data)
    phase = race_ctx["phase"]
    weekday = today_local().weekday()  # 0=Mo

    if rec == "low":
        return {
            "primary": {"sport": "run", "title": "Regeneratives Laufen",
                        "duration_min": 30, "intensity": "easy", "zones": "Z1",
                        "structure": "Locker traben, komplett im Wohlfühltempo. Abbrechen wenn's sich zäh anfühlt.",
                        "rationale": "Erholungssignale niedrig — heute nur lockern."},
            "optional": None, "day_verdict": "easy",
            "recovery_note": "Erholung niedrig: kein intensives Training heute."}

    if phase == "Race Week":
        return {
            "primary": {"sport": "bike", "title": "Aktivierung mit Steigerungen",
                        "duration_min": 40, "intensity": "easy", "zones": "Z1-Z2",
                        "structure": "30 min locker Z1-Z2, dazwischen 4×20 s zügige Steigerungen. Beine wecken, nicht ermüden.",
                        "rationale": "Race Week — Spritzigkeit halten, Frische sichern."},
            "optional": None, "day_verdict": "easy", "recovery_note": ""}

    # Standard-Wochenstruktur (polarisiert) nach Wochentag
    plan = {
        0: ("swim", "Technik & Grundlage Schwimmen", 60, "easy", "Z1-Z2",
            "10×100 m Technik mit Pausen, dann 800 m locker durchschwimmen."),
        1: ("run", "Intervalle Laufen", 60, "hard", "Z4",
            "15 min Einlaufen, 5×3 min Z4 / 2 min Trab, 10 min Auslaufen."),
        2: ("bike", "Grundlage Rad", 90, "easy", "Z2",
            "Gleichmäßig Z2, flach bis welliges Profil, Trittfrequenz 90+."),
        3: ("run", "Schwellenlauf", 55, "moderate", "Z3",
            "15 min locker, 2×10 min Z3 / 3 min Trab, 10 min aus."),
        4: ("rest", "Ruhetag", 0, "easy", "-",
            "Bewusst regenerieren: Mobility, Schlaf, Ernährung."),
        5: ("bike", "Lange Ausfahrt", 150, "easy", "Z2",
            "Langer GA1-Ride, gleichmäßig Z2, gut essen/trinken."),
        6: ("run", "Langer Lauf", 90, "easy", "Z2",
            "Ruhiger Dauerlauf Z2, letzte 15 min leicht zügiger wenn frisch."),
    }
    sport, title, dur, inten, zones, struct = plan[weekday]
    return {
        "primary": {"sport": sport, "title": title, "duration_min": dur,
                    "intensity": inten, "zones": zones, "structure": struct,
                    "rationale": f"{phase}-Phase, Standardstruktur nach Wochentag."},
        "optional": None,
        "day_verdict": inten,
        "recovery_note": "" if rec != "ok" else ""}


def fallback_week_plan(race_ctx):
    days = {
        "Mo": {"sport": "swim", "focus": "Technik & Grundlage", "duration_min": 60, "intensity": "easy"},
        "Di": {"sport": "run",  "focus": "Intervalle Z4",        "duration_min": 60, "intensity": "hard"},
        "Mi": {"sport": "bike", "focus": "Grundlage Z2",         "duration_min": 90, "intensity": "easy"},
        "Do": {"sport": "run",  "focus": "Schwelle Z3",          "duration_min": 55, "intensity": "moderate"},
        "Fr": {"sport": "rest", "focus": "Ruhetag",              "duration_min": 0,  "intensity": "easy"},
        "Sa": {"sport": "bike", "focus": "Lange Ausfahrt Z2",    "duration_min": 150,"intensity": "easy"},
        "So": {"sport": "run",  "focus": "Langer Lauf Z2",       "duration_min": 90, "intensity": "easy"},
    }
    return {
        "week_theme": f"{race_ctx['phase']}-Woche: polarisiert mit 2 Qualitätseinheiten.",
        "days": days,
        "weekly_load_note": "80/20 Polarisierung, ~8 h Volumen, 1 Ruhetag.",
        "key_session": "Sa",
    }


# ─── HTML / Plaintext (unverändert im Look, nur Readiness ergänzt) ─────────────

def render_metric(label, value, unit="", warn_below=None, good_above=None):
    if value is None:
        display, color = "–", "#888"
    else:
        display = str(value)
        if warn_below is not None and isinstance(value, (int, float)) and value < warn_below:
            color = "#e05c4a"
        elif good_above is not None and isinstance(value, (int, float)) and value >= good_above:
            color = "#3aaa6e"
        else:
            color = "#1a1a2e"
    return f"""
    <td style="padding:8px 12px;text-align:center;vertical-align:top;">
      <div style="font-size:22px;font-weight:700;color:{color};line-height:1.1;">{display}<span style="font-size:11px;font-weight:400;color:#888;margin-left:2px;">{unit}</span></div>
      <div style="font-size:10px;color:#999;text-transform:uppercase;letter-spacing:.5px;margin-top:2px;">{label}</div>
    </td>"""


def zone_bar(label, pct, color):
    if not pct:
        return ""
    bar_w = min(int(pct * 1.8), 180)
    return f"""
    <tr>
      <td style="font-size:12px;color:#555;padding:3px 0;width:30px;">{label}</td>
      <td style="padding:3px 6px;"><div style="background:{color};width:{bar_w}px;height:12px;border-radius:3px;display:inline-block;"></div></td>
      <td style="font-size:12px;color:#333;padding:3px 0;">{pct}%</td>
    </tr>"""


def workout_card(w, is_optional=False):
    if not w:
        return ""
    icol = {"easy": "#3aaa6e", "moderate": "#f5a623", "hard": "#e05c4a"}
    icon = {"swim": "🏊", "bike": "🚴", "run": "🏃", "strength": "💪", "rest": "😴"}
    tag = "Optional" if is_optional else "Heute"
    color = icol.get(w.get("intensity", ""), "#888")
    ic = icon.get(w.get("sport", ""), "🎯")
    return f"""
    <div style="background:#f9f9fb;border-left:4px solid {color};border-radius:8px;padding:16px 18px;margin:12px 0;">
      <div style="font-size:10px;color:#aaa;text-transform:uppercase;letter-spacing:.8px;">{tag}</div>
      <div style="font-size:17px;font-weight:700;color:#1a1a2e;margin:4px 0 8px;">{ic} {w.get('title','Einheit')}</div>
      <table cellpadding="0" cellspacing="0" style="margin-bottom:10px;"><tr>
        <td style="padding-right:16px;font-size:12px;color:#555;">⏱ {w.get('duration_min','?')} min</td>
        <td style="padding-right:16px;font-size:12px;color:#555;">📊 {w.get('zones','–')}</td>
        <td style="font-size:12px;color:{color};font-weight:600;">{str(w.get('intensity','')).capitalize()}</td>
      </tr></table>
      <div style="font-size:13px;color:#333;line-height:1.6;margin-bottom:8px;">{w.get('structure','')}</div>
      <div style="font-size:11px;color:#888;font-style:italic;">{w.get('rationale','')}</div>
    </div>"""


def week_plan_table(plan):
    if not plan or "days" not in plan:
        return ""
    ibg = {"easy": "#e8f7ef", "moderate": "#fff4e0", "hard": "#fdecea"}
    icl = {"easy": "#2e8b57", "moderate": "#cc8800", "hard": "#c0392b"}
    icon = {"swim": "🏊", "bike": "🚴", "run": "🏃", "strength": "💪", "rest": "😴",
            "swim/run": "🏊🏃", "bike/run": "🚴🏃"}
    day_map = {"Mo": "Mon", "Di": "Tue", "Mi": "Wed", "Do": "Thu", "Fr": "Fri", "Sa": "Sat", "So": "Sun"}
    today_eng = today_local().strftime("%a")
    rows = ""
    for day_de, info in plan["days"].items():
        if not isinstance(info, dict):
            continue
        is_today = day_map.get(day_de, "") == today_eng
        bg = "#eef3ff" if is_today else "transparent"
        fw = "700" if is_today else "400"
        inten = info.get("intensity", "easy")
        rows += f"""
        <tr style="background:{bg};">
          <td style="padding:8px 10px;font-size:13px;font-weight:{fw};color:#1a1a2e;width:32px;">{day_de}</td>
          <td style="padding:8px 4px;font-size:16px;">{icon.get(str(info.get('sport','')).lower(),'🎯')}</td>
          <td style="padding:8px 10px;font-size:13px;color:#333;">{info.get('focus','')}</td>
          <td style="padding:8px 6px;font-size:11px;text-align:center;"><span style="background:{ibg.get(inten,'#eee')};color:{icl.get(inten,'#333')};border-radius:10px;padding:2px 8px;font-weight:600;">{info.get('duration_min','?')}min</span></td>
          <td style="padding:8px 6px;font-size:11px;color:{icl.get(inten,'#333')};">{str(inten).capitalize()}</td>
        </tr>"""
    return f"""
    <div style="overflow:hidden;border-radius:8px;border:1px solid #ebebeb;">
      <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">{rows}</table>
    </div>
    <div style="margin-top:8px;font-size:11px;color:#888;font-style:italic;">{plan.get('weekly_load_note','')}</div>"""


def build_html(data, workout, week_plan, race_ctx):
    today = today_local()
    weekday_de = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"][today.weekday()]
    months_de = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]
    date_str = f"{weekday_de}, {today.day:02d}. {months_de[today.month - 1]} {today.year}"

    sleep, body, load, zones = data["sleep"], data["body"], data["load"], data["zones_7d"]

    sc = sleep.get("score")
    if   sc and sc >= 80: sq_label, sq_color = "Erholt", "#3aaa6e"
    elif sc and sc >= 60: sq_label, sq_color = "Ok", "#f5a623"
    elif sc:              sq_label, sq_color = "Schlecht", "#e05c4a"
    else:                 sq_label, sq_color = "–", "#888"

    phase_colors = {"Base": "#3aaa6e", "Build": "#f5a623", "Peak": "#e05c4a",
                    "Taper": "#9b59b6", "Race Week": "#c0392b", "Off-season": "#888"}
    phase_color = phase_colors.get(race_ctx["phase"], "#888")

    zp = zones.get("pct", {})
    zbars = (zone_bar("Z1", zp.get("z1"), "#3aaa6e") + zone_bar("Z2", zp.get("z2"), "#5bc8af") +
             zone_bar("Z3", zp.get("z3"), "#f5a623") + zone_bar("Z4", zp.get("z4"), "#e07b3a") +
             zone_bar("Z5", zp.get("z5"), "#e05c4a"))
    easy_pct = zones.get("easy_pct")
    if easy_pct is not None:
        polarized_status = (f'<span style="color:#3aaa6e;font-weight:700;">✓ {easy_pct}% Easy</span>'
                            if easy_pct >= 78 else
                            f'<span style="color:#e05c4a;font-weight:700;">⚠ Nur {easy_pct}% Easy — mehr Z1/Z2!</span>')
    else:
        polarized_status = '<span style="color:#888;">Keine Zonendaten</span>'

    primary_html  = workout_card(workout.get("primary")) if workout else ""
    optional_html = workout_card(workout.get("optional"), True) if workout and workout.get("optional") else ""
    recovery_note = (f'<div style="background:#fff8e1;border-radius:6px;padding:10px 14px;font-size:12px;color:#8a6000;margin-top:8px;">⚡ {workout["recovery_note"]}</div>'
                     if workout and workout.get("recovery_note") else "")

    week_html = week_plan_table(week_plan) if week_plan else '<div style="color:#aaa;font-size:13px;">Kein Wochenplan verfügbar</div>'

    race_rows = ""
    for r in (race_ctx.get("all_upcoming") or []):
        days = r["days_out"]
        bar_w = max(min(int((1 - days / 120) * 120), 120) if days > 0 else 120, 0)
        race_rows += f"""
        <tr>
          <td style="padding:6px 0;font-size:13px;font-weight:600;color:#1a1a2e;">{r['name']}</td>
          <td style="padding:6px 10px;"><div style="background:#e8e8f0;border-radius:4px;width:120px;height:8px;overflow:hidden;"><div style="background:{phase_color};width:{bar_w}px;height:8px;border-radius:4px;"></div></div></td>
          <td style="padding:6px 0;font-size:12px;color:#888;">{days}d · {r.get('goal','')}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Training {today.isoformat()}</title></head>
<body style="margin:0;padding:0;background:#f0f0f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<div style="max-width:580px;margin:20px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 2px 20px rgba(0,0,0,.08);">
  <div style="background:#1a1a2e;padding:22px 24px 18px;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td style="vertical-align:top;">
        <div style="font-size:11px;color:#8888aa;text-transform:uppercase;letter-spacing:1px;">Training Dashboard</div>
        <div style="font-size:20px;font-weight:700;color:#fff;margin-top:2px;">{date_str}</div>
      </td>
      <td style="vertical-align:top;text-align:right;white-space:nowrap;">
        <div style="display:inline-block;background:{phase_color};color:#fff;font-size:11px;font-weight:700;padding:4px 10px;border-radius:20px;letter-spacing:.5px;">{race_ctx['phase'].upper()}</div>
        <div style="font-size:11px;color:{sq_color};margin-top:6px;font-weight:600;">Schlaf: {sq_label}</div>
      </td>
    </tr></table>
  </div>
  <div style="padding:16px 20px 8px;">
    <div style="font-size:10px;color:#aaa;text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px;">Status</div>
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        {render_metric("Body Battery", body.get('battery'), "/100", warn_below=40, good_above=70)}
        {render_metric("Readiness", body.get('readiness'), "/100", warn_below=40, good_above=70)}
        {render_metric("HRV", body.get('hrv'), "ms", warn_below=40, good_above=70)}
        {render_metric("Ruhepuls", body.get('rhr'), "bpm")}
      </tr>
      <tr>
        {render_metric("Schlaf", sleep.get('duration_h'), "h", warn_below=6.5, good_above=7.5)}
        {render_metric("Schlaf Score", sleep.get('score'), "/100", warn_below=60, good_above=80)}
        {render_metric("CTL", load.get('ctl'), "TSS")}
        {render_metric("TSB", load.get('tsb'), "", warn_below=-25)}
      </tr>
    </table>
  </div>
  <div style="height:1px;background:#f0f0f0;margin:0 20px;"></div>
  <div style="padding:16px 20px 8px;">
    <div style="font-size:10px;color:#aaa;text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px;">Einheit heute</div>
    {recovery_note}{primary_html}{optional_html}
  </div>
  <div style="height:1px;background:#f0f0f0;margin:0 20px;"></div>
  <div style="padding:16px 20px 8px;">
    <div style="font-size:10px;color:#aaa;text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px;">Wochenplan · {week_plan.get('week_theme','') if week_plan else ''}</div>
    {week_html}
  </div>
  <div style="height:1px;background:#f0f0f0;margin:0 20px;"></div>
  <div style="padding:16px 20px 8px;">
    <div style="font-size:10px;color:#aaa;text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px;">Zonenverteilung (7 Tage)</div>
    <table cellpadding="0" cellspacing="0">{zbars}</table>
    <div style="margin-top:8px;font-size:12px;">{polarized_status}</div>
  </div>
  <div style="height:1px;background:#f0f0f0;margin:0 20px;"></div>
  <div style="padding:16px 20px 20px;">
    <div style="font-size:10px;color:#aaa;text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px;">Wettkämpfe</div>
    <table width="100%" cellpadding="0" cellspacing="0">{race_rows}</table>
  </div>
</div></body></html>"""


def build_plaintext(data, workout, race_ctx):
    lines = [f"Training Dashboard — {data['date']} ({race_ctx['phase']})", ""]
    b, s, load = data["body"], data["sleep"], data["load"]
    lines.append(f"Schlaf: {s['duration_h']}h, Score {s['score']}")
    lines.append(f"Body Battery {b['battery']}/100 · Readiness {b['readiness']} · HRV {b['hrv']}ms · RHR {b['rhr']}bpm")
    lines.append(f"Load: ATL {load['atl']} · CTL {load['ctl']} · TSB {load['tsb']}")
    lines.append("")
    if workout and workout.get("primary"):
        p = workout["primary"]
        lines.append(f"HEUTE: {p.get('title','Einheit')} — {p.get('duration_min','?')}min {p.get('intensity','')}")
        lines.append(f"  {p.get('structure','')}")
    lines.append("")
    lines.append("Für die volle Ansicht bitte HTML-Version öffnen.")
    return "\n".join(lines)


# ─── Email ────────────────────────────────────────────────────────────────────

def send_email(html, text, subject):
    smtp_user = os.environ["GMAIL_USER"]
    smtp_pass = os.environ["GMAIL_APP_PASSWORD"]
    to_addr   = os.environ.get("GMAIL_TO", smtp_user)
    smtp_host = os.environ.get("SMTP_HOST", "smtp.mail.me.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))

    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, smtp_user, to_addr
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    for attempt in range(3):
        try:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as s:
                s.login(smtp_user, smtp_pass)
                s.sendmail(smtp_user, [to_addr], msg.as_string())
            log.info(f"Email an {to_addr} gesendet")
            return True
        except Exception as e:
            log.warning(f"Email-Versuch {attempt + 1} fehlgeschlagen: {e}")
            time.sleep(5)
    return False


# ─── Garmin Workout Upload ─────────────────────────────────────────────────────

def upload_workout_to_garmin(workout_data, garmin_client):
    if not workout_data or not workout_data.get("primary"):
        return
    p = workout_data["primary"]
    sport = {"run": "running", "bike": "cycling", "swim": "swimming",
             "strength": "strength_training", "rest": None}.get(p.get("sport"), "other")
    if not sport:
        return
    dur = int((p.get("duration_min") or 0) * 60)
    if dur < 60:
        return
    payload = {
        "workoutName": p.get("title", "Training"), "sport": sport,
        "estimatedDurationInSecs": dur,
        "workoutSegments": [{"segmentOrder": 1, "sportType": {"sportTypeKey": sport},
            "workoutSteps": [{"type": "ExecutableStepDTO", "stepOrder": 1,
                "stepType": {"stepTypeKey": "interval"},
                "durationType": {"durationTypeKey": "time"}, "durationValue": dur,
                "targetType": {"workoutTargetTypeKey": "no.target"}}]}]}
    try:
        garmin_client.garth.connectapi("/workout-service/workout", method="POST", json=payload)
        log.info("Workout zu Garmin Connect hochgeladen")
    except Exception as e:
        log.warning(f"Garmin-Workout-Upload fehlgeschlagen (unkritisch): {e}")


# ─── State: schon heute gesendet? ──────────────────────────────────────────────

STATE_FILE = os.environ.get("STATE_FILE", "/tmp/dashboard_sent_date.txt")


def already_sent_today():
    if os.environ.get("FORCE_SEND", "").lower() in ("1", "true", "yes"):
        return False
    try:
        with open(STATE_FILE) as f:
            return f.read().strip() == today_local().isoformat()
    except FileNotFoundError:
        return False


def mark_sent():
    try:
        with open(STATE_FILE, "w") as f:
            f.write(today_local().isoformat())
    except Exception as e:
        log.warning(f"State konnte nicht geschrieben werden: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if already_sent_today():
        log.info("Dashboard heute bereits gesendet — Ende.")
        return

    log.info("Verbinde mit Garmin...")
    garmin = garmin_connect()

    log.info("Hole Daten...")
    data = fetch_all_data(garmin)

    # WICHTIG: kein hartes Abbrechen mehr, wenn Schlaf fehlt.
    if not data["sleep"]["synced"]:
        log.warning("Schlaf noch nicht gesynct — fahre mit konservativer Planung fort.")

    race_ctx = race_context()

    # Claude mit garantiertem Fallback.
    workout = week_plan = None
    api_key = os.environ.get("CLAUDE_API_KEY")
    if api_key:
        claude = Anthropic(api_key=api_key)
        log.info("Claude: heutiges Workout...")
        workout = build_todays_workout(claude, data, race_ctx)
        log.info("Claude: Wochenplan...")
        week_plan = build_week_plan(claude, data, race_ctx)
    else:
        log.warning("CLAUDE_API_KEY fehlt — nutze regelbasierten Fallback.")

    if not workout or not workout.get("primary"):
        log.warning("Kein valides Claude-Workout — Fallback-Coach übernimmt.")
        workout = fallback_workout(data, race_ctx)
    if not week_plan or not week_plan.get("days"):
        log.warning("Kein valider Claude-Wochenplan — Fallback-Wochenplan.")
        week_plan = fallback_week_plan(race_ctx)

    today_str = today_local().isoformat()
    phase = race_ctx["phase"]
    p = workout.get("primary", {})
    subject = (f"🏋 {p.get('title','Training')} · {p.get('duration_min','?')}min "
               f"{p.get('intensity','')} · {phase}")

    html = build_html(data, workout, week_plan, race_ctx)
    text = build_plaintext(data, workout, race_ctx)

    preview_path = os.environ.get("SAVE_HTML")
    if preview_path:
        try:
            with open(preview_path, "w", encoding="utf-8") as f:
                f.write(html)
            log.info(f"HTML-Preview geschrieben: {preview_path}")
        except Exception as e:
            log.warning(f"Preview konnte nicht geschrieben werden: {e}")

    log.info("Sende Email...")
    if send_email(html, text, subject):
        mark_sent()
        log.info("Lade Workout zu Garmin...")
        upload_workout_to_garmin(workout, garmin)
        log.info("Fertig.")
    else:
        log.error("Email fehlgeschlagen — nicht als gesendet markiert.")
        sys.exit(1)


if __name__ == "__main__":
    main()
