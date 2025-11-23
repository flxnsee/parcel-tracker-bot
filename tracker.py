import os
import time
import json
import random
import html
from pathlib import Path
import requests
from datetime import datetime, timedelta
from flask import request, jsonify, Flask
import threading

app = Flask(__name__)

TRACKING_NUMBER = os.environ.get("TRACKING_NUMBER")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

TRACK123_API_KEY = os.environ.get("TRACK123_API_KEY")

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
    result = {
        "status_text": "UNKNOWN_STATUS",
        "time_str": "невідомо",
        "origin": "Unknown",
        "destination": "Unknown",
        "raw_last_event": None,
        "tracking_number": TRACKING_NUMBER or "Unknown",
    }

    try:
        root = api_response.get("data", api_response)
        accepted = root.get("accepted", {})
        items = accepted.get("content", root.get("content", []))

        if not items:
            return result

        tracking = items[0]

        # трек-номер (Track123 зазвичай використовує "trackNo")
        result["tracking_number"] = (
                tracking.get("trackNo")
                or tracking.get("trackingNumber")
                or result["tracking_number"]
        )

        result["origin"] = tracking.get("shipFrom") or "Unknown"

        # Якщо з вебхука прийде shipTo / destination, можна взяти звідти
        result["destination"] = tracking.get("shipTo") or "UA"

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
                # беремо першу фразу до коми як основний статус
                result["status_text"] = raw_detail.split(",")[0]
        else:
            if last_tracking_time:
                result["time_str"] = last_tracking_time
    except:
        pass

    return result

def format_message(tracking_number: str, meta: dict, *, initial: bool) -> str:
    theme = random.choice(EMOJI_THEMES)

    status_text = html.escape(meta.get("status_text", "UNKNOWN"))
    time_str = html.escape(meta.get("time_str", "—"))

    origin = meta.get("origin", "Unknown")
    dest = meta.get("destination", "Unknown")

    origin_flag = get_flag_emoji(origin)
    dest_flag = get_flag_emoji(dest)

    # 🛠 Ось тут була помилка
    last_event = meta.get("raw_last_event") or {}
    # спочатку пробуємо description, якщо немає — eventDetail
    desc = last_event.get("description") or last_event.get("eventDetail")

    header = "ПОЧАТОК МОНІТОРИНГУ" if initial else "ОНОВЛЕННЯ СТАТУСУ"

    msg = [
        f"<b>{theme['header']} {header}</b>",
        "",
        f"📦 <b>Посилка:</b> "
        f"<a href='https://www.track123.com/en/nums={tracking_number}'>"
        f"<code>{tracking_number}</code></a>",
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

@app.get("/")
def home():
    return "Bot is running!"

@app.post("/track123-webhook")
def track123_webhook():
    payload = request.get_json(silent=True)

    try:
        print("=== TRACK123 WEBHOOK RAW PAYLOAD ===")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("====================================")
    except Exception as e:
        print("Не вдалося вивести JSON:", e)

    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid json"}), 400

    meta = extract_main_fields(payload)

    current_status = meta.get("status_text", "Unknown status")
    tracking_number = meta.get("tracking_number") or TRACKING_NUMBER or "Unknown"

    last_status = load_last_status()
    initial = last_status is None

    if initial or current_status != last_status:
        msg = format_message(tracking_number, meta, initial=initial)
        send_telegram(msg)
        save_last_status(last_status)

    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)