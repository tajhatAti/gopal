"""
Hugging Face dataset repo -> Telegram channel poster (Render Web Service version)
------------------------------------------------------
- HF dataset repo theke video/image download kore
- 50MB er niche hole bot token diye pathabe (fast, simple)
- 50MB er upore hole string session (Pyrogram user account) diye pathabe
  (Telegram Bot API te 50MB upload limit ache, kintu user account/MTProto diye
  2GB porjonto pathano jay)
- Render FREE Web Service e deploy korar jonno: ekta choto Flask server
  chalu thake (/ping route), UptimeRobot প্রতি 5 min e ping korle server
  ghumiye jabe na (free tier 15 min inactive hole sleep hoye jay).
- Upload kaj ekta background thread e ekbar automatic start hoy.

Install:
    pip install pyrogram tgcrypto huggingface_hub python-dotenv requests flask

Run (local test):
    python hf_to_telegram.py

Render e deploy korar somoy:
    Start command: python hf_to_telegram.py
    Environment variables gula Render dashboard > Environment e boshate hobe
    (nichey .env file e value gula deya ache, oigulai copy kore boshabe)
"""

import asyncio
import os
import threading
import time

# Pyrogram's current sync wrapper calls asyncio.get_event_loop() while it is
# imported. Python 3.14 no longer creates a loop automatically, so importing
# Pyrogram on Render fails with "There is no current event loop". Create one
# before importing Pyrogram (the wrapper can then reuse it).
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from dotenv import load_dotenv
from huggingface_hub import HfApi, hf_hub_download
from pyrogram import Client
from flask import Flask

load_dotenv()

# ---------------- CONFIG (.env / Render Environment theke asche) ----------------
HF_REPO_ID = os.environ["HF_REPO_ID"]
HF_REPO_TYPE = "dataset"

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL = os.environ["CHANNEL"]

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]

# Telegram Bot API limits are different for videos and photos.
# Photos are limited to 10 MB, while videos are limited to 50 MB.
BOT_VIDEO_LIMIT_MB = 50
BOT_PHOTO_LIMIT_MB = 10
DOWNLOAD_DIR = "./hf_downloads"
PORT = int(os.environ.get("PORT", 10000))
# -----------------------------------------

VIDEO_EXT = (".mp4", ".mov", ".mkv")
IMAGE_EXT = (".jpg", ".jpeg", ".png")

app = Flask(__name__)
status = {"state": "starting", "done": 0, "total": 0, "current": ""}


@app.route("/")
@app.route("/ping")
def ping():
    return {
        "status": status["state"],
        "done": status["done"],
        "total": status["total"],
        "current": status["current"],
    }


def list_repo_media():
    api = HfApi()
    files = api.list_repo_files(repo_id=HF_REPO_ID, repo_type=HF_REPO_TYPE)
    return [f for f in files if f.lower().endswith(VIDEO_EXT + IMAGE_EXT)]


def download_file(filename):
    path = hf_hub_download(
        repo_id=HF_REPO_ID,
        repo_type=HF_REPO_TYPE,
        filename=filename,
        local_dir=DOWNLOAD_DIR,
    )
    return path


def send_with_bot(local_path, filename):
    """Bot API diye pathano. Telegram-er error hole tar details log koro."""
    import requests

    is_video = filename.lower().endswith(VIDEO_EXT)
    method = "sendVideo" if is_video else "sendPhoto"
    field = "video" if is_video else "photo"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    try:
        with open(local_path, "rb") as f:
            resp = requests.post(
                url,
                data={"chat_id": CHANNEL},
                files={field: (os.path.basename(filename), f)},
                timeout=(15, 300),
            )
        # HTTP 200 is not enough: Telegram also returns {"ok": false, ...}.
        result = resp.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Telegram network error: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError(
            f"Telegram returned non-JSON response ({resp.status_code}): "
            f"{resp.text[:300]}"
        ) from exc

    if not resp.ok or not result.get("ok"):
        error = result.get("description", resp.text[:300])
        raise RuntimeError(f"Telegram {method} failed ({resp.status_code}): {error}")

    return True


def send_with_userbot(local_path, filename):
    """Pyrogram (string session) diye pathano (>50MB er jonno)."""
    is_video = filename.lower().endswith(VIDEO_EXT)

    with Client(
        "userbot_session",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=STRING_SESSION,
        in_memory=True,
    ) as client:
        if is_video:
            client.send_video(CHANNEL, local_path)
        else:
            client.send_photo(CHANNEL, local_path)


def run_upload_job():
    local_path = None
    try:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        files = list_repo_media()
        status["total"] = len(files)
        status["state"] = "running"
        print(f"Repo te {len(files)} ta media file paoa gelo.")

        for idx, filename in enumerate(files, start=1):
            status["current"] = filename
            print(f"\n--- {filename} ---")
            print("Download hocche...")
            local_path = download_file(filename)

            size_mb = os.path.getsize(local_path) / (1024 * 1024)
            is_video = filename.lower().endswith(VIDEO_EXT)
            bot_limit = BOT_VIDEO_LIMIT_MB if is_video else BOT_PHOTO_LIMIT_MB
            print(f"Size: {size_mb:.2f} MB (Bot limit: {bot_limit} MB)")

            try:
                if size_mb <= bot_limit:
                    print("Bot token diye pathano hocche...")
                    send_with_bot(local_path, filename)
                    print("Pathano hoyeche.")
                else:
                    print(f"{bot_limit}MB er beshi, userbot diye pathano hocche...")
                    send_with_userbot(local_path, filename)
                    print("Pathano hoyeche.")
            except Exception as e:
                print(f"Error: {e}")
            finally:
                if local_path and os.path.exists(local_path):
                    os.remove(local_path)
                local_path = None
                status["done"] = idx

        status["state"] = "finished"
        print("\nSob media process shesh.")
    except Exception as e:
        status["state"] = "failed"
        status["current"] = str(e)
        print(f"Upload job failed: {e}")


def start_background_job():
    time.sleep(3)  # server age up hote dao
    run_upload_job()


if __name__ == "__main__":
    threading.Thread(target=start_background_job, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
