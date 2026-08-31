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

import os
import threading
import time
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

SIZE_LIMIT_MB = 50
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
    """Bot API diye pathano (<=50MB er jonno)."""
    import requests

    is_video = filename.lower().endswith(VIDEO_EXT)
    method = "sendVideo" if is_video else "sendPhoto"
    field = "video" if is_video else "photo"

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    with open(local_path, "rb") as f:
        resp = requests.post(url, data={"chat_id": CHANNEL}, files={field: f})
    return resp.ok


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
        print(f"Size: {size_mb:.2f} MB")

        try:
            if size_mb <= SIZE_LIMIT_MB:
                print("Bot token diye pathano hocche...")
                ok = send_with_bot(local_path, filename)
                print("Pathano hoyeche." if ok else "Bot diye pathate fail korlo.")
            else:
                print("50MB er beshi, userbot (string session) diye pathano hocche...")
                send_with_userbot(local_path, filename)
                print("Pathano hoyeche.")
        except Exception as e:
            print(f"Error: {e}")
        finally:
            if os.path.exists(local_path):
                os.remove(local_path)
            status["done"] = idx

    status["state"] = "finished"
    print("\nSob media process shesh.")


def start_background_job():
    time.sleep(3)  # server age up hote dao
    run_upload_job()


if __name__ == "__main__":
    threading.Thread(target=start_background_job, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
