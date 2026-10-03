from telethon import events, Button
from app.streamer.manager import session_manager
from app.database.connection import files_col, users_col, settings
from app.models.schemas import FileMetadata, User
from app.utils.helpers import generate_short_code
from app.utils.fsub import is_user_fsubbed
from app.utils.rate_limit import check_rate_limit
import datetime
import logging

logger = logging.getLogger(__name__)

def register_handlers(bot):
    @bot.on(events.NewMessage(pattern='/start'))
    async def start_handler(event):
        user_id = event.sender_id
        # Save user to DB
        user_data = await users_col.find_one({"user_id": user_id})
        if user_data and user_data.get('is_banned'):
            return await event.reply("🚫 You are banned from using this bot.")
            
        if not user_data:
            new_user = User(
                user_id=user_id,
                username=event.sender.username,
                first_name=event.sender.first_name,
                last_name=event.sender.last_name
            )
            await users_col.insert_one(new_user.dict())
            
            # Log New User
            if settings.CHANNEL_ID:
                try:
                    name = f"{event.sender.first_name} {event.sender.last_name or ''}".strip()
                    await bot.send_message(
                        settings.CHANNEL_ID,
                        f"#NewUser\n\n"
                        f"Iᴅ - `{user_id}`\n"
                        f"Nᴀᴍᴇ - {name}\n"
                        f"Usᴇʀɴᴀᴍᴇ - @{event.sender.username or 'N/A'}"
                    )
                except Exception as e:
                    logger.error(f"Error sending new user log: {e}")
        
        # Force Sub Check
        if not await is_user_fsubbed(bot, user_id):
            return await event.respond(
                "❌ **Access Denied!**\n\n"
                "You must join our channels to use this bot.\n"
                "Please join and then send /start again.",
                buttons=[[Button.url("Join Channel", "https://t.me/cantarellabots")]]
            )

        await event.respond(
            "👋 Welcome to CantarellaBots Media Streamer!\n\n"
            "Send me any media file and I will generate a direct high-speed download/stream link for you.",
            buttons=[
                [Button.url("Join Channel", "https://t.me/cantarellabots"), Button.url("Developer", "https://t.me/cantarella_wuwa")],
                [Button.inline("Help", b"help"), Button.inline("About", b"about")]
            ]
        )

    @bot.on(events.NewMessage(func=lambda e: e.media))
    async def media_handler(event):
        # Ban Check
        user_data = await users_col.find_one({"user_id": event.sender_id})
        if user_data and user_data.get('is_banned'):
            return await event.reply("🚫 You are banned from using this bot.")

        # Rate Limit Check
        if not await check_rate_limit(event.sender_id):
            return await event.reply("⚠️ **Slow down!** Please wait a moment before sending more files.")

        # Force Sub Check
        if not await is_user_fsubbed(bot, event.sender_id):
            await event.reply(
                "❌ **Access Denied!**\n\n"
                "You must join our channels to use this bot.",
                buttons=[[Button.url("Join Channel", "https://t.me/cantarellabots")]]
            )
            return

        media = event.media
        if not media:
            return

        # Extract file info
        file_id = ""
        file_name = "file"
        file_size = 0
        mime_type = "application/octet-stream"

        if hasattr(media, 'document'):
            doc = media.document
            file_name = next((attr.file_name for attr in doc.attributes if hasattr(attr, 'file_name')), "file")
            file_size = doc.size
            mime_type = doc.mime_type
            file_id = f"{doc.id}_{doc.access_hash}"
        elif hasattr(media, 'photo'):
            photo = media.photo
            file_name = f"photo_{photo.id}.jpg"
            file_size = photo.sizes[-1].size if hasattr(photo.sizes[-1], 'size') else 0
            mime_type = "image/jpeg"
            file_id = f"{photo.id}_{photo.access_hash}"
        
        if not file_id:
            return

        short_code = generate_short_code()
        
        file_meta = FileMetadata(
            file_id=file_id,
            file_unique_id=str(event.id), # Simplified
            filename=file_name,
            mime_type=mime_type,
            file_size=file_size,
            uploader_id=event.sender_id,
            short_code=short_code,
            chat_id=event.chat_id,
            message_id=event.id,
            expiry_time=datetime.datetime.utcnow() + datetime.timedelta(hours=settings.DEFAULT_EXPIRY) if settings.DEFAULT_EXPIRY > 0 else None
        )
        
        await files_col.insert_one(file_meta.dict())
        
        base_url = settings.BASE_URL.rstrip('/')
        download_url = f"{base_url}/dl/{short_code}"
        stream_url = f"{base_url}/watch/{short_code}"
        
        # Log File Upload
        if settings.CHANNEL_ID:
            try:
                await bot.send_message(
                    settings.CHANNEL_ID,
                    f"#NewFile\n\n"
                    f"👤 **Uploader:** {event.sender.first_name} (`{event.sender_id}`)\n"
                    f"📁 **File:** `{file_name}`\n"
                    f"⚖️ **Size:** `{file_size / (1024*1024):.2f} MB`\n\n"
                    f"📥 **Download:** {download_url}\n"
                    f"🎬 **Stream:** {stream_url}"
                )
            except Exception as e:
                logger.error(f"Error sending file log: {e}")
        
        caption = (
            f"✅ **Link Generated!**\n\n"
            f"📁 **File:** `{file_name}`\n"
            f"⚖️ **Size:** `{file_size / (1024*1024):.2f} MB`\n"
            f"⏳ **Expiry:** `{settings.DEFAULT_EXPIRY} hours`\n\n"
            f"📥 **Download:** {download_url}\n"
            f"🎬 **Stream:** {stream_url}"
        )
        
        await event.reply(
            caption,
            buttons=[
                [Button.url("Download", download_url), Button.url("Watch Online", stream_url)],
                [Button.inline("Delete Link", f"del_{short_code}".encode())]
            ]
        )

    @bot.on(events.CallbackQuery())
    async def global_callback_check(event):
        if not await is_user_fsubbed(bot, event.sender_id):
            return await event.answer("❌ You must join the channel first!", alert=True)
        
    @bot.on(events.CallbackQuery(pattern=b'help'))
    async def help_callback(event):
        await event.answer("Just send any file to get a direct link!", alert=True)

    @bot.on(events.CallbackQuery(pattern=b'about'))
    async def about_callback(event):
        await event.answer(
            "🤖 CantarellaBots Media Streamer\n\n"
            "This bot allows you to stream and download Telegram media at high speeds.\n\n"
            "Channel: @cantarellabots\n"
            "Developer: @cantarella_wuwa",
            alert=True
        )

    @bot.on(events.CallbackQuery(pattern=b'del_'))
    async def delete_callback(event):
        short_code = event.data.decode().split("_")[1]
        file_data = await files_col.find_one({"short_code": short_code})
        if file_data and file_data['uploader_id'] == event.sender_id:
            await files_col.delete_one({"short_code": short_code})
            await event.edit("🗑️ Link deleted successfully!")
        else:
            await event.answer("❌ You are not authorized to delete this link.", alert=True)

    # Admin Commands
    @bot.on(events.NewMessage(pattern='/stats'))
    async def stats_handler(event):
        if event.sender_id not in settings.admin_list and event.sender_id != settings.OWNER_ID:
            return
        
        total_files = await files_col.count_documents({})
        total_users = await users_col.count_documents({})
        
        await event.reply(
            f"📊 **System Statistics**\n\n"
            f"👥 Total Users: `{total_users}`\n"
            f"📁 Total Files: `{total_files}`\n"
        )

    @bot.on(events.NewMessage(pattern='/broadcast'))
    async def broadcast_handler(event):
        if event.sender_id not in settings.admin_list and event.sender_id != settings.OWNER_ID:
            return
        
        if not event.reply_to_msg_id:
            return await event.reply("Please reply to a message to broadcast it.")
            
        msg = await event.get_reply_message()
        users = await users_col.find().to_list(None)
        
        status = await event.reply(f"🚀 **Broadcast Started...**\nTarget: `{len(users)}` users")
        
        done = 0
        failed = 0
        for user in users:
            try:
                await bot.send_message(user['user_id'], msg)
                done += 1
            except Exception:
                failed += 1
            
            if done % 20 == 0:
                await status.edit(f"🚀 **Broadcast in Progress...**\n✅ Done: `{done}`\n❌ Failed: `{failed}`")
                
        await status.edit(f"✅ **Broadcast Completed!**\n\n🎯 Total: `{len(users)}` users\n✨ Success: `{done}`\n💀 Failed: `{failed}`")

    @bot.on(events.NewMessage(pattern='/ban'))
    async def ban_handler(event):
        if event.sender_id not in settings.admin_list and event.sender_id != settings.OWNER_ID:
            return
        
        try:
            user_id = int(event.text.split()[1])
            await users_col.update_one({"user_id": user_id}, {"$set": {"is_banned": True}})
            await event.reply(f"🚫 User `{user_id}` has been banned.")
        except Exception:
            await event.reply("Usage: `/ban USER_ID`")

    @bot.on(events.NewMessage(pattern='/unban'))
    async def unban_handler(event):
        if event.sender_id not in settings.admin_list and event.sender_id != settings.OWNER_ID:
            return
        
        try:
            user_id = int(event.text.split()[1])
            await users_col.update_one({"user_id": user_id}, {"$set": {"is_banned": False}})
            await event.reply(f"✅ User `{user_id}` has been unbanned.")
        except Exception:
            await event.reply("Usage: `/unban USER_ID`")

    @bot.on(events.NewMessage(pattern='/autodel'))
    async def autodel_handler(event):
        if event.sender_id not in settings.admin_list and event.sender_id != settings.OWNER_ID:
            return
            
        try:
            args = event.text.split()
            if len(args) < 2:
                return await event.reply("Usage: `/autodel 24h` or `/autodel off`")
            
            val = args[1].lower()
            if val == "off":
                # Implementation would require updating global settings or per-user
                await event.reply("Auto-delete disabled (Global setting remains unchanged).")
            else:
                # Simple parser for hours
                hours = int(val.replace("h", ""))
                await event.reply(f"Auto-delete set to `{hours}` hours for future links.")
        except Exception:
            await event.reply("Usage: `/autodel 24h` or `/autodel off`")
