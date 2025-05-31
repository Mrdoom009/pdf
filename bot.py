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
import traceback

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
MAX_CONCURRENT_DOWNLOADS = 6  # Reduced for better stability
DOWNLOAD_TIMEOUT = 300  # Increased to 5 minutes
DOWNLOAD_RETRIES = 7  # Increased retries

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
    workers=100,  # Reduced workers for stability
    sleep_threshold=180,  # Increased sleep threshold
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
    
    logger.info(f"Starting download for file: {file_path}")
    
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            # Download with timeout
            logger.info(f"Attempt {attempt}/{DOWNLOAD_RETRIES} for file {file_id}")
            await asyncio.wait_for(
                app.download_media(message, file_name=str(file_path)),
                timeout=DOWNLOAD_TIMEOUT
            )
            
            # Validate file
            if os.path.exists(file_path) and os.path.getsize(file_path) > 1024:
                logger.info(f"Download successful: {file_path} ({os.path.getsize(file_path)} bytes)")
                return file_path
                
            # Cleanup if invalid
            if os.path.exists(file_path):
                logger.warning(f"Small file detected: {file_path} ({os.path.getsize(file_path)} bytes)")
                os.remove(file_path)
                
        except asyncio.TimeoutError:
            logger.warning(f"Download timeout on attempt {attempt}")
        except Exception as e:
            logger.warning(f"Download error on attempt {attempt}: {str(e)}")
            
        await asyncio.sleep(2 ** attempt)  # Exponential backoff
    
    raise Exception(f"Download failed after {DOWNLOAD_RETRIES} attempts")

def optimize_image(img_path: Path):
    """Optimize image size for PDF"""
    try:
        with Image.open(img_path) as img:
            max_width = int((A4[0] / 72) * TARGET_DPI)
            max_height = int((A4[1] / 72) * TARGET_DPI)
            
            if img.width > max_width or img.height > max_height:
                logger.info(f"Optimizing image: {img_path}")
                img.thumbnail((max_width, max_height), Image.LANCZOS)
                img.save(img_path, quality=90, optimize=True)
    except Exception as e:
        logger.error(f"Image optimization failed: {e}")

def generate_pdf(images: list, mode: str = 'grid') -> bytes:
    """Generate PDF based on mode (grid or single)"""
    if not images:
        raise ValueError("No images to process")
    
    pdf_buffer = io.BytesIO()
    logger.info(f"Generating PDF in {mode} mode with {len(images)} images")
    
    if mode == 'single':
        # Single image per page mode
        c = canvas.Canvas(pdf_buffer)
        page_count = 0
        
        for i, img_path in enumerate(images):
            try:
                if not os.path.exists(img_path):
                    logger.warning(f"Skipping missing file: {img_path}")
                    continue
                    
                with Image.open(img_path) as img:
                    width, height = img.size
                    logger.info(f"Processing image {i+1}: {img_path} ({width}x{height})")
                    
                    # Create page with image dimensions
                    c.setPageSize((width, height))
                    
                    # Draw the image to fill the entire page
                    c.drawImage(
                        ImageReader(img),
                        0, 0,
                        width=width,
                        height=height,
                        preserveAspectRatio=True,
                        anchor='c',
                        mask='auto'
                    )
                    
                    # End page and start new one (except for last image)
                    if i < len(images) - 1:
                        c.showPage()
                    
                    page_count += 1
                    
            except Exception as e:
                logger.error(f"Error processing image {img_path}: {e}")
                logger.error(traceback.format_exc())
        
        if page_count > 0:
            c.save()
    
    else:
        # Grid mode (3 images per page)
        c = canvas.Canvas(pdf_buffer, pagesize=A4)
        page_count = 0
        total_images = len(images)
        
        for i in range(0, total_images, IMAGES_PER_PAGE):
            # Start new page if not first page
            if page_count > 0:
                c.showPage()
            page_count += 1
            
            current_y = A4[1]
            batch = images[i:i+IMAGES_PER_PAGE]
            available_height = A4[1] - (VERTICAL_SPACING * (len(batch) - 1))
            img_height = available_height / len(batch)
            
            logger.info(f"Processing page {page_count} with {len(batch)} images")
            
            for j, img_path in enumerate(batch):
                try:
                    if not os.path.exists(img_path):
                        logger.warning(f"Skipping missing file: {img_path}")
                        continue
                        
                    with Image.open(img_path) as img:
                        img_width, img_height_orig = img.size
                        aspect = img_height_orig / img_width
                        
                        width = A4[0]
                        height = width * aspect
                        
                        if height > img_height:
                            height = img_height
                            width = height / aspect
                        
                        x_offset = (A4[0] - width) / 2
                        
                        logger.info(f"  - Image {j+1}: {width}x{height} at {current_y}")
                        
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
                    logger.error(f"Error processing image {img_path}: {e}")
                    logger.error(traceback.format_exc())
        
        c.save()
    
    # Get PDF data from buffer
    pdf_data = pdf_buffer.getvalue()
    pdf_buffer.close()
    
    if len(pdf_data) == 0:
        raise RuntimeError("Generated PDF is empty")
    
    logger.info(f"PDF generated successfully: {len(pdf_data)} bytes")
    return pdf_data

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    await message.reply(
        "🖼️ **Image to PDF Bot**\n\n"
        "• /begin - Create PDF with 3 images per page\n"
        "• /begin2 - Create PDF with 1 image per page\n"
        "• /stop - Finish & create PDF\n"
        "• /cancel - Cancel session\n\n"
        "Features: Full-width images, ordered, HQ",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_message(filters.command(["begin", "begin2"]))
async def start_session(client: Client, message: Message):
    user_id = message.from_user.id
    user_dir = TEMP_DIR / str(user_id)
    
    if user_dir.exists():
        shutil.rmtree(user_dir, ignore_errors=True)
    user_dir.mkdir(parents=True, exist_ok=True)
    
    # Determine mode from command
    mode = 'single' if message.command[0] == "begin2" else 'grid'
    
    sessions[user_id] = {
        "image_refs": [],
        "active": True,
        "dir": user_dir,
        "media_groups": set(),
        "sequence": 0,
        "mode": mode  # Store the PDF mode
    }
    
    mode_desc = "1 image per page" if mode == 'single' else "3 images per page"
    await message.reply(f"📸 Session started in {mode_desc} mode! Send images now. /stop when done.")

@app.on_message(filters.command("stop"))
async def stop_session(client: Client, message: Message):
    user_id = message.from_user.id
    session = sessions.get(user_id)
    
    if not session or not session["active"]:
        return await message.reply("❌ No active session! /begin or /begin2 to start.")
    
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
    
    # Process results in order
    downloaded_images = []
    for idx, result in enumerate(results):
        if not isinstance(result, Exception) and result is not None:
            downloaded_images.append(result)
        else:
            logger.error(f"Download failed for image {idx+1}: {result}")
            await progress_msg.reply(f"❌ Failed image {idx+1}. Skipping...")
    
    session["downloaded_images"] = downloaded_images
    success = len(session["downloaded_images"])
    
    await progress_msg.edit_text(
        f"✅ Downloaded {success}/{count} images!\n"
        "📝 Send PDF filename:"
    )
    session["waiting_for_name"] = True

async def download_and_process(message, user_dir, idx, progress_msg, total):
    try:
        # Update progress every 2 images for better feedback
        if (idx + 1) % 2 == 0 or (idx + 1) == total:
            await progress_msg.edit_text(f"⏳ Downloading {idx+1}/{total} images...")
        
        img_path = await robust_download(message, user_dir)
        
        # Run optimization in thread pool
        await asyncio.get_event_loop().run_in_executor(None, optimize_image, img_path)
        
        return img_path
    except Exception as e:
        logger.error(f"Image processing failed: {e}")
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
            session["downloaded_images"],
            session["mode"]  # Pass the mode to PDF generator
        )
        
        await pdf_progress.edit_text("✅ PDF created! Sending...")
        
        # Create temporary file for sending
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(pdf_data)
            tmp_path = tmp.name
        
        # Determine page count
        if session["mode"] == 'single':
            page_count = len(session["downloaded_images"])
        else:
            page_count = (len(session["downloaded_images"]) + IMAGES_PER_PAGE - 1) // IMAGES_PER_PAGE
        
        # Send PDF document
        await client.send_document(
            chat_id=user_id,
            document=tmp_path,
            file_name=f"{filename}.pdf",
            caption=(
                f"✅ PDF Generated\n"
                f"• Images: {len(session['downloaded_images'])}\n"
                f"• Pages: {page_count}\n"
                f"• Mode: {'1 image per page' if session['mode'] == 'single' else '3 images per page'}"
            ),
            parse_mode=enums.ParseMode.MARKDOWN
        )
        
        # Cleanup temporary file
        os.unlink(tmp_path)
    except Exception as e:
        logger.error(f"PDF creation failed: {e}")
        logger.error(traceback.format_exc())
        await message.reply("❌ PDF creation failed. Try /begin or /begin2 again.")
    finally:
        clean_session(user_id)

@app.on_message(filters.private & (filters.photo | filters.document | filters.media_group))
async def handle_image(client: Client, message: Message):
    user_id = message.from_user.id
    session = sessions.get(user_id)
    
    if not session or not session["active"]:
        return
    
    # Update sequence counter before processing
    session["sequence"] += 1
    seq = session["sequence"]
    
    if message.media_group_id:
        group_id = message.media_group_id
        
        if group_id in session["media_groups"]:
            return
        
        session["media_groups"].add(group_id)
        
        try:
            media_group = await client.get_media_group(user_id, message.id)
            
            # Process media group in order
            for msg in media_group:
                if is_image(msg):
                    session["image_refs"].append({
                        "message": msg,
                        "sequence": seq
                    })
                    seq += 1
            
            # Update global sequence counter
            session["sequence"] = seq
        except Exception as e:
            logger.error(f"Media group error: {e}")
            logger.error(traceback.format_exc())
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
            try:
                shutil.rmtree(user_dir, ignore_errors=True)
                logger.info(f"Cleaned session data for user {user_id}")
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
        del sessions[user_id]

def run_bot():
    while True:
        try:
            logger.info("Starting Telegram bot...")
            app.run()
        except Exception as e:
            logger.error(f"Bot crashed: {e}")
            logger.error(traceback.format_exc())
            logger.info("Restarting bot in 5s")
            time.sleep(5)

if __name__ == "__main__":
    logger.info("Starting PDF Converter Bot with Flask server...")
    run_bot()
