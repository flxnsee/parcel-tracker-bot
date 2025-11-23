import os
import json
import random
import html
from pathlib import Path
import requests
from datetime import datetime, timedelta
from flask import request, jsonify, Flask
import threading
from pymongo import MongoClient

app = Flask(__name__)

client = MongoClient(os.environ.get("MONGO_URL"))
db = client["trackbot"]

users = db.users
trackings = db.trackings
subscriptions = db.subscriptions

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TRACK123_API_KEY = os.environ.get("TRACK123_API_KEY")
REFRESH_INTERVAL = 6 * 60 * 60

EMOJI_THEMES = [
    {"header": "🔔", "pin": "📍", "route": "✈️", "time": "🕒"},
    {"header": "🆙", "pin": "📌", "route": "🛳️", "time": "⏱️"},
    {"header": "📣", "pin": "🚩", "route": "🚚", "time": "🕰️"},
]

def get_flag_emoji(code: str) -> str:
    if not code or len(code) != 2:
        return "🌍"

    try:
        return "".join(chr(127397 + ord(c)) for c in code.upper())
    except:
        return "🌍"

def send_telegram(chat_id: int, message: str):
    if not TELEGRAM_TOKEN:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url ,data = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })

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
    root = api_response.get("data", api_response)

    result = {
        "status_text": "UNKNOWN",
        "time_str": "UNKNOWN",
        "origin": "UNKNOWN",
        "destination": "UNKNOWN",
        "tracking_number": root.get("trackNo") or root.get("trackingNumber"),
        "raw_last_event": None,
    }

    result["origin"] = root.get("shipFrom", "UNKNOWN")
    result["destination"] = root.get("shipTo", "UNKNOWN")

    logistics = root.get("localLogisticsInfo", {})
    details = logistics.get("trackingDetails") or root.get("trackingDetails") or []

    last_tracking_time = (
        root.get("lastTrackingTime") or root.get("shipTime")
    )

    if details:
        last_event = details[0]
        result["raw_last_event"] = last_event

        tz = last_event.get("timezone", "+08:00")
        time = last_event.get("eventTime") or last_tracking_time
        result["time_str"] = convert_to_kyiv_time(time, tz)

        detail = (
            last_event.get("eventDetail")
            or logistics.get("transitSubStatus")
            or root.get("trackingStatus")
        )

        if detail:
            result["status_text"] = detail.split(",")[0]

        return result

def format_message(tracking_number: str, meta: dict, *, initial: bool) -> str:
    theme = random.choice(EMOJI_THEMES)

    status = html.escape(meta["status_text"])
    time_str = html.escape(meta["time_str"])
    origin = meta["origin"]
    dest = meta["destination"]

    event = meta["raw_last_event"] or {}
    desc = event.get("description") or event.get("eventDetail")

    header = "ПОЧАТОК МОНІТОРИНГУ" if initial else "ОНОВЛЕННЯ СТАТУСУ"

    msg = [
        f"<b>{theme['header']} {header}</b>",
        f"📦 <b>Посилка:</b> <code>{track_no}</code>",
        f"{theme['pin']} <b>Статус:</b> {status}",
        "",
        f"{theme['route']} <b>Маршрут:</b> "
        f"{get_flag_emoji(origin)} {origin} ➜ {get_flag_emoji(dest)} {dest}",
    ]

    if desc:
        msg.append(f"<blockquote>{html.escape(desc)}</blockquote>")

    msg.append(f"<i>{theme['time']} {time_str}</i>")

    return "\n".join(msg)

def refresh_all_trackings():
    all_tracks = trackings.find({})

    for t in all_tracks:
        try:
            url = "https://api.track123.com/gateway/open-api/tk/v2.1/track/refresh"
            headers = {
                "Track123-Api-Secret": TRACK123_API_KEY,
                "Content-Type": "application/json",
            }
            payload = {"trackNos": [t["track_no"]]}
            requests.post(url, json = payload, headers = headers, timeout = 10)
        except Exception as e:
            print("Refresh error: ", e)

        threading.Timer(REFRESH_INTERVAL, refresh_all_trackings).start()

@app.post("/telegram-webhook")
def telegram_webhook():
    update = request.get_json(silent = True) or {}
    message = update.get("message", {})
    text = message.get("text", "")
    chat_id = message.get("chat", {}).get("id")

    if not chat_id or not text:
        return jsonify({"ok": True})

    if text.startswith("/start"):
        send_telegram(chat_id, "Привіт! Напиши:\n/track <номер>")

        return jsonify({"ok": True})

    if text.startswith("/track"):
        parts = text.split(maxsplit = 1)

        if len(parts) < 2:
            send_telegram(chat_id, "❗ Формат: /track AEBT123456789")

            return jsonify({"ok": True})

        track_no = parts[1].strip()

        users.update_one(
            {"chat_id": chat_id},
            {"$set": {"track_no": track_no}},
            upsert = True
        )

        subscriptions.update_one(
            {"chat_id": chat_id, "track_no": track_no},
            {"$set": {"chat_id": chat_id, "track_no": track_no}},
            upsert = True
        )

        send_telegram(chat_id, f"🟢 Я слідкую за треком <code>{track_no}</code>!")

        return jsonify({"ok": True})

@app.post("/track123-webhook")
def track123_webhook():
    payload = request.get_json(silent = True) or {}

    meta = extract_main_fields(payload)
    track_no = meta.get("tracking_number")

    if not track_no:
        return jsonify({"error": "no track"}), 400

    new_status = meta["status_text"]

    old_track = trackings.find_one({"track_no": track_no})
    old_status = old_track.get("last_status") if old_track else None

    if old_status == new_status:
        return jsonify({"ok": True})

    trackings.update_one(
        {"track_no": track_no},
        {"$set": {
            "last_status": new_status,
            "last_update": datetime.utcnow()
        }},
        upsert = True
    )

    subs = subscriptions.find({"track_no": track_no})
    msg = format_message(track_no, meta, initial = False)

    for s in subs:
        send_telegram(s["chat_id"], msg)

    return jsonify({"ok": True})

@app.get("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    refresh_all_trackings()
    app.run(host="0.0.0.0", port = int(os.environ.get("PORT", 8080)))