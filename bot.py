import os
import logging
import asyncio
import tempfile
import shutil
import threading
import math
import time
from pathlib import Path
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from pyrogram import Client, filters, enums
from pyrogram.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    Message
)
from flask import Flask
import aiofiles
import aiofiles.os

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
    logger.error("Missing API credentials!")
    exit(1)

# Session management
sessions = {}
PAGE_WIDTH, PAGE_HEIGHT = A4
TEMP_DIR = Path("user_data")
TEMP_DIR.mkdir(exist_ok=True)

# PDF layout configuration
IMAGES_PER_PAGE = 3
VERTICAL_SPACING = 20  # Points between images
TARGET_DPI = 150       # Balance quality and file size
MAX_CONCURRENT_DOWNLOADS = 8  # Optimal for most servers

# Create Flask app for web server
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "📸 Telegram PDF Bot is Running!"

@flask_app.route('/health')
def health_check():
    return "OK", 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=8000)

# Start Flask in a separate thread
flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()

# Create Pyrogram client with optimized settings
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
        mime = message.document.mime_type or ""
        return mime.startswith("image/")
    return False

async def robust_download(client: Client, message: Message, path: Path, max_retries=5) -> Path:
    """Robust image download with advanced error handling"""
    if message.photo:
        file_id = message.photo.file_id
        ext = ".jpg"
    else:
        file_id = message.document.file_id
        ext = Path(message.document.file_name or "image").suffix or ".jpg"
    
    # Create a unique filename
    file_path = path / f"{int(time.time())}_{file_id}{ext}"
    
    # Download with exponential backoff retry
    for attempt in range(max_retries):
        try:
            # Try downloading with timeout
            await asyncio.wait_for(
                client.download_media(message, file_name=str(file_path)),
                timeout=120  # 2 minute timeout
            )
            
            # Verify file exists and has content
            if await aiofiles.os.path.exists(file_path):
                file_size = (await aiofiles.os.stat(file_path)).st_size
                if file_size > 1024:  # At least 1KB
                    return file_path
                else:
                    logger.warning(f"Small file detected: {file_path} ({file_size} bytes)")
            
            # If file is invalid, delete it
            if await aiofiles.os.path.exists(file_path):
                await aiofiles.os.remove(file_path)
                
        except asyncio.TimeoutError:
            logger.warning(f"Download timeout (attempt {attempt+1}/{max_retries})")
        except Exception as e:
            logger.warning(f"Download attempt {attempt+1}/{max_retries} failed: {e}")
        
        # Exponential backoff: 1s, 2s, 4s, 8s, 16s
        await asyncio.sleep(2 ** attempt)
    
    raise Exception(f"Failed to download after {max_retries} attempts")

def optimize_image_size(img_path: Path):
    """Resize image to target DPI for PDF optimization"""
    try:
        with Image.open(img_path) as img:
            # Calculate max dimensions for A4 at target DPI
            max_width = int((PAGE_WIDTH / 72) * TARGET_DPI)
            max_height = int((PAGE_HEIGHT / 72) * TARGET_DPI)
            
            # Only resize if image is larger than target
            if img.width > max_width or img.height > max_height:
                img.thumbnail((max_width, max_height), Image.LANCZOS)
                img.save(img_path, quality=90, optimize=True)
    except Exception as e:
        logger.error(f"Image optimization error: {e}")

def generate_hq_pdf(images: list, output_path: str, progress_callback=None) -> int:
    """
    Generate high-quality PDF with 3 images per page
    - Images maintain original aspect ratio
    - Each image spans full page width
    - Vertical spacing between images
    - Page height adjusts to fit 3 images
    """
    c = canvas.Canvas(output_path, pagesize=A4)
    c.setPageCompression(1)  # Enable compression
    page_count = 0
    total_images = len(images)
    
    # Process images in batches of IMAGES_PER_PAGE
    for i in range(0, total_images, IMAGES_PER_PAGE):
        # Start a new page only if not the first page
        if page_count > 0:
            c.showPage()
        page_count += 1
        
        current_y = PAGE_HEIGHT
        batch = images[i:i+IMAGES_PER_PAGE]
        
        # Calculate total height needed for this batch
        total_height = 0
        aspect_ratios = []
        
        for img_path in batch:
            try:
                if img_path.exists():
                    with Image.open(img_path) as img:
                        aspect_ratios.append(img.height / img.width)
            except:
                aspect_ratios.append(1.0)  # Default aspect ratio
        
        # Calculate available height per image
        available_height = PAGE_HEIGHT - (VERTICAL_SPACING * (len(batch) - 1))
        height_per_image = available_height / len(batch)
        
        # Draw each image in this page
        for idx, img_path in enumerate(batch):
            try:
                # Verify image exists
                if not img_path.exists():
                    logger.warning(f"Skipping missing file: {img_path}")
                    continue
                
                with Image.open(img_path) as img:
                    # Get aspect ratio
                    aspect = aspect_ratios[idx]
                    
                    # Calculate scaled dimensions
                    scaled_width = PAGE_WIDTH
                    scaled_height = scaled_width * aspect
                    
                    # Adjust height to fit allocated space
                    if scaled_height > height_per_image:
                        scaled_height = height_per_image
                        scaled_width = scaled_height / aspect
                    
                    # Center horizontally
                    x_offset = (PAGE_WIDTH - scaled_width) / 2
                    
                    # Draw image with original quality
                    c.drawImage(
                        ImageReader(img),
                        x_offset,
                        current_y - scaled_height,
                        width=scaled_width,
                        height=scaled_height,
                        preserveAspectRatio=True,
                        mask='auto'
                    )
                    
                    # Move down for next image
                    current_y -= scaled_height + VERTICAL_SPACING
                    
            except Exception as e:
                logger.error(f"Error processing {img_path}: {e}")
        
        # Update progress after each page
        if progress_callback:
            processed = min(i + IMAGES_PER_PAGE, total_images)
            progress_callback(processed, total_images)
    
    c.save()
    return page_count

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    """Send welcome message"""
    await message.reply(
        "🖼️ **Image to PDF Converter**\n\n"
        "1. Send /begin to start a session\n"
        "2. Send your images (photos or documents)\n"
        "3. Send /stop when done\n"
        "4. I'll download all images and create your PDF\n\n"
        "Features:\n"
        "- 3 images per page with proper spacing\n"
        "- Full-width images\n"
        "- Optimized PDF size with good quality\n"
        "- Custom PDF filename",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_message(filters.command("begin"))
async def start_session(client: Client, message: Message):
    """Start a new image collection session"""
    user_id = message.from_user.id
    user_dir = TEMP_DIR / str(user_id)
    
    # Clean previous session
    if user_dir.exists():
        shutil.rmtree(user_dir, ignore_errors=True)
    user_dir.mkdir(parents=True, exist_ok=True)
    
    # Initialize session
    sessions[user_id] = {
        "image_refs": [],  # Store message references
        "active": True,
        "dir": user_dir,
        "media_groups": set(),
        "sequence": 0  # Maintain global sequence counter
    }
    
    await message.reply(
        "📸 **Session started!**\n"
        "Send me images now. When done, send /stop\n\n"
        "_I'll maintain the order of your images in the PDF._",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_message(filters.command("stop"))
async def stop_session(client: Client, message: Message):
    """Stop image collection and download images"""
    user_id = message.from_user.id
    if user_id not in sessions or not sessions[user_id]["active"]:
        await message.reply("❌ No active session! Send /begin to start.")
        return
    
    sessions[user_id]["active"] = False
    count = len(sessions[user_id]["image_refs"])
    
    if count == 0:
        await message.reply("⚠️ No images received! Session canceled.")
        clean_session(user_id)
        return
    
    # Start downloading images with progress updates
    progress_msg = await message.reply(f"⏳ Downloading 0/{count} images...")
    sessions[user_id]["downloaded_images"] = []
    sessions[user_id]["progress_msg"] = progress_msg
    
    # Sort images by sequence number to maintain order
    sorted_refs = sorted(sessions[user_id]["image_refs"], key=lambda x: x["sequence"])
    
    # Download images with enhanced concurrency control
    download_tasks = []
    for idx, img_ref in enumerate(sorted_refs):
        task = asyncio.create_task(
            process_image(
                client,
                img_ref["message"],
                sessions[user_id]["dir"],
                idx,
                progress_msg,
                count
            )
        )
        download_tasks.append(task)
    
    # Wait for all downloads to complete
    results = await asyncio.gather(*download_tasks, return_exceptions=True)
    
    # Process results in order
    downloaded_images = []
    for idx, result in enumerate(results):
        if not isinstance(result, Exception) and result is not None:
            downloaded_images.append((idx, result))
        else:
            logger.error(f"Download failed for image {idx+1}: {result}")
            await progress_msg.reply(f"❌ Failed to download image {idx+1}. Skipping...")
    
    # Sort by original index and extract paths
    downloaded_images.sort(key=lambda x: x[0])
    sessions[user_id]["downloaded_images"] = [img[1] for img in downloaded_images]
    
    # Final progress update
    success_count = len(sessions[user_id]["downloaded_images"])
    await progress_msg.edit_text(
        f"✅ Downloaded {success_count}/{count} images successfully!\n\n"
        "Please send a name for your PDF file:"
    )
    
    # Set state to wait for PDF name
    sessions[user_id]["waiting_for_name"] = True

async def process_image(client: Client, message: Message, user_dir: Path, idx: int, progress_msg: Message, total: int):
    """Download and process a single image with progress updates"""
    try:
        # Download image with robust mechanism
        img_path = await robust_download(client, message, user_dir)
        
        # Optimize image size for PDF
        await asyncio.get_event_loop().run_in_executor(
            None, 
            optimize_image_size, 
            img_path
        )
        
        # Update progress every 5 images or when significant
        if (idx + 1) % 5 == 0 or (idx + 1) == total:
            await progress_msg.edit_text(f"⏳ Downloading {idx+1}/{total} images...")
        
        return img_path
    except Exception as e:
        logger.error(f"Download error for image {idx+1}: {e}")
        return e  # Return exception for handling

@app.on_message(filters.command("cancel"))
async def cancel_session(client: Client, message: Message):
    """Cancel current session"""
    user_id = message.from_user.id
    if user_id in sessions:
        clean_session(user_id)
    await message.reply("❌ Session canceled!")

@app.on_message(filters.private & filters.text)
async def handle_text(client: Client, message: Message):
    """Handle PDF filename input"""
    user_id = message.from_user.id
    if user_id not in sessions:
        return
    
    # Handle PDF filename
    if sessions[user_id].get("waiting_for_name"):
        # Clean filename
        filename = message.text.strip()
        if not filename:
            await message.reply("⚠️ Please send a valid filename!")
            return
        if len(filename) > 50:
            filename = filename[:50]
        
        # Generate PDF with progress
        try:
            # Create progress message
            pdf_progress_msg = await message.reply("🔄 Creating your PDF... 0%")
            
            # Progress callback function
            def pdf_progress_callback(processed, total):
                percent = int((processed / total) * 100)
                asyncio.run_coroutine_threadsafe(
                    pdf_progress_msg.edit_text(f"🔄 Creating your PDF... {percent}%"),
                    app.loop
                )
            
            # Create PDF
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                pdf_path = tmp.name
            
            images = sessions[user_id]["downloaded_images"]
            
            # Create PDF with progress updates
            page_count = generate_hq_pdf(
                images, 
                pdf_path,
                progress_callback=pdf_progress_callback
            )
            
            # Final progress update
            await pdf_progress_msg.edit_text("✅ PDF created! Sending now...")
            
            # Send PDF with custom filename
            await client.send_document(
                chat_id=user_id,
                document=pdf_path,
                file_name=f"{filename}.pdf",
                caption=(
                    f"✅ **PDF Generated!**\n"
                    f"• Images: `{len(images)}`\n"
                    f"• Pages: `{page_count}`\n"
                    f"• Layout: Up to 3 images per page with spacing"
                ),
                parse_mode=enums.ParseMode.MARKDOWN
            )
            
            # Cleanup
            os.unlink(pdf_path)
            clean_session(user_id)
            
        except Exception as e:
            logger.error(f"PDF error: {e}")
            await message.reply("❌ Failed to generate PDF. Please try /begin again.")
            clean_session(user_id)

@app.on_message(filters.private & (filters.photo | filters.document | filters.media_group))
async def handle_image(client: Client, message: Message):
    """Handle incoming images and media groups"""
    user_id = message.from_user.id
    
    # Validate session
    if user_id not in sessions or not sessions[user_id]["active"]:
        return
    
    # Update sequence counter
    sessions[user_id]["sequence"] += 1
    seq = sessions[user_id]["sequence"]
    
    # Handle media groups (albums)
    if message.media_group_id:
        group_id = message.media_group_id
        
        # Skip if we've already processed this group
        if group_id in sessions[user_id]["media_groups"]:
            return
        
        # Mark this media group as being processed
        sessions[user_id]["media_groups"].add(group_id)
        
        try:
            # Get media group immediately without delay
            media_group = await client.get_media_group(user_id, message.id)
            
            # Store each image in the group in sequence order
            for msg in media_group:
                if is_image(msg):
                    sessions[user_id]["image_refs"].append({
                        "message": msg,
                        "type": "photo" if msg.photo else "document",
                        "sequence": seq
                    })
                    seq += 1  # Increment sequence for each image in group
            
            # Update global sequence counter
            sessions[user_id]["sequence"] = seq
        except Exception as e:
            logger.error(f"Media group error: {e}")
            await message.reply("⚠️ Failed to process image album. Some images might be missing.")
        finally:
            # Remove group from processing set
            sessions[user_id]["media_groups"].discard(group_id)
        return
    
    # Handle single image
    if not is_image(message):
        return
    
    try:
        # Store image reference (download later)
        sessions[user_id]["image_refs"].append({
            "message": message,
            "type": "photo" if message.photo else "document",
            "sequence": seq
        })
    except Exception as e:
        logger.error(f"Image error: {e}")

async def clean_session(user_id):
    """Clean up user session asynchronously"""
    if user_id in sessions:
        user_dir = sessions[user_id].get("dir")
        if user_dir and await aiofiles.os.path.exists(user_dir):
            await aiofiles.os.rmtree(user_dir, ignore_errors=True)
        del sessions[user_id]

def run_bot():
    """Run the bot with restart capabilities"""
    while True:
        try:
            logger.info("Starting Telegram bot...")
            app.run()
        except Exception as e:
            logger.error(f"Bot crashed: {e}")
            logger.info("Restarting bot in 5 seconds...")
            time.sleep(5)

if __name__ == "__main__":
    logger.info("Starting PDF Converter Bot with Flask server...")
    run_bot()
