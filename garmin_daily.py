#!/usr/bin/env python3
"""
Garmin Training Dashboard
Polls for today's sleep sync, then builds + emails a training dashboard.
"""

import os, json, time, logging, smtplib, datetime, statistics
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

RACE_CALENDAR = [
    {"name": "Halbmarathon Geburtstag", "date": "2026-07-12", "type": "run",
     "goal": "Sub 2:00 h"},
    {"name": "Königsbrunn Middle Distance", "date": "2026-09-20", "type": "triathlon",
     "goal": "Sub 6:00 h (1.9k/80k/20k)"},
]

POLARIZED_TARGET = 0.80   # 80% easy
ANTHROPIC_MODEL  = "claude-sonnet-4-20250514"

# ─── Garmin ───────────────────────────────────────────────────────────────────

def garmin_connect():
    import garminconnect
    email    = os.environ["GARMIN_EMAIL"]
    password = os.environ["GARMIN_PASSWORD"]
    token_store = os.environ.get("GARM_TOKENS_DIR", "/tmp/garmin_tokens")
    os.makedirs(token_store, exist_ok=True)

    client = garminconnect.Garmin(email, password, is_cn=False,
                                   prompt_mfa=lambda: os.environ.get("GARMIN_MFA",""))
    for attempt in range(3):
        try:
            client.login(token_store)
            log.info("Garmin login OK")
            return client
        except Exception as e:
            log.warning(f"Login attempt {attempt+1} failed: {e}")
            if attempt < 2:
                time.sleep(10 * (attempt + 1))
    raise RuntimeError("Garmin login failed after 3 attempts")


def safe_get(fn, default=None):
    for attempt in range(3):
        try:
            return fn()
        except Exception as e:
            log.warning(f"API call failed (attempt {attempt+1}): {e}")
            time.sleep(5)
    log.error("Returning default after 3 failures")
    return default


def fetch_all_data(client):
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    two_weeks_ago = today - datetime.timedelta(days=14)

    # ── Sleep (key: must exist to trigger dashboard) ──
    sleep_raw = safe_get(
        lambda: client.get_sleep_data(yesterday.isoformat()), {}
    )
    daily_sleep = (sleep_raw.get("dailySleepDTO") or
                   sleep_raw.get("sleepDTO") or
                   sleep_raw.get("daily_sleep") or {})

    sleep_score     = daily_sleep.get("sleepScores", {}).get("overall", {}).get("value") or \
                      daily_sleep.get("sleepScore") or \
                      daily_sleep.get("overallScore")
    sleep_duration  = daily_sleep.get("sleepTimeSeconds", 0) / 3600
    rem_seconds     = daily_sleep.get("remSleepSeconds", 0)
    deep_seconds    = daily_sleep.get("deepSleepSeconds", 0)
    light_seconds   = daily_sleep.get("lightSleepSeconds", 0)

    # ── Body Battery + HRV ──
    body_battery = safe_get(
        lambda: client.get_body_battery(today.isoformat()), []
    )
    bb_now = None
    if body_battery:
        charged = [e.get("charged") for e in body_battery if e.get("charged") is not None]
        if charged:
            bb_now = max(charged)

    hrv_raw = safe_get(
        lambda: client.get_hrv_data(today.isoformat()), {}
    )
    hrv_summary = hrv_raw.get("hrvSummary", {}) if isinstance(hrv_raw, dict) else {}
    hrv_value   = hrv_summary.get("lastNight") or hrv_summary.get("weeklyAvg")

    # ── Stats today ──
    stats_raw = safe_get(
        lambda: client.get_stats(today.isoformat()), {}
    )
    rhr         = stats_raw.get("restingHeartRate")
    stress      = stats_raw.get("averageStressLevel")
    steps       = stats_raw.get("totalSteps")
    calories    = stats_raw.get("totalKilocalories")
    vo2max      = stats_raw.get("maxMetValue") or stats_raw.get("vo2MaxValue")

    # ── Activities last 14 days ──
    activities_raw = safe_get(
        lambda: client.get_activities_by_date(
            two_weeks_ago.isoformat(), today.isoformat()
        ), []
    ) or []

    activities = []
    for a in activities_raw[:20]:
        act_type = (a.get("activityType", {}).get("typeKey") or
                    a.get("activityType") or "unknown").lower()
        duration_min = (a.get("duration") or a.get("movingDuration") or 0) / 60
        distance_km  = (a.get("distance") or 0) / 1000
        tss          = a.get("trainingStressScore") or a.get("tss")
        avg_hr       = a.get("averageHR") or a.get("averageHeartRate")
        max_hr       = a.get("maxHR") or a.get("maxHeartRate")

        hr_zones = {}
        zones_raw = (a.get("heartRateZones") or
                     a.get("hrZones") or
                     a.get("zones") or [])
        for z in zones_raw:
            zone_num = z.get("zoneNumber") or z.get("zone")
            secs     = z.get("secsInZone") or z.get("seconds") or z.get("duration") or 0
            if zone_num:
                hr_zones[f"z{zone_num}"] = round(secs / 60, 1)

        act_date = (a.get("startTimeLocal") or a.get("beginTimestamp") or
                    a.get("startTimeGMT") or "")[:10]

        activities.append({
            "date": act_date,
            "type": act_type,
            "duration_min": round(duration_min, 1),
            "distance_km": round(distance_km, 2),
            "tss": tss,
            "avg_hr": avg_hr,
            "max_hr": max_hr,
            "hr_zones": hr_zones,
            "name": a.get("activityName", ""),
        })

    # ── Zone analysis (last 7 days) ──
    week_ago = today - datetime.timedelta(days=7)
    recent_acts = [a for a in activities if a["date"] >= week_ago.isoformat()]

    zone_totals = {"z1": 0, "z2": 0, "z3": 0, "z4": 0, "z5": 0}
    total_training_min = 0
    for a in recent_acts:
        for z, mins in a["hr_zones"].items():
            if z in zone_totals:
                zone_totals[z] += mins
        if not a["hr_zones"]:
            total_training_min += a["duration_min"]

    all_zone_mins = sum(zone_totals.values())
    if all_zone_mins > 0:
        zone_pct = {z: round(m / all_zone_mins * 100, 1)
                    for z, m in zone_totals.items()}
        easy_pct = zone_pct.get("z1", 0) + zone_pct.get("z2", 0)
    else:
        zone_pct = {}
        easy_pct = None

    # ── Training load (simple ATL/CTL from TSS) ──
    tss_values = [a["tss"] for a in activities if a["tss"] is not None]
    atl = round(statistics.mean(tss_values[-7:]),  1) if len(tss_values) >= 3 else None
    ctl = round(statistics.mean(tss_values[-42:]), 1) if len(tss_values) >= 7 else None
    tsb = round(ctl - atl, 1) if (atl and ctl) else None

    return {
        "date": today.isoformat(),
        "sleep": {
            "score": sleep_score,
            "duration_h": round(sleep_duration, 2),
            "rem_min": round(rem_seconds / 60),
            "deep_min": round(deep_seconds / 60),
            "light_min": round(light_seconds / 60),
            "synced": sleep_score is not None or sleep_duration > 0,
        },
        "body": {
            "battery": bb_now,
            "hrv": hrv_value,
            "rhr": rhr,
            "stress": stress,
            "steps": steps,
            "calories": calories,
            "vo2max": vo2max,
        },
        "load": {"atl": atl, "ctl": ctl, "tsb": tsb},
        "zones_7d": {"totals_min": zone_totals, "pct": zone_pct, "easy_pct": easy_pct},
        "activities_14d": activities,
        "recent_count": len(recent_acts),
    }


# ─── Race Phase ───────────────────────────────────────────────────────────────

def race_context():
    today = datetime.date.today()
    upcoming = []
    for r in RACE_CALENDAR:
        race_date = datetime.date.fromisoformat(r["date"])
        days_out  = (race_date - today).days
        if days_out >= -3:
            upcoming.append({**r, "days_out": days_out})

    if not upcoming:
        return {"phase": "Off-season", "next_race": None, "days_out": None}

    next_race = min(upcoming, key=lambda x: x["days_out"])
    days_out  = next_race["days_out"]

    if days_out <= 7:
        phase = "Race Week"
    elif days_out <= 21:
        phase = "Taper"
    elif days_out <= 56:
        phase = "Build"
    else:
        phase = "Base"

    return {"phase": phase, "next_race": next_race, "days_out": days_out,
            "all_upcoming": upcoming}


# ─── Claude Calls ─────────────────────────────────────────────────────────────

def call_claude(client, system_prompt, user_prompt, max_tokens=1200):
    for attempt in range(2):
        try:
            resp = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return resp.content[0].text
        except Exception as e:
            log.warning(f"Claude call attempt {attempt+1} failed: {e}")
            time.sleep(5)
    return None


def build_todays_workout(claude_client, data, race_ctx):
    system = """Du bist ein erfahrener Triathlon-Coach.
Antworte NUR mit einem JSON-Objekt — kein Prosa, keine Markdown-Backticks.
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
  "optional": {
    "sport": "...",
    "title": "...",
    "duration_min": <int>,
    "intensity": "...",
    "zones": "...",
    "structure": "...",
    "rationale": "..."
  } | null,
  "day_verdict": "<easy|moderate|hard — basierend auf Erholung>",
  "recovery_note": "<1 Satz falls Schlaf/BB schlecht>"
}"""

    user = f"""Athletendaten heute ({data['date']}):

Schlaf: Score={data['sleep']['score']}, Dauer={data['sleep']['duration_h']}h, Tief={data['sleep']['deep_min']}min, REM={data['sleep']['rem_min']}min
Body Battery: {data['body']['battery']}/100
HRV: {data['body']['hrv']} ms
Ruhepuls: {data['body']['rhr']} bpm
Stress gestern: {data['body']['stress']}

Trainingsload: ATL={data['load']['atl']}, CTL={data['load']['ctl']}, TSB={data['load']['tsb']}
Zonenverteilung letzte 7 Tage: {json.dumps(data['zones_7d']['pct'])}
Easyanteil: {data['zones_7d']['easy_pct']}% (Ziel: ≥80%)

Letzte 5 Aktivitäten:
{json.dumps(data['activities_14d'][:5], indent=2)}

Race-Phase: {race_ctx['phase']}
Nächstes Rennen: {race_ctx['next_race']['name'] if race_ctx['next_race'] else 'keins'} in {race_ctx['days_out']} Tagen
Ziel: {race_ctx['next_race']['goal'] if race_ctx['next_race'] else '-'}

Wochentag: {datetime.date.today().strftime('%A')}

Regeln:
- Polarisiertes Training: 80% Z1-Z2, max 20% Z3-Z5
- Wenn TSB < -20 oder BB < 40 oder Schlaf < 6h → nur Easy oder Rest
- Bei Race Week nur Aktivierungseinheiten, kein Stress
- Wenn Easy-Anteil < 70%: primary muss Z1-Z2 sein egal was
- Chainrings 52-36T, Shimano Dura-Ace Di2"""

    raw = call_claude(claude_client, system, user, max_tokens=1000)
    if not raw:
        return None
    try:
        clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        return json.loads(clean)
    except Exception as e:
        log.error(f"Workout JSON parse error: {e}\nRaw: {raw[:300]}")
        return None


def build_week_plan(claude_client, data, race_ctx):
    system = """Du bist ein erfahrener Triathlon-Coach.
Antworte NUR mit einem JSON-Objekt — kein Prosa, keine Markdown-Backticks.
Schema:
{
  "week_theme": "<1 Satz Wochenmotto>",
  "days": {
    "Mo": {"sport": "...", "focus": "...", "duration_min": <int>, "intensity": "easy|moderate|hard"},
    "Di": {...},
    "Mi": {...},
    "Do": {...},
    "Fr": {...},
    "Sa": {...},
    "So": {...}
  },
  "weekly_load_note": "<1 Satz zu Gesamtvolumen und Polarisierung>",
  "key_session": "<welcher Tag ist die wichtigste Einheit>"
}"""

    today_weekday = datetime.date.today().strftime("%A")
    user = f"""Erstelle einen Wochenplan (Heute ist {today_weekday}, {data['date']}).

Phase: {race_ctx['phase']}
Nächstes Rennen: {race_ctx['next_race']['name'] if race_ctx['next_race'] else 'keins'} in {race_ctx['days_out']} Tagen (Ziel: {race_ctx['next_race']['goal'] if race_ctx['next_race'] else '-'})

Trainingsload: ATL={data['load']['atl']}, CTL={data['load']['ctl']}, TSB={data['load']['tsb']}
Zonenverteilung letzte 7 Tage: {json.dumps(data['zones_7d']['pct'])}
Aktivitäten letzte 14 Tage (Überblick): {json.dumps([{'date':a['date'],'type':a['type'],'duration_min':a['duration_min'],'intensity_approx':'hard' if a['avg_hr'] and a['avg_hr']>160 else 'easy'} for a in data['activities_14d']], indent=2)}

Regeln:
- Triathlet: Schwimmen + Radfahren + Laufen
- 80/20 Polarisierung einhalten
- Max 2 harte Einheiten pro Woche
- 1 Ruhetag (meist Freitag oder Montag)
- Lange Einheit am Wochenende (Bike oder Lauf)
- Bei TSB < -15: Volumen reduzieren
- Taper-Phase: Frequenz halten, Volumen -30-40%"""

    raw = call_claude(claude_client, system, user, max_tokens=1200)
    if not raw:
        return None
    try:
        clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        return json.loads(clean)
    except Exception as e:
        log.error(f"Week plan JSON parse error: {e}\nRaw: {raw[:300]}")
        return None


# ─── HTML Dashboard ───────────────────────────────────────────────────────────

def render_metric(label, value, unit="", warn_below=None, good_above=None):
    if value is None:
        display = "–"
        color = "#888"
    else:
        display = str(value)
        if warn_below and isinstance(value, (int, float)) and value < warn_below:
            color = "#e05c4a"
        elif good_above and isinstance(value, (int, float)) and value >= good_above:
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
      <td style="padding:3px 6px;">
        <div style="background:{color};width:{bar_w}px;height:12px;border-radius:3px;display:inline-block;"></div>
      </td>
      <td style="font-size:12px;color:#333;padding:3px 0;">{pct}%</td>
    </tr>"""


def workout_card(w, is_optional=False):
    if not w:
        return ""
    intensity_colors = {"easy": "#3aaa6e", "moderate": "#f5a623", "hard": "#e05c4a"}
    sport_icons = {"swim": "🏊", "bike": "🚴", "run": "🏃", "strength": "💪", "rest": "😴"}
    tag = "Optional" if is_optional else "Heute"
    color = intensity_colors.get(w.get("intensity", ""), "#888")
    icon  = sport_icons.get(w.get("sport", ""), "🎯")
    return f"""
    <div style="background:#f9f9fb;border-left:4px solid {color};border-radius:8px;padding:16px 18px;margin:12px 0;">
      <div style="font-size:10px;color:#aaa;text-transform:uppercase;letter-spacing:.8px;">{tag}</div>
      <div style="font-size:17px;font-weight:700;color:#1a1a2e;margin:4px 0 8px;">
        {icon} {w.get('title','Einheit')}
      </div>
      <table cellpadding="0" cellspacing="0" style="margin-bottom:10px;">
        <tr>
          <td style="padding-right:16px;font-size:12px;color:#555;">⏱ {w.get('duration_min','?')} min</td>
          <td style="padding-right:16px;font-size:12px;color:#555;">📊 {w.get('zones','–')}</td>
          <td style="font-size:12px;color:{color};font-weight:600;">{w.get('intensity','').capitalize()}</td>
        </tr>
      </table>
      <div style="font-size:13px;color:#333;line-height:1.6;margin-bottom:8px;">{w.get('structure','')}</div>
      <div style="font-size:11px;color:#888;font-style:italic;">{w.get('rationale','')}</div>
    </div>"""


def week_plan_table(plan):
    if not plan or "days" not in plan:
        return ""
    intensity_bg = {"easy": "#e8f7ef", "moderate": "#fff4e0", "hard": "#fdecea"}
    intensity_col = {"easy": "#2e8b57", "moderate": "#cc8800", "hard": "#c0392b"}
    sport_icons = {"swim": "🏊", "bike": "🚴", "run": "🏃", "strength": "💪", "rest": "😴",
                   "swim/run": "🏊🏃", "bike/run": "🚴🏃"}

    rows = ""
    today_short = datetime.date.today().strftime("%a")[:2]
    day_map = {"Mo": "Mon", "Di": "Tue", "Mi": "Wed", "Do": "Thu",
               "Fr": "Fri", "Sa": "Sat", "So": "Sun"}

    for day_de, info in plan["days"].items():
        is_today = day_map.get(day_de, "")[:2].lower() == today_short.lower()
        bg = "#eef3ff" if is_today else "transparent"
        fw = "700" if is_today else "400"
        intensity = info.get("intensity", "easy")
        pill_bg  = intensity_bg.get(intensity, "#eee")
        pill_col = intensity_col.get(intensity, "#333")
        sport = info.get("sport", "")
        icon  = sport_icons.get(sport.lower(), "🎯")
        rows += f"""
        <tr style="background:{bg};">
          <td style="padding:8px 10px;font-size:13px;font-weight:{fw};color:#1a1a2e;width:32px;">{day_de}</td>
          <td style="padding:8px 4px;font-size:16px;">{icon}</td>
          <td style="padding:8px 10px;font-size:13px;color:#333;">{info.get('focus','')}</td>
          <td style="padding:8px 6px;font-size:11px;text-align:center;">
            <span style="background:{pill_bg};color:{pill_col};border-radius:10px;padding:2px 8px;font-weight:600;">{info.get('duration_min','?')}min</span>
          </td>
          <td style="padding:8px 6px;font-size:11px;color:{pill_col};">{intensity.capitalize()}</td>
        </tr>"""

    return f"""
    <div style="overflow:hidden;border-radius:8px;border:1px solid #ebebeb;">
      <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
        {rows}
      </table>
    </div>
    <div style="margin-top:8px;font-size:11px;color:#888;font-style:italic;">{plan.get('weekly_load_note','')}</div>
    """


def build_html(data, workout, week_plan, race_ctx):
    today = datetime.date.today()
    weekday_de = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag","Sonntag"][today.weekday()]
    date_str = today.strftime(f"{weekday_de}, %d. %B %Y")

    sleep  = data["sleep"]
    body   = data["body"]
    load   = data["load"]
    zones  = data["zones_7d"]

    # Sleep quality label
    sc = sleep.get("score")
    if   sc and sc >= 80: sq_label, sq_color = "Erholt", "#3aaa6e"
    elif sc and sc >= 60: sq_label, sq_color = "Ok",     "#f5a623"
    elif sc:              sq_label, sq_color = "Schlecht","#e05c4a"
    else:                 sq_label, sq_color = "–",       "#888"

    # Phase badge
    phase_colors = {
        "Base": "#3aaa6e", "Build": "#f5a623",
        "Peak": "#e05c4a", "Taper": "#9b59b6",
        "Race Week": "#c0392b", "Off-season": "#888"
    }
    phase_color = phase_colors.get(race_ctx["phase"], "#888")

    # Zone bars
    zp   = zones.get("pct", {})
    zbars = (
        zone_bar("Z1", zp.get("z1"), "#3aaa6e") +
        zone_bar("Z2", zp.get("z2"), "#5bc8af") +
        zone_bar("Z3", zp.get("z3"), "#f5a623") +
        zone_bar("Z4", zp.get("z4"), "#e07b3a") +
        zone_bar("Z5", zp.get("z5"), "#e05c4a")
    )
    easy_pct = zones.get("easy_pct")
    if easy_pct is not None:
        polarized_status = (
            f'<span style="color:#3aaa6e;font-weight:700;">✓ {easy_pct}% Easy</span>'
            if easy_pct >= 78 else
            f'<span style="color:#e05c4a;font-weight:700;">⚠ Nur {easy_pct}% Easy — mehr Z1/Z2!</span>'
        )
    else:
        polarized_status = '<span style="color:#888;">Keine Zonendaten</span>'

    # Workout HTML
    primary_html  = workout_card(workout.get("primary"))  if workout else ""
    optional_html = workout_card(workout.get("optional"), True) if workout and workout.get("optional") else ""
    recovery_note = (f'<div style="background:#fff8e1;border-radius:6px;padding:10px 14px;'
                     f'font-size:12px;color:#8a6000;margin-top:8px;">'
                     f'⚡ {workout["recovery_note"]}</div>'
                     if workout and workout.get("recovery_note") else "")

    # Week plan HTML
    week_html = week_plan_table(week_plan) if week_plan else (
        '<div style="color:#aaa;font-size:13px;">Kein Wochenplan verfügbar</div>'
    )

    # Race countdown
    race_rows = ""
    for r in (race_ctx.get("all_upcoming") or []):
        days = r["days_out"]
        bar_w = min(int((1 - days/120) * 120), 120) if days > 0 else 120
        race_rows += f"""
        <tr>
          <td style="padding:6px 0;font-size:13px;font-weight:600;color:#1a1a2e;">{r['name']}</td>
          <td style="padding:6px 10px;">
            <div style="background:#e8e8f0;border-radius:4px;width:120px;height:8px;overflow:hidden;">
              <div style="background:{phase_color};width:{bar_w}px;height:8px;border-radius:4px;"></div>
            </div>
          </td>
          <td style="padding:6px 0;font-size:12px;color:#888;">{days}d · {r.get('goal','')}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Training {today.isoformat()}</title>
</head>
<body style="margin:0;padding:0;background:#f0f0f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<div style="max-width:580px;margin:20px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 2px 20px rgba(0,0,0,.08);">

  <!-- Header -->
  <div style="background:#1a1a2e;padding:22px 24px 18px;">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;">
      <div>
        <div style="font-size:11px;color:#8888aa;text-transform:uppercase;letter-spacing:1px;">Training Dashboard</div>
        <div style="font-size:20px;font-weight:700;color:#fff;margin-top:2px;">{date_str}</div>
      </div>
      <div style="text-align:right;">
        <div style="display:inline-block;background:{phase_color};color:#fff;font-size:11px;font-weight:700;
                    padding:4px 10px;border-radius:20px;letter-spacing:.5px;">{race_ctx['phase'].upper()}</div>
        <div style="font-size:11px;color:{sq_color};margin-top:6px;font-weight:600;">Schlaf: {sq_label}</div>
      </div>
    </div>
  </div>

  <!-- Body Metrics -->
  <div style="padding:16px 20px 8px;">
    <div style="font-size:10px;color:#aaa;text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px;">Status</div>
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        {render_metric("Body Battery", body.get('battery'), "/100", warn_below=40, good_above=70)}
        {render_metric("HRV", body.get('hrv'), "ms", warn_below=40, good_above=70)}
        {render_metric("Ruhepuls", body.get('rhr'), "bpm", warn_below=None)}
        {render_metric("Schlaf", sleep.get('duration_h'), "h", warn_below=6.5, good_above=7.5)}
      </tr>
      <tr>
        {render_metric("Schlaf Score", sleep.get('score'), "/100", warn_below=60, good_above=80)}
        {render_metric("ATL", load.get('atl'), "TSS")}
        {render_metric("CTL", load.get('ctl'), "TSS")}
        {render_metric("TSB", load.get('tsb'), "", warn_below=-25)}
      </tr>
    </table>
  </div>

  <div style="height:1px;background:#f0f0f0;margin:0 20px;"></div>

  <!-- Today's Workout -->
  <div style="padding:16px 20px 8px;">
    <div style="font-size:10px;color:#aaa;text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px;">Einheit heute</div>
    {recovery_note}
    {primary_html}
    {optional_html}
  </div>

  <div style="height:1px;background:#f0f0f0;margin:0 20px;"></div>

  <!-- Week Plan -->
  <div style="padding:16px 20px 8px;">
    <div style="font-size:10px;color:#aaa;text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px;">
      Wochenplan · {week_plan.get('week_theme','') if week_plan else ''}
    </div>
    {week_html}
  </div>

  <div style="height:1px;background:#f0f0f0;margin:0 20px;"></div>

  <!-- Zone Balance -->
  <div style="padding:16px 20px 8px;">
    <div style="font-size:10px;color:#aaa;text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px;">Zonenverteilung (7 Tage)</div>
    <table cellpadding="0" cellspacing="0">{zbars}</table>
    <div style="margin-top:8px;font-size:12px;">{polarized_status}</div>
  </div>

  <div style="height:1px;background:#f0f0f0;margin:0 20px;"></div>

  <!-- Race Countdown -->
  <div style="padding:16px 20px 20px;">
    <div style="font-size:10px;color:#aaa;text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px;">Wettkämpfe</div>
    <table width="100%" cellpadding="0" cellspacing="0">{race_rows}</table>
  </div>

</div>
</body>
</html>"""


# ─── Email ────────────────────────────────────────────────────────────────────

def send_email(html, subject):
    smtp_user   = os.environ["ICLOUD_EMAIL"]
    smtp_pass   = os.environ["ICLOUD_APP_PASSWORD"]
    to_addr     = os.environ.get("DASHBOARD_TO", smtp_user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_user
    msg["To"]      = to_addr
    msg.attach(MIMEText(html, "html", "utf-8"))

    for attempt in range(3):
        try:
            with smtplib.SMTP_SSL("smtp.mail.me.com", 587) as s:
                s.login(smtp_user, smtp_pass)
                s.sendmail(smtp_user, to_addr, msg.as_string())
            log.info(f"Email sent to {to_addr}")
            return True
        except Exception as e:
            log.warning(f"Email attempt {attempt+1} failed: {e}")
            time.sleep(5)
    return False


# ─── Garmin Workout Upload ─────────────────────────────────────────────────────

def upload_workout_to_garmin(workout_data, garmin_client):
    if not workout_data or not workout_data.get("primary"):
        return
    primary = workout_data["primary"]
    sport_map = {"run": "running", "bike": "cycling", "swim": "swimming",
                 "strength": "strength_training", "rest": None}
    sport = sport_map.get(primary.get("sport"), "other")
    if not sport:
        return

    duration_sec = (primary.get("duration_min") or 0) * 60
    if duration_sec < 60:
        return

    try:
        import garth
        payload = {
            "workoutName": primary.get("title", "Training"),
            "sport": sport,
            "estimatedDurationInSecs": duration_sec,
            "workoutSegments": [{
                "segmentOrder": 1,
                "sportType": {"sportTypeKey": sport},
                "workoutSteps": [{
                    "type": "ExecutableStepDTO",
                    "stepOrder": 1,
                    "stepType": {"stepTypeKey": "interval"},
                    "durationType": {"durationTypeKey": "time"},
                    "durationValue": duration_sec,
                    "targetType": {"workoutTargetTypeKey": "no.target"},
                }]
            }]
        }
        garth.connectapi("/workout-service/workout", method="POST", json=payload)
        log.info("Workout uploaded to Garmin Connect")
    except Exception as e:
        log.warning(f"Garmin workout upload failed (non-critical): {e}")


# ─── State: Already Sent Today ─────────────────────────────────────────────────

STATE_FILE = "/tmp/dashboard_sent_date.txt"

def already_sent_today():
    try:
        with open(STATE_FILE) as f:
            return f.read().strip() == datetime.date.today().isoformat()
    except FileNotFoundError:
        return False

def mark_sent():
    with open(STATE_FILE, "w") as f:
        f.write(datetime.date.today().isoformat())


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if already_sent_today():
        log.info("Dashboard already sent today — exiting.")
        return

    log.info("Connecting to Garmin...")
    garmin = garmin_connect()

    log.info("Fetching data...")
    data = fetch_all_data(garmin)

    if not data["sleep"]["synced"]:
        log.info("Sleep not yet synced — will retry later.")
        return

    log.info("Sleep synced. Building dashboard...")
    race_ctx = race_context()

    claude = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    log.info("Calling Claude: today's workout...")
    workout = build_todays_workout(claude, data, race_ctx)

    log.info("Calling Claude: week plan...")
    week_plan = build_week_plan(claude, data, race_ctx)

    log.info("Rendering HTML...")
    today_str = datetime.date.today().isoformat()
    phase = race_ctx["phase"]
    subject = f"🏋 Training {today_str} · {phase}"
    if workout and workout.get("primary"):
        p = workout["primary"]
        subject = f"🏋 {p.get('title','Training')} · {p.get('duration_min','?')}min {p.get('intensity','')} · {phase}"

    html = build_html(data, workout, week_plan, race_ctx)

    log.info("Sending email...")
    sent = send_email(html, subject)

    if sent:
        mark_sent()
        log.info("Uploading workout to Garmin...")
        upload_workout_to_garmin(workout, garmin)
        log.info("Done.")
    else:
        log.error("Email failed — not marking as sent.")
        exit(1)


if __name__ == "__main__":
    main()
