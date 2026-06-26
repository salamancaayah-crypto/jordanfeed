import os
import unittest
import sqlite3
import tempfile
from fastapi.testclient import TestClient

# Mock the env vars for testing before importing the script
os.environ["TELEGRAM_TOKEN"] = "mock_telegram_token"
os.environ["META_VERIFY_TOKEN"] = "test_verify_token"
os.environ["META_ACCESS_TOKEN"] = "mock_meta_access_token"

import regram_forwarder
from regram_forwarder import app, init_db, DB_PATH, create_or_get_token, link_instagram_account, get_telegram_chat_id

class TestRegramForwarder(unittest.TestCase):
    
    @classmethod
    def setUpClass(cls):
        # Create a temp DB for testing
        cls.db_fd, cls.temp_db_path = tempfile.mkstemp()
        regram_forwarder.DB_PATH = cls.temp_db_path
        regram_forwarder.META_VERIFY_TOKEN = "test_verify_token"
        init_db()
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        os.close(cls.db_fd)
        try:
            os.unlink(cls.temp_db_path)
        except PermissionError:
            pass

    def setUp(self):
        # Clear DB before each test
        conn = sqlite3.connect(regram_forwarder.DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM mappings")
        conn.commit()
        conn.close()

    def test_database_mapping_and_token_generation(self):
        chat_id = "12345678"
        token, igsid = create_or_get_token(chat_id)
        
        self.assertTrue(token.startswith("REG-"))
        self.assertIsNone(igsid)
        
        # Test getting same token
        token2, igsid2 = create_or_get_token(chat_id)
        self.assertEqual(token, token2)
        
        # Test linking
        linked_chat = link_instagram_account(token, "instagram_user_999")
        self.assertEqual(linked_chat, chat_id)
        
        # Test mapping retrieval
        retrieved_chat = get_telegram_chat_id("instagram_user_999")
        self.assertEqual(retrieved_chat, chat_id)

    def test_webhook_verification(self):
        # Test correct token
        response = self.client.get("/webhook?hub.mode=subscribe&hub.verify_token=test_verify_token&hub.challenge=challenge_accepted")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "challenge_accepted")

        # Test incorrect token
        response = self.client.get("/webhook?hub.mode=subscribe&hub.verify_token=wrong_token&hub.challenge=challenge_accepted")
        self.assertEqual(response.status_code, 403)

    def test_webhook_link_registration(self):
        chat_id = "55555"
        token, _ = create_or_get_token(chat_id)
        
        # Mock telegram bot sendMessage method
        original_send_message = regram_forwarder.bot.send_message
        original_send_dm = regram_forwarder.send_instagram_dm
        
        telegram_notified = False
        instagram_notified = False
        
        def mock_bot_send_message(target_chat, text, **kwargs):
            nonlocal telegram_notified
            if target_chat == chat_id and "ربط حساب" in text:
                telegram_notified = True
                
        def mock_send_instagram_dm(target_ig, text):
            nonlocal instagram_notified
            if target_ig == "ig_user_123" and "تم ربط الحساب" in text:
                instagram_notified = True

        regram_forwarder.bot.send_message = mock_bot_send_message
        regram_forwarder.send_instagram_dm = mock_send_instagram_dm

        # Send mock webhook with linking token
        payload = {
            "object": "instagram",
            "entry": [
                {
                    "id": "page_id_123",
                    "time": 1234567,
                    "messaging": [
                        {
                            "sender": {"id": "ig_user_123"},
                            "recipient": {"id": "page_id_123"},
                            "message": {
                                "mid": "mid.1111",
                                "text": token
                            }
                        }
                    ]
                }
            ]
        }
        
        response = self.client.post("/webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(telegram_notified)
        self.assertTrue(instagram_notified)
        
        # Verify stored in DB
        self.assertEqual(get_telegram_chat_id("ig_user_123"), chat_id)
        
        # Restore mock methods
        regram_forwarder.bot.send_message = original_send_message
        regram_forwarder.send_instagram_dm = original_send_dm

    def test_shortcode_extraction(self):
        from regram_forwarder import extract_shortcode
        
        # Test basic URLs
        self.assertEqual(extract_shortcode("https://www.instagram.com/reel/DZ71pJVsCSt/"), "DZ71pJVsCSt")
        self.assertEqual(extract_shortcode("https://www.instagram.com/p/DZ71pJVsCSt/"), "DZ71pJVsCSt")
        self.assertEqual(extract_shortcode("https://instagram.com/p/DZ71pJVsCSt"), "DZ71pJVsCSt")
        
        # Test URLs with query parameters and trailing slash
        self.assertEqual(extract_shortcode("https://www.instagram.com/reel/DZ71pJVsCSt/?igsh=MXRndTNpN3dxcWF1Zw=="), "DZ71pJVsCSt")
        self.assertEqual(extract_shortcode("https://www.instagram.com/p/DZ71pJVsCSt/?utm_source=ig_web_copy_link"), "DZ71pJVsCSt")
        
        # Test URLs with query parameters but no trailing slash
        self.assertEqual(extract_shortcode("https://www.instagram.com/reel/DZ71pJVsCSt?igsh=MXRndTNpN3dxcWF1Zw=="), "DZ71pJVsCSt")
        self.assertEqual(extract_shortcode("https://www.instagram.com/p/DZ71pJVsCSt?utm_source=ig_web_copy_link"), "DZ71pJVsCSt")
        
        # Test TV or share links
        self.assertEqual(extract_shortcode("https://www.instagram.com/tv/DZ71pJVsCSt/"), "DZ71pJVsCSt")
        self.assertEqual(extract_shortcode("https://www.instagram.com/share/r/DZ71pJVsCSt/"), "DZ71pJVsCSt")

    def test_webhook_media_forwarding_without_username(self):
        # Link a user first
        chat_id = "987654"
        token, _ = create_or_get_token(chat_id)
        link_instagram_account(token, "ig_user_caption_test")
        
        # Mock download_and_forward_media
        original_download = regram_forwarder.download_and_forward_media
        download_args = []
        
        def mock_download_and_forward_media(*args, **kwargs):
            download_args.append((args, kwargs))
            
        regram_forwarder.download_and_forward_media = mock_download_and_forward_media
        
        # Webhook payload representing a shared post
        payload = {
            "object": "instagram",
            "entry": [
                {
                    "id": "page_id_123",
                    "time": 1234567,
                    "messaging": [
                        {
                            "sender": {"id": "ig_user_caption_test"},
                            "recipient": {"id": "page_id_123"},
                            "message": {
                                "mid": "mid.2222",
                                "attachments": [
                                    {
                                        "type": "video",
                                        "payload": {
                                            "url": "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=123",
                                            "title": "This is a great caption!"
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        
        response = self.client.post("/webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        
        # Restore mock
        regram_forwarder.download_and_forward_media = original_download
        
        # Check that download was invoked correctly
        self.assertEqual(len(download_args), 1)
        args, kwargs = download_args[0]
        self.assertEqual(args[0], "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=123")
        self.assertEqual(args[1], "video")
        self.assertEqual(args[2], chat_id)
        
        caption = kwargs.get("original_caption") if "original_caption" in kwargs else args[5]
        self.assertEqual(caption, "This is a great caption!")

    def test_caption_formatting_with_hashtag(self):
        # Mock bot.send_video
        original_send_video = regram_forwarder.bot.send_video
        sent_captions = []
        
        def mock_send_video(chat_id, media_file, caption=None, **kwargs):
            sent_captions.append(caption)
            
        regram_forwarder.bot.send_video = mock_send_video
        
        # Mock requests.get
        import requests
        original_get = requests.get
        
        class MockResponse:
            def __init__(self):
                self.status_code = 200
                self.headers = {"Content-Type": "video/mp4"}
            def iter_content(self, chunk_size=8192):
                return [b"dummy_data"]
                
        def mock_get(*args, **kwargs):
            return MockResponse()
            
        requests.get = mock_get
        
        try:
            # Run download_and_forward_media
            regram_forwarder.download_and_forward_media(
                "http://example.com/video.mp4",
                "video",
                "123456",
                None,
                None,
                "Original Caption Here",
                "",
                "creator_name"
            )
            
            self.assertEqual(len(sent_captions), 1)
            self.assertEqual(sent_captions[0], "#creator_name\n\nOriginal Caption Here")
        finally:
            regram_forwarder.bot.send_video = original_send_video
            requests.get = original_get

    def test_caption_caching_for_carousel_items(self):
        # Link a user first
        chat_id = "112233"
        token, _ = create_or_get_token(chat_id)
        link_instagram_account(token, "ig_user_carousel_test")
        
        # Mock download_and_forward_media
        original_download = regram_forwarder.download_and_forward_media
        download_args = []
        
        def mock_download_and_forward_media(*args, **kwargs):
            download_args.append((args, kwargs))
            
        regram_forwarder.download_and_forward_media = mock_download_and_forward_media
        
        try:
            # 1. Send first slide with caption "A new chapter begins 💗."
            payload1 = {
                "object": "instagram",
                "entry": [{
                    "id": "page_id_123",
                    "time": 1234567,
                    "messaging": [{
                        "sender": {"id": "ig_user_carousel_test"},
                        "recipient": {"id": "page_id_123"},
                        "message": {
                            "mid": "mid.slide1",
                            "attachments": [{
                                "type": "ig_post",
                                "payload": {
                                    "url": "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=image1",
                                    "title": "A new chapter begins 💗."
                                }
                            }]
                        }
                    }]
                }]
            }
            res1 = self.client.post("/webhook", json=payload1)
            self.assertEqual(res1.status_code, 200)
            
            # 2. Send second slide (video) of type "unsupported_type" with NO caption
            payload2 = {
                "object": "instagram",
                "entry": [{
                    "id": "page_id_123",
                    "time": 1234568,
                    "messaging": [{
                        "sender": {"id": "ig_user_carousel_test"},
                        "recipient": {"id": "page_id_123"},
                        "message": {
                            "mid": "mid.slide2",
                            "attachments": [{
                                "type": "unsupported_type",
                                "payload": {
                                    "url": "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=video2"
                                    # No title/caption in payload
                                }
                            }]
                        }
                    }]
                }]
            }
            res2 = self.client.post("/webhook", json=payload2)
            self.assertEqual(res2.status_code, 200)
            
            # Check that both downloads were triggered
            self.assertEqual(len(download_args), 2)
            
            # Verify slide 1 arguments
            args1, kwargs1 = download_args[0]
            caption1 = kwargs1.get("original_caption") if "original_caption" in kwargs1 else args1[5]
            self.assertEqual(caption1, "A new chapter begins 💗.")
            
            # Verify slide 2 arguments (should reuse the cached caption)
            args2, kwargs2 = download_args[1]
            caption2 = kwargs2.get("original_caption") if "original_caption" in kwargs2 else args2[5]
            self.assertEqual(caption2, "A new chapter begins 💗.")
            
        finally:
            regram_forwarder.bot.send_media = original_download
            regram_forwarder.download_and_forward_media = original_download

if __name__ == "__main__":
    unittest.main()
