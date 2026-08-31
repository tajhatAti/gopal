"""Hugging Face dataset -> Telegram video bot.

The bot lists the media files in the Hugging Face dataset in private chat.
A user can press a button (or send a file name) to receive that file. Files
that fit in Telegram Bot API's limits are sent by the bot; larger files are
sent with the configured Pyrogram user session.

Required Render environment variables:
    HF_REPO_ID, BOT_TOKEN, API_ID, API_HASH, STRING_SESSION
Optional:
    HF_TOKEN, CHANNEL, PORT

CHANNEL is only needed if you also want to use a channel as a destination.
The bot must be started by the user first; Telegram does not allow a bot to
start a private conversation with an arbitrary user.
"""

import asyncio
import os
import sqlite3
import threading
import time
from pathlib import Path

# Pyrogram's sync wrapper calls get_event_loop() while it is imported. Python
# 3.14 no longer creates one automatically.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import requests
from dotenv import load_dotenv
from flask import Flask
from huggingface_hub import HfApi, hf_hub_download
from pyrogram import Client

load_dotenv()

HF_REPO_ID = os.environ["HF_REPO_ID"]
HF_REPO_TYPE = "dataset"
HF_TOKEN = os.environ.get("HF_TOKEN")
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL = os.environ.get("CHANNEL", "")
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]
DOWNLOAD_DIR = Path("./hf_downloads")
CACHE_DB = os.environ.get("CACHE_DB", "video_cache.sqlite3")
PORT = int(os.environ.get("PORT", 10000))

VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm")
IMAGE_EXT = (".jpg", ".jpeg", ".png")
BOT_VIDEO_LIMIT_MB = 50
BOT_PHOTO_LIMIT_MB = 10
PAGE_SIZE = 40

app = Flask(__name__)
status = {"state": "starting", "done": 0, "total": 0, "current": ""}
media_files = []
media_lock = threading.Lock()


@app.route("/")
@app.route("/ping")
def ping():
    return {
        "status": status["state"],
        "done": status["done"],
        "total": status["total"],
        "current": status["current"],
    }


def telegram(method, **kwargs):
    """Call the Bot API and raise an actionable error on failure."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = requests.post(url, data=kwargs, timeout=(15, 120))
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Telegram returned invalid JSON: {response.text[:300]}") from exc
    if not response.ok or not result.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {result.get('description', response.text[:300])}")
    return result["result"]


def list_repo_media():
    api = HfApi(token=HF_TOKEN)
    files = api.list_repo_files(repo_id=HF_REPO_ID, repo_type=HF_REPO_TYPE)
    return sorted(
        f for f in files
        if f.lower().endswith(VIDEO_EXT + IMAGE_EXT)
    )


def refresh_media():
    files = list_repo_media()
    with media_lock:
        media_files[:] = files
        status["total"] = len(files)
    print(f"HF repo te {len(files)} ta media file paoa gelo.")
    return files


def download_file(filename):
    return hf_hub_download(
        repo_id=HF_REPO_ID,
        repo_type=HF_REPO_TYPE,
        filename=filename,
        token=HF_TOKEN,
        local_dir=str(DOWNLOAD_DIR),
    )


def send_with_bot(local_path, filename, chat_id):
    is_video = filename.lower().endswith(VIDEO_EXT)
    method = "sendVideo" if is_video else "sendPhoto"
    field = "video" if is_video else "photo"
    with open(local_path, "rb") as file_handle:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
        response = requests.post(
            url,
            data={"chat_id": chat_id},
            files={field: (os.path.basename(filename), file_handle)},
            timeout=(15, 600),
        )
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Telegram returned invalid JSON: {response.text[:300]}") from exc
    if not response.ok or not result.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {result.get('description', response.text[:300])}")


def make_userbot_client():
    return Client(
        "userbot_session",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=STRING_SESSION,
        in_memory=True,
    )


def validate_user_session():
    """Fail early if STRING_SESSION/API credentials are invalid."""
    if not CHANNEL:
        raise RuntimeError("CHANNEL environment variable is required")
    with make_userbot_client() as client:
        me = client.get_me()
        print(f"User session OK: @{me.username or me.id}; channel target: {CHANNEL}")


def send_with_userbot(local_path, filename):
    """Post every new file to the archive channel using the personal account."""
    is_video = filename.lower().endswith(VIDEO_EXT)
    with make_userbot_client() as client:
        if is_video:
            return client.send_video(CHANNEL, local_path, supports_streaming=True)
        return client.send_photo(CHANNEL, local_path)


def init_cache():
    with sqlite3.connect(CACHE_DB) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                filename TEXT PRIMARY KEY,
                channel_message_id INTEGER NOT NULL
            )
        """)
        db.commit()


def cached_message_id(filename):
    with sqlite3.connect(CACHE_DB) as db:
        row = db.execute(
            "SELECT channel_message_id FROM videos WHERE filename = ?", (filename,)
        ).fetchone()
    return row[0] if row else None


def save_cached_message(filename, message_id):
    with sqlite3.connect(CACHE_DB) as db:
        db.execute(
            "INSERT OR REPLACE INTO videos(filename, channel_message_id) VALUES (?, ?)",
            (filename, message_id),
        )
        db.commit()


def remove_cached_message(filename):
    with sqlite3.connect(CACHE_DB) as db:
        db.execute("DELETE FROM videos WHERE filename = ?", (filename,))
        db.commit()


def copy_from_channel(chat_id, message_id):
    """copyMessage copies without Telegram's forwarded-from tag."""
    return telegram(
        "copyMessage",
        chat_id=chat_id,
        from_chat_id=CHANNEL,
        message_id=message_id,
    )


def make_keyboard(page=0):
    with media_lock:
        files = list(media_files)
    start = page * PAGE_SIZE
    rows = []
    for index in range(start, min(start + PAGE_SIZE, len(files)), 2):
        row = []
        for item_index in range(index, min(index + 2, len(files))):
            name = Path(files[item_index]).stem
            # Telegram button labels should be short, while callback data is an index.
            row.append({"text": name[:28], "callback_data": f"video:{item_index}"})
        rows.append(row)
    navigation = []
    if page > 0:
        navigation.append({"text": "⬅️ Previous", "callback_data": f"page:{page - 1}"})
    if start + PAGE_SIZE < len(files):
        navigation.append({"text": "Next ➡️", "callback_data": f"page:{page + 1}"})
    if navigation:
        rows.append(navigation)
    return {"inline_keyboard": rows}


def send_menu(chat_id, page=0, edit_message_id=None):
    with media_lock:
        count = len(media_files)
    text = (
        f"🎬 {count} ta video/file available.\n\n"
        "Nicher button-e click kore video nin, ba file-er naam likhun."
    )
    if edit_message_id:
        telegram("editMessageText", chat_id=chat_id, message_id=edit_message_id,
                 text=text, reply_markup=__import__("json").dumps(make_keyboard(page)))
    else:
        telegram("sendMessage", chat_id=chat_id, text=text,
                 reply_markup=__import__("json").dumps(make_keyboard(page)))


def deliver_file(chat_id, index):
    with media_lock:
        if index < 0 or index >= len(media_files):
            raise RuntimeError("Video list changed. Please send /start again.")
        filename = media_files[index]

    status["current"] = filename
    old_message_id = cached_message_id(filename)
    if old_message_id:
        telegram("sendMessage", chat_id=chat_id, text="📦 Channel archive থেকে পাঠানো হচ্ছে...")
        try:
            copy_from_channel(chat_id, old_message_id)
            telegram("sendMessage", chat_id=chat_id, text="✅ Video pathano hoyeche.")
            return
        except Exception:
            # The channel post may have been deleted; rebuild the archive entry.
            remove_cached_message(filename)

    telegram("sendMessage", chat_id=chat_id, text=f"⏳ Downloading: {Path(filename).name}")
    local_path = None
    try:
        local_path = download_file(filename)
        size_mb = os.path.getsize(local_path) / (1024 * 1024)
        print(f"New file: {filename} ({size_mb:.2f} MB), posting to channel...")

        # Every first request is archived in the channel. This bypasses the
        # Bot API upload limit; afterwards copyMessage sends it without a
        # forwarded-from tag and without downloading it again.
        channel_message = send_with_userbot(local_path, filename)
        save_cached_message(filename, channel_message.id)
        copy_from_channel(chat_id, channel_message.id)
        telegram("sendMessage", chat_id=chat_id, text="✅ Video channel-e save kore pathano hoyeche.")
    finally:
        if local_path and os.path.exists(local_path):
            os.remove(local_path)


def handle_update(update):
    callback = update.get("callback_query")
    if callback:
        callback_id = callback["id"]
        chat_id = callback["message"]["chat"]["id"]
        telegram("answerCallbackQuery", callback_query_id=callback_id)
        data = callback.get("data", "")
        if data.startswith("page:"):
            page = int(data.split(":", 1)[1])
            send_menu(chat_id, page, callback["message"]["message_id"])
        elif data.startswith("video:"):
            threading.Thread(
                target=deliver_safely, args=(chat_id, int(data.split(":", 1)[1])), daemon=True
            ).start()
        return

    message = update.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return
    text = (message.get("text") or "").strip()
    if text in ("/start", "/videos", "/help"):
        send_menu(chat_id)
        return
    if text:
        with media_lock:
            matches = [i for i, name in enumerate(media_files) if text.lower() in name.lower()]
        if len(matches) == 1:
            threading.Thread(target=deliver_safely, args=(chat_id, matches[0]), daemon=True).start()
        elif matches:
            telegram("sendMessage", chat_id=chat_id, text="একাধিক file পাওয়া গেছে:",
                     reply_markup=__import__("json").dumps({"inline_keyboard": [
                         [{"text": Path(media_files[i]).stem[:28], "callback_data": f"video:{i}"}]
                         for i in matches[:PAGE_SIZE]
                     ]}))
        else:
            telegram("sendMessage", chat_id=chat_id, text="File পাওয়া যায়নি। /start চাপুন এবং button ব্যবহার করুন।")


def deliver_safely(chat_id, index):
    try:
        deliver_file(chat_id, index)
    except Exception as exc:
        print(f"Delivery error for {chat_id}: {exc}")
        try:
            telegram("sendMessage", chat_id=chat_id, text=f"❌ পাঠানো যায়নি: {exc}")
        except Exception as notify_error:
            print(f"Could not notify user: {notify_error}")


def bot_polling():
    status["state"] = "loading"
    try:
        init_cache()
        validate_user_session()
        refresh_media()
        telegram("deleteWebhook", drop_pending_updates=False)
        telegram("getMe")
        status["state"] = "running"
        offset = None
        while True:
            params = {"timeout": 50}
            if offset is not None:
                params["offset"] = offset
            updates = telegram("getUpdates", **params)
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    handle_update(update)
                except Exception as exc:
                    print(f"Update error: {exc}")
    except Exception as exc:
        status["state"] = "failed"
        status["current"] = str(exc)
        print(f"Bot polling failed: {exc}")


if __name__ == "__main__":
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=bot_polling, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
