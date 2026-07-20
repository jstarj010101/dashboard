#!/usr/bin/env python3
"""
Family Dashboard renderer for a jailbroken Kindle 4.

Pulls:
  - Google Calendar events (today + next few days)
  - Google Sheets "Meal Planner" (today's/tomorrow's lunch and dinners)
  - Weather from Open-Meteo (no API key required)

Renders everything onto a 600x800 grayscale PNG suitable for the
Kindle 4's e-ink screen, then dithers it to look good on a 16-gray-level
panel. Designed to be run on a schedule (e.g. GitHub Actions cron).

Output: dashboard.png in the current working directory.
"""

import os
import sys
import datetime
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# CONFIG - edit these for your setup
# ---------------------------------------------------------------------------

# Kindle 4 native screen resolution (portrait)
SCREEN_W, SCREEN_H = 600, 800

# Local timezone for displaying times (adjust to your location)
TIMEZONE = ZoneInfo("Europe/London")

# Weather location (Milan example - replace with your lat/lon)
WEATHER_LAT = 45.4642
WEATHER_LON = 9.1900
WEATHER_LOCATION_NAME = "Home"

# Google Calendar: which calendar(s) to pull from ("primary" = main calendar)
CALENDAR_IDS = ["primary"]
CALENDAR_DAYS_AHEAD = 3  # today + this many days

# Google Sheets: "Meal Planner" spreadsheet
# Layout: row 1 = day names across columns B-H (Monday..Sunday)
#         subsequent rows = a meal label in column A (e.g. "Lunch",
#         "Dinner EZ", "Dinner JK") with that meal for each day across B-H
SHEET_ID = os.environ.get("DASHBOARD_SHEET_ID", "14J9HIoxqAvfFb0daOgo6IHwOI3iHvIg4_FllhHFbq5U")
SHEET_RANGE = "Sheet1!A1:H10"

# Google service account credentials JSON (as a string, injected via env var
# in CI - see the accompanying GitHub Actions workflow)
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")

# Fonts - DejaVu is preinstalled on ubuntu-latest GitHub Actions runners
FONT_DIR = "/usr/share/fonts/truetype/dejavu"
FONT_REGULAR = os.path.join(FONT_DIR, "DejaVuSans.ttf")
FONT_BOLD = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")

OUTPUT_PATH = "dashboard.png"


# ---------------------------------------------------------------------------
# DATA FETCHING
# ---------------------------------------------------------------------------

def get_weather():
    """Fetch current conditions + a short forecast from Open-Meteo."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
        "&current=temperature_2m,weather_code,precipitation"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=auto&forecast_days=4"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        current = {
            "temp": round(data["current"]["temperature_2m"]),
            "code": data["current"]["weather_code"],
        }
        daily = []
        for i in range(len(data["daily"]["time"])):
            daily.append({
                "date": data["daily"]["time"][i],
                "code": data["daily"]["weather_code"][i],
                "max": round(data["daily"]["temperature_2m_max"][i]),
                "min": round(data["daily"]["temperature_2m_min"][i]),
                "pop": data["daily"]["precipitation_probability_max"][i],
            })
        return {"current": current, "daily": daily}
    except Exception as e:
        print(f"Weather fetch failed: {e}", file=sys.stderr)
        return None


WEATHER_CODE_LABELS = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Fog", 51: "Light drizzle", 53: "Drizzle", 55: "Drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow",
    80: "Rain showers", 81: "Rain showers", 82: "Violent showers",
    95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm",
}


def weather_label(code):
    return WEATHER_CODE_LABELS.get(code, "—")


def get_calendar_events():
    """Fetch upcoming events from Google Calendar using a service account."""
    if not GOOGLE_CREDENTIALS_JSON:
        print("No Google credentials configured, skipping calendar.", file=sys.stderr)
        return []
    try:
        import json
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            creds_info,
            scopes=["https://www.googleapis.com/auth/calendar.readonly"],
        )
        service = build("calendar", "v3", credentials=creds)

        now = datetime.datetime.now(TIMEZONE)
        time_min = now.replace(hour=0, minute=0, second=0).isoformat()
        time_max = (now + datetime.timedelta(days=CALENDAR_DAYS_AHEAD)).isoformat()

        events = []
        for cal_id in CALENDAR_IDS:
            result = service.events().list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            events.extend(result.get("items", []))

        events.sort(key=lambda e: e["start"].get("dateTime", e["start"].get("date", "")))
        return events
    except Exception as e:
        print(f"Calendar fetch failed: {e}", file=sys.stderr)
        return []


def get_meal_plan():
    """
    Fetch the Meal Planner sheet and return a dict keyed by day name, e.g.:
        {"Monday": {"Lunch": "Panini", "Dinner EZ": "Pasta", "Dinner JK": "Tortellini"}, ...}
    Row 1 holds day names across columns B-H; each following row is a meal
    label (column A) with that meal for each day across columns B-H.
    """
    if not GOOGLE_CREDENTIALS_JSON or not SHEET_ID:
        print("No Sheets credentials/ID configured, skipping meal plan.", file=sys.stderr)
        return {}
    try:
        import json
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            creds_info,
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        service = build("sheets", "v4", credentials=creds)
        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=SHEET_RANGE
        ).execute()
        rows = result.get("values", [])
        if not rows:
            return {}

        day_headers = rows[0][1:8]  # columns B-H on the header row
        plan = {day: {} for day in day_headers if day}

        for row in rows[1:]:
            if not row:
                continue
            meal_label = row[0] if len(row) > 0 else ""
            if not meal_label:
                continue
            for i, day in enumerate(day_headers):
                if not day:
                    continue
                col_index = i + 1  # +1 to skip the label column
                value = row[col_index] if len(row) > col_index else ""
                if value:
                    plan[day][meal_label] = value
        return plan
    except Exception as e:
        print(f"Sheets fetch failed: {e}", file=sys.stderr)
        return {}


# ---------------------------------------------------------------------------
# RENDERING
# ---------------------------------------------------------------------------

def load_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def format_event_time(event):
    start = event["start"].get("dateTime")
    if start:
        dt = datetime.datetime.fromisoformat(start).astimezone(TIMEZONE)
        return dt.strftime("%H:%M")
    return "All day"


def wrap_text(draw, text, font, max_width):
    """Simple word-wrap helper, returns a list of lines."""
    words = text.split()
    lines = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_dashboard(weather, events, meal_plan):
    img = Image.new("L", (SCREEN_W, SCREEN_H), color=255)  # 255 = white
    draw = ImageDraw.Draw(img)

    font_title = load_font(FONT_BOLD, 34)
    font_h2 = load_font(FONT_BOLD, 24)
    font_body = load_font(FONT_REGULAR, 20)
    font_body_bold = load_font(FONT_BOLD, 20)
    font_small = load_font(FONT_REGULAR, 16)
    font_weather_temp = load_font(FONT_BOLD, 48)

    margin = 24
    y = margin

    # --- Header: day + date ---
    now = datetime.datetime.now(TIMEZONE)
    draw.text((margin, y), now.strftime("%A"), font=font_title, fill=0)
    date_str = now.strftime("%d %B %Y")
    draw.text((SCREEN_W - margin - draw.textlength(date_str, font=font_body), y + 8),
              date_str, font=font_body, fill=0)
    y += 50
    draw.line([(margin, y), (SCREEN_W - margin, y)], fill=0, width=2)
    y += 16

    # --- Weather block ---
    if weather:
        cur = weather["current"]
        draw.text((margin, y), f"{cur['temp']}°C", font=font_weather_temp, fill=0)
        draw.text((margin + 130, y + 10), weather_label(cur["code"]), font=font_body, fill=0)
        draw.text((margin + 130, y + 34), WEATHER_LOCATION_NAME, font=font_small, fill=80)

        # 3-day mini forecast on the right
        fx_x = SCREEN_W - margin - 300
        for i, day in enumerate(weather["daily"][1:4]):
            dx = fx_x + i * 100
            d = datetime.date.fromisoformat(day["date"])
            draw.text((dx, y), d.strftime("%a"), font=font_small, fill=0)
            draw.text((dx, y + 20), f"{day['max']}°/{day['min']}°", font=font_small, fill=0)
            draw.text((dx, y + 40), f"{day['pop']}% rain", font=font_small, fill=100)
        y += 80
    else:
        draw.text((margin, y), "Weather unavailable", font=font_body, fill=100)
        y += 40

    draw.line([(margin, y), (SCREEN_W - margin, y)], fill=0, width=1)
    y += 16

    # --- Calendar section ---
    draw.text((margin, y), "Today & upcoming", font=font_h2, fill=0)
    y += 36

    if events:
        today = now.date()
        last_day_shown = None
        for event in events[:12]:
            start = event["start"].get("dateTime") or event["start"].get("date")
            event_date = (
                datetime.datetime.fromisoformat(start).astimezone(TIMEZONE).date()
                if event["start"].get("dateTime")
                else datetime.date.fromisoformat(start)
            )
            if event_date != last_day_shown:
                label = "Today" if event_date == today else event_date.strftime("%A %d %b")
                draw.text((margin, y), label, font=font_body_bold, fill=0)
                y += 26
                last_day_shown = event_date

            time_str = format_event_time(event)
            title = event.get("summary", "(no title)")
            draw.text((margin + 20, y), time_str, font=font_small, fill=0)
            lines = wrap_text(draw, title, font_body, SCREEN_W - margin - 100)
            draw.text((margin + 90, y), lines[0] if lines else title, font=font_body, fill=0)
            y += 28
            for extra_line in lines[1:2]:
                draw.text((margin + 90, y), extra_line, font=font_body, fill=0)
                y += 26
            if y > SCREEN_H - 220:
                break
    else:
        draw.text((margin, y), "No upcoming events", font=font_body, fill=100)
        y += 30

    y += 10
    draw.line([(margin, y), (SCREEN_W - margin, y)], fill=0, width=1)
    y += 16

    # --- Meal plan section (from the "Meal Planner" Google Sheet) ---
    draw.text((margin, y), "Meals", font=font_h2, fill=0)
    y += 36

    today_name = now.strftime("%A")
    tomorrow_name = (now + datetime.timedelta(days=1)).strftime("%A")
    # Preferred order for meal rows if present; anything else is appended after
    meal_order = ["Lunch", "Dinner EZ", "Dinner JK"]

    def draw_day_meals(day_name, label):
        nonlocal y
        meals = meal_plan.get(day_name, {})
        if not meals:
            return
        draw.text((margin, y), label, font=font_body_bold, fill=0)
        y += 26
        ordered_keys = [k for k in meal_order if k in meals] + \
                       [k for k in meals if k not in meal_order]
        for meal_label in ordered_keys:
            text = f"{meal_label}: {meals[meal_label]}"
            lines = wrap_text(draw, text, font_body, SCREEN_W - margin - 40)
            for line in lines[:2]:
                draw.text((margin + 20, y), line, font=font_body, fill=0)
                y += 26
        y += 6

    if meal_plan:
        draw_day_meals(today_name, "Today")
        draw_day_meals(tomorrow_name, "Tomorrow")
        if not meal_plan.get(today_name) and not meal_plan.get(tomorrow_name):
            draw.text((margin, y), "No meals listed for today/tomorrow", font=font_body, fill=100)
            y += 30
    else:
        draw.text((margin, y), "Meal plan unavailable", font=font_body, fill=100)
        y += 30

    # --- Footer: last updated ---
    footer_text = f"Updated {now.strftime('%H:%M')}"
    draw.text(
        (margin, SCREEN_H - 30),
        footer_text,
        font=font_small,
        fill=120,
    )

    return img


def to_eink_friendly(img):
    """Convert to 1-bit dithered image for a crisper look on 16-gray e-ink."""
    # Floyd-Steinberg dithering, built into Pillow's convert("1")
    return img.convert("1")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    weather = get_weather()
    events = get_calendar_events()
    meal_plan = get_meal_plan()

    img = render_dashboard(weather, events, meal_plan)

    # Save a grayscale version (nicer if your eips/viewer supports gray)
    img.save(OUTPUT_PATH)

    # Save a dithered 1-bit version as an alternative, in case pure
    # black/white looks better on your particular K4 panel
    dithered_path = OUTPUT_PATH.replace(".png", "_dithered.png")
    to_eink_friendly(img).save(dithered_path)

    print(f"Wrote {OUTPUT_PATH} and {dithered_path}")


if __name__ == "__main__":
    main()
