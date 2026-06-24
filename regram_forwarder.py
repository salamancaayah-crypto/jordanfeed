import os
import sqlite3
import random
import string
import threading
import logging
import requests
import uvicorn
from fastapi import FastAPI, Request, Query, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse
import telebot

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("forwarder.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("InstagramTelegramForwarder")

# Load environment variables manually
def load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    parts = line.strip().split("=", 1)
                    if len(parts) == 2:
                        key, val = parts
                        os.environ[key.strip()] = val.strip().strip('"').strip("'").strip()

load_env()

# Config parameters
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "my_secret_verify_token")

try:
    import config_token
    META_ACCESS_TOKEN = getattr(config_token, "META_ACCESS_TOKEN", "")
except ImportError:
    META_ACCESS_TOKEN = ""

if not META_ACCESS_TOKEN:
    META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "")

PORT = int(os.environ.get("PORT", 8000))
HOST = os.environ.get("HOST", "0.0.0.0")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forwarder.db")

# ----------------- Database Setup -----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mappings (
            telegram_chat_id TEXT PRIMARY KEY,
            instagram_igsid TEXT,
            link_token TEXT,
            linked_at INTEGER
        )
    """)
    # Pre-insert user's mapping so it survives container restarts
    cursor.execute("""
        INSERT OR REPLACE INTO mappings (telegram_chat_id, instagram_igsid, link_token, linked_at)
        VALUES ('338725979', '814728531594388', 'REG-TJVE', 1)
    """)
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully with default mapping.")

init_db()

# DB Helper functions
def create_or_get_token(chat_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Check if user already exists
    cursor.execute("SELECT link_token, instagram_igsid FROM mappings WHERE telegram_chat_id = ?", (str(chat_id),))
    row = cursor.fetchone()
    
    if row:
        token, igsid = row
        conn.close()
        return token, igsid
        
    # Generate new token REG-XXXX
    random_str = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    token = f"REG-{random_str}"
    
    cursor.execute(
        "INSERT INTO mappings (telegram_chat_id, link_token, linked_at) VALUES (?, ?, ?)",
        (str(chat_id), token, int(threading.Event().is_set())) # Placeholder for timestamp
    )
    conn.commit()
    conn.close()
    logger.info(f"Generated new token {token} for Telegram Chat ID {chat_id}")
    return token, None

def link_instagram_account(token, igsid):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Find token
    cursor.execute("SELECT telegram_chat_id FROM mappings WHERE link_token = ?", (token.strip().upper(),))
    row = cursor.fetchone()
    
    if row:
        chat_id = row[0]
        cursor.execute(
            "UPDATE mappings SET instagram_igsid = ?, linked_at = ? WHERE link_token = ?",
            (igsid, int(threading.Event().is_set()), token.strip().upper())
        )
        conn.commit()
        conn.close()
        logger.info(f"Successfully linked IG {igsid} to Telegram Chat ID {chat_id}")
        return chat_id
        
    conn.close()
    return None

def get_telegram_chat_id(igsid):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_chat_id FROM mappings WHERE instagram_igsid = ?", (str(igsid),))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row[0]
    return None

# ----------------- Telegram Bot Setup -----------------
bot = telebot.TeleBot(TELEGRAM_TOKEN)

@bot.message_handler(commands=['start'])
def handle_start(message):
    chat_id = message.chat.id
    token, igsid = create_or_get_token(chat_id)
    
    if igsid:
        welcome_msg = (
            "✅ **حسابك مرتبط بالفعل!**\n\n"
            "يمكنك مشاركة أي ريلز (Reels)، صور، أو منشورات من تطبيق إنستغرام مباشرة "
            "إلى حساب البوت على إنستغرام، وسيصلك الفيديو هنا فوراً."
        )
    else:
        welcome_msg = (
            "👋 **أهلاً بك في بوت تحويل الريلز الشخصي!**\n\n"
            "لربط حسابك على إنستغرام وتفعيل التحميل التلقائي دون كلمات مرور، يرجى اتباع الخطوات التالية:\n\n"
            "1️⃣ افتح تطبيق إنستغرام وابحث عن حساب البوت الخاص بنا.\n"
            "2️⃣ أرسل رسالة خاصة (DM) تحتوي على هذا الكود تماماً:\n"
            f"`{token}`\n\n"
            "💡 بمجرد إرسال الكود، سيتم ربط حسابك وستتمكن من مشاركة أي فيديو ريلز مباشرة لحساب البوت لتصلك هنا!"
        )
    bot.send_message(chat_id, welcome_msg, parse_mode="Markdown")

# ----------------- Instagram Messaging API Helpers -----------------
def send_instagram_dm(recipient_igsid, text_message):
    if not META_ACCESS_TOKEN:
        logger.warning("META_ACCESS_TOKEN is not configured. Cannot send Instagram DM reply.")
        return
        
    if META_ACCESS_TOKEN.startswith("IGAA"):
        url = f"https://graph.instagram.com/v20.0/me/messages?access_token={META_ACCESS_TOKEN}"
    else:
        url = f"https://graph.facebook.com/v20.0/me/messages?access_token={META_ACCESS_TOKEN}"
    
    payload = {
        "recipient": {"id": recipient_igsid},
        "message": {"text": text_message}
    }
    try:
        res = requests.post(url, json=payload)
        if res.status_code == 200:
            logger.info(f"Sent Instagram DM reply to {recipient_igsid}")
        else:
            logger.error(f"Failed to send Instagram DM reply: {res.text}")
    except Exception as e:
        logger.error(f"Error sending Instagram DM: {e}")

# ----------------- FastAPI Webhook Server -----------------
app = FastAPI(title="Instagram Telegram Webhook Linker")

@app.on_event("startup")
def startup_event():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN is missing! Cannot start Telegram polling.")
        return
    # Start Telegram Polling in background thread
    tg_thread = threading.Thread(target=run_telegram_polling, daemon=True)
    tg_thread.start()


@app.get("/webhook", response_class=PlainTextResponse)
def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge")
):
    """Handles Meta's Webhook validation challenge."""
    if hub_mode == "subscribe" and hub_verify_token == META_VERIFY_TOKEN:
        logger.info("Webhook verification challenge PASSED.")
        return hub_challenge
    logger.warning("Webhook verification challenge FAILED. Verify Token mismatch.")
    raise HTTPException(status_code=403, detail="Verification token mismatch")

@app.get("/diagnostic/logs", response_class=PlainTextResponse)
def get_diagnostic_logs():
    log_file = "forwarder.log"
    if os.path.exists(log_file):
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return "".join(lines[-150:])
    return "Log file not found."


def download_and_forward_media(url: str, media_type: str, telegram_chat_id: str):
    """Downloads media from CDN and forwards to Telegram."""
    temp_filename = f"temp_media_{random.randint(1000, 9999)}"
    
    try:
        logger.info(f"Downloading media from Meta CDN: {url}")
        response = requests.get(url, stream=True, timeout=30)
        
        if response.status_code != 200:
            logger.error(f"Failed to download media from CDN. HTTP Status: {response.status_code}")
            bot.send_message(telegram_chat_id, "❌ فشل تحميل الفيديو من خوادم إنستغرام.")
            return

        # Determine extension based on headers or simple fallback
        content_type = response.headers.get("Content-Type", "")
        if "video" in content_type:
            temp_filename += ".mp4"
            is_video = True
        elif "image" in content_type:
            temp_filename += ".jpg"
            is_video = False
        else:
            # Fallback to mp4
            temp_filename += ".mp4"
            is_video = True

        with open(temp_filename, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        logger.info(f"Media downloaded successfully. Sending to Telegram chat {telegram_chat_id}...")
        
        with open(temp_filename, "rb") as media_file:
            if is_video:
                bot.send_video(telegram_chat_id, media_file, caption="🎞 إليك فيديو الريلز المطلب:")
            else:
                bot.send_photo(telegram_chat_id, media_file, caption="🖼 إليك المنشور المطلب:")
                
        logger.info(f"Media forwarded successfully to Telegram.")
        
    except Exception as e:
        logger.error(f"Error during media download and forwarding: {e}")
        bot.send_message(telegram_chat_id, f"❌ حدث خطأ أثناء معالجة وإرسال الفيديو: {e}")
    finally:
        # Clean up temp file
        if os.path.exists(temp_filename):
            os.remove(temp_filename)
            logger.info(f"Temporary file {temp_filename} deleted.")

@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    """Processes incoming Instagram Messaging events from Meta."""
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"Invalid JSON body received: {e}")
        return {"status": "error", "message": "Invalid JSON"}
        
    logger.info(f"Webhook received payload: {body}")
    
    if body.get("object") != "instagram":
        return {"status": "ignored"}
        
    for entry in body.get("entry", []):
        for messaging in entry.get("messaging", []):
            sender_igsid = messaging.get("sender", {}).get("id")
            message = messaging.get("message", {})
            
            if not sender_igsid or not message:
                continue
                
            message_text = message.get("text", "").strip()
            attachments = message.get("attachments", [])
            
            # Case 1: Account Linking via Token Code
            if message_text.upper().startswith("REG-"):
                logger.info(f"Received linking request with token {message_text} from IGSID {sender_igsid}")
                telegram_chat_id = link_instagram_account(message_text, sender_igsid)
                
                if telegram_chat_id:
                    # Notify Telegram user
                    bot.send_message(
                        telegram_chat_id,
                        "🎉 **تم ربط حساب إنستغرام الخاص بك بنجاح!**\n\n"
                        "الآن عند تصفح إنستغرام، قم بمشاركة (Share) أي فيديو ريلز "
                        "لحساب البوت على إنستغرام وسيتم إرساله إليك هنا تلقائياً."
                    )
                    # Reply back on Instagram DM
                    send_instagram_dm(sender_igsid, "✅ تم ربط الحساب بنجاح! تحقق من تيليجرام.")
                else:
                    # Fail reply on Instagram
                    send_instagram_dm(sender_igsid, "❌ الكود غير صحيح أو انتهت صلاحيته. يرجى التأكد من كتابته بشكل صحيح من بوت تيليجرام.")
                    
            # Case 2: Media Shared (Reels, Posts, Stories)
            elif attachments:
                for attachment in attachments:
                    att_type = attachment.get("type")
                    payload = attachment.get("payload", {})
                    media_url = payload.get("url")
                    
                    if media_url:
                        # Check if sender is mapped to a Telegram Chat ID
                        telegram_chat_id = get_telegram_chat_id(sender_igsid)
                        
                        if telegram_chat_id:
                            logger.info(f"Forwarding shared media of type {att_type} from IGSID {sender_igsid} to Telegram Chat {telegram_chat_id}")
                            background_tasks.add_task(
                                download_and_forward_media,
                                media_url,
                                att_type,
                                telegram_chat_id
                            )
                        else:
                            logger.warning(f"Received media from unlinked IGSID {sender_igsid}")
                            send_instagram_dm(
                                sender_igsid,
                                "⚠️ حسابك غير مرتبط ببوت تيليجرام بعد.\n\n"
                                "يرجى فتح بوت تيليجرام والحصول على كود الربط، ثم إرساله هنا لتتمكن من استخدام الخدمة."
                            )

    return {"status": "success"}

# ----------------- Runner -----------------
def run_telegram_polling():
    logger.info("Starting Telegram Bot Polling thread...")
    try:
        bot.infinity_polling()
    except Exception as e:
        logger.error(f"Telegram polling thread crashed: {e}")

if __name__ == "__main__":
    if not TELEGRAM_TOKEN:
        logger.critical("TELEGRAM_TOKEN is missing! Please configure it in .env file.")
        exit(1)
        
    # Start FastAPI server in main thread (startup event will trigger Telegram polling)
    logger.info(f"Starting FastAPI Webhook Server on {HOST}:{PORT}...")
    uvicorn.run(app, host=HOST, port=PORT)
