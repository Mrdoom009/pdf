import os  
import sys  
import asyncio  
import tempfile  
import shutil  
import time  
import re  
import io  
import uuid  
from pathlib import Path  
from PIL import Image  
from reportlab.pdfgen import canvas  
from pyrogram import Client, filters  
from pyrogram.types import Message  
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
DOWNLOAD_TIMEOUT = 300  
DOWNLOAD_RETRIES = 5  
  
# Session management  
sessions = {}  
  
# Flask server setup  
flask_app = Flask(__name__)  
  
@flask_app.route('/health')  
def health_check():  
    return Response("OK", status=200)  
  
def run_flask():  
    flask_app.run(host='0.0.0.0', port=8000)  
  
# Start Flask thread  
flask_thread = threading.Thread(target=run_flask, daemon=True)  
flask_thread.start()  
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
  
# Utility to check if a message is an image  
def is_image(message: Message) -> bool:  
    return bool(message.photo or   
               (message.document and message.document.mime_type and   
                message.document.mime_type.startswith("image/")))  
  
# Download image with retries  
async def download_image(message: Message, path: Path) -> Path:  
    file_id = message.photo.file_id if message.photo else message.document.file_id  
    if message.photo:  
        ext = ".jpg"  
    else:  
        fname = message.document.file_name or "image"  
        ext = Path(fname).suffix or ".jpg"  
    file_path = path / f"{file_id}{ext}"  
    for attempt in range(DOWNLOAD_RETRIES):  
        try:  
            await app.download_media(message, file_name=str(file_path))  
            if file_path.exists() and file_path.stat().st_size > 1024:  
                return file_path  
        except Exception:  
            pass  
        await asyncio.sleep(2 ** attempt)  
    raise Exception("Download failed")  
  
# Generate PDF from list of image paths  
def generate_pdf(images: list) -> bytes:  
    pdf_buffer = io.BytesIO()  
    c = canvas.Canvas(pdf_buffer)  
    for img_path in images:  
        try:  
            with Image.open(img_path) as img:  
                img_width, img_height = img.size  
                aspect_ratio = img_width / img_height  
                page_width = 595  # A4 width in points  
                page_height = page_width / aspect_ratio  
                c.setPageSize((page_width, page_height))  
                c.drawImage(str(img_path), 0, 0, width=page_width, height=page_height, preserveAspectRatio=False)  
                c.showPage()  
        except Exception:  
            pass  
    c.save()  
    return pdf_buffer.getvalue()  
  
# /begin handler: starts a new session  
@app.on_message(filters.command("begin"))  
async def start_session(_, message: Message):  
    user_id = message.from_user.id  
    user_dir = TEMP_DIR / str(user_id)  
    if user_dir.exists():  
        shutil.rmtree(user_dir, ignore_errors=True)  
    user_dir.mkdir(parents=True, exist_ok=True)  
    sessions[user_id] = {"images": [], "dir": user_dir, "active": True}  
    await message.reply("📸 Session started! Send images. /stop when done.")  
  
# /stop handler: downloads, generates, and sends PDF with random name  
@app.on_message(filters.command("stop"))  
async def stop_session(_, message: Message):  
    user_id = message.from_user.id  
    session = sessions.get(user_id)  
    if not session or not session["active"]:  
        return await message.reply("❌ No active session! Send /begin first.")  
    session["active"] = False  
    if not session["images"]:  
        clean_session(user_id)  
        return await message.reply("⚠️ No images received!")  
    progress_msg = await message.reply("⏳ Downloading images...")  
    downloaded = []  
    for idx, msg in enumerate(session["images"]):  
        try:  
            img_path = await download_image(msg, session["dir"])  
            downloaded.append(img_path)  
            if (idx + 1) % 5 == 0:  
                await progress_msg.edit_text(f"⏳ Downloaded {idx+1}/{len(session['images'])} images...")  
        except Exception:  
            await progress_msg.reply(f"❌ Failed image {idx+1}. Skipping...")  
    if not downloaded:  
        clean_session(user_id)  
        return await progress_msg.edit_text("❌ All downloads failed! Session aborted.")  
    await progress_msg.edit_text("✅ Download complete! Generating PDF...")  
    try:  
        pdf_data = generate_pdf(downloaded)  
        random_name = f"{uuid.uuid4().hex}.pdf"  
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:  
            tmp.write(pdf_data)  
            tmp_path = tmp.name  
        await app.send_document(
            chat_id=user_id,
            document=tmp_path,
            file_name=random_name,
            caption=f"✅ PDF Generated • {len(downloaded)} pages"
        )  
    except Exception:  
        await message.reply("❌ PDF creation failed")  
    finally:  
        clean_session(user_id)  
        if 'tmp_path' in locals() and os.path.exists(tmp_path):  
            try: os.unlink(tmp_path)  
            except: pass  
  
# Image handler: collects images during active session  
@app.on_message(filters.private & (filters.photo | filters.document))  
async def handle_image(_, message: Message):  
    user_id = message.from_user.id  
    session = sessions.get(user_id)  
    if not session or not session["active"]:  
        return  
    if is_image(message):  
        session["images"].append(message)  
  
# Cleanup session data and temp files  
def clean_session(user_id):  
    if user_id in sessions:  
        session = sessions.pop(user_id)  
        user_dir = session.get("dir")  
        if user_dir and user_dir.exists():  
            try: shutil.rmtree(user_dir, ignore_errors=True)  
            except: pass  
  
# Bot runner with auto-restart  
def run_bot():  
    while True:  
        try: app.run()  
        except Exception: time.sleep(5)  
  
if __name__ == "__main__":  
    run_bot()  
