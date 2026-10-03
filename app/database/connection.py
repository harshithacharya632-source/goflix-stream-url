from motor.motor_asyncio import AsyncIOMotorClient
from pydantic_settings import BaseSettings
from typing import Optional
import os

class Settings(BaseSettings):
    MONGODB_URI: str = "mongodb://localhost:27017/tg_media_bot"
    REDIS_URL: str = "redis://localhost:6379/0"
    API_ID: int
    API_HASH: str
    BOT_TOKEN: str
    OWNER_ID: int
    ADMINS: str = ""
    FORCE_SUB_CHANNELS: str = ""
    BASE_URL: str = "http://localhost:8000"
    DEFAULT_EXPIRY: int = 24  # hours
    CHANNEL_ID: Optional[int] = None
    PORT: int = 8000
    DEBUG: bool = False
    SESSIONS: str = ""
    
    @property
    def admin_list(self):
        return [int(x) for x in self.ADMINS.split(",") if x.strip()]
    
    @property
    def fsub_list(self):
        return [int(x) for x in self.FORCE_SUB_CHANNELS.split(",") if x.strip()]

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()

client = AsyncIOMotorClient(settings.MONGODB_URI)
db = client.get_database("tg_media_bot")

# Collections
files_col = db.files
users_col = db.users
settings_col = db.settings
logs_col = db.logs

async def create_indexes():
    await files_col.create_index("short_code", unique=True)
    await files_col.create_index("file_id")
    await users_col.create_index("user_id", unique=True)
    await files_col.create_index("expiry_time")
