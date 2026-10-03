from app.cache.redis_client import redis_client
import time

# Memory fallback for when Redis is unavailable
memory_cache = {}

async def check_rate_limit(user_id: int, limit: int = 5, period: int = 60):
    """
    Check if a user has exceeded the rate limit.
    limit: number of requests
    period: time window in seconds
    """
    key = f"rate_limit:{user_id}"
    now = time.time()
    
    try:
        current_count = await redis_client.get(key)
        
        if current_count and int(current_count) >= limit:
            return False
            
        async with redis_client.pipeline(transaction=True) as pipe:
            await pipe.incr(key)
            await pipe.expire(key, period)
            await pipe.execute()
        return True
    except Exception as e:
        # Fallback to in-memory rate limiting if Redis is down
        if user_id not in memory_cache:
            memory_cache[user_id] = []
        
        # Clean up old timestamps
        memory_cache[user_id] = [t for t in memory_cache[user_id] if now - t < period]
        
        if len(memory_cache[user_id]) >= limit:
            return False
            
        memory_cache[user_id].append(now)
        return True
