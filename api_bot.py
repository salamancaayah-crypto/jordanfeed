import os
import re
import time
import requests
import telebot
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
RAPIDAPI_HOST = os.getenv("RAPIDAPI_HOST", "instagram-downloader-download-instagram-videos-stories.p.rapidapi.com")
RAPIDAPI_URL = os.getenv("RAPIDAPI_URL", "https://instagram-downloader-download-instagram-videos-stories.p.rapidapi.com/index")

if not TELEGRAM_TOKEN:
    print("❌ Error: TELEGRAM_TOKEN is missing from environment variables!")
    exit(1)

bot = telebot.TeleBot(TELEGRAM_TOKEN)
print("🚀 Telegram API-Based Bot started successfully in Polling mode...")

def download_file(url, extension="mp4"):
    """Downloads a file from a URL and returns the temporary filename."""
    temp_filename = f"temp_media_{int(time.time())}.{extension}"
    try:
        print(f"📥 Downloading media from API link: {url}")
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
        
        with open(temp_filename, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return temp_filename
    except Exception as e:
        print(f"❌ Failed to download file from URL: {e}")
        if os.path.exists(temp_filename):
            os.remove(temp_filename)
        return None

def extract_shortcode(url):
    pattern = r'/(?:p|reel|tv|stories/[^/]+)/([A-Za-z0-9_-]+)'
    match = re.search(pattern, url)
    if match:
        return match.group(1)
    return None

def resolve_via_html_proxies(instagram_url):
    """Attempts to resolve the video using free OG embed proxies without keys."""
    shortcode = extract_shortcode(instagram_url)
    if not shortcode:
        return None
        
    # Standard OG embed proxy list
    proxies = [
        f"https://ddinstagram.com/reel/{shortcode}/",
        f"https://fixinstagram.com/reel/{shortcode}/",
        f"https://www.vxinstagram.com/reel/{shortcode}/",
        f"https://ddinstagram.com/p/{shortcode}/",
        f"https://fixinstagram.com/p/{shortcode}/",
    ]
    
    headers = {
        "User-Agent": "TelegramBot (like TwitterBot)"
    }
    
    for proxy_url in proxies:
        try:
            print(f"🔗 Trying HTML proxy: {proxy_url}")
            response = requests.get(proxy_url, headers=headers, timeout=8)
            if response.status_code == 200:
                html = response.text
                match = re.search(r'<meta\s+property="og:video"\s+content="([^"]+)"', html)
                if not match:
                    match = re.search(r'content="([^"]+)"\s+property="og:video"', html)
                if not match:
                    match = re.search(r'<meta\s+name="twitter:player:stream"\s+content="([^"]+)"', html)
                    
                if match:
                    video_url = match.group(1).replace("&amp;", "&")
                    print(f"✅ Found direct video link via proxy: {video_url[:60]}...")
                    return video_url
        except Exception as e:
            print(f"⚠️ Proxy failed: {e}")
            
    return None

def resolve_instagram_via_api(instagram_url):
    """Calls the RapidAPI endpoint to get the direct download link."""
    if not RAPIDAPI_KEY:
        return None
        
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_HOST
    }
    querystring = {"url": instagram_url}
    
    try:
        print(f"🔍 Sending request to RapidAPI for: {instagram_url}")
        response = requests.get(RAPIDAPI_URL, headers=headers, params=querystring, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        # Robust parser for different RapidAPI response formats
        # 1. Standard download URL keys
        for key in ["url", "video_url", "download_url", "media", "download"]:
            if key in data and isinstance(data[key], str) and data[key].startswith("http"):
                return data[key]
                
        # 2. Nested data formats (e.g. data['data']['url'] or data['links'][0]['url'])
        if "data" in data and isinstance(data["data"], dict):
            sub_data = data["data"]
            for key in ["url", "video_url", "download_url", "file_url"]:
                if key in sub_data and isinstance(sub_data[key], str) and sub_data[key].startswith("http"):
                    return sub_data[key]
                    
        if "links" in data and isinstance(data["links"], list) and len(data["links"]) > 0:
            first_link = data["links"][0]
            if isinstance(first_link, dict) and "url" in first_link:
                return first_link["url"]
                
        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
            if "url" in data[0]:
                return data[0]["url"]
                
        print(f"⚠️ Response format not automatically parsed. Full response: {data}")
        return None
    except Exception as e:
        print(f"❌ RapidAPI request failed: {e}")
        return None

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    message_text = message.text.strip() if message.text else ""
    
    # Check if the message contains an Instagram link
    if "instagram.com" in message_text:
        url_match = re.search(r'(https?://[^\s]*instagram\.com/[^\s]*)', message_text)
        if not url_match:
            return
            
        instagram_url = url_match.group(1)
        chat_id = message.chat.id
        
        bot.reply_to(message, "⏳ جاري التحميل والتحويل...")
        
        # 1. Try free HTML proxies first
        media_url = resolve_via_html_proxies(instagram_url)
        
        # 2. Fallback to RapidAPI if proxies fail
        if not media_url and RAPIDAPI_KEY:
            media_url = resolve_instagram_via_api(instagram_url)
            
        if not media_url:
            bot.send_message(chat_id, "❌ عذراً، فشل استخراج رابط التحميل.")
            return
            
        # Determine extension (rough guess)
        ext = "mp4"
        if ".jpg" in media_url.lower() or ".png" in media_url.lower() or "image" in media_url.lower():
            ext = "jpg"
            
        # 2. Download the file locally
        temp_file = download_file(media_url, ext)
        if not temp_file:
            bot.send_message(chat_id, "❌ فشل تحميل الملف من خوادم الـ API.")
            return
            
        # 3. Send to Telegram
        try:
            bot.send_message(chat_id, "📤 جاري الإرسال إلى تيليجرام...")
            with open(temp_file, 'rb') as f:
                if ext == "mp4":
                    # Send as video
                    bot.send_video(chat_id, f, supports_streaming=True)
                    f.seek(0)
                    # Send as uncompressed document (Full Quality)
                    bot.send_document(chat_id, f, visible_file_name=f"reel_{int(time.time())}.mp4")
                else:
                    # Send as photo
                    bot.send_photo(chat_id, f)
                    f.seek(0)
                    # Send as uncompressed document (Full Quality)
                    bot.send_document(chat_id, f, visible_file_name=f"image_{int(time.time())}.jpg")
                    
            print(f"✅ Success: Media forwarded to Chat {chat_id}")
        except Exception as e:
            bot.send_message(chat_id, f"❌ فشل إرسال الملف إلى تيليجرام: {e}")
        finally:
            # Clean up
            if os.path.exists(temp_file):
                os.remove(temp_file)

if __name__ == "__main__":
    try:
        bot.infinity_polling()
    except KeyboardInterrupt:
        print("\nStopping bot...")
