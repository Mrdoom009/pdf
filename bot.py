import os
import sys
import asyncio
import tempfile
import shutil
import time
import io
import re
from pathlib import Path
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
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
IMAGES_PER_PAGE = 3
VERTICAL_SPACING = 20
MAX_CONCURRENT_DOWNLOADS = 6
DOWNLOAD_TIMEOUT = 300
DOWNLOAD_RETRIES = 7

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
    workers=100,
    sleep_threshold=180,
    in_memory=True
)

def is_image(message: Message) -> bool:
    return bool(message.photo or 
               (message.document and message.document.mime_type and 
                message.document.mime_type.startswith("image/")))

async def robust_download(message: Message, path: Path) -> Path:
    file_id = message.photo.file_id if message.photo else message.document.file_id
    
    # Handle file extension
    if message.photo:
        ext = ".jpg"
    else:
        fname = message.document.file_name or "image"
        ext = Path(fname).suffix or ".jpg"
        if not ext or ext.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
            ext = ".jpg"
    
    file_path = path / f"{int(time.time())}_{file_id}{ext}"
    
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            await asyncio.wait_for(
                app.download_media(message, file_name=str(file_path)),
                timeout=DOWNLOAD_TIMEOUT
            )
            if os.path.exists(file_path) and os.path.getsize(file_path) > 1024:
                return file_path
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass
        await asyncio.sleep(2 ** attempt)
    raise Exception("Download failed")

def optimize_image(img_path: Path) -> Path:
    with Image.open(img_path) as img:
        # Convert unsupported formats to JPEG
        if img.format not in ['JPEG', 'PNG']:
            new_path = img_path.with_suffix(".jpg")
            img.convert('RGB').save(new_path, "JPEG", quality=90)
            os.remove(img_path)
            return new_path
            
        # Resize if needed
        max_size = (2480, 3508)  # A4 at 300dpi
        if img.width > max_size[0] or img.height > max_size[1]:
            img.thumbnail(max_size, Image.LANCZOS)
            img.save(img_path, optimize=True)
    return img_path

def generate_pdf(images: list, mode: str) -> bytes:
    pdf_buffer = io.BytesIO()
    
    if mode == 'single':
        c = canvas.Canvas(pdf_buffer)
        for i, img_path in enumerate(images):
            with Image.open(img_path) as img:
                c.setPageSize((img.width, img.height))
                c.drawImage(
                    ImageReader(img), 0, 0, 
                    width=img.width, height=img.height,
                    preserveAspectRatio=True, anchor='c'
                )
                if i < len(images) - 1:
                    c.showPage()
        c.save()
    
    else:  # Grid mode
        c = canvas.Canvas(pdf_buffer, pagesize=A4)
        for i in range(0, len(images), IMAGES_PER_PAGE):
            if i > 0:
                c.showPage()
            current_y = A4[1]
            batch = images[i:i+IMAGES_PER_PAGE]
            img_height = (A4[1] - VERTICAL_SPACING * (len(batch)-1)) / len(batch)
            
            for img_path in batch:
                with Image.open(img_path) as img:
                    aspect = img.height / img.width
                    width = A4[0]
                    height = width * aspect
                    
                    if height > img_height:
                        height = img_height
                        width = height / aspect
                    
                    x_offset = (A4[0] - width) / 2
                    c.drawImage(
                        ImageReader(img), x_offset, current_y - height,
                        width=width, height=height, preserveAspectRatio=True
                    )
                    current_y -= height + VERTICAL_SPACING
        c.save()
    
    return pdf_buffer.getvalue()

@app.on_message(filters.command("start"))
async def start_command(_, message: Message):
    await message.reply(
        "🖼️ **Image to PDF Bot**\n\n"
        "• /begin - 3 images per page\n"
        "• /begin2 - 1 image per page\n"
        "• /stop - Finish & create PDF\n"
        "• /cancel - Cancel session",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_message(filters.command(["begin", "begin2"]))
async def start_session(_, message: Message):
    user_id = message.from_user.id
    user_dir = TEMP_DIR / str(user_id)
    
    if user_dir.exists():
        shutil.rmtree(user_dir, ignore_errors=True)
    user_dir.mkdir(parents=True, exist_ok=True)
    
    mode = 'single' if message.command[0] == "begin2" else 'grid'
    sessions[user_id] = {
        "image_refs": [],
        "active": True,
        "dir": user_dir,
        "media_groups": set(),
        "sequence": 0,
        "mode": mode
    }
    
    mode_desc = "1 image per page" if mode == 'single' else "3 images per page"
    await message.reply(f"📸 {mode_desc} mode started! Send images. /stop when done.")

@app.on_message(filters.command("stop"))
async def stop_session(_, message: Message):
    user_id = message.from_user.id
    session = sessions.get(user_id)
    
    if not session or not session["active"]:
        return await message.reply("❌ No active session!")
    
    session["active"] = False
    if not session["image_refs"]:
        clean_session(user_id)
        return await message.reply("⚠️ No images received!")
    
    progress_msg = await message.reply("⏳ Downloading images...")
    session["downloaded_images"] = []
    
    # Process images in sequence
    sorted_refs = sorted(session["image_refs"], key=lambda x: x["sequence"])
    for idx, ref in enumerate(sorted_refs):
        try:
            img_path = await robust_download(ref["message"], session["dir"])
            img_path = await asyncio.get_event_loop().run_in_executor(
                None, optimize_image, img_path
            )
            session["downloaded_images"].append(img_path)
            if idx % 5 == 0:
                await progress_msg.edit_text(f"⏳ Downloaded {idx+1}/{len(sorted_refs)} images...")
        except Exception:
            pass
    
    await progress_msg.edit_text("✅ Download complete! Send PDF filename:")
    session["waiting_for_name"] = True

@app.on_message(filters.command("cancel"))
async def cancel_session(_, message: Message):
    user_id = message.from_user.id
    if user_id in sessions:
        clean_session(user_id)
    await message.reply("❌ Session canceled")

@app.on_message(filters.private & filters.text)
async def handle_filename(client: Client, message: Message):
    user_id = message.from_user.id
    session = sessions.get(user_id)
    
    if not session or not session.get("waiting_for_name"):
        return
    
    # Sanitize filename
    filename = re.sub(r'[^\w\-_\. ]', '_', (message.text.strip() or "document")[:50])
    pdf_msg = await message.reply("🔄 Creating PDF...")
    
    try:
        pdf_data = await asyncio.get_event_loop().run_in_executor(
            None, generate_pdf, session["downloaded_images"], session["mode"]
        )
        
        # Create temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(pdf_data)
            tmp_path = tmp.name
        
        # Determine page count
        page_count = len(session["downloaded_images"]) if session["mode"] == 'single' else \
            (len(session["downloaded_images"]) + IMAGES_PER_PAGE - 1) // IMAGES_PER_PAGE
        
        # Send PDF
        await client.send_document(
            chat_id=user_id,
            document=tmp_path,
            file_name=f"{filename}.pdf",
            caption=f"✅ PDF Generated • {len(session['downloaded_images'])} images • {page_count} pages"
        )
    except Exception:
        await message.reply("❌ PDF creation failed")
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        clean_session(user_id)

@app.on_message(filters.private & (filters.photo | filters.document | filters.media_group))
async def handle_image(_, message: Message):
    user_id = message.from_user.id
    session = sessions.get(user_id)
    
    if not session or not session["active"]:
        return
    
    # Handle media groups
    if message.media_group_id:
        group_id = message.media_group_id
        if group_id in session["media_groups"]:
            return
        
        session["media_groups"].add(group_id)
        try:
            media_group = await app.get_media_group(user_id, message.id)
            for msg in media_group:
                if is_image(msg):
                    session["image_refs"].append({
                        "message": msg,
                        "sequence": session["sequence"]
                    })
                    session["sequence"] += 1
        except Exception:
            pass
        session["media_groups"].discard(group_id)
        return
    
    # Handle single image
    if is_image(message):
        session["image_refs"].append({
            "message": message,
            "sequence": session["sequence"]
        })
        session["sequence"] += 1

def clean_session(user_id):
    """Robust session cleanup"""
    session = sessions.pop(user_id, None)
    if session:
        user_dir = session.get("dir")
        if user_dir and os.path.exists(user_dir):
            try:
                shutil.rmtree(user_dir, ignore_errors=True)
            except Exception:
                pass

def run_bot():
    while True:
        try:
            print("Starting Telegram bot...")
            app.run()
        except Exception:
            time.sleep(5)

if __name__ == "__main__":
    run_bot()
