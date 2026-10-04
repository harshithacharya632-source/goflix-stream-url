from telethon import TelegramClient
from telethon.sessions import StringSession
from app.database.connection import settings
import logging

logger = logging.getLogger(__name__)

class SessionManager:
    def __init__(self):
        self.clients = []
        self.bot_client = None
        self._index = 0

    async def start(self):
        from telethon.sessions import MemorySession
        from telethon.crypto import aes
        if aes.cryptg:
            logger.info("cryptg is active: fast download decryption")
        else:
            logger.warning("cryptg is NOT installed: downloads will be VERY slow. pip install cryptg")

        # Main bot client
        self.bot_client = TelegramClient(MemorySession(), settings.API_ID, settings.API_HASH)
        await self.bot_client.start(bot_token=settings.BOT_TOKEN)
        logger.info("Bot client started")
        self.clients = [self.bot_client]

        # Extra bots: each one is another download lane (Telegram limits one account's speed)
        for i, token in enumerate(t.strip() for t in settings.BOT_TOKENS.split(",") if t.strip()):
            if token == settings.BOT_TOKEN:
                continue
            try:
                extra = TelegramClient(MemorySession(), settings.API_ID, settings.API_HASH, connection_retries=5)
                await extra.start(bot_token=token)
                self.clients.append(extra)
                logger.info(f"Extra bot {i+1} started")
            except Exception as e:
                logger.error(f"Extra bot {i+1} failed: {e}")

        # User sessions (also fast lanes). They must be members of the storage channel.
        for i, session_str in enumerate(s.strip() for s in settings.SESSIONS.split(",") if s.strip()):
            try:
                client = TelegramClient(
                    StringSession(session_str),
                    settings.API_ID,
                    settings.API_HASH,
                    connection_retries=5
                )
                await client.start()
                self.clients.append(client)
                logger.info(f"User Session {i+1} started")
            except Exception as e:
                logger.error(f"Session {i+1} failed: {e}")

        logger.info(f"Streaming with {len(self.clients)} Telegram connection(s)")

    async def stop(self):
        for client in self.clients:
            await client.disconnect()

    def get_client(self):
        if not self.clients:
            return self.bot_client
        client = self.clients[self._index]
        self._index = (self._index + 1) % len(self.clients)
        return client

    def get_all_clients(self):
        """Return all available clients for parallel downloading"""
        return self.clients if self.clients else [self.bot_client]

session_manager = SessionManager()
