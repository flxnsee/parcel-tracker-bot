import os
import time
import json
import random
import html
from pathlib import Path
import requests
from datetime import datetime, timedelta
from flask import Flask
import threading

TRACKING_NUMBER = os.environ.get("TRACKING_NUMBER")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

TRACK123_API_KEY = os.environ.get("TRACK123_API_KEY")
TRACK123_QUERY_URL = "https://api.track123.com/gateway/open-api/tk/v2.1/track/query"

CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "600"))
STATUS_FILE = Path("last_status.json")

EMOJI_THEMES = [
    {"header": "🔔", "pin": "📍", "route": "✈️", "time": "🕒"},
    {"header": "🆙", "pin": "📌", "route": "🛳️", "time": "⏱️"},
    {"header": "📣", "pin": "🚩", "route": "🚚", "time": "🕰️"},
]

def get_flag_emoji(code: str) -> str:
    if not code or len(code) != 2:
        return "🌍"

    try:
        base = 127397

        return "".join(chr(base + ord(c)) for c in code.upper())
    except:
        return "🌍"

def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}

    try:
        requests.post(url, data=data, timeout=10)
    except:
        pass

def get_track123_raw(tracking_number: str) -> dict:
    headers = {"Track123-Api-Secret": TRACK123_API_KEY, "Content-Type": "application/json", "accept": "application/json"}
    payload = {"trackNos": [tracking_number]}
    resp = requests.post(TRACK123_QUERY_URL, json=payload, headers=headers, timeout=15)
    resp.raise_for_status()

    return resp.json()

def convert_to_kyiv_time(dt_str: str, tz_str: str) -> str:
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        sign = 1 if tz_str.startswith("+") else -1
        hours, minutes = map(int, tz_str[1:].split(":"))
        offset = timedelta(hours=hours, minutes=minutes) * sign
        dt_utc = dt - offset
        dt_kyiv = dt_utc + timedelta(hours=2)

        return dt_kyiv.strftime("%Y-%m-%d %H:%M:%S")
    except:
        return dt_str

def extract_main_fields(api_response: dict) -> dict:
    result = {"status_text": "UNKNOWN_STATUS", "time_str": "невідомо", "origin": "Unknown", "destination": "Unknown", "raw_last_event": None}

    try:
        accepted = api_response.get("data", {}).get("accepted", {})
        items = accepted.get("content", [])

        if not items:
            return result

        tracking = items[0]
        result["origin"] = tracking.get("shipFrom") or "Unknown"
        result["destination"] = "UA"
        logistics = tracking.get("localLogisticsInfo", {})
        details = logistics.get("trackingDetails", [])
        last_tracking_time = tracking.get("lastTrackingTime")

        if details:
            last_event = details[0]
            result["raw_last_event"] = last_event
            tz = last_event.get("timezone", "+08:00")
            if last_tracking_time:
                result["time_str"] = convert_to_kyiv_time(last_tracking_time, tz)
            else:
                result["time_str"] = last_event.get("eventTime", "невідомо")
            raw_detail = last_event.get("eventDetail", "")
            if raw_detail:
                result["status_text"] = raw_detail.split(",")[0]
        else:
            if last_tracking_time:
                result["time_str"] = last_tracking_time
    except:
        pass

    return result

def get_current_status_and_meta() -> tuple[str, dict]:
    raw = get_track123_raw(TRACKING_NUMBER)
    info = extract_main_fields(raw)

    return info["status_text"], info

def format_message(tracking_number: str, meta: dict, *, initial: bool) -> str:
    theme = random.choice(EMOJI_THEMES)
    status_text = html.escape(meta.get("status_text", "UNKNOWN"))
    time_str = html.escape(meta.get("time_str", "—"))
    origin = meta.get("origin", "Unknown")
    dest = meta.get("destination", "Unknown")
    origin_flag = get_flag_emoji(origin)
    dest_flag = get_flag_emoji(dest)
    desc = meta.get("raw_last_event", {}).get("description")
    header = "ПОЧАТОК МОНІТОРИНГУ" if initial else "ОНОВЛЕННЯ СТАТУСУ"
    msg = [
        f"<b>{theme['header']} {header}</b>",
        "",
        f"📦 <b>Посилка:</b> <a href='https://www.track123.com/en/nums={tracking_number}'><code>{tracking_number}</code></a>",
        f"{theme['pin']} <b>Статус:</b> {status_text}",
        "",
        f"<b>{theme['route']} Маршрут:</b> {origin_flag} {origin} ➔ {dest_flag} {dest}",
    ]

    if desc:
        msg.append(f"<blockquote>💬 {html.escape(desc)}</blockquote>")

    msg.append(f"<i>{theme['time']} {time_str}</i>")

    return "\n".join(msg)

def load_last_status() -> str | None:
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text(encoding="utf-8")).get("status")
        except:
            return None

    return None

def save_last_status(status: str) -> None:
    try:
        STATUS_FILE.write_text(json.dumps({"status": status}, ensure_ascii=False, indent=2), encoding="utf-8")
    except:
        pass

def main():
    if not TRACKING_NUMBER or not TRACK123_API_KEY:
        raise RuntimeError("TRACKING_NUMBER або TRACK123_API_KEY не заданий.")

    print(f"🚀 Бот запущено для посилки: {TRACKING_NUMBER}")

    last_status = load_last_status()

    print("Останній збережений статус:", last_status)

    if last_status is None:
        current, meta = get_current_status_and_meta()
        save_last_status(current)
        send_telegram(format_message(TRACKING_NUMBER, meta, initial=True))
        last_status = current

    while True:
        try:
            current, meta = get_current_status_and_meta()

            if current != last_status:
                send_telegram(format_message(TRACKING_NUMBER, meta, initial=False))
                save_last_status(current)
                last_status = current

                print("✅ Статус змінився!")
            else:
                print("ℹ️ Статус не змінився.")
        except Exception as e:
            print("❌ Помилка:", e)

        time.sleep(CHECK_INTERVAL_SECONDS)

app = Flask(__name__)

@app.get("/")
def home():
    return "Bot is running"

def run_flask():
    app.run(host = "0.0.0.0", port = int(os.environ.get("PORT", 5000)))

if __name__ == "__main__":
    threading.Thread(target = run_flask).start()
    main()