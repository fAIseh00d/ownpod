from functools import lru_cache
from redis import Redis
from config.settings import REDIS_URL


@lru_cache(maxsize=1)
def get_redis() -> Redis:
    return Redis.from_url(REDIS_URL, decode_responses=True)


