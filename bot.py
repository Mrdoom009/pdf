import os
import logging
import threading
import time
from pathlib import Path
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
import tempfile
import shutil
import asyncio
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
    logger.error("Missing API_ID, API_HASH, or BOT_TOKEN in environment variables!")
    exit(1)

# Temporary storage
TEMP_DIR = Path("user_data")
TEMP_DIR.mkdir(exist_ok=True)

# User session data
user_sessions = {}
session_lock = asyncio.Lock()  # Lock for session operations

# Create Flask app for web server
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "Telegram PDF Bot is running!"

@flask_app.route('/health')
def health_check():
    return "OK", 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=8000)

# Start Flask in a separate thread
flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()

# Pyrogram client with improved connection settings
app = Client(
    "pdf_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=100,
    sleep_threshold=60,
    in_memory=True
)

def is_valid_image(message: Message) -> bool:
    """Check if message contains a valid image"""
    if message.photo:
        return True
    if message.document:
        file_name = (message.document.file_name or "").lower()
        mime_type = (message.document.mime_type or "").lower()
        return (file_name.endswith(('.png', '.jpg', '.jpeg', '.webp'))) or \
               ('image' in mime_type)
    return False

async def download_image(message: Message, user_dir: Path) -> str:
    """Download image from message and return file path"""
    if message.photo:
        file_id = message.photo.file_id
        ext = ".jpg"
    else:
        file_id = message.document.file_id
        ext = Path(message.document.file_name).suffix.lower()
        if not ext or ext not in (".png", ".jpg", ".jpeg", ".webp"):
            ext = ".jpg"
    
    file_path = user_dir / f"{file_id}{ext}"
    await message.download(file_name=str(file_path))
    return str(file_path)

def generate_pdf(image_paths, output_path):
    """Generate PDF from images with dynamic layout"""
    page_width, page_height = A4
    margin = 15 * mm
    usable_width = page_width - 2 * margin
    usable_height = page_height - 2 * margin
    
    c = canvas.Canvas(output_path, pagesize=A4)
    current_y = page_height - margin
    page_count = 1
    
    for image_path in image_paths:
        try:
            with Image.open(image_path) as img:
                img_width, img_height = img.size
                aspect_ratio = img_height / img_width
                
                # Calculate scaled dimensions
                scaled_width = min(usable_width, img_width)
                scaled_height = scaled_width * aspect_ratio
                
                # Check if image fits in remaining space
                if scaled_height > (current_y - margin):
                    # Start new page
                    c.showPage()
                    current_y = page_height - margin
                    page_count += 1
                
                # Center image horizontally
                x_pos = margin + (usable_width - scaled_width) / 2
                
                # Draw image
                c.drawImage(
                    image_path,
                    x_pos,
                    current_y - scaled_height,
                    width=scaled_width,
                    height=scaled_height,
                    preserveAspectRatio=True,
                    mask='auto'
                )
                
                # Update vertical position
                current_y -= scaled_height
        except Exception as e:
            logger.error(f"Error processing image {image_path}: {e}")
            continue
    
    c.save()
    return page_count

def clean_user_data(user_id):
    """Remove user's temporary files"""
    user_dir = TEMP_DIR / str(user_id)
    if user_dir.exists():
        shutil.rmtree(user_dir, ignore_errors=True)
    if user_id in user_sessions:
        del user_sessions[user_id]

async def update_session_message(user_id, client: Client, text):
    """Update or create session status message"""
    try:
        if user_id in user_sessions and user_sessions[user_id].get("message_id"):
            await client.edit_message_text(
                chat_id=user_id,
                message_id=user_sessions[user_id]["message_id"],
                text=text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📦 Generate PDF", callback_data="generate")],
                    [InlineKeyboardButton("🔄 Reset Session", callback_data="reset")]
                ])
            )
        else:
            async with session_lock:
                if user_id in user_sessions:
                    new_msg = await client.send_message(
                        chat_id=user_id,
                        text=text,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("📦 Generate PDF", callback_data="generate")],
                            [InlineKeyboardButton("🔄 Reset Session", callback_data="reset")]
                        ])
                    )
                    user_sessions[user_id]["message_id"] = new_msg.id
    except Exception as e:
        logger.error(f"Error updating session message: {e}")

@app.on_message(filters.private & (filters.photo | filters.document | filters.media_group))
async def handle_image(client: Client, message: Message):
    """Handle incoming images and media groups"""
    # Skip if not valid image and not media group
    if not is_valid_image(message) and not message.media_group_id:
        return
    
    user_id = message.from_user.id
    user_dir = TEMP_DIR / str(user_id)
    user_dir.mkdir(exist_ok=True)
    
    # Initialize session
    async with session_lock:
        if user_id not in user_sessions:
            user_sessions[user_id] = {
                "images": [],
                "message_id": None,
                "processing": False,
                "media_groups": set()
            }
    
    # Skip if already processing
    if user_sessions[user_id].get("processing"):
        return
    
    # Handle media groups (albums)
    if message.media_group_id:
        group_id = message.media_group_id
        
        # Skip if we're already processing this media group
        if group_id in user_sessions[user_id]["media_groups"]:
            return
        
        # Mark this group as being processed
        user_sessions[user_id]["media_groups"].add(group_id)
        
        try:
            # Get all messages in the media group
            await asyncio.sleep(1)  # Wait for all group messages to arrive
            group_messages = await client.get_media_group(
                chat_id=message.chat.id,
                message_id=message.id
            )
            
            added_count = 0
            for msg in group_messages:
                if is_valid_image(msg):
                    try:
                        file_path = await download_image(msg, user_dir)
                        user_sessions[user_id]["images"].append(file_path)
                        added_count += 1
                    except Exception as e:
                        logger.error(f"Error downloading media group image: {e}")
            
            if added_count > 0:
                count = len(user_sessions[user_id]["images"])
                await update_session_message(
                    user_id,
                    client,
                    f"✅ Added {added_count} images from album! Total images: {count}\n"
                    "Press 'Generate PDF' when ready or add more images."
                )
            else:
                await message.reply_text("❌ No valid images found in the album.")
        
        except Exception as e:
            logger.error(f"Error processing media group: {e}")
            await message.reply_text("❌ Failed to process image album. Please try again.")
        finally:
            # Remove group from processing set
            if user_id in user_sessions:
                user_sessions[user_id]["media_groups"].discard(group_id)
        return
    
    # Handle single image
    try:
        file_path = await download_image(message, user_dir)
        user_sessions[user_id]["images"].append(file_path)
        count = len(user_sessions[user_id]["images"])
        
        await update_session_message(
            user_id,
            client,
            f"✅ Image added! Total images: {count}\n"
            "Press 'Generate PDF' when ready or add more images."
        )
    
    except Exception as e:
        logger.error(f"Error handling image: {e}")
        await message.reply_text("❌ Failed to process image. Please try again.")

@app.on_callback_query(filters.regex(r"^(generate|reset)$"))
async def handle_callback(client, callback_query):
    """Handle button clicks"""
    user_id = callback_query.from_user.id
    
    if user_id not in user_sessions:
        await callback_query.answer("Session expired! Please start over.", show_alert=True)
        return
    
    try:
        if callback_query.data == "generate":
            # Check if there are images
            if not user_sessions[user_id]["images"]:
                await callback_query.answer("No images to process!", show_alert=True)
                return
            
            # Set processing flag
            user_sessions[user_id]["processing"] = True
            await callback_query.answer("Generating PDF...")
            
            # Update status message
            await client.edit_message_text(
                chat_id=user_id,
                message_id=user_sessions[user_id]["message_id"],
                text="⏳ Generating PDF... Please wait"
            )
            
            # Generate PDF
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                pdf_path = tmp.name
            
            image_count = len(user_sessions[user_id]["images"])
            page_count = generate_pdf(user_sessions[user_id]["images"], pdf_path)
            
            # Send PDF
            await client.send_document(
                chat_id=user_id,
                document=pdf_path,
                caption=f"✅ PDF Generated!\n"
                        f"• Images processed: {image_count}\n"
                        f"• Pages created: {page_count}",
                file_name="converted_images.pdf"
            )
            
            # Clean up
            os.unlink(pdf_path)
            clean_user_data(user_id)
            
        elif callback_query.data == "reset":
            clean_user_data(user_id)
            await callback_query.answer("Session reset! You can start over.", show_alert=True)
    
    except Exception as e:
        logger.error(f"Callback error: {e}")
        await callback_query.answer("❌ Processing failed. Please try again.", show_alert=True)
    finally:
        # Reset processing flag
        if user_id in user_sessions:
            user_sessions[user_id]["processing"] = False

def run_bot():
    """Run the bot with restart capabilities"""
    while True:
        try:
            logger.info("Starting Telegram bot...")
            app.run()
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            break
        except Exception as e:
            logger.error(f"Bot crashed: {str(e)}")
            logger.info("Restarting bot in 5 seconds...")
            time.sleep(5)

if __name__ == "__main__":
    logger.info("Starting bot and web server...")
    run_bot()
