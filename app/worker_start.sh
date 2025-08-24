#!/usr/bin/env bash
set -e

# Wait for Redis to be reachable
python - <<'PY'
import os, time, sys, redis
url = os.getenv("REDIS_URL", "redis://redis:6379/0")
r = redis.from_url(url)
for _ in range(60):
    try:
        if r.ping():
            sys.exit(0)
    except Exception:
        time.sleep(1)
print("Redis not reachable after 60s", file=sys.stderr); sys.exit(1)
PY

# Single-process worker → strictly one job at a time on this GPU
exec rq worker --url "$REDIS_URL" default
