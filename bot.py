import os
import logging
import asyncio
import tempfile
import shutil
import threading
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
    workers=100,
    sleep_threshold=120,  # Increased sleep threshold
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

async def download_image(message: Message, path: Path) -> Path:
    """Download image with original quality and ensure completion"""
    if message.photo:
        # For photos, use the largest available size
        file_id = message.photo.file_id
        ext = ".jpg"
    else:
        file_id = message.document.file_id
        ext = Path(message.document.file_name or "image").suffix or ".jpg"
    
    # Create a unique filename
    file_path = path / f"{int(time.time())}_{file_id}{ext}"
    
    # Download with retry mechanism
    for attempt in range(3):
        try:
            await message.download(file_name=str(file_path))
            # Verify file exists and has content
            if file_path.exists() and file_path.stat().st_size > 0:
                return file_path
        except Exception as e:
            logger.warning(f"Download attempt {attempt+1} failed: {e}")
            await asyncio.sleep(1)
    
    raise Exception("Failed to download image after 3 attempts")

def generate_hq_pdf(images: list, output_path: str) -> int:
    """Generate high-quality PDF with full-width images"""
    c = canvas.Canvas(output_path, pagesize=A4, pageCompression=0)
    page_count = 0
    current_y = PAGE_HEIGHT
    
    for img_path in images:
        try:
            # Verify image exists before processing
            if not img_path.exists():
                logger.warning(f"Skipping missing file: {img_path}")
                continue
                
            with Image.open(img_path) as img:
                # Calculate dimensions while maintaining aspect ratio
                img_width, img_height = img.size
                aspect = img_height / img_width
                scaled_width = PAGE_WIDTH
                scaled_height = scaled_width * aspect
                
                # Check if image fits on current page
                if scaled_height > current_y:
                    c.showPage()
                    page_count += 1
                    current_y = PAGE_HEIGHT
                
                # Draw image at full width with original quality
                c.drawImage(
                    ImageReader(img),   # Preserve original quality
                    0,                  # X position (full width)
                    current_y - scaled_height,  # Y position
                    width=scaled_width,
                    height=scaled_height,
                    preserveAspectRatio=True,
                    anchor='n',
                    mask='auto'
                )
                
                # Update vertical position
                current_y -= scaled_height
        except Exception as e:
            logger.error(f"Error processing {img_path}: {e}")
    
    # Save the last page
    if current_y < PAGE_HEIGHT:
        page_count += 1
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
        "4. Press 'Generate PDF' button\n\n"
        "I'll create a high-quality PDF with your images at full width!",
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
        "images": [],
        "active": True,
        "dir": user_dir,
        "media_groups": set()  # Track processed media groups
    }
    
    await message.reply(
        "📸 **Session started!**\n"
        "Send me images now. When done, send /stop\n\n"
        "_I'll maintain the order of your images in the PDF._",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_message(filters.command("stop"))
async def stop_session(client: Client, message: Message):
    """Stop image collection and show summary"""
    user_id = message.from_user.id
    if user_id not in sessions or not sessions[user_id]["active"]:
        await message.reply("❌ No active session! Send /begin to start.")
        return
    
    sessions[user_id]["active"] = False
    count = len(sessions[user_id]["images"])
    
    if count == 0:
        await message.reply("⚠️ No images received! Session canceled.")
        clean_session(user_id)
        return
    
    # Create confirmation buttons
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Cancel Session", callback_data="cancel")],
        [InlineKeyboardButton("📤 Generate PDF", callback_data="generate")]
    ])
    
    await message.reply(
        f"🛑 **Session stopped!**\n"
        f"• Images received: `{count}`\n\n"
        "Press **Generate PDF** to create your document\n"
        "or **Cancel Session** to start over.",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("generate"))
async def handle_generate(client, callback_query):
    """Handle PDF generation request"""
    user_id = callback_query.from_user.id
    if user_id not in sessions:
        await callback_query.answer("Session expired!", show_alert=True)
        return
    
    await callback_query.answer("Creating your PDF...")
    await callback_query.message.edit("🔄 **Creating high-quality PDF...**")
    
    try:
        # Generate PDF
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            pdf_path = tmp.name
        
        images = sessions[user_id]["images"]
        page_count = generate_hq_pdf(images, pdf_path)
        
        # Send PDF with custom filename
        await client.send_document(
            chat_id=user_id,
            document=pdf_path,
            file_name="Your_Document.pdf",
            caption=(
                f"✅ **PDF Generated!**\n"
                f"• Images: `{len(images)}`\n"
                f"• Pages: `{page_count}`\n"
                f"• Quality: High (Original Resolution)"
            ),
            parse_mode=enums.ParseMode.MARKDOWN
        )
        
        # Cleanup
        os.unlink(pdf_path)
        clean_session(user_id)
        
    except Exception as e:
        logger.error(f"PDF error: {e}")
        await callback_query.message.edit("❌ Failed to generate PDF. Please try /begin again.")
        clean_session(user_id)

@app.on_callback_query(filters.regex("cancel"))
async def handle_cancel(client, callback_query):
    """Handle session cancellation"""
    user_id = callback_query.from_user.id
    if user_id in sessions:
        clean_session(user_id)
    await callback_query.answer("Session canceled!", show_alert=True)
    await callback_query.message.edit("❌ Session canceled!")

@app.on_message(filters.private & (filters.photo | filters.document | filters.media_group))
async def handle_image(client: Client, message: Message):
    """Handle incoming images and media groups"""
    user_id = message.from_user.id
    
    # Validate session
    if user_id not in sessions or not sessions[user_id]["active"]:
        return
    
    # Handle media groups (albums)
    if message.media_group_id:
        group_id = message.media_group_id
        
        # Skip if we've already processed this group
        if group_id in sessions[user_id]["media_groups"]:
            return
        
        # Mark this media group as being processed
        sessions[user_id]["media_groups"].add(group_id)
        
        try:
            # Wait a bit for all parts of the media group to arrive
            await asyncio.sleep(2)
            media_group = await client.get_media_group(user_id, message.id)
            
            # Download each image in the group
            for msg in media_group:
                if is_image(msg):
                    try:
                        user_dir = sessions[user_id]["dir"]
                        img_path = await download_image(msg, user_dir)
                        sessions[user_id]["images"].append(img_path)
                    except Exception as e:
                        logger.error(f"Error downloading image from media group: {e}")
            
            # Update user on progress
            count = len(sessions[user_id]["images"])
            await message.reply(f"✅ Added {len(media_group)} images from album. Total: `{count}`",
                               parse_mode=enums.ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Media group error: {e}")
            await message.reply("❌ Failed to process image album. Please try again.")
        finally:
            # Remove group from processing set
            sessions[user_id]["media_groups"].discard(group_id)
        return
    
    # Handle single image
    if not is_image(message):
        return
    
    try:
        # Download image with original quality
        user_dir = sessions[user_id]["dir"]
        img_path = await download_image(message, user_dir)
        sessions[user_id]["images"].append(img_path)
        
        # Send quick confirmation
        count = len(sessions[user_id]["images"])
        if count % 5 == 0:  # Update every 5 images
            await message.reply(f"✅ Added `{count}` images so far...",
                               parse_mode=enums.ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Image error: {e}")
        await message.reply("❌ Failed to process image. Please try again.")

def clean_session(user_id):
    """Clean up user session"""
    if user_id in sessions:
        user_dir = sessions[user_id]["dir"]
        if user_dir.exists():
            shutil.rmtree(user_dir, ignore_errors=True)
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
