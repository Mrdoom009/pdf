import os
import logging
import asyncio
import tempfile
import shutil
from pathlib import Path
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from pyrogram import Client, filters, enums
from pyrogram.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    Message,
    CallbackQuery
)

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

# Create Pyrogram client with optimized settings
app = Client(
    "pdf_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=100,
    sleep_threshold=60,
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
    """Download image with original quality"""
    if message.photo:
        file_id = message.photo.file_id
        ext = ".jpg"
    else:
        file_id = message.document.file_id
        ext = Path(message.document.file_name or "image").suffix or ".jpg"
    
    file_path = path / f"{file_id}{ext}"
    await message.download(file_name=str(file_path))
    return file_path

def generate_high_quality_pdf(images: list, output_path: str) -> int:
    """Generate PDF with full-width images maintaining quality"""
    c = canvas.Canvas(output_path, pagesize=A4)
    page_count = 0
    current_y = PAGE_HEIGHT
    
    for img_path in images:
        try:
            with Image.open(img_path) as img:
                # Calculate dimensions while maintaining aspect ratio
                img_width, img_height = img.size
                aspect = img_height / img_width
                scaled_width = PAGE_WIDTH
                scaled_height = scaled_width * aspect
                
                # Check if image fits on current page
                if scaled_height > current_y:
                    # Start new page
                    c.showPage()
                    page_count += 1
                    current_y = PAGE_HEIGHT
                
                # Draw image at full width
                c.drawImage(
                    ImageReader(img),  # Preserve original quality
                    0,                 # X position (full width)
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
    
    c.save()
    return page_count + 1  # +1 for the last page

@app.on_message(filters.command("begin"))
async def start_session(client: Client, message: Message):
    """Start a new image collection session"""
    user_id = message.from_user.id
    user_dir = TEMP_DIR / str(user_id)
    
    # Clean previous session
    if user_dir.exists():
        shutil.rmtree(user_dir, ignore_errors=True)
    user_dir.mkdir()
    
    # Initialize session
    sessions[user_id] = {
        "images": [],
        "active": True,
        "dir": user_dir
    }
    
    await message.reply(
        "📸 <b>Session started!</b>\n"
        "Send me images now. When done, send /stop\n\n"
        "<i>I'll maintain the order of your images in the PDF.</i>",
        parse_mode=enums.ParseMode.HTML
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
    
    # Ask for PDF filename
    await message.reply(
        f"🛑 <b>Session stopped!</b>\n"
        f"• Images received: {count}\n\n"
        "Please send a name for your PDF file:",
        parse_mode=enums.ParseMode.HTML
    )
    
    # Set state to wait for filename
    sessions[user_id]["waiting_for_name"] = True

@app.on_message(filters.command("cancel"))
async def cancel_session(client: Client, message: Message):
    """Cancel current session"""
    user_id = message.from_user.id
    if user_id in sessions:
        clean_session(user_id)
    await message.reply("❌ Session canceled!")

@app.on_message(filters.private & (filters.photo | filters.document))
async def handle_image(client: Client, message: Message):
    """Handle incoming images"""
    user_id = message.from_user.id
    
    # Validate session and image
    if user_id not in sessions or not sessions[user_id]["active"]:
        return
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
            await message.reply(f"✅ Added {count} images so far...")
    except Exception as e:
        logger.error(f"Image error: {e}")
        await message.reply("❌ Failed to process image. Please try again.")

@app.on_message(filters.private & filters.text)
async def handle_filename(client: Client, message: Message):
    """Handle PDF filename input"""
    user_id = message.from_user.id
    if user_id not in sessions:
        return
    if not sessions[user_id].get("waiting_for_name"):
        return
    
    # Clean filename
    filename = message.text.strip()
    if not filename:
        await message.reply("⚠️ Please send a valid filename!")
        return
    if len(filename) > 50:
        filename = filename[:50]
    
    # Generate PDF
    try:
        await message.reply("🔄 <b>Creating high-quality PDF...</b>", 
                           parse_mode=enums.ParseMode.HTML)
        
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            pdf_path = tmp.name
        
        images = sessions[user_id]["images"]
        page_count = generate_high_quality_pdf(images, pdf_path)
        
        # Send PDF with custom filename
        await client.send_document(
            chat_id=user_id,
            document=pdf_path,
            file_name=f"{filename}.pdf",
            caption=(
                f"✅ <b>PDF Generated!</b>\n"
                f"• Images: {len(images)}\n"
                f"• Pages: {page_count}\n"
                f"• Quality: High (Original Resolution)"
            ),
            parse_mode=enums.ParseMode.HTML
        )
        
        # Cleanup
        os.unlink(pdf_path)
        clean_session(user_id)
        
    except Exception as e:
        logger.error(f"PDF error: {e}")
        await message.reply("❌ Failed to generate PDF. Please try /begin again.")
        clean_session(user_id)

def clean_session(user_id):
    """Clean up user session"""
    if user_id in sessions:
        user_dir = sessions[user_id]["dir"]
        if user_dir.exists():
            shutil.rmtree(user_dir, ignore_errors=True)
        del sessions[user_id]

if __name__ == "__main__":
    logger.info("Starting PDF Converter Bot...")
    app.run()
