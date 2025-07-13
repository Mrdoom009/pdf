import os
import sys
import asyncio
import tempfile
import shutil
import time
import re
import io
from pathlib import Path
from PIL import Image
from reportlab.pdfgen import canvas
from pyrogram import Client, filters
from pyrogram.types import Message, ReplyKeyboardMarkup, ReplyKeyboardRemove, ForceReply
from flask import Flask, Response
import threading

# Validate credentials
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

if not all([API_ID, API_HASH, BOT_TOKEN]):
    sys.exit("❌ Missing API credentials!")

# Configuration
TEMP_DIR = Path("user_data")
TEMP_DIR.mkdir(exist_ok=True)
DOWNLOAD_RETRIES = 5

# Session management
sessions = {}

# Flask server setup
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return Response("OK", status=200)

def run_flask():
    flask_app.run(host='0.0.0.0', port=8000)

# Start Flask thread
t = threading.Thread(target=run_flask, daemon=True)

t.start()
time.sleep(1)

# Pyrogram client
app = Client(
    "pdf_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=50,
    sleep_threshold=120,
    in_memory=True
)


def is_image(message: Message) -> bool:
    return bool(message.photo or 
               (message.document and message.document.mime_type 
                and message.document.mime_type.startswith("image/")))

async def download_image(msg: Message, path: Path) -> Path:
    file_id = msg.photo.file_id if msg.photo else msg.document.file_id
    ext = ".jpg" if msg.photo else Path(msg.document.file_name or "image").suffix or ".jpg"
    destination = path / f"{file_id}{ext}"

    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            await app.download_media(msg, file_name=str(destination))
            if destination.exists() and destination.stat().st_size > 1024:
                return destination
        except Exception:
            pass
        await asyncio.sleep(2 ** attempt)
    raise Exception("Download failed")


def generate_pdf(image_paths: list) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer)
    for img_path in image_paths:
        try:
            with Image.open(img_path) as im:
                w, h = im.size
                ratio = w / h
                page_w = 595
                page_h = page_w / ratio
                c.setPageSize((page_w, page_h))
                c.drawImage(str(img_path), 0, 0, width=page_w, height=page_h)
                c.showPage()
        except Exception:
            continue
    c.save()
    return buffer.getvalue()

# Custom reply keyboards
stop_keyboard = ReplyKeyboardMarkup([["/stop"]], resize_keyboard=True, one_time_keyboard=True)

@app.on_message(filters.command("begin"))
async def start_session(_, message: Message):
    uid = message.from_user.id
    user_dir = TEMP_DIR / str(uid)
    shutil.rmtree(user_dir, ignore_errors=True)
    user_dir.mkdir(parents=True, exist_ok=True)
    sessions[uid] = {"images": [], "dir": user_dir, "active": True}
    await message.reply(
        "📸 Session started! Send your images now.",
        reply_markup=stop_keyboard
    )

@app.on_message(filters.command("stop"))
async def stop_session(_, message: Message):
    uid = message.from_user.id
    session = sessions.get(uid)
    if not session or not session.get("active"):
        return await message.reply(
            "❌ No active session. Use /begin to start.",
            reply_markup=ReplyKeyboardRemove()
        )

    session["active"] = False
    images = session.get("images", [])
    if not images:
        clean_session(uid)
        return await message.reply(
            "⚠️ No images received. Session canceled.",
            reply_markup=ReplyKeyboardRemove()
        )

    progress = await message.reply(
        f"⏳ Downloading {len(images)} images...",
        reply_markup=ReplyKeyboardRemove()
    )

    downloaded = []
    for idx, img_msg in enumerate(images, 1):
        try:
            path = await download_image(img_msg, session["dir"])
            downloaded.append(path)
            bar = "█" * int(20 * idx / len(images)) + "░" * (20 - int(20 * idx / len(images)))
            await progress.edit_text(f"⏳ [{bar}] {idx}/{len(images)} images")
        except Exception:
            await progress.reply(f"❌ Failed to download image {idx}, skipping.")

    if not downloaded:
        clean_session(uid)
        return await progress.edit_text("❌ All downloads failed. Session aborted.")

    session.update({"downloaded": downloaded, "waiting": True})
    await progress.edit_text(
        "✅ Download complete! Please reply with the filename for your PDF (without extension):",
        reply_markup=ForceReply(selective=True)
    )

@app.on_message(filters.private & filters.text)
async def handle_filename(client: Client, message: Message):
    uid = message.from_user.id
    session = sessions.get(uid)
    if not session or not session.get("waiting"):
        return
    session["waiting"] = False

    # Sanitize and limit filename
    base = re.sub(r"[^\w\-_. ]", "_", message.text.strip())[:50] or "document"
    try:
        pdf = generate_pdf(session["downloaded"])
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(pdf)
            tmp_file = tmp.name
        await client.send_document(
            message.chat.id,
            document=tmp_file,
            file_name=f"{base}.pdf",
            caption=f"✅ PDF created: {len(session['downloaded'])} pages"
        )
    except Exception:
        await message.reply("❌ Failed to create PDF.")
    finally:
        clean_session(uid)
        try:
            os.unlink(tmp_file)
        except:
            pass

@app.on_message(filters.private & (filters.photo | filters.document))
async def handle_image(_, message: Message):
    uid = message.from_user.id
    session = sessions.get(uid)
    if session and session.get("active") and is_image(message):
        session["images"].append(message)


def clean_session(user_id):
    sess = sessions.pop(user_id, None)
    if sess:
        shutil.rmtree(sess.get("dir", Path()), ignore_errors=True)


def run_bot():
    while True:
        try:
            app.run()
        except Exception:
            time.sleep(5)

if __name__ == "__main__":
    run_bot()
