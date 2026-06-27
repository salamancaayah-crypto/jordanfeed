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
import psycopg2
from psycopg2 import pool


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

# Cache to store the last caption shared by each user to handle carousel items (like unsupported_type videos) that lack captions in subsequent webhooks
LAST_CAPTIONS_CACHE = {}
LAST_CAPTIONS_CACHE_LOCK = threading.Lock()

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
DATABASE_URL = os.environ.get("DATABASE_URL")
IS_POSTGRES = DATABASE_URL is not None and DATABASE_URL.strip() != ""
PLACEHOLDER = "%s" if IS_POSTGRES else "?"

if IS_POSTGRES:
    try:
        connection_pool = pool.SimpleConnectionPool(
            1, 5,
            DATABASE_URL,
            connect_timeout=10
        )
        logger.info("Neon PostgreSQL connection pool initialized.")
    except Exception as e:
        logger.critical(f"Failed to initialize PostgreSQL connection pool: {e}")
        raise e
else:
    connection_pool = None

def get_db_connection():
    if IS_POSTGRES and connection_pool:
        return connection_pool.getconn()
    else:
        return sqlite3.connect(DB_PATH, timeout=30.0)

def release_db_connection(conn):
    if IS_POSTGRES and connection_pool:
        connection_pool.putconn(conn)
    else:
        conn.close()

def init_db():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if IS_POSTGRES:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS mappings (
                    telegram_chat_id TEXT PRIMARY KEY,
                    instagram_igsid TEXT,
                    link_token TEXT,
                    linked_at INTEGER,
                    instagram_username TEXT
                )
            """)
        else:
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

        # Create follows table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS follows (
                telegram_chat_id TEXT,
                instagram_username TEXT,
                last_shortcode TEXT,
                PRIMARY KEY (telegram_chat_id, instagram_username)
            )
        """)
        logger.info("Database: Created/verified 'follows' table.")
            
        # Pre-insert user's mapping so it survives container restarts
        if IS_POSTGRES:
            cursor.execute("""
                INSERT INTO mappings (telegram_chat_id, instagram_igsid, link_token, linked_at)
                VALUES ('338725979', '814728531594388', 'REG-TJVE', 1)
                ON CONFLICT (telegram_chat_id) DO UPDATE SET
                    instagram_igsid = EXCLUDED.instagram_igsid,
                    link_token = EXCLUDED.link_token,
                    linked_at = EXCLUDED.linked_at
            """)
        else:
            cursor.execute("""
                INSERT OR REPLACE INTO mappings (telegram_chat_id, instagram_igsid, link_token, linked_at)
                VALUES ('338725979', '814728531594388', 'REG-TJVE', 1)
            """)
        conn.commit()
    finally:
        release_db_connection(conn)
    logger.info("Database initialized successfully.")

init_db()

# DB Helper functions
def create_or_get_token(chat_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Check if user already exists
        cursor.execute(f"SELECT link_token, instagram_igsid FROM mappings WHERE telegram_chat_id = {PLACEHOLDER}", (str(chat_id),))
        row = cursor.fetchone()
        
        if row:
            token, igsid = row
            return token, igsid
            
        # Generate new token REG-XXXX
        random_str = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        token = f"REG-{random_str}"
        
        cursor.execute(
            f"INSERT INTO mappings (telegram_chat_id, link_token, linked_at) VALUES ({PLACEHOLDER}, {PLACEHOLDER}, {PLACEHOLDER})",
            (str(chat_id), token, int(threading.Event().is_set())) # Placeholder for timestamp
        )
        conn.commit()
        logger.info(f"Generated new token {token} for Telegram Chat ID {chat_id}")
        return token, None
    finally:
        release_db_connection(conn)

def link_instagram_account(token, igsid):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Find token
        cursor.execute(f"SELECT telegram_chat_id FROM mappings WHERE link_token = {PLACEHOLDER}", (token.strip().upper(),))
        row = cursor.fetchone()
        
        if row:
            chat_id = row[0]
            cursor.execute(
                f"UPDATE mappings SET instagram_igsid = {PLACEHOLDER}, linked_at = {PLACEHOLDER} WHERE link_token = {PLACEHOLDER}",
                (igsid, int(threading.Event().is_set()), token.strip().upper())
            )
            conn.commit()
            logger.info(f"Successfully linked IG {igsid} to Telegram Chat ID {chat_id}")
            return chat_id
            
        return None
    finally:
        release_db_connection(conn)

# Auto-tracking DB helper functions
def follow_user(chat_id, username, last_shortcode):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cleaned_username = username.strip().lower().lstrip('@')
        if IS_POSTGRES:
            cursor.execute("""
                INSERT INTO follows (telegram_chat_id, instagram_username, last_shortcode)
                VALUES (%s, %s, %s)
                ON CONFLICT (telegram_chat_id, instagram_username)
                DO UPDATE SET last_shortcode = EXCLUDED.last_shortcode
            """, (str(chat_id), cleaned_username, last_shortcode))
        else:
            cursor.execute("""
                INSERT OR REPLACE INTO follows (telegram_chat_id, instagram_username, last_shortcode)
                VALUES (?, ?, ?)
            """, (str(chat_id), cleaned_username, last_shortcode))
        conn.commit()
        logger.info(f"DB: Chat {chat_id} followed {cleaned_username} starting at shortcode {last_shortcode}")
    finally:
        release_db_connection(conn)

def unfollow_user_db(chat_id, username):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cleaned_username = username.strip().lower().lstrip('@')
        cursor.execute(
            f"DELETE FROM follows WHERE telegram_chat_id = {PLACEHOLDER} AND instagram_username = {PLACEHOLDER}",
            (str(chat_id), cleaned_username)
        )
        conn.commit()
        logger.info(f"DB: Chat {chat_id} unfollowed {cleaned_username}")
    finally:
        release_db_connection(conn)

def get_followed_users(chat_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT instagram_username FROM follows WHERE telegram_chat_id = {PLACEHOLDER}", (str(chat_id),))
        rows = cursor.fetchall()
        return [row[0] for row in rows]
    finally:
        release_db_connection(conn)

def get_follow_count(chat_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM follows WHERE telegram_chat_id = {PLACEHOLDER}", (str(chat_id),))
        row = cursor.fetchone()
        return row[0] if row else 0
    finally:
        release_db_connection(conn)

def update_last_shortcode(chat_id, username, shortcode):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cleaned_username = username.strip().lower().lstrip('@')
        cursor.execute(
            f"UPDATE follows SET last_shortcode = {PLACEHOLDER} WHERE telegram_chat_id = {PLACEHOLDER} AND instagram_username = {PLACEHOLDER}",
            (shortcode, str(chat_id), cleaned_username)
        )
        conn.commit()
        logger.info(f"DB: Updated last_shortcode for chat {chat_id}, user {cleaned_username} to {shortcode}")
    finally:
        release_db_connection(conn)

def get_all_subscriptions():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_chat_id, instagram_username, last_shortcode FROM follows")
        return cursor.fetchall()
    finally:
        release_db_connection(conn)




def get_creator_username(shortcode: str) -> str:
    """Queries toinstagram.com proxy to extract the original creator's Instagram username."""
    if not shortcode:
        return ""
        
    url = f"https://toinstagram.com/p/{shortcode}/"
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    try:
        logger.info(f"Querying toinstagram.com to resolve creator username for shortcode {shortcode}...")
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            html = res.text
            
            # Method 1: Extract from the public URL path in the HTML (most robust and standard)
            match = re.search(r'instagram\.com/([A-Za-z0-9_.-]+)/(?:p|reel|tv|reels)/', html)
            if match:
                username = match.group(1).strip()
                if username not in ["p", "reel", "reels", "tv", "explore", "developer", "about", "legal", "terms", "privacy", "share"]:
                    logger.info(f"Resolved creator username '{username}' from URL pattern for shortcode {shortcode}")
                    return username
                    
            # Method 2: Extract from "username on Date" meta description
            match = re.search(r'content="([A-Za-z0-9_.-]+)\s+on\s+[A-Z][a-z]+\s+\d+', html)
            if match:
                username = match.group(1).strip()
                logger.info(f"Resolved creator username '{username}' from meta description date for shortcode {shortcode}")
                return username

            # Method 3: "likes, comments - username on Date"
            match = re.search(r'comments\s*-\s*([A-Za-z0-9_.-]+)\s+on\s+', html)
            if match:
                username = match.group(1).strip()
                logger.info(f"Resolved creator username '{username}' from comments snippet for shortcode {shortcode}")
                return username
            
            # Method 4: "username on Instagram"
            match = re.search(r'content="([A-Za-z0-9_.-]+)\s+on\s+Instagram', html)
            if match:
                username = match.group(1).strip()
                logger.info(f"Resolved creator username '{username}' from Instagram meta for shortcode {shortcode}")
                return username
                
        else:
            logger.warning(f"toinstagram.com returned status {res.status_code} for shortcode {shortcode}")
    except Exception as e:
        logger.error(f"Error resolving creator username for shortcode {shortcode} via toinstagram.com: {e}")
        
    return ""

def get_telegram_chat_id(igsid):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT telegram_chat_id FROM mappings WHERE instagram_igsid = {PLACEHOLDER}", (str(igsid),))
        row = cursor.fetchone()
        if row:
            return row[0]
        return None
    finally:
        release_db_connection(conn)


# ----------------- Telegram Bot Setup -----------------
bot = telebot.TeleBot(TELEGRAM_TOKEN)
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID", "338725979")

def is_allowed_user(message):
    chat_id = str(message.chat.id)
    if chat_id != ALLOWED_CHAT_ID:
        logger.warning(f"Unauthorized access attempt by Chat ID {chat_id}")
        try:
            bot.send_message(message.chat.id, "⚠️ هذا البوت شخصي ومغلق للاستخدام العام.")
        except Exception as e:
            logger.error(f"Failed to send unauthorized warning: {e}")
        return False
    return True

RSS_BRIDGE_INSTANCES = [
    "https://rss-bridge.sans-nuage.fr",
    "https://rss-bridge.org/bridge01",
    "https://rss-bridge.cheredeprince.net",
    "https://rss-bridge.lewd.tech"
]

def fetch_rss_feed(username):
    cleaned_username = username.strip().lower().lstrip('@')
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    for instance in RSS_BRIDGE_INSTANCES:
        url = f"{instance}/?action=display&bridge=InstagramBridge&u={cleaned_username}&format=Json"
        try:
            logger.info(f"Fetching RSS feed for '{cleaned_username}' from {instance}...")
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if "items" in data:
                    logger.info(f"Successfully fetched feed from {instance} for '{cleaned_username}' with {len(data['items'])} items.")
                    return data["items"]
            logger.warning(f"Instance {instance} returned status code {res.status_code} for user '{cleaned_username}'")
        except Exception as e:
            logger.error(f"Error fetching from instance {instance} for user '{cleaned_username}': {e}")
            
    logger.error(f"All RSS-Bridge instances failed to fetch feed for user '{cleaned_username}'.")
    return None

@bot.message_handler(commands=['start'])
def handle_start(message):
    if not is_allowed_user(message):
        return
        
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

@bot.message_handler(commands=['follow'])
def handle_follow(message):
    if not is_allowed_user(message):
        return
        
    chat_id = message.chat.id
    
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(chat_id, "💡 طريقة الاستخدام:\n`/follow username [channel_username_or_id]`", parse_mode="Markdown")
        return
        
    username = parts[1].strip()
    # Basic validation
    if not re.match(r'^[A-Za-z0-9_.-]+$', username.lstrip('@')):
        bot.send_message(chat_id, "❌ اسم المستخدم غير صالح. يرجى إدخال اسم مستخدم صحيح.")
        return

    # Determine target chat and verify bot access
    target_chat = str(chat_id)
    target_name = "محادثتك الخاصة"
    
    if len(parts) >= 3:
        input_chat = parts[2].strip()
        try:
            chat_info = bot.get_chat(input_chat)
            target_chat = str(chat_info.id)
            target_name = f"القناة/المجموعة: {chat_info.title or chat_info.username or input_chat}"
        except Exception as e:
            bot.send_message(
                chat_id,
                f"❌ تعذر الوصول إلى القناة/المجموعة '{input_chat}'.\n"
                f"تأكد من إضافة البوت كمسؤول (Administrator) فيها أولاً، وأن المعرف صحيح.\n"
                f"الخطأ: {e}"
            )
            return

    # Check limit of 10 followed accounts for the target chat
    if get_follow_count(target_chat) >= 10:
        bot.send_message(chat_id, f"❌ لقد تجاوزت القناة/الوجهة المحددة الحد الأقصى للمتابعة (10 حسابات).")
        return
        
    bot.send_message(chat_id, f"🔍 جاري التحقق من الحساب @{username.lstrip('@')}...")
    
    # Try to fetch feed to verify public existence and get latest shortcode
    items = fetch_rss_feed(username)
    if items is None:
        bot.send_message(
            chat_id, 
            f"❌ تعذر العثور على الحساب @{username.lstrip('@')} أو قد يكون حساباً خاصاً (Private).\n"
            "تأكد من كتابة الاسم بشكل صحيح ومن كون الحساب عاماً."
        )
        return
        
    if not items:
        # User has no posts, but account exists
        logger.info(f"User @{username} has no posts. Initializing tracking with empty shortcode.")
        follow_user(target_chat, username, "")
        bot.send_message(
            chat_id, 
            f"✅ **تم بدء التتبع بنجاح!**\n\n"
            f"👤 **الحساب المتابَع:** @{username.lstrip('@')}\n"
            f"📍 **الوجهة:** {target_name}\n"
            f"🔄 **المحتوى المشمول:** ريلز (Reels)، صور (Photos)، فيديوهات، ومنشورات متعددة (Carousels).\n"
            f"(لا توجد منشورات حالياً للبدء منها)"
        )
        return
        
    # Get latest post shortcode
    latest_item = items[0]
    latest_url = latest_item.get("url", "")
    latest_shortcode = extract_shortcode(latest_url)
    
    if not latest_shortcode:
        latest_shortcode = ""
        
    follow_user(target_chat, username, latest_shortcode)
    
    # Send test message to channel if it's not private chat
    if target_chat != str(chat_id):
        try:
            bot.send_message(
                target_chat, 
                f"📢 **تم تفعيل تتبع Instagram في هذه القناة!**\n"
                f"سيتم إرسال المنشورات الجديدة (ريلز، صور، فيديوهات) لحساب @{username.lstrip('@')} هنا تلقائياً."
            )
        except Exception as te:
            logger.error(f"Could not send welcome message to target chat {target_chat}: {te}")
            
    bot.send_message(
        chat_id, 
        f"✅ **تم بدء التتبع بنجاح!**\n\n"
        f"👤 **الحساب المتابَع:** @{username.lstrip('@')}\n"
        f"📍 **الوجهة:** {target_name}\n"
        f"🔄 **المحتوى المشمول:** ريلز (Reels)، صور (Photos)، فيديوهات، ومنشورات متعددة (Carousels).\n"
        f"آخر منشور تم رصده للبدء منه: `{latest_shortcode or 'لا يوجد'}`"
    )

@bot.message_handler(commands=['unfollow'])
def handle_unfollow(message):
    if not is_allowed_user(message):
        return
        
    chat_id = message.chat.id
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(chat_id, "💡 طريقة الاستخدام:\n`/unfollow username [channel_username_or_id]`", parse_mode="Markdown")
        return
        
    username = parts[1].strip()
    
    target_chat = str(chat_id)
    target_name = "محادثتك الخاصة"
    
    if len(parts) >= 3:
        input_chat = parts[2].strip()
        try:
            chat_info = bot.get_chat(input_chat)
            target_chat = str(chat_info.id)
            target_name = f"القناة/المجموعة: {chat_info.title or chat_info.username or input_chat}"
        except Exception:
            target_chat = input_chat
            target_name = f"القناة/المحادثة: {input_chat}"
            
    unfollow_user_db(target_chat, username)
    bot.send_message(chat_id, f"✅ تم إلغاء تتبع @{username.lstrip('@')} لـ {target_name} بنجاح.")

@bot.message_handler(commands=['following'])
def handle_following(message):
    if not is_allowed_user(message):
        return
        
    chat_id = message.chat.id
    all_subs = get_all_subscriptions()
    
    if not all_subs:
        bot.send_message(chat_id, "ℹ️ أنت لا تتابع أي حساب حالياً.")
        return
        
    # Group subscriptions by target chat for clear listing
    grouped = {}
    for sub_chat_id, username, last_sc in all_subs:
        grouped.setdefault(sub_chat_id, []).append(username)
        
    msg = "📋 الحسابات التي تتابعها حالياً ومكان نشرها:\n\n"
    
    for sub_chat_id, usernames in grouped.items():
        if sub_chat_id == str(chat_id):
            name = "💬 محادثتك الخاصة"
        else:
            try:
                chat_info = bot.get_chat(sub_chat_id)
                name = f"📢 {chat_info.title or chat_info.username or sub_chat_id}"
            except Exception:
                name = f"📢 القناة/المحادثة ID: {sub_chat_id}"
                
        msg += f"📍 **{name}**:\n"
        for user in usernames:
            msg += f"  • @{user}\n"
        msg += "\n"
        
    bot.send_message(chat_id, msg, parse_mode="Markdown")

@bot.message_handler(commands=['help'])
def handle_help(message):
    if not is_allowed_user(message):
        return
        
    help_text = (
        "📖 **دليل استخدام بوت Regram Forwarder الشخصي**\n\n"
        "هذا البوت مخصص لتحميل منشورات وريلز إنستغرام وتتبع الحسابات العامة تلقائياً وإرسالها لك أو لقنواتك.\n\n"
        "📌 **الحماية والخصوصية:**\n"
        "🔒 البوت شخصي ومقفل للاستخدام الخاص بك فقط. لن يتمكن أي مستخدم آخر من تفعيل الأوامر أو استخدامه.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ **1️⃣ تتبع الحسابات تلقائياً (Auto-Tracking):**\n"
        "يقوم البوت بفحص الحسابات المتابَعة كل 15 دقيقة وإرسال أي منشورات جديدة تلقائياً.\n\n"
        "🔹 `/follow username`\n"
        "بدء تتبع حساب وإرسال منشوراته الجديدة إلى **محادثتك الخاصة**.\n\n"
        "🔹 `/follow username @channel` أو `/follow username -100xxxxxxx`\n"
        "بدء تتبع حساب وإرسال منشوراته الجديدة إلى **قناة أو مجموعة محددة** (يجب إضافة البوت كمسؤول Admin فيها أولاً).\n\n"
        "🔹 `/unfollow username [channel]`\n"
        "إلغاء تتبع الحساب للوجهة المحددة (الخاص أو القناة).\n\n"
        "🔹 `/following`\n"
        "عرض قائمة بجميع الحسابات التي تتبعها حالياً مجمّعة حسب وجهة النشر.\n\n"
        "⚠️ **الحد الأقصى:** 10 حسابات متابَعة لكل وجهة (الخاص أو القناة).\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📥 **2️⃣ التحميل الفوري عبر إنستغرام (DMs):**\n"
        "يمكنك إرسال أو مشاركة أي منشور/ريلز لحساب البوت على إنستغرام وسيقوم البوت بإرسال الميديا لك هنا فوراً:\n"
        "• مشاركة (Share) الريلز/المنشورات مباشرة من تطبيق إنستغرام إلى حساب البوت الخاص بنا.\n"
        "• إرسال رابط المنشور كرسالة نصية في الـ DM لحساب البوت.\n"
        "• يدعم البوت المنشورات العادية، الريلز، الكاروسيل (ألبوم الصور والفيديوهات بالكامل).\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 **نوع المحتوى المدعوم:**\n"
        "• ريلز (Reels) وفيديوهات عامة بجودة عالية.\n"
        "• صور منفردة.\n"
        "• ألبومات الصور والفيديوهات (Carousels) - يتم تحميل وإرسال الألبوم بالكامل بالتتابع مع إرفاق الكابشن الأصلي.\n"
    )
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")



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

# ----------------- Instagram Auto-Tracking Logic -----------------
def forward_tracked_post(chat_id, username, shortcode, title=""):
    """
    Resolves, downloads, and forwards a tracked post to Telegram.
    Returns True if successfully sent, False on failure.
    """
    post_url = f"https://www.instagram.com/p/{shortcode}/"
    logger.info(f"Forwarding tracked post {shortcode} for @{username} to chat {chat_id}...")
    
    # Try to resolve via proxy
    carousel_urls = []
    try:
        carousel_urls = resolve_via_proxy(post_url, "vxinstagram.com")
        if not carousel_urls:
            logger.info("vxinstagram failed. Trying ddinstagram...")
            carousel_urls = resolve_via_proxy(post_url, "ddinstagram.com")
    except Exception as e:
        logger.error(f"Error resolving tracked post {shortcode} via proxies: {e}")

    try:
        if carousel_urls:
            if len(carousel_urls) > 1:
                # Carousel post
                logger.info(f"Forwarding tracked carousel with {len(carousel_urls)} items to chat {chat_id}")
                download_and_forward_carousel(
                    carousel_urls,
                    str(chat_id),
                    original_caption=title,
                    shortcode=shortcode,
                    raise_on_error=True
                )
            else:
                # Single media post
                url_to_use, type_to_use = carousel_urls[0]
                logger.info(f"Forwarding tracked single media of type {type_to_use} to chat {chat_id}")
                download_and_forward_media(
                    url_to_use,
                    type_to_use,
                    str(chat_id),
                    original_caption=title,
                    shortcode=shortcode,
                    creator_username=username,
                    raise_on_error=True
                )
        else:
            # Fallback if proxy failed: send text link directly so they don't miss it
            logger.warning(f"Could not resolve tracked post {shortcode} via proxies. Sending fallback link.")
            fallback_text = (
                f"📢 **منشور جديد من @{username}**\n\n"
                f"<code>{title}</code>\n\n"
                f"🔗 {post_url}"
            )
            bot.send_message(chat_id, fallback_text, parse_mode="HTML")
            
        logger.info(f"Tracked post {shortcode} forwarded successfully to chat {chat_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to forward tracked post {shortcode} to chat {chat_id}: {e}")
        return False


def run_auto_track_loop():
    logger.info("Starting Instagram Auto-tracking background loop...")
    
    # Run immediate check on boot, then enter periodic check
    while True:
        try:
            logger.info("Auto-track loop: Starting checking cycle...")
            all_subs = get_all_subscriptions()
            
            if not all_subs:
                logger.info("Auto-track loop: No active tracking subscriptions.")
            else:
                # Group by username to deduplicate requests
                by_username = {}
                for chat_id, username, last_shortcode in all_subs:
                    by_username.setdefault(username, []).append((chat_id, last_shortcode))
                
                # Fetch feeds and process
                for username, subscribers in by_username.items():
                    try:
                        items = fetch_rss_feed(username)
                        if items is None:
                            logger.warning(f"Auto-track loop: Skipping @{username} due to feed fetch error.")
                            continue
                        if not items:
                            logger.info(f"Auto-track loop: @{username} has no posts in feed.")
                            continue
                            
                        # Process for each subscriber
                        for chat_id, last_shortcode in subscribers:
                            try:
                                if not last_shortcode:
                                    # Initialize tracking for new subscription
                                    newest_item = items[0]
                                    newest_url = newest_item.get("url", "")
                                    newest_shortcode = extract_shortcode(newest_url)
                                    if newest_shortcode:
                                        update_last_shortcode(chat_id, username, newest_shortcode)
                                        logger.info(f"Auto-track loop: Initialized tracking for @{username} for chat {chat_id} at {newest_shortcode}")
                                    continue
                                    
                                # Search for last_shortcode index in the feed
                                found_idx = -1
                                for i, item in enumerate(items):
                                    shortcode = extract_shortcode(item.get("url", ""))
                                    if shortcode == last_shortcode:
                                        found_idx = i
                                        break
                                        
                                if found_idx != -1:
                                    # New posts are those in index 0 to found_idx - 1
                                    new_posts_items = items[0:found_idx]
                                    # Process oldest first (reverse it)
                                    new_posts_items.reverse()
                                    
                                    logger.info(f"Auto-track loop: Found {len(new_posts_items)} new posts for @{username} (chat {chat_id})")
                                    
                                    # Forward each post
                                    for item in new_posts_items:
                                        post_url = item.get("url", "")
                                        shortcode = extract_shortcode(post_url)
                                        title = item.get("title", "")
                                        
                                        if not shortcode:
                                            continue
                                            
                                        # Forward post
                                        success = forward_tracked_post(chat_id, username, shortcode, title)
                                        if success:
                                            # Update last shortcode to keep progress
                                            update_last_shortcode(chat_id, username, shortcode)
                                            # Sleep 0.5s between consecutive Telegram messages for rate limits
                                            time.sleep(0.5)
                                        else:
                                            # Stop processing for this user, retry in next cycle
                                            logger.warning(f"Auto-track loop: Failed to forward post {shortcode} for @{username} to chat {chat_id}. Stopping queue for this cycle.")
                                            break
                                else:
                                    # last_shortcode not found in the feed, reset and warn the user
                                    logger.warning(f"Auto-track loop: last_shortcode '{last_shortcode}' not found in feed of @{username} for chat {chat_id}. Resetting track.")
                                    newest_item = items[0]
                                    newest_url = newest_item.get("url", "")
                                    newest_shortcode = extract_shortcode(newest_url)
                                    if newest_shortcode:
                                        update_last_shortcode(chat_id, username, newest_shortcode)
                                        try:
                                            bot.send_message(chat_id, f"⚠️ تم إعادة مزامنة متابعة @{username}")
                                        except Exception as te:
                                            logger.error(f"Failed to send resync message to Telegram chat {chat_id}: {te}")
                            except Exception as sub_e:
                                logger.error(f"Auto-track loop: Error processing subscriber {chat_id} for @{username}: {sub_e}")
                                
                    except Exception as user_e:
                        logger.error(f"Auto-track loop: Error processing username @{username}: {user_e}")
                    
                    # Sleep 3-5 seconds between checking different usernames to rate-limit request spam
                    time.sleep(random.randint(3, 5))
                    
        except Exception as cycle_e:
            logger.error(f"Auto-track loop: Cycle crashed with error: {cycle_e}")
            
        logger.info("Auto-track loop: Checking cycle finished. Sleeping for 15 minutes...")
        time.sleep(900)


# ----------------- FastAPI Webhook Server -----------------
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    if TELEGRAM_TOKEN:
        # Start Telegram Polling in background thread
        tg_thread = threading.Thread(target=run_telegram_polling, daemon=True)
        tg_thread.start()
        
        # Start Instagram Auto-tracking background thread
        track_thread = threading.Thread(target=run_auto_track_loop, daemon=True)
        track_thread.start()
    else:
        logger.error("TELEGRAM_TOKEN is missing! Cannot start Telegram polling or tracking.")
    yield

app = FastAPI(title="Instagram Telegram Webhook Linker", lifespan=lifespan)


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
    shortcode: str = "",
    creator_username: str = "",
    raise_on_error: bool = False
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
                try:
                    bot.send_message(telegram_chat_id, "❌ فشل تحميل الفيديو/المنشور من خوادم إنستغرام.")
                except Exception:
                    pass
            if raise_on_error:
                raise Exception(f"HTTP Status {response.status_code} when downloading CDN media")
            return

        # Check if we accidentally downloaded an HTML page (e.g. if the proxy failed or link was private)
        content_type = response.headers.get("Content-Type", "")
        if "text/html" in content_type:
            logger.error(f"Failed to download media: server returned HTML page instead of media stream. Content-Type: {content_type}")
            if index is None or index == 1:
                try:
                    bot.send_message(telegram_chat_id, "❌ لا يمكن تحميل هذا المنشور/الريلز. قد يكون الحساب خاصاً (Private) أو الرابط غير صالح.")
                except Exception:
                    pass
            if raise_on_error:
                raise Exception("Server returned HTML page instead of media stream")
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
        
        # Resolve creator username if not passed but shortcode exists
        if not creator_username and shortcode:
            try:
                creator_username = get_creator_username(shortcode)
            except Exception as e:
                logger.error(f"Error fetching creator username in background task: {e}")

        # HTML escape helper
        def escape_html(text: str) -> str:
            if not text:
                return ""
            return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        caption_parts = []
        
        # 1. Header (Index + Username without hashtag)
        header_text = ""
        if index is not None and total is not None:
            header_text = f"({index}/{total})"
            
        if creator_username:
            escaped_username = escape_html(creator_username)
            if header_text:
                header_text = f"{header_text} {escaped_username}"
            else:
                header_text = escaped_username
                
        if header_text:
            caption_parts.append(header_text)
            
        # 2. Original Caption (wrapped in <code> block to be copyable)
        if original_caption:
            original_caption_clean = original_caption.strip()
            escaped_original = escape_html(original_caption_clean)
            caption_parts.append(f"<code>{escaped_original}</code>")
            
        # 3. Footer (Username with hashtag)
        if creator_username:
            escaped_username = escape_html(creator_username)
            caption_parts.append(f"#{escaped_username}")
            
        caption = "\n\n".join(caption_parts)
        if not caption:
            caption = None
            
        with open(temp_filename, "rb") as media_file:
            if is_video:
                bot.send_video(telegram_chat_id, media_file, caption=caption, parse_mode="HTML", supports_streaming=True)
            else:
                bot.send_photo(telegram_chat_id, media_file, caption=caption, parse_mode="HTML")
                
        # Send as uncompressed document to preserve quality
        try:
            doc_filename = ""
            if creator_username:
                doc_filename = f"{creator_username}"
                if shortcode:
                    doc_filename += f"_{shortcode}"
            else:
                if shortcode:
                    doc_filename = f"{shortcode}"
            
            if not doc_filename:
                doc_filename = "media"
                
            doc_filename += ".mp4" if is_video else ".jpg"
            
            with open(temp_filename, "rb") as document_file:
                bot.send_document(
                    telegram_chat_id, 
                    (doc_filename, document_file, 'application/octet-stream'), 
                    caption=f"📄 {doc_filename}"
                )
            logger.info("Media sent as uncompressed document successfully.")
        except Exception as doc_e:
            logger.error(f"Failed to send media as document: {doc_e}")
                
        logger.info(f"Media forwarded successfully to Telegram.")
        
    except Exception as e:
        logger.error(f"Error during media download and forwarding: {e}")
        if index is None or index == 1:
            try:
                bot.send_message(telegram_chat_id, f"❌ حدث خطأ أثناء معالجة وإرسال الفيديو/المنشور: {e}")
            except Exception:
                pass
        if raise_on_error:
            raise e
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
    shortcode: str = "",
    raise_on_error: bool = False
):
    """Downloads all media items of a carousel sequentially with a rate-limit sleep."""
    total = len(urls_and_types)
    logger.info(f"Starting sequential carousel download & forward for {total} items...")
    
    # Resolve creator username once for the whole carousel
    creator_username = ""
    if shortcode:
        try:
            creator_username = get_creator_username(shortcode)
        except Exception as e:
            logger.error(f"Error fetching creator username for carousel: {e}")
            
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
                shortcode,
                creator_username,
                raise_on_error=raise_on_error
            )
        except Exception as e:
            logger.error(f"Failed to forward carousel item {idx+1}/{total}: {e}")
            if raise_on_error:
                raise e
        
        # Sleep 0.5 seconds between slides to rate limit Telegram API calls
        if idx < total - 1:
            logger.info("Sleeping 0.5s before next carousel item to rate-limit Telegram API...")
            time.sleep(0.5)

def extract_shortcode(url: str) -> str:
    """Extracts the Instagram shortcode from a Reel or Post URL."""
    if not url or "lookaside.fbsbx.com" in url or "ig_messaging_cdn" in url:
        return ""
        
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

                # Caption caching logic to handle carousel items (e.g. video files sent as unsupported_type)
                if sender_igsid:
                    with LAST_CAPTIONS_CACHE_LOCK:
                        if original_caption:
                            LAST_CAPTIONS_CACHE[sender_igsid] = {
                                "caption": original_caption,
                                "timestamp": time.time()
                            }
                            logger.info(f"Cached caption for user {sender_igsid}: {original_caption[:30]}...")
                        else:
                            cached = LAST_CAPTIONS_CACHE.get(sender_igsid)
                            if cached and (time.time() - cached["timestamp"] < 60):
                                original_caption = cached["caption"]
                                # Update timestamp to keep it alive for other slides in the same carousel
                                cached["timestamp"] = time.time()
                                logger.info(f"Reused cached caption for user {sender_igsid}: {original_caption[:30]}...")
                
                if media_url:
                    # Check if sender is mapped to a Telegram Chat ID
                    telegram_chat_id = get_telegram_chat_id(sender_igsid)
                    
                    if telegram_chat_id:
                        # Extract shortcode to resolve creator username later
                        shortcode = extract_shortcode(media_url)
                        
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
                                shortcode
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
                                shortcode
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
