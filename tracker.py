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
TELEGRAM_SECRET_TOKEN = os.environ.get("TELEGRAM_SECRET_TOKEN")
TRACK123_WEBHOOK_SECRET = os.environ.get("TRACK123_WEBHOOK_SECRET")

EMOJI_THEMES = [
    {"header": "🔔", "pin": "📍", "route": "✈️", "time": "🕒"},
    {"header": "🆙", "pin": "📌", "route": "🛳️", "time": "⏱️"},
    {"header": "📣", "pin": "🚩", "route": "🚚", "time": "🕰️"},
]

def esc(value) -> str:
    return html.escape(str(value), quote=False)

def make_track123_headers() -> dict:
    return {
        "Track123-Api-Secret": TRACK123_API_KEY,
        "Content-Type": "application/json",
        "accept": "application/json",
    }

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
        tracking = items[0] if items else root
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

    headers = make_track123_headers()
    payload = [
        {
            "trackNo": track_no,
            "courierCode": "cainiao",
            "courierNameEN": "Cainiao",
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

    headers = make_track123_headers()
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

def query_track123_track(track_no: str):
    if not TRACK123_API_KEY:
        return None

    headers = make_track123_headers()
    payload = {"trackNos": [track_no]}

    try:
        resp = requests.post(
            TRACK123_QUERY_URL,
            json=payload,
            headers=headers,
            timeout=15,
        )
        if not resp.ok:
            print("track/query error:", resp.status_code, resp.text)
            return None

        return resp.json()
    except Exception as e:
        print("track/query exception:", e)
        return None

def fetch_initial_status(track_no: str, chat_id: int) -> bool:
    if not TRACK123_API_KEY:
        return False

    registered = register_tracking(track_no)
    data = query_track123_track(track_no)

    if not data:
        if registered:
            delete_tracking(track_no)
        return False

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

def format_message(tracking_number: str, meta: dict, *, initial: bool) -> str:
    theme = random.choice(EMOJI_THEMES)

    status = esc(meta.get("status_text", "UNKNOWN"))
    time_str = esc(meta.get("time_str", "невідомо"))

    origin_raw = meta.get("origin", "Unknown")
    dest_raw = meta.get("destination", "Unknown")

    origin = esc(origin_raw)
    dest = esc(dest_raw)

    event = meta.get("raw_last_event") or {}
    desc = event.get("description") or event.get("eventDetail")

    header = "ПОЧАТОК МОНІТОРИНГУ" if initial else "ОНОВЛЕННЯ СТАТУСУ"

    msg = [
        f"<b>{theme['header']} {header}</b>",
        f"📦 <b>Посилка:</b> <code>{esc(tracking_number)}</code>",
        f"{theme['pin']} <b>Статус:</b> {status}",
        "",
        f"{theme['route']} <b>Маршрут:</b> "
        f"{get_flag_emoji(origin_raw)} {origin} ➜ {get_flag_emoji(dest_raw)} {dest}",
    ]

    if desc:
        msg.append(f"<blockquote>{esc(desc)}</blockquote>")

    msg.append(f"<i>{theme['time']} {time_str}</i>")

    return "\n".join(msg)

def refresh_all_trackings():
    if not TRACK123_API_KEY:
        print("❌ No Track123 API key — refresh aborted")
        return

    all_tracks = list(trackings.find({}, {"track_no": 1}))

    headers = make_track123_headers()

    print(f"🔄 Refresh started for {len(all_tracks)} trackings")

    for t in all_tracks:
        track_no = t["track_no"]

        try:
            print(f"➡️ Sending refresh request for {track_no}")
            payload = {"trackNos": [track_no]}

            resp = requests.post(
                TRACK123_REFRESH_URL,
                json=payload,
                headers=headers,
                timeout=10,
            )

            print(f"⬅️ Response for {track_no}: {resp.status_code} {resp.text[:100]}")

            if not resp.ok:
                print("⚠️ Refresh error:", track_no, resp.status_code, resp.text)

        except Exception as e:
            print("❌ Refresh exception:", track_no, e)

    print("✅ Refresh cycle finished")

    threading.Timer(REFRESH_INTERVAL, refresh_all_trackings).start()

def format_detailed_info(track_no: str, meta: dict, history: list) -> str:
    theme = random.choice(EMOJI_THEMES)

    status = esc(meta.get("status_text", "UNKNOWN"))
    time_str = esc(meta.get("time_str", "невідомо"))

    origin_raw = meta.get("origin", "Unknown")
    dest_raw = meta.get("destination", "Unknown")
    origin = esc(origin_raw)
    dest = esc(dest_raw)

    msg = [
        f"<b>{theme['header']} Детальна інформація про посилку</b>",
        "───────────────",
        f"📦 <b>Номер:</b> <code>{esc(track_no)}</code>",
        f"{theme['pin']} <b>Статус:</b> {status}",
        f"{theme['route']} <b>Маршрут:</b> "
        f"{get_flag_emoji(origin_raw)} {origin} ➜ {get_flag_emoji(dest_raw)} {dest}",
        "",
        f"<i>{theme['time']} Останнє оновлення: {time_str}</i>",
        "",
        "<b>📜 Історія подій:</b>",
    ]

    if not history:
        msg.append("Немає даних про історію.")
        return "\n".join(msg)

    for ev in history:
        ev_time = ev.get("eventTime", "???")
        ev_desc = ev.get("description") or ev.get("eventDetail") or "Немає опису"
        tz = ev.get("timezone", "+08:00")
        ev_time_kyiv = convert_to_kyiv_time(ev_time, tz)

        msg.append(
            f"\n• <b>{esc(ev_time_kyiv)}</b>\n"
            f"<blockquote>{esc(ev_desc)}</blockquote>"
        )

    return "\n".join(msg)

@app.post("/telegram-webhook")
def telegram_webhook():
    if TELEGRAM_SECRET_TOKEN:
        incoming_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if incoming_secret != TELEGRAM_SECRET_TOKEN:
            return jsonify({"ok": False}), 403

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
            "Привіт! Я бот для відстеження посилок 📦\n\n"
            "Доступні команди:\n"
            "• <b>/track</b> <i>НОМЕР</i> — почати відстежувати посилку\n"
            "• <b>/list</b> — список всіх ваших посилок\n"
            "• <b>/untrack</b> <i>НОМЕР</i> — припинити відстеження\n"
            "• <b>/info</b> <i>НОМЕР</i> — детальна інформація та історія подій",
        )
        return jsonify({"ok": True})

    if lower.startswith("/list"):
        subs = list(subscriptions.find({"chat_id": chat_id}))

        if not subs:
            send_telegram(
                chat_id,
                "📭 Ви ще не відстежуєте жодної посилки.\n"
                "Додайте посилку командою:\n<b>/track</b> <i>НОМЕР</i>",
            )
            return jsonify({"ok": True})

        track_nos = sorted({s["track_no"] for s in subs})
        tracks_cursor = trackings.find({"track_no": {"$in": track_nos}})
        tracks_map = {t["track_no"]: t for t in tracks_cursor}

        lines = ["📦 <b>Ваші посилки:</b>", ""]

        for tn in track_nos:
            tr = tracks_map.get(tn, {})
            status = tr.get("last_status", "статус ще невідомий")
            time_str = tr.get("time_str") or "час невідомий"
            origin_raw = tr.get("origin", "Unknown")
            dest_raw = tr.get("destination", "Unknown")
            origin = esc(origin_raw)
            dest = esc(dest_raw)

            lines.append(
                f"• <code>{esc(tn)}</code>\n"
                f"  🏷 {esc(status)}\n"
                f"  🌍 {get_flag_emoji(origin_raw)} {origin} ➜ {get_flag_emoji(dest_raw)} {dest}\n"
                f"  ⏱ <i>{esc(time_str)}</i>\n"
            )

        send_telegram(chat_id, "\n".join(lines))
        return jsonify({"ok": True})

    if lower.startswith("/untrack"):
        parts = text.split(maxsplit=1)

        if len(parts) < 2:
            send_telegram(
                chat_id,
                "❗ Формат: <b>/untrack</b> <i>ABCD0123456789</i>",
            )
            return jsonify({"ok": True})

        track_no = parts[1].strip()

        result = subscriptions.delete_one({"chat_id": chat_id, "track_no": track_no})

        if result.deleted_count == 0:
            send_telegram(
                chat_id,
                f"ℹ️ Ви не відстежували посилку <i>{esc(track_no)}</i>.",
            )
            return jsonify({"ok": True})

        remaining = subscriptions.count_documents({"track_no": track_no})
        if remaining == 0:
            trackings.delete_one({"track_no": track_no})

        send_telegram(
            chat_id,
            f"🗑 Відстежування посилки <i>{esc(track_no)}</i> зупинене!",
        )
        return jsonify({"ok": True})

    if lower.startswith("/track"):
        parts = text.split(maxsplit=1)

        if len(parts) < 2:
            send_telegram(
                chat_id,
                "❗ Формат: <b>/track</b> <i>ABCD0123456789</i>",
            )
            return jsonify({"ok": True})

        track_no = parts[1].strip()

        existing_sub = subscriptions.find_one(
            {"chat_id": chat_id, "track_no": track_no}
        )

        if existing_sub:
            tr = trackings.find_one({"track_no": track_no}) or {}
            status = tr.get("last_status", "статус ще невідомий")
            time_str = tr.get("time_str") or "час невідомий"

            send_telegram(
                chat_id,
                "ℹ️ Ви вже відстежуєте цю посилку.\n"
                f"Поточний статус: {esc(status)} (<i>{esc(time_str)}</i>)\n\n"
                "Подивитися всі посилки: <b>/list</b>",
            )
            return jsonify({"ok": True})

        success = fetch_initial_status(track_no, chat_id)

        if not success:
            send_telegram(
                chat_id,
                "❌ Не вдалося знайти таку посилку.\n"
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
            f"🟢 Я відстежую посилку <i>{esc(track_no)}</i>.\n"
            f"Подивитися всі посилки: <b>/list</b>\n"
            f"Деталі: <b>/info</b> <i>{esc(track_no)}</i>",
        )
        return jsonify({"ok": True})

    if lower.startswith("/info"):
        parts = text.split(maxsplit=1)

        if len(parts) < 2:
            send_telegram(
                chat_id,
                "❗ Формат: <b>/info</b> <i>ABCD0123456789</i>",
            )
            return jsonify({"ok": True})

        track_no = parts[1].strip()

        tr = trackings.find_one({"track_no": track_no})
        if not tr:
            send_telegram(
                chat_id,
                f"❌ Немає даних про посилку <i>{esc(track_no)}</i>.\n"
                "Спробуй додати її командою <b>/track</b>.",
            )
            return jsonify({"ok": True})

        data = query_track123_track(track_no)

        if not data:
            send_telegram(chat_id, "⚠️ Не вдалося отримати дані")
            return jsonify({"ok": True})

        meta = extract_main_fields(data)

        root = data.get("data", data)
        info = root.get("accepted", root)
        content = info.get("content") or root.get("content") or []
        tracking = content[0] if content else root

        logistics = tracking.get("localLogisticsInfo", {})
        history = (
            logistics.get("trackingDetails")
            or tracking.get("trackingDetails")
            or []
        )

        msg = format_detailed_info(track_no, meta, history)
        send_telegram(chat_id, msg)
        return jsonify({"ok": True})

    return jsonify({"ok": True})

@app.post("/track123-webhook")
def track123_webhook():
    if TRACK123_WEBHOOK_SECRET:
        incoming_secret = request.headers.get("X-Track123-Secret")
        if incoming_secret != TRACK123_WEBHOOK_SECRET:
            return jsonify({"ok": False}), 403

    payload = request.get_json(silent=True) or {}

    meta = extract_main_fields(payload)
    track_no = meta.get("tracking_number")

    if not track_no:
        return jsonify({"error": "no track"}), 400

    new_status = meta.get("status_text", "UNKNOWN")
    time_str = meta.get("time_str", "невідомо")

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
                "time_str": time_str,
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