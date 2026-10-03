import asyncio
import datetime
from app.database.connection import files_col
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cleanup_worker")

async def cleanup_expired_links():
    while True:
        try:
            now = datetime.datetime.utcnow()
            result = await files_col.delete_many({
                "expiry_time": {"$lt": now},
                "expiry_time": {"$ne": None}
            })
            if result.deleted_count > 0:
                logger.info(f"Deleted {result.deleted_count} expired links")
        except Exception as e:
            logger.error(f"Error in cleanup worker: {e}")
            
        await asyncio.sleep(3600) # Check every hour

if __name__ == "__main__":
    asyncio.run(cleanup_expired_links())
