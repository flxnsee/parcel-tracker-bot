import os
import time
import random
import html
import requests
from datetime import datetime, timedelta, timezone
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
PARCELS_API_KEY = os.environ.get("PARCELS_API_KEY")
PARCELS_LANGUAGE = os.environ.get("PARCELS_LANGUAGE", "en")
PARCELS_DESTINATION_COUNTRY = os.environ.get("PARCELS_DESTINATION_COUNTRY", "Ukraine")
REFRESH_INTERVAL = 10 * 60
PARCELS_TRACKING_URL = "https://parcelsapp.com/api/v3/shipments/tracking"
KYIV_TZ = timezone(timedelta(hours=2))
MAX_TRACK_NO_LENGTH = 64

EMOJI_THEMES = [
    {"header": "🔔", "pin": "📍", "route": "✈️", "time": "🕒"},
    {"header": "🆙", "pin": "📌", "route": "🛳️", "time": "⏱️"},
    {"header": "📣", "pin": "🚩", "route": "🚚", "time": "🕰️"},
]

session = requests.Session()

def esc(value) -> str:
    return html.escape(str(value), quote=False)

def sanitize_tracking_number(raw: str) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_."
    cleaned = "".join(ch for ch in raw if ch in allowed)
    if not cleaned:
        return ""
    return cleaned[:MAX_TRACK_NO_LENGTH]

def get_flag_emoji(code: str) -> str:
    if not code or len(code) != 2:
        return "🌍"
    try:
        return "".join(chr(127397 + ord(c)) for c in code.upper())
    except Exception:
        return "🌍"

def send_telegram(chat_id: int, message: str):
    if not TELEGRAM_TOKEN:
        print("⚠️ TELEGRAM_TOKEN is not set, cannot send message")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        session.post(
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

def parse_iso_to_kyiv(dt_str: str) -> str:
    if not dt_str:
        return "невідомо"
    try:
        s = dt_str.strip()
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_kyiv = dt.astimezone(KYIV_TZ)
        return dt_kyiv.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return dt_str

def query_parcels_track(track_no: str):
    if not PARCELS_API_KEY:
        print("❌ No PARCELS_API_KEY set")
        return None

    payload = {
        "shipments": [
            {
                "trackingId": str(track_no),
                "destinationCountry": PARCELS_DESTINATION_COUNTRY,
            }
        ],
        "language": PARCELS_LANGUAGE,
        "apiKey": PARCELS_API_KEY,
    }

    try:
        resp = session.post(
            PARCELS_TRACKING_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=25,
        )

        print("Parcels POST:", track_no, resp.status_code, resp.text[:300])

        if not resp.ok:
            return None

        data = resp.json()

        if data.get("error"):
            print("Parcels error:", data["error"])
            return None

        uuid = data.get("uuid")
        shipments = data.get("shipments") or []
        done = data.get("done", False)

        final_shipments = shipments

        if uuid and not done:
            for _ in range(3):
                time.sleep(2)

                status_resp = session.get(
                    PARCELS_TRACKING_URL,
                    params={"uuid": uuid, "apiKey": PARCELS_API_KEY},
                    headers={"Accept": "application/json"},
                    timeout=25,
                )

                print("Parcels GET:", track_no, status_resp.status_code, status_resp.text[:300])

                if not status_resp.ok:
                    break

                status_data = status_resp.json()

                if status_data.get("shipments"):
                    final_shipments = status_data["shipments"]

                if status_data.get("done", False):
                    break

        data["shipments"] = final_shipments
        return data

    except Exception as e:
        print("Parcels exception:", e)
        return None

def extract_main_fields(api_response: dict) -> dict:
    root = api_response.get("data", api_response)

    shipments = root.get("shipments") or []
    shipment = shipments[0] if shipments else {}

    states = shipment.get("states") or []
    last_state = shipment.get("lastState") or (states[-1] if states else {})

    status_text = (
        last_state.get("status")
        or shipment.get("status")
        or "UNKNOWN"
    )

    time_raw = last_state.get("date") or ""

    origin = (
        shipment.get("origin")
        or shipment.get("originCode")
        or "Unknown"
    )

    destination = (
        shipment.get("destination")
        or shipment.get("destinationCode")
        or "Unknown"
    )

    origin_code = shipment.get("originCode") or ""
    dest_code = shipment.get("destinationCode") or ""

    tracking_number = shipment.get("trackingId") or shipment.get("tracking_id")

    result = {
        "status_text": str(status_text),
        "time_str": parse_iso_to_kyiv(time_raw) if time_raw else "невідомо",
        "origin": origin,
        "destination": destination,
        "origin_code": origin_code,
        "destination_code": dest_code,
        "tracking_number": tracking_number,
        "raw_last_event": last_state or (states[-1] if states else None),
        "states": states,
    }

    return result

def fetch_initial_status(track_no: str, chat_id: int) -> bool:
    data = query_parcels_track(track_no)

    if not data:
        return False

    meta = extract_main_fields(data)

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
                "origin_code": meta.get("origin_code", ""),
                "destination_code": meta.get("destination_code", ""),
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

    origin_code = meta.get("origin_code") or ""
    dest_code = meta.get("destination_code") or ""

    origin = esc(origin_raw)
    dest = esc(dest_raw)

    event = meta.get("raw_last_event") or {}
    desc = (
        event.get("status")
        or event.get("description")
        or event.get("message")
    )

    header = "ПОЧАТОК МОНІТОРИНГУ" if initial else "ОНОВЛЕННЯ СТАТУСУ"

    msg = [
        f"<b>{theme['header']} {header}</b>",
        f"📦 <b>Посилка:</b> <code>{esc(tracking_number)}</code>",
        f"{theme['pin']} <b>Статус:</b> {status}",
        "",
        f"{theme['route']} <b>Маршрут:</b> "
        f"{get_flag_emoji(origin_code)} {origin} ➜ {get_flag_emoji(dest_code)} {dest}",
    ]

    if desc:
        msg.append(f"<blockquote>{esc(desc)}</blockquote>")

    msg.append(f"<i>{theme['time']} {time_str}</i>")

    return "\n".join(msg)

def format_detailed_info(track_no: str, meta: dict, history: list) -> str:
    theme = random.choice(EMOJI_THEMES)

    status = esc(meta.get("status_text", "UNKNOWN"))
    time_str = esc(meta.get("time_str", "невідомо"))

    origin_raw = meta.get("origin", "Unknown")
    dest_raw = meta.get("destination", "Unknown")
    origin_code = meta.get("origin_code") or ""
    dest_code = meta.get("destination_code") or ""

    origin = esc(origin_raw)
    dest = esc(dest_raw)

    msg = [
        f"<b>{theme['header']} Детальна інформація про посилку</b>",
        "───────────────",
        f"📦 <b>Номер:</b> <code>{esc(track_no)}</code>",
        f"{theme['pin']} <b>Статус:</b> {status}",
        f"{theme['route']} <b>Маршрут:</b> "
        f"{get_flag_emoji(origin_code)} {origin} ➜ {get_flag_emoji(dest_code)} {dest}",
        "",
        f"<i>{theme['time']} Останнє оновлення: {time_str}</i>",
        "",
        "<b>📜 Історія подій:</b>",
    ]

    if not history:
        msg.append("Немає даних про історію.")
        return "\n".join(msg)

    for ev in history:
        ev_time_raw = ev.get("date") or ev.get("time") or "???"
        ev_time = parse_iso_to_kyiv(ev_time_raw)

        status_text = (
            ev.get("status")
            or ev.get("description")
            or ev.get("message")
            or "Немає опису"
        )
        location = ev.get("location") or ""
        if location:
            desc = f"{status_text} ({location})"
        else:
            desc = status_text

        msg.append(
            f"\n• <b>{esc(ev_time)}</b>\n"
            f"<blockquote>{esc(desc)}</blockquote>"
        )

    return "\n".join(msg)

def refresh_all_trackings():
    if not PARCELS_API_KEY:
        print("❌ No PARCELS_API_KEY set — refresh aborted")
        return

    all_tracks = list(trackings.find({}, {"track_no": 1}))

    print(f"🔄 Parcels auto-refresh started, {len(all_tracks)} trackings")

    for t in all_tracks:
        track_no = t.get("track_no")
        if not track_no:
            continue

        print(f"➡️ Перевіряю {track_no}")

        data = query_parcels_track(track_no)
        if not data:
            print(f"⚠️ Parcels не повернув даних для {track_no}")
            continue

        meta = extract_main_fields(data)
        new_status = meta.get("status_text", "UNKNOWN")
        time_str = meta.get("time_str", "невідомо")

        if new_status == "UNKNOWN":
            print(f"⚠️ Status became UNKNOWN for {track_no}, skipping update")
            continue

        old = trackings.find_one({"track_no": track_no})
        old_status = old.get("last_status") if old else None

        if old_status == new_status:
            continue

        print(f"🟢 Оновлення статусу {track_no}: {old_status} → {new_status}")

        trackings.update_one(
            {"track_no": track_no},
            {
                "$set": {
                    "last_status": new_status,
                    "last_update": datetime.utcnow(),
                    "origin": meta.get("origin", "Unknown"),
                    "destination": meta.get("destination", "Unknown"),
                    "origin_code": meta.get("origin_code", ""),
                    "destination_code": meta.get("destination_code", ""),
                    "time_str": time_str,
                }
            },
            upsert=True,
        )

        subs = list(subscriptions.find({"track_no": track_no}))
        if not subs:
            continue

        msg = format_message(track_no, meta, initial=False)
        for s in subs:
            send_telegram(s["chat_id"], msg)

    threading.Timer(REFRESH_INTERVAL, refresh_all_trackings).start()

@app.post("/telegram-webhook")
def telegram_webhook():
    try:
        update = request.get_json(silent=True) or {}

        message = update.get("message") or update.get("edited_message") or {}
        text = (message.get("text") or "").strip()
        chat = message.get("chat") or {}
        from_user = message.get("from") or {}

        chat_id = chat.get("id")

        if not chat_id or not text:
            return jsonify({"ok": True})

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg_raw = parts[1] if len(parts) > 1 else ""
        arg = sanitize_tracking_number(arg_raw) if arg_raw else ""

        if cmd == "/start":
            send_telegram(
                chat_id,
                "Привіт! Я бот для відстеження посилок 📦\n\n"
                "Доступні команди:\n"
                "• <b>/track</b> <i>НОМЕР</i> — почати відстежувати посилку\n"
                "• <b>/list</b> — список всіх ваших посилок\n"
                "• <b>/untrack</b> <i>НОМЕР</i> — припинити відстеження\n"
                "• <b>/info</b> <i>НОМЕР</i> — детальна інформація та історія подій",
            )

        elif cmd == "/list":
            subs = list(subscriptions.find({"chat_id": chat_id}))

            if not subs:
                send_telegram(
                    chat_id,
                    "📭 Ви ще не відстежуєте жодної посилки.\n"
                    "Додайте посилку командою:\n<b>/track</b> <i>НОМЕР</i>",
                )
            else:
                track_nos = sorted({s["track_no"] for s in subs if s.get("track_no")})
                tracks_cursor = trackings.find({"track_no": {"$in": track_nos}})
                tracks_map = {t["track_no"]: t for t in tracks_cursor}

                lines = ["📦 <b>Ваші посилки:</b>", ""]

                for tn in track_nos:
                    tr = tracks_map.get(tn, {})
                    status = tr.get("last_status", "статус ще невідомий")
                    time_str = tr.get("time_str") or "час невідомий"
                    origin_raw = tr.get("origin", "Unknown")
                    dest_raw = tr.get("destination", "Unknown")
                    origin_code = tr.get("origin_code") or ""
                    dest_code = tr.get("destination_code") or ""
                    origin = esc(origin_raw)
                    dest = esc(dest_raw)

                    lines.append(
                        f"• <code>{esc(tn)}</code>\n"
                        f"  🏷 {esc(status)}\n"
                        f"  🌍 {get_flag_emoji(origin_code)} {origin} ➜ "
                        f"{get_flag_emoji(dest_code)} {dest}\n"
                        f"  ⏱ <i>{esc(time_str)}</i>\n"
                    )

                send_telegram(chat_id, "\n".join(lines))

        elif cmd == "/untrack":
            if not arg:
                send_telegram(
                    chat_id,
                    "❗ Формат: <b>/untrack</b> <i>ABCD0123456789</i>",
                )
            else:
                track_no = arg
                result = subscriptions.delete_one({"chat_id": chat_id, "track_no": track_no})

                if result.deleted_count == 0:
                    send_telegram(
                        chat_id,
                        f"ℹ️ Ви не відстежували посилку <i>{esc(track_no)}</i>.",
                    )
                else:
                    remaining = subscriptions.count_documents({"track_no": track_no})
                    if remaining == 0:
                        trackings.delete_one({"track_no": track_no})

                    send_telegram(
                        chat_id,
                        f"🗑 Відстежування посилки <i>{esc(track_no)}</i> зупинене!",
                    )

        elif cmd == "/track":
            if not arg:
                send_telegram(
                    chat_id,
                    "❗ Формат: <b>/track</b> <i>ABCD0123456789</i>",
                )
            else:
                track_no = arg

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
                else:
                    success = fetch_initial_status(track_no, chat_id)

                    if not success:
                        send_telegram(
                            chat_id,
                            "❌ Не вдалося знайти таку посилку через Parcels.\n"
                            "Перевір, чи правильно введений номер!",
                        )
                    else:
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

        elif cmd == "/info":
            if not arg:
                send_telegram(
                    chat_id,
                    "❗ Формат: <b>/info</b> <i>ABCD0123456789</i>",
                )
            else:
                track_no = arg

                tr = trackings.find_one({"track_no": track_no})
                if not tr:
                    send_telegram(
                        chat_id,
                        f"❌ Немає даних про посилку <i>{esc(track_no)}</i>.\n"
                        "Спробуй додати її командою <b>/track</b>.",
                    )
                else:
                    data = query_parcels_track(track_no)

                    if not data:
                        send_telegram(chat_id, "⚠️ Не вдалося отримати дані від Parcels")
                    else:
                        meta = extract_main_fields(data)

                        root = data.get("data", data)
                        shipments = root.get("shipments") or []
                        shipment = shipments[0] if shipments else {}
                        history = shipment.get("states") or []

                        msg = format_detailed_info(track_no, meta, history)
                        send_telegram(chat_id, msg)

    except Exception as e:
        print("telegram_webhook exception:", repr(e))

    return jsonify({"ok": True})

@app.get("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    refresh_all_trackings()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))