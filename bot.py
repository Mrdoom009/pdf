import os
import logging
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
import tempfile
import shutil

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot setup
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Temporary storage
TEMP_DIR = Path("user_data")
TEMP_DIR.mkdir(exist_ok=True)

# User session data
user_sessions = {}

app = Client("pdf_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

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
                preserveAspectRatio=True
            )
            
            # Update vertical position
            current_y -= scaled_height
    
    c.save()
    return page_count

def clean_user_data(user_id):
    """Remove user's temporary files"""
    user_dir = TEMP_DIR / str(user_id)
    if user_dir.exists():
        shutil.rmtree(user_dir)
    if user_id in user_sessions:
        del user_sessions[user_id]

# FIX: Updated image filter for Pyrofork compatibility
@app.on_message(filters.photo | (filters.document & filters.regex(r'\.(jpg|jpeg|png)$')))
async def handle_image(client: Client, message: Message):
    """Handle incoming images"""
    user_id = message.from_user.id
    user_dir = TEMP_DIR / str(user_id)
    user_dir.mkdir(exist_ok=True)
    
    # Initialize session
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "images": [],
            "processing": False
        }
    
    # Skip if already processing
    if user_sessions[user_id]["processing"]:
        return
    
    try:
        # Download image
        if message.photo:
            file_id = message.photo.file_id
            ext = ".jpg"
        else:
            file_id = message.document.file_id
            ext = Path(message.document.file_name).suffix.lower()
            if ext not in (".jpg", ".jpeg", ".png"):
                return
        
        file_path = user_dir / f"{file_id}{ext}"
        await message.download(file_name=str(file_path))
        
        # Add to user's image list
        user_sessions[user_id]["images"].append(str(file_path))
        
        # Send confirmation with buttons
        count = len(user_sessions[user_id]["images"])
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📦 Generate PDF", callback_data="generate")],
            [InlineKeyboardButton("🔄 Reset Session", callback_data="reset")]
        ])
        await message.reply_text(
            f"✅ Image added! Total images: {count}\n"
            "Press 'Generate PDF' when ready or add more images.",
            reply_markup=keyboard
        )
    
    except Exception as e:
        logger.error(f"Error handling image: {e}")
        await message.reply_text("❌ Failed to process image. Please try again.")

@app.on_callback_query()
async def handle_callback(client, callback_query):
    """Handle button clicks"""
    user_id = callback_query.from_user.id
    data = callback_query.data
    
    if user_id not in user_sessions:
        await callback_query.answer("Session expired! Please start over.")
        return
    
    try:
        if data == "generate":
            # Check if there are images
            if not user_sessions[user_id]["images"]:
                await callback_query.answer("No images to process!", show_alert=True)
                return
            
            # Set processing flag
            user_sessions[user_id]["processing"] = True
            await callback_query.answer("Generating PDF...")
            
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
                        f"• Pages created: {page_count}"
            )
            
            # Clean up
            os.unlink(pdf_path)
            clean_user_data(user_id)
            
        elif data == "reset":
            clean_user_data(user_id)
            await callback_query.answer("Session reset!", show_alert=True)
    
    except Exception as e:
        logger.error(f"Callback error: {e}")
        await callback_query.answer("❌ Processing failed. Please try again.")
    finally:
        # Reset processing flag
        if user_id in user_sessions:
            user_sessions[user_id]["processing"] = False

if __name__ == "__main__":
    logger.info("Starting bot...")
    app.run()
