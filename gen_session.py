import os
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
from dotenv import load_dotenv

# Load your API_ID and API_HASH from .env
load_dotenv()

async def generate_session():
    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")

    if not api_id or not api_hash:
        print("❌ Error: API_ID or API_HASH not found in .env file!")
        return

    print("🚀 Telegram Session Generator")
    print("----------------------------")
    
    # We use StringSession() to get a portable string
    async with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        session_str = client.session.save()
        print("\n✅ LOGIN SUCCESSFUL!")
        print("\n--- YOUR SESSION STRING (COPY THIS) ---")
        print(f"\n{session_str}\n")
        print("---------------------------------------")
        print("\nStep 1: Copy the string above.")
        print("Step 2: Paste it into your .env file under SESSIONS.")
        print("Note: You can add multiple strings separated by commas for even more speed.")

if __name__ == "__main__":
    asyncio.run(generate_session())
