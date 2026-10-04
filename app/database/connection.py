from pydantic_settings import BaseSettings

# Pure streaming service: no bot commands, no database, no force-subscribe.
# Every file is read live from Telegram by channel + message ID -- CHANNEL_ID
# must be the same log/storage channel your main Goflix bot already forwards
# uploads into, so this service can see the same files.
class Settings(BaseSettings):
    API_ID: int
    API_HASH: str
    BOT_TOKEN: str
    CHANNEL_ID: int  # the log/storage channel your main bot forwards files into

    # Optional: comma-separated Telegram USER session strings for faster
    # parallel streaming. Leave blank to stream using just the bot token.
    SESSIONS: str = ""

    # Optional: extra BOT tokens (comma-separated) for more speed. Telegram limits
    # how fast ONE account can download, so every extra bot adds another lane.
    # Each extra bot must be an admin/member of the CHANNEL_ID storage channel.
    BOT_TOKENS: str = ""

    BASE_URL: str = "http://localhost:8000"

    # Optional: used for rate limiting; falls back to in-memory automatically
    # if Redis isn't reachable (see app/utils/rate_limit.py).
    REDIS_URL: str = "redis://localhost:6379/0"

    PORT: int = 8000
    DEBUG: bool = False

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()
