import os
import subprocess
import time
import re

def run():
    print("🧹 Cleaning up old tunnel processes...")
    try:
        # Kill any existing cloudflared process
        subprocess.run(["pkill", "-f", "cloudflared"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
        
    print("🚀 Starting Cloudflare Tunnel in background...")
    log_file = "cloudflared.log"
    if os.path.exists(log_file):
        try:
            os.remove(log_file)
        except Exception:
            pass
            
    # Start cloudflared in the background and redirect output to cloudflared.log
    try:
        process = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", "http://localhost:8000"],
            stdout=open(log_file, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT
        )
    except FileNotFoundError:
        print("❌ Error: 'cloudflared' command not found! Please run 'pkg install cloudflared -y' first.")
        return
    except Exception as e:
        print(f"❌ Failed to start cloudflared: {e}")
        return
        
    print("⏳ Waiting for Cloudflare to generate your public link...")
    tunnel_url = None
    for i in range(12):
        time.sleep(1)
        if os.path.exists(log_file):
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    content = f.read()
                    # Match the trycloudflare link
                    match = re.search(r'https://[a-zA-Z0-9_-]+\.trycloudflare\.com', content)
                    if match:
                        tunnel_url = match.group(0)
                        break
            except Exception:
                pass
                
    if tunnel_url:
        print("\n" + "="*70)
        print("🎉 YOUR PUBLIC LINK IS READY!")
        print(f"👉 Copy this link: {tunnel_url}/webhook")
        print("Paste it as the 'Callback URL' in Facebook Developer Portal.")
        print("="*70 + "\n")
    else:
        print("❌ Failed to retrieve the Cloudflare tunnel link automatically.")
        print("Check 'cloudflared.log' file for details.")
        return

    print("🤖 Starting the Telegram Bot in foreground...")
    try:
        subprocess.run(["python", "regram_forwarder.py"])
    except KeyboardInterrupt:
        print("\nStopping bot...")
    finally:
        print("🧹 Cleaning up tunnel...")
        process.terminate()
        try:
            subprocess.run(["pkill", "-f", "cloudflared"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        print("👋 Done!")

if __name__ == "__main__":
    run()
