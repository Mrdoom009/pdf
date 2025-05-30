import os
import asyncio
import tempfile
import shutil
import time
import io
from pathlib import Path
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from flask import Flask, Response
import logging

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot setup
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Validate credentials
if not all([API_ID, API_HASH, BOT_TOKEN]):
    exit("Missing API credentials!")

# Configuration
TEMP_DIR = Path("user_data")
TEMP_DIR.mkdir(exist_ok=True)
IMAGES_PER_PAGE = 3
VERTICAL_SPACING = 20
TARGET_DPI = 150
MAX_CONCURRENT_DOWNLOADS = 8
DOWNLOAD_TIMEOUT = 180
DOWNLOAD_RETRIES = 5

# Session management
sessions = {}

# Flask server setup
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "PDF Bot Online"

@flask_app.route('/health')
def health_check():
    return Response("OK", status=200)

def run_flask():
    flask_app.run(host='0.0.0.0', port=8000)

# Start Flask in a separate thread
import threading
flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()

# Pyrogram client
app = Client(
    "pdf_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=200,
    sleep_threshold=120,
    in_memory=True
)

def is_image(message: Message) -> bool:
    """Check if message contains a valid image"""
    if message.photo:
        return True
    if message.document:
        return (message.document.mime_type or "").startswith("image/")
    return False

async def robust_download(message: Message, path: Path) -> Path:
    """Advanced download with retries and validation"""
    file_id = message.photo.file_id if message.photo else message.document.file_id
    ext = ".jpg" if message.photo else Path(message.document.file_name or "image").suffix or ".jpg"
    file_path = path / f"{int(time.time())}_{file_id}{ext}"
    
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            # Download with timeout
            await asyncio.wait_for(
                app.download_media(message, file_name=str(file_path)),
                timeout=DOWNLOAD_TIMEOUT
            )
            
            # Validate file
            if os.path.exists(file_path) and os.path.getsize(file_path) > 1024:
                return file_path
                
            # Cleanup if invalid
            if os.path.exists(file_path):
                os.remove(file_path)
                
        except Exception as e:
            logger.warning(f"Download attempt {attempt+1} failed: {e}")
            
        await asyncio.sleep(1)  # Wait between retries
    
    raise Exception("Download failed after retries")

def optimize_image(img_path: Path):
    """Optimize image size for PDF"""
    try:
        with Image.open(img_path) as img:
            max_width = int((A4[0] / 72) * TARGET_DPI)
            max_height = int((A4[1] / 72) * TARGET_DPI)
            
            if img.width > max_width or img.height > max_height:
                img.thumbnail((max_width, max_height), Image.LANCZOS)
                img.save(img_path, quality=90, optimize=True)
    except Exception as e:
        logger.error(f"Image optimization failed: {e}")

def generate_pdf(images: list) -> bytes:
    """Generate PDF with 3 images per page using file paths"""
    # Create a BytesIO buffer for PDF
    pdf_buffer = io.BytesIO()
    
    # Create canvas directly to memory buffer
    c = canvas.Canvas(pdf_buffer, pagesize=A4)
    page_count = 0
    total_images = len(images)
    
    for i in range(0, total_images, IMAGES_PER_PAGE):
        if page_count > 0:
            c.showPage()
        page_count += 1
        
        current_y = A4[1]
        batch = images[i:i+IMAGES_PER_PAGE]
        available_height = A4[1] - (VERTICAL_SPACING * (len(batch) - 1))
        img_height = available_height / len(batch)
        
        for img_path in batch:
            try:
                # Verify image exists before processing
                if not os.path.exists(img_path):
                    logger.warning(f"Skipping missing file: {img_path}")
                    continue
                    
                with Image.open(img_path) as img:
                    # Get image dimensions
                    img_width, img_height_orig = img.size
                    aspect = img_height_orig / img_width
                    
                    # Calculate dimensions
                    width = A4[0]
                    height = width * aspect
                    
                    # Adjust if too tall
                    if height > img_height:
                        height = img_height
                        width = height / aspect
                    
                    # Center horizontally
                    x_offset = (A4[0] - width) / 2
                    
                    # Draw image using PIL Image object
                    c.drawImage(
                        ImageReader(img),
                        x_offset,
                        current_y - height,
                        width=width,
                        height=height,
                        preserveAspectRatio=True,
                        mask='auto'
                    )
                    current_y -= height + VERTICAL_SPACING
            except Exception as e:
                logger.error(f"Error drawing image: {e}")
    
    c.save()
    
    # Get PDF data from buffer
    pdf_data = pdf_buffer.getvalue()
    pdf_buffer.close()
    return pdf_data

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    await message.reply(
        "🖼️ **Image to PDF Bot**\n\n"
        "• /begin - Start session\n"
        "• /stop - Finish & create PDF\n"
        "• /cancel - Cancel session\n\n"
        "Features: 3 images/page, full-width, ordered, HQ",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_message(filters.command("begin"))
async def start_session(client: Client, message: Message):
    user_id = message.from_user.id
    user_dir = TEMP_DIR / str(user_id)
    
    if user_dir.exists():
        shutil.rmtree(user_dir, ignore_errors=True)
    user_dir.mkdir(parents=True, exist_ok=True)
    
    sessions[user_id] = {
        "image_refs": [],
        "active": True,
        "dir": user_dir,
        "media_groups": set(),
        "sequence": 0
    }
    
    await message.reply("📸 Session started! Send images now. /stop when done.")

@app.on_message(filters.command("stop"))
async def stop_session(client: Client, message: Message):
    user_id = message.from_user.id
    session = sessions.get(user_id)
    
    if not session or not session["active"]:
        return await message.reply("❌ No active session! /begin to start.")
    
    session["active"] = False
    count = len(session["image_refs"])
    
    if count == 0:
        clean_session(user_id)
        return await message.reply("⚠️ No images received! Session canceled.")
    
    progress_msg = await message.reply(f"⏳ Downloading 0/{count} images...")
    session["downloaded_images"] = []
    
    # Download images in order with concurrency
    sorted_refs = sorted(session["image_refs"], key=lambda x: x["sequence"])
    download_tasks = []
    
    for idx, ref in enumerate(sorted_refs):
        task = asyncio.create_task(
            download_and_process(ref["message"], session["dir"], idx, progress_msg, count)
        )
        download_tasks.append(task)
    
    results = await asyncio.gather(*download_tasks, return_exceptions=True)
    session["downloaded_images"] = [r for r in results if not isinstance(r, Exception) and r is not None]
    
    success = len(session["downloaded_images"])
    await progress_msg.edit_text(
        f"✅ Downloaded {success}/{count} images!\n"
        "📝 Send PDF filename:"
    )
    session["waiting_for_name"] = True

async def download_and_process(message, user_dir, idx, progress_msg, total):
    try:
        img_path = await robust_download(message, user_dir)
        # Run optimization in thread pool
        await asyncio.get_event_loop().run_in_executor(None, optimize_image, img_path)
        
        # Update progress every 5 images
        if (idx + 1) % 5 == 0:
            await progress_msg.edit_text(f"⏳ Downloading {idx+1}/{total} images...")
        
        return img_path
    except Exception as e:
        logger.error(f"Image processing failed: {e}")
        await progress_msg.reply(f"❌ Failed image {idx+1}. Skipping...")
        return None

@app.on_message(filters.command("cancel"))
async def cancel_session(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id in sessions:
        clean_session(user_id)
    await message.reply("❌ Session canceled!")

@app.on_message(filters.private & filters.text)
async def handle_filename(client: Client, message: Message):
    user_id = message.from_user.id
    session = sessions.get(user_id)
    
    if not session or not session.get("waiting_for_name"):
        return
    
    filename = (message.text.strip() or "document")[:50]
    pdf_progress = await message.reply("🔄 Creating PDF...")
    
    try:
        # Generate PDF in a thread
        pdf_data = await asyncio.get_event_loop().run_in_executor(
            None,
            generate_pdf,
            session["downloaded_images"]
        )
        
        await pdf_progress.edit_text("✅ PDF created! Sending...")
        
        # Create temporary file for sending
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(pdf_data)
            tmp_path = tmp.name
        
        # Send PDF document
        await client.send_document(
            chat_id=user_id,
            document=tmp_path,
            file_name=f"{filename}.pdf",
            caption=f"✅ PDF Generated • {len(session['downloaded_images'])} images • {len(session['downloaded_images'])//IMAGES_PER_PAGE + 1} pages"
        )
        
        # Cleanup temporary file
        os.unlink(tmp_path)
    except Exception as e:
        logger.error(f"PDF creation failed: {e}")
        await message.reply("❌ PDF creation failed. Try /begin again.")
    finally:
        clean_session(user_id)

@app.on_message(filters.private & (filters.photo | filters.document | filters.media_group))
async def handle_image(client: Client, message: Message):
    user_id = message.from_user.id
    session = sessions.get(user_id)
    
    if not session or not session["active"]:
        return
    
    session["sequence"] += 1
    seq = session["sequence"]
    
    if message.media_group_id:
        group_id = message.media_group_id
        
        if group_id in session["media_groups"]:
            return
        
        session["media_groups"].add(group_id)
        
        try:
            media_group = await client.get_media_group(user_id, message.id)
            for msg in media_group:
                if is_image(msg):
                    session["image_refs"].append({
                        "message": msg,
                        "sequence": seq
                    })
                    seq += 1
            session["sequence"] = seq
        except Exception as e:
            logger.error(f"Media group error: {e}")
        finally:
            session["media_groups"].discard(group_id)
        return
    
    if is_image(message):
        session["image_refs"].append({
            "message": message,
            "sequence": seq
        })

def clean_session(user_id):
    """Clean up user session synchronously"""
    if user_id in sessions:
        session = sessions[user_id]
        user_dir = session.get("dir")
        if user_dir and os.path.exists(user_dir):
            shutil.rmtree(user_dir, ignore_errors=True)
        del sessions[user_id]

def run_bot():
    while True:
        try:
            logger.info("Starting Telegram bot...")
            app.run()
        except Exception as e:
            logger.error(f"Bot crashed: {e} - Restarting in 5s")
            time.sleep(5)

if __name__ == "__main__":
    run_bot()
