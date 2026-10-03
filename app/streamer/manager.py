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
        # Start Bot Client
        self.bot_client = TelegramClient(MemorySession(), settings.API_ID, settings.API_HASH)
        await self.bot_client.start(bot_token=settings.BOT_TOKEN)
        logger.info("Bot client started")

        # Start User Clients (for high-speed streaming)
        session_strings = [s.strip() for s in settings.SESSIONS.split(",") if s.strip()]
        
        if not session_strings:
            logger.warning("No user sessions found! Using bot for streaming.")
            self.clients.append(self.bot_client)
        else:
            for i, session_str in enumerate(session_strings):
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
            
            if not self.clients:
                self.clients.append(self.bot_client)

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
