import os
import random
import html
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

TRACK123_REFRESH_URL = "https://api.track123.com/gateway/open-api/tk/v2.1/track/refresh"
TRACK123_QUERY_URL = "https://api.track123.com/gateway/open-api/tk/v2.1/track/query"
TRACK123_IMPORT_URL = "https://api.track123.com/gateway/open-api/tk/v2.1/track/import"
TRACK123_DELETE_URL = "https://api.track123.com/gateway/open-api/tk/v2.1/track/delete"

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
    except Exception:
        return "🌍"


def send_telegram(chat_id: int, message: str):
    if not TELEGRAM_TOKEN:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as e:
        print("Telegram error:", e)


def convert_to_kyiv_time(dt_str: str, tz_str: str) -> str:
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        sign = 1 if tz_str.startswith("+") else -1
        hours, minutes = map(int, tz_str[1:].split(":"))
        offset = timedelta(hours=hours, minutes=minutes) * sign
        dt_utc = dt - offset
        dt_kyiv = dt_utc + timedelta(hours=2)
        return dt_kyiv.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return dt_str


def extract_main_fields(api_response: dict) -> dict:
    root = api_response.get("data", api_response)

    if "accepted" in root:
        accepted = root.get("accepted") or {}
        items = accepted.get("content") or root.get("content") or []
        if items:
            tracking = items[0]
        else:
            tracking = root
    else:
        tracking = root

    result = {
        "status_text": "UNKNOWN",
        "time_str": "невідомо",
        "origin": tracking.get("shipFrom", "Unknown"),
        "destination": tracking.get("shipTo", "Unknown"),
        "tracking_number": tracking.get("trackNo") or tracking.get("trackingNumber"),
        "raw_last_event": None,
    }

    logistics = tracking.get("localLogisticsInfo", {})
    details = logistics.get("trackingDetails") or tracking.get("trackingDetails") or []

    last_tracking_time = (
        tracking.get("lastTrackingTime")
        or tracking.get("nextUpdateTime")
        or tracking.get("shipTime")
    )

    if details:
        last_event = details[0]
        result["raw_last_event"] = last_event

        tz = last_event.get("timezone", "+08:00")
        time_val = last_event.get("eventTime") or last_tracking_time
        if time_val:
            result["time_str"] = convert_to_kyiv_time(time_val, tz)

        detail = (
            last_event.get("eventDetail")
            or logistics.get("transitSubStatus")
            or tracking.get("transitSubStatus")
            or tracking.get("trackingStatus")
        )
        if detail:
            result["status_text"] = str(detail).split(",")[0]

    return result


def register_tracking(track_no: str) -> bool:
    if not TRACK123_API_KEY:
        return False

    headers = {
        "Track123-Api-Secret": TRACK123_API_KEY,
        "Content-Type": "application/json",
        "accept": "application/json",
    }

    payload = [
        {
            "trackNo": track_no,
        }
    ]

    try:
        resp = requests.post(
            TRACK123_IMPORT_URL,
            json=payload,
            headers=headers,
            timeout=15,
        )
        if not resp.ok:
            print("import error:", resp.status_code, resp.text)
            return False
        return True
    except Exception as e:
        print("import exception:", e)
        return False


def delete_tracking(track_no: str) -> bool:
    if not TRACK123_API_KEY:
        return False

    headers = {
        "Track123-Api-Secret": TRACK123_API_KEY,
        "Content-Type": "application/json",
        "accept": "application/json",
    }

    payload = {"trackNos": [track_no]}

    try:
        resp = requests.post(
            TRACK123_DELETE_URL,
            json=payload,
            headers=headers,
            timeout=15,
        )
        if not resp.ok:
            print("delete error:", resp.status_code, resp.text)
            return False
        return True
    except Exception as e:
        print("delete exception:", e)
        return False


def fetch_initial_status(track_no: str, chat_id: int) -> bool:
    if not TRACK123_API_KEY:
        return False

    headers = {
        "Track123-Api-Secret": TRACK123_API_KEY,
        "Content-Type": "application/json",
        "accept": "application/json",
    }

    registered = register_tracking(track_no)

    try:
        payload = {"trackNos": [track_no]}
        resp = requests.post(
            TRACK123_QUERY_URL,
            json=payload,
            headers=headers,
            timeout=15,
        )

        if not resp.ok:
            print("track/query error:", resp.status_code, resp.text)
            if registered:
                delete_tracking(track_no)
            return False

        data = resp.json()
        meta = extract_main_fields(data)

        tn = meta.get("tracking_number")
        if not tn:
            print(f"Track {track_no} not found in Track123 response")
            if registered:
                delete_tracking(track_no)
            return False

        new_status = meta.get("status_text", "UNKNOWN")
        time_str = meta.get("time_str", "UNKNOWN")

        trackings.update_one(
            {"track_no": track_no},
            {
                "$set": {
                    "last_status": new_status,
                    "last_update": datetime.utcnow(),
                    "origin": meta.get("origin", "UNKNOWN"),
                    "destination": meta.get("destination", "UNKNOWN"),
                    "time_str": time_str,
                },
                "$setOnInsert": {
                    "track_no": track_no,
                    "created_at": datetime.utcnow(),
                },
            },
            upsert=True,
        )

        return True

    except Exception as e:
        print("initial fetch error:", e)
        if registered:
            delete_tracking(track_no)
        return False


def format_message(tracking_number: str, meta: dict, *, initial: bool) -> str:
    theme = random.choice(EMOJI_THEMES)

    status = html.escape(meta.get("status_text", "UNKNOWN"))
    time_str = html.escape(meta.get("time_str", "невідомо"))
    origin = meta.get("origin", "Unknown")
    dest = meta.get("destination", "Unknown")

    event = meta.get("raw_last_event") or {}
    desc = event.get("description") or event.get("eventDetail")

    header = "ПОЧАТОК МОНІТОРИНГУ" if initial else "ОНОВЛЕННЯ СТАТУСУ"

    msg = [
        f"<b>{theme['header']} {header}</b>",
        f"📦 <b>Посилка:</b> <code>{tracking_number}</code>",
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
    if not TRACK123_API_KEY:
        return

    all_tracks = list(trackings.find({}, {"track_no": 1}))

    for t in all_tracks:
        try:
            headers = {
                "Track123-Api-Secret": TRACK123_API_KEY,
                "Content-Type": "application/json",
            }
            payload = {"trackNos": [t["track_no"]]}
            resp = requests.post(
                TRACK123_REFRESH_URL,
                json=payload,
                headers=headers,
                timeout=10,
            )

            if not resp.ok:
                print("Refresh error:", t["track_no"], resp.status_code, resp.text)
        except Exception as e:
            print("Refresh exception:", t["track_no"], e)

    threading.Timer(REFRESH_INTERVAL, refresh_all_trackings).start()


@app.post("/telegram-webhook")
def telegram_webhook():
    update = request.get_json(silent=True) or {}

    message = update.get("message") or update.get("edited_message") or {}
    text = (message.get("text") or "").strip()
    chat = message.get("chat") or {}
    from_user = message.get("from") or {}

    chat_id = chat.get("id")

    if not chat_id or not text:
        return jsonify({"ok": True})

    lower = text.lower()

    if lower.startswith("/start"):
        send_telegram(
            chat_id,
            "Привіт! Я бот для відстеження посилок\n\n"
            "Команди:\n"
            "• /track <i>НОМЕР</i> — почати відслідковувати посилку\n"
            "• /list — список всіх ваших посилок\n"
            "• /untrack <i>НОМЕР</i> — перестати відслідковувати посилку",
        )
        return jsonify({"ok": True})

    if lower.startswith("/list"):
        subs = list(subscriptions.find({"chat_id": chat_id}))

        if not subs:
            send_telegram(
                chat_id,
                "📭 Ви ще не відстежуєте жодної посилки.\n"
                "Додайте посилку командою:\n/track <i>НОМЕР</i>",
            )
            return jsonify({"ok": True})

        track_nos = sorted({s["track_no"] for s in subs})
        tracks_cursor = trackings.find({"track_no": {"$in": track_nos}})
        tracks_map = {t["track_no"]: t for t in tracks_cursor}

        lines = ["📦 <b>Ваші посилки:</b>"]

        for tn in track_nos:
            tr = tracks_map.get(tn, {})
            status = tr.get("last_status", "статус ще невідомий")
            last_update = tr.get("last_update")

            if isinstance(last_update, datetime):
                last_str = last_update.strftime("%Y-%m-%d %H:%M:%S")
            else:
                last_str = "час невідомий"

            lines.append(
                f"• <code>{tn}</code> — {html.escape(status)} "
                f"(<i>{last_str}</i>)"
            )

        send_telegram(chat_id, "\n".join(lines))
        return jsonify({"ok": True})

    if lower.startswith("/untrack"):
        parts = text.split(maxsplit=1)

        if len(parts) < 2:
            send_telegram(
                chat_id,
                "❗ Формат: /untrack <i>ABCD0123456789</i>",
            )
            return jsonify({"ok": True})

        track_no = parts[1].strip()

        result = subscriptions.delete_one({"chat_id": chat_id, "track_no": track_no})

        if result.deleted_count == 0:
            send_telegram(
                chat_id,
                f"ℹ️ Ви не відстежували посилку <i>>{track_no}</i>.",
            )
            return jsonify({"ok": True})

        remaining = subscriptions.count_documents({"track_no": track_no})
        if remaining == 0:
            trackings.delete_one({"track_no": track_no})

        send_telegram(
            chat_id,
            f"🗑 Відстежування посилки <i>{track_no}</i> зупинене!",
        )
        return jsonify({"ok": True})

    if lower.startswith("/track"):
        parts = text.split(maxsplit=1)

        if len(parts) < 2:
            send_telegram(
                chat_id,
                "❗ Формат: /track <i>ABCD0123456789</i>",
            )
            return jsonify({"ok": True})

        track_no = parts[1].strip()

        existing_sub = subscriptions.find_one(
            {"chat_id": chat_id, "track_no": track_no}
        )

        if existing_sub:
            tr = trackings.find_one({"track_no": track_no}) or {}
            status = tr.get("last_status", "статус ще невідомий")
            last_update = tr.get("last_update")

            if isinstance(last_update, datetime):
                last_str = last_update.strftime("%Y-%m-%d %H:%М:%S")
            else:
                last_str = "час невідомий"

            send_telegram(
                chat_id,
                "ℹ️ Ви вже відстежуєте цю посилку.\n"
                f"Поточний статус: {html.escape(status)} (<i>{last_str}</i>)\n\n"
                "Подивитися всі посилки: <i>/list</i>",
            )
            return jsonify({"ok": True})

        success = fetch_initial_status(track_no, chat_id)

        if not success:
            send_telegram(
                chat_id,
                "❌ Не вдалося знайти таку посилку\n"
                "Перевір, чи правильно введений номер!",
            )
            return jsonify({"ok": True})

        users.update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "chat_id": chat_id,
                    "username": from_user.get("username"),
                    "first_name": from_user.get("first_name"),
                    "updated_at": datetime.utcnow(),
                },
                "$setOnInsert": {"created_at": datetime.utcnow()},
            },
            upsert=True,
        )

        trackings.update_one(
            {"track_no": track_no},
            {
                "$setOnInsert": {
                    "track_no": track_no,
                    "created_at": datetime.utcnow(),
                }
            },
            upsert=True,
        )

        subscriptions.update_one(
            {"chat_id": chat_id, "track_no": track_no},
            {
                "$set": {
                    "chat_id": chat_id,
                    "track_no": track_no,
                    "created_at": datetime.utcnow(),
                }
            },
            upsert=True,
        )

        send_telegram(
            chat_id,
            f"🟢 Я відстежую посилку <i>{track_no}</i>.\n"
            f"Подивитися всі посилки: <i>/list</i>",
        )
        return jsonify({"ok": True})

    return jsonify({"ok": True})


@app.post("/track123-webhook")
def track123_webhook():
    payload = request.get_json(silent=True) or {}

    meta = extract_main_fields(payload)
    track_no = meta.get("tracking_number")

    if not track_no:
        return jsonify({"error": "no track"}), 400

    new_status = meta.get("status_text", "UNKNOWN")

    old_track = trackings.find_one({"track_no": track_no})
    old_status = old_track.get("last_status") if old_track else None

    if old_status == new_status:
        return jsonify({"ok": True})

    trackings.update_one(
        {"track_no": track_no},
        {
            "$set": {
                "last_status": new_status,
                "last_update": datetime.utcnow(),
                "origin": meta.get("origin", "Unknown"),
                "destination": meta.get("destination", "Unknown"),
            }
        },
        upsert=True,
    )

    subs = list(subscriptions.find({"track_no": track_no}))

    if not subs:
        return jsonify({"ok": True})

    msg = format_message(track_no, meta, initial=(old_status is None))

    for s in subs:
        send_telegram(s["chat_id"], msg)

    return jsonify({"ok": True})


@app.get("/")
def home():
    return "Bot is running!"


if __name__ == "__main__":
    refresh_all_trackings()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))