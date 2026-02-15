import os

import redis

url = os.environ.get("PIXAV_REDIS_URL", "redis://:pixav@localhost:6379/0")
print(f"Checking Redis at: {url}")

try:
    r = redis.from_url(url)
    print(f"Ping response: {r.ping()}")
except redis.exceptions.AuthenticationError:
    print("AUTH REQUIRED (AuthenticationError)")
except Exception as e:
    print(f"ERROR: {e}")
