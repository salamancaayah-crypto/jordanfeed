import os
import sqlite3
import random
import string
import threading
import logging
import requests
import uvicorn
import time
import re
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
    # Migration: add instagram_username column if not present
    try:
        cursor.execute("ALTER TABLE mappings ADD COLUMN instagram_username TEXT")
        logger.info("Database migration: Added instagram_username column.")
    except sqlite3.OperationalError:
        pass
        
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
        
        # Proactively fetch and cache username in background thread
        threading.Thread(target=get_instagram_username, args=(igsid,), daemon=True).start()
        
        return chat_id
        
    conn.close()
    return None

def get_instagram_username(igsid):
    """Gets the Instagram username from DB cache or queries Graph API if not cached."""
    if not igsid:
        return ""
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Check cache first
    try:
        cursor.execute("SELECT instagram_username FROM mappings WHERE instagram_igsid = ?", (str(igsid),))
        row = cursor.fetchone()
        if row and row[0]:
            conn.close()
            return row[0]
    except Exception as e:
        logger.error(f"Error checking username cache: {e}")
        
    # Fetch from Graph API if not cached
    username = ""
    if META_ACCESS_TOKEN:
        if META_ACCESS_TOKEN.startswith("IGAA"):
            url = f"https://graph.instagram.com/v20.0/{igsid}"
        else:
            url = f"https://graph.facebook.com/v20.0/{igsid}"
            
        params = {
            "fields": "username",
            "access_token": META_ACCESS_TOKEN
        }
        try:
            logger.info(f"Querying Instagram username for IGSID {igsid}...")
            res = requests.get(url, params=params, timeout=10)
            if res.status_code == 200:
                username = res.json().get("username", "")
                if username:
                    # Update database cache
                    cursor.execute("UPDATE mappings SET instagram_username = ? WHERE instagram_igsid = ?", (username, str(igsid)))
                    conn.commit()
                    logger.info(f"Cached username '{username}' for IGSID {igsid}")
            else:
                logger.error(f"Failed to query username from Graph API: {res.text}")
        except Exception as e:
            logger.error(f"Error querying username from Graph API: {e}")
            
    conn.close()
    return username

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


def download_and_forward_media(
    url: str, 
    media_type: str, 
    telegram_chat_id: str, 
    index: int = None, 
    total: int = None,
    original_caption: str = "",
    instagram_username: str = ""
):
    """Downloads media from CDN and forwards to Telegram."""
    temp_filename = f"temp_media_{random.randint(1000, 9999)}"
    
    try:
        logger.info(f"Downloading media from Meta CDN: {url}")
        
        # Lookaside CDN URLs require authorization header
        headers = {}
        if "lookaside.fbsbx.com" in url and META_ACCESS_TOKEN:
            headers["Authorization"] = f"Bearer {META_ACCESS_TOKEN}"
            logger.info("Applying Meta Page Access Token authorization header for secure download.")
            
        response = requests.get(url, headers=headers, stream=True, timeout=30)
        
        if response.status_code != 200:
            logger.error(f"Failed to download media from CDN. HTTP Status: {response.status_code}")
            # Only send error message on Telegram if it's the first slide or a single media to avoid spam
            if index is None or index == 1:
                bot.send_message(telegram_chat_id, "❌ فشل تحميل الفيديو/المنشور من خوادم إنستغرام.")
            return

        # Check if we accidentally downloaded an HTML page (e.g. if the proxy failed or link was private)
        content_type = response.headers.get("Content-Type", "")
        if "text/html" in content_type:
            logger.error(f"Failed to download media: server returned HTML page instead of media stream. Content-Type: {content_type}")
            if index is None or index == 1:
                bot.send_message(telegram_chat_id, "❌ لا يمكن تحميل هذا المنشور/الريلز. قد يكون الحساب خاصاً (Private) أو الرابط غير صالح.")
            return

        # Determine extension based on headers or simple fallback
        content_type = response.headers.get("Content-Type", "")
        media_type_upper = media_type.upper()
        
        if "video" in content_type or media_type_upper in ["VIDEO", "IG_REEL", "REEL"]:
            temp_filename += ".mp4"
            is_video = True
        elif "image" in content_type or media_type_upper in ["IMAGE", "IMAGE_SHARE"]:
            temp_filename += ".jpg"
            is_video = False
        else:
            # Fallback based on content type detect
            if "video" in content_type:
                temp_filename += ".mp4"
                is_video = True
            elif "image" in content_type:
                temp_filename += ".jpg"
                is_video = False
            else:
                temp_filename += ".mp4"
                is_video = True

        with open(temp_filename, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        logger.info(f"Media downloaded successfully. Sending to Telegram chat {telegram_chat_id}...")
        
        caption_prefix = "🎞" if is_video else "🖼"
        caption_type_str = "فيديو الريلز" if is_video else "المنشور"
        if index is not None and total is not None:
            base_caption = f"{caption_prefix} إليك {caption_type_str} المطلوب ({index}/{total}):"
        else:
            base_caption = f"{caption_prefix} إليك {caption_type_str} المطلوب:"
            
        extra_parts = []
        if original_caption:
            extra_parts.append(original_caption.strip())
        if instagram_username:
            extra_parts.append(f"#{instagram_username}")
            
        if extra_parts:
            caption = f"{base_caption}\n\n" + "\n\n".join(extra_parts)
        else:
            caption = base_caption
            
        with open(temp_filename, "rb") as media_file:
            if is_video:
                bot.send_video(telegram_chat_id, media_file, caption=caption)
            else:
                bot.send_photo(telegram_chat_id, media_file, caption=caption)
                
        logger.info(f"Media forwarded successfully to Telegram.")
        
    except Exception as e:
        logger.error(f"Error during media download and forwarding: {e}")
        if index is None or index == 1:
            bot.send_message(telegram_chat_id, f"❌ حدث خطأ أثناء معالجة وإرسال الفيديو/المنشور: {e}")
    finally:
        # Clean up temp file
        if os.path.exists(temp_filename):
            os.remove(temp_filename)
            logger.info(f"Temporary file {temp_filename} deleted.")

def get_carousel_media_urls(media_id: str, token: str):
    """Queries Instagram Graph API to retrieve all media URLs for a carousel post."""
    if not token or not media_id:
        return []
    
    url = f"https://graph.facebook.com/v20.0/{media_id}"
    params = {
        "fields": "id,media_type,media_url,children{id,media_type,media_url}",
        "access_token": token
    }
    try:
        logger.info(f"Querying Graph API for media {media_id} details...")
        res = requests.get(url, params=params, timeout=15)
        if res.status_code == 200:
            data = res.json()
            media_type = data.get("media_type")
            
            if media_type == "CAROUSEL_ALBUM" and "children" in data:
                children_data = data["children"].get("data", [])
                urls = []
                for child in children_data:
                    child_url = child.get("media_url")
                    child_type = child.get("media_type") # IMAGE or VIDEO
                    if child_url:
                        urls.append((child_url, child_type))
                return urls
            else:
                # Single image or video
                parent_url = data.get("media_url")
                parent_type = data.get("media_type")
                if parent_url:
                    return [(parent_url, parent_type)]
        else:
            logger.error(f"Failed to query media details for {media_id}. HTTP Status: {res.status_code}, Response: {res.text}")
    except Exception as e:
        logger.error(f"Error querying media details for {media_id}: {e}")
    return []

def download_and_forward_carousel(
    urls_and_types, 
    telegram_chat_id: str,
    original_caption: str = "",
    instagram_username: str = ""
):
    """Downloads all media items of a carousel sequentially with a rate-limit sleep."""
    total = len(urls_and_types)
    logger.info(f"Starting sequential carousel download & forward for {total} items...")
    
    for idx, (url, m_type) in enumerate(urls_and_types):
        try:
            # Download and forward this item
            download_and_forward_media(
                url, 
                m_type, 
                telegram_chat_id, 
                idx + 1, 
                total,
                original_caption,
                instagram_username
            )
        except Exception as e:
            logger.error(f"Failed to forward carousel item {idx+1}/{total}: {e}")
        
        # Sleep 0.5 seconds between slides to rate limit Telegram API calls
        if idx < total - 1:
            logger.info("Sleeping 0.5s before next carousel item to rate-limit Telegram API...")
            time.sleep(0.5)

def extract_shortcode(url: str) -> str:
    """Extracts the Instagram shortcode from a Reel or Post URL."""
    # First try the corrected regex
    match = re.search(r'/(?:p|reel|tv|share/[rp]|reels)/([A-Za-z0-9_-]+)', url)
    if match:
        return match.group(1)
    
    # Fallback: clean up trailing parameters, trailing slashes, and look for last non-empty part
    clean_url = url.split('?')[0]
    parts = [p for p in clean_url.split('/') if p]
    if parts:
        last = parts[-1]
        if len(last) > 3:
            return last
    return ""

def resolve_via_proxy(url: str, domain: str = "vxinstagram.com"):
    """Resolves any public Instagram Reel or Post URL to its direct CDN media URLs and types using a proxy domain."""
    shortcode = extract_shortcode(url)
    if not shortcode:
        logger.warning(f"Could not extract shortcode from URL: {url}")
        return []
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    urls = []
    seen_final_urls = set()
    
    # Try index 1 to 10 for potential carousel
    for index in range(1, 11):
        post_type = "reel" if "reel" in url else "p"
        proxy_url = f"https://{domain}/{post_type}/{shortcode}/{index}/"
        
        try:
            logger.info(f"Querying {domain} for shortcode {shortcode} at index {index}...")
            res = requests.get(proxy_url, headers=headers, timeout=10)
            if res.status_code != 200:
                logger.info(f"{domain} returned status code {res.status_code} for index {index}. Stopping.")
                break
                
            text = res.text
            video_match = re.search(r'<meta[^>]*property="og:video"[^>]*content="([^"]+)"', text)
            image_match = re.search(r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"', text)
            
            media_url = ""
            media_type = ""
            if video_match:
                media_url = video_match.group(1)
                media_type = "VIDEO"
            elif image_match:
                media_url = image_match.group(1)
                media_type = "IMAGE"
                
            if not media_url:
                logger.info(f"No media meta tags found at index {index} on {domain}. Stopping.")
                break
                
            # Follow redirects with a HEAD request to detect duplicate content
            try:
                head_res = requests.head(media_url, headers=headers, allow_redirects=True, timeout=10)
                final_url = head_res.url
            except Exception as e:
                logger.error(f"HEAD request failed for {media_url}: {e}")
                final_url = media_url
                
            if final_url in seen_final_urls:
                logger.info(f"Duplicate media detected at index {index} (Final URL already seen). Stopping.")
                break
                
            seen_final_urls.add(final_url)
            urls.append((media_url, media_type))
            
            # For reels, we always stop after index 1
            if post_type == "reel":
                logger.info("Reel detected, stopping after index 1.")
                break
                
        except Exception as e:
            logger.error(f"Error resolving index {index} via {domain}: {e}")
            break
            
    return urls

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
                    
            else:
                # Extract media details if shared or pasted as text link
                media_url = ""
                att_type = ""
                media_id = ""
                original_caption = ""
                
                # Check if it is a shared attachment
                if attachments:
                    for attachment in attachments:
                        att_type = attachment.get("type")
                        payload = attachment.get("payload", {})
                        media_url = payload.get("url")
                        media_id = payload.get("ig_post_media_id") or payload.get("id") or payload.get("reel_video_id") or payload.get("video_id")
                        original_caption = payload.get("title", "")
                        break # Process the first media attachment
                
                # Check if it is a copy-pasted link in text message
                elif "instagram.com" in message_text:
                    url_match = re.search(r'(https?://[^\s]*instagram\.com/[^\s]*)', message_text)
                    if url_match:
                        media_url = url_match.group(1)
                        att_type = "link"
                
                if media_url:
                    # Check if sender is mapped to a Telegram Chat ID
                    telegram_chat_id = get_telegram_chat_id(sender_igsid)
                    
                    if telegram_chat_id:
                        # Fetch Instagram Username of the sender
                        instagram_username = get_instagram_username(sender_igsid)
                        
                        carousel_urls = []
                        
                        # 1. Try to resolve the URL using the proxy if it is a public instagram.com URL
                        if "instagram.com" in media_url:
                            try:
                                carousel_urls = resolve_via_proxy(media_url, "vxinstagram.com")
                                if not carousel_urls:
                                    logger.info("vxinstagram failed to resolve media. Trying fallback to ddinstagram...")
                                    carousel_urls = resolve_via_proxy(media_url, "ddinstagram.com")
                            except Exception as e:
                                logger.error(f"Error resolving instagram.com URL via proxy: {e}")
                        
                        # 2. Fallback to Facebook Graph API for Lookaside URLs if we have a media ID
                        if not carousel_urls and media_id and META_ACCESS_TOKEN:
                            try:
                                carousel_urls = get_carousel_media_urls(media_id, META_ACCESS_TOKEN)
                            except Exception as e:
                                logger.error(f"Error checking carousel for media_id {media_id} via Graph API: {e}")
                        
                        # Forward media to Telegram
                        if carousel_urls and len(carousel_urls) > 1:
                            logger.info(f"Forwarding shared carousel with {len(carousel_urls)} items from IGSID {sender_igsid} to Telegram Chat {telegram_chat_id}")
                            background_tasks.add_task(
                                download_and_forward_carousel,
                                carousel_urls,
                                telegram_chat_id,
                                original_caption,
                                instagram_username
                            )
                        else:
                            # Single media fallback
                            if carousel_urls:
                                url_to_use, type_to_use = carousel_urls[0]
                            else:
                                url_to_use, type_to_use = media_url, att_type
                                
                            logger.info(f"Forwarding single shared media of type {type_to_use} from IGSID {sender_igsid} to Telegram Chat {telegram_chat_id}")
                            background_tasks.add_task(
                                download_and_forward_media,
                                url_to_use,
                                type_to_use,
                                telegram_chat_id,
                                None,
                                None,
                                original_caption,
                                instagram_username
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
