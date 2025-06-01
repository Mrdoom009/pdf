import os
import sys
import asyncio
import tempfile
import shutil
import time
import re
import io  # Added missing import
from pathlib import Path
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from pyrogram import Client, filters, enums
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
DOWNLOAD_TIMEOUT = 300  # 5 minutes timeout
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
time.sleep(1)  # Ensure Flask starts before Pyrogram

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
               (message.document and message.document.mime_type and 
                message.document.mime_type.startswith("image/")))

async def download_image(message: Message, path: Path) -> Path:
    file_id = message.photo.file_id if message.photo else message.document.file_id
    
    # Handle file extension
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
        except Exception as e:
            print(f"Download attempt {attempt+1} failed: {e}")
        await asyncio.sleep(2 ** attempt)
    
    raise Exception("Download failed after multiple attempts")

def generate_pdf(images: list) -> bytes:
    """Generate PDF with one image per page"""
    pdf_buffer = io.BytesIO()
    c = canvas.Canvas(pdf_buffer)
    
    for img_path in images:
        try:
            with Image.open(img_path) as img:
                # Set page size to image dimensions
                c.setPageSize((img.width, img.height))
                
                # Draw image to fill entire page
                c.drawImage(
                    str(img_path), 0, 0,
                    width=img.width, height=img.height,
                    preserveAspectRatio=True
                )
                # Start new page for next image
                c.showPage()
        except Exception as e:
            print(f"Skipping invalid image {img_path}: {e}")
    
    c.save()
    return pdf_buffer.getvalue()

@app.on_message(filters.command("begin"))
async def start_session(_, message: Message):
    user_id = message.from_user.id
    user_dir = TEMP_DIR / str(user_id)
    
    # Clean previous session
    if user_dir.exists():
        shutil.rmtree(user_dir, ignore_errors=True)
    user_dir.mkdir(parents=True, exist_ok=True)
    
    sessions[user_id] = {
        "images": [],  # Store messages in order
        "dir": user_dir,
        "active": True,
        "media_groups": set()  # Track processed media groups
    }
    
    await message.reply("📸 Session started! Send images. /stop when done.")

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
    
    # Download images in order
    for idx, msg in enumerate(session["images"]):
        try:
            img_path = await download_image(msg, session["dir"])
            downloaded.append(img_path)
            if (idx + 1) % 5 == 0:
                await progress_msg.edit_text(f"⏳ Downloaded {idx+1}/{len(session['images'])} images...")
        except Exception as e:
            print(f"Image download failed: {e}")
            await progress_msg.reply(f"❌ Failed image {idx+1}. Skipping...")
    
    if not downloaded:
        clean_session(user_id)
        return await progress_msg.edit_text("❌ All downloads failed! Session aborted.")
    
    # Request filename
    await progress_msg.edit_text("✅ Download complete! Send PDF filename:")
    session["downloaded"] = downloaded
    session["waiting"] = True

@app.on_message(filters.private & filters.text)
async def handle_filename(client: Client, message: Message):
    user_id = message.from_user.id
    session = sessions.get(user_id)
    
    if not session or not session.get("waiting"):
        return
    
    # Sanitize filename
    filename = re.sub(r'[^\w\-_\. ]', '_', message.text.strip()[:50])
    if not filename:
        filename = "document"
    
    tmp_path = None
    try:
        pdf_data = generate_pdf(session["downloaded"])
        
        # Create temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(pdf_data)
            tmp_path = tmp.name
        
        # Send PDF
        await client.send_document(
            chat_id=user_id,
            document=tmp_path,
            file_name=f"{filename}.pdf",
            caption=f"✅ PDF Generated • {len(session['downloaded'])} pages"
        )
    except Exception as e:
        print(f"PDF creation failed: {e}")
        await message.reply("❌ PDF creation failed")
    finally:
        clean_session(user_id)
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

@app.on_message(filters.private & (filters.photo | filters.document | filters.media_group))
async def handle_image(_, message: Message):
    user_id = message.from_user.id
    session = sessions.get(user_id)
    
    if not session or not session["active"]:
        return
    
    # Handle media groups
    if message.media_group_id:
        # Skip if already processed
        if message.media_group_id in session["media_groups"]:
            return
            
        session["media_groups"].add(message.media_group_id)
        
        try:
            media_group = await app.get_media_group(message.chat.id, message.id)
            # Filter only valid images
            session["images"].extend([msg for msg in media_group if is_image(msg)])
        except Exception as e:
            print(f"Media group error: {e}")
        return
    
    # Handle single image
    if is_image(message):
        session["images"].append(message)

def clean_session(user_id):
    """Cleanup session data"""
    if user_id in sessions:
        session = sessions.pop(user_id)
        user_dir = session.get("dir")
        if user_dir and user_dir.exists():
            try:
                shutil.rmtree(user_dir, ignore_errors=True)
            except Exception as e:
                print(f"Cleanup error: {e}")

def run_bot():
    while True:
        try:
            print("Starting Telegram bot...")
            app.run()
        except Exception as e:
            print(f"Bot crashed: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run_bot()
