import os

# Configuration settings
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
RESULT_TTL_SEC = int(os.getenv("OWNPOD_RESULT_TTL_SEC", "1800"))
RUNSYNC_TIMEOUT_MS = int(os.getenv("OWNPOD_RUNSYNC_TIMEOUT_MS", "30000"))
DESCRIPTION_PREFIX = os.getenv("OWNPOD_DESCRIPTION_PREFIX", "ownpod")
WORKER_URLS = [u.strip() for u in os.getenv("OWNPOD_WORKERS", "http://localhost:18000").split(",") if u.strip()]
LOG_LEVEL = os.getenv("OWNPOD_LOG_LEVEL", "INFO").upper()
IDEMPOTENCY_TTL_SEC = int(os.getenv("OWNPOD_IDEMPOTENCY_TTL_SEC", "86400"))

# Helpers
def _parse_bool(value: str, default: bool = True) -> bool:
    if value is None:
        return default
    v = str(value).strip().lower()
    if v in ("1", "true", "yes", "y", "on"): return True
    if v in ("0", "false", "no", "n", "off"): return False
    return default

# Remote worker HTTP configuration
WORKER_VERIFY_TLS = _parse_bool(os.getenv("OWNPOD_WORKER_VERIFY_TLS", "true"), True)
# Optional custom CA bundle path for self-signed certs; takes precedence over boolean verify
WORKER_CA_BUNDLE = os.getenv("OWNPOD_WORKER_CA_BUNDLE") or ""

# Optional headers for requests to workers (JSON object string)
import json as _json
_headers_raw = os.getenv("OWNPOD_WORKER_HEADERS", "")
try:
    WORKER_HEADERS = _json.loads(_headers_raw) if _headers_raw.strip() else {}
    if not isinstance(WORKER_HEADERS, dict):
        WORKER_HEADERS = {}
except Exception:
    WORKER_HEADERS = {}

# Timeouts (seconds)
CONNECT_TIMEOUT_S = float(os.getenv("OWNPOD_WORKER_CONNECT_TIMEOUT_S", "5"))
RUN_READ_TIMEOUT_S = float(os.getenv("OWNPOD_WORKER_RUN_READ_TIMEOUT_S", "30"))
STATUS_TIMEOUT_S = float(os.getenv("OWNPOD_WORKER_STATUS_TIMEOUT_S", "15"))
CANCEL_TIMEOUT_S = float(os.getenv("OWNPOD_WORKER_CANCEL_TIMEOUT_S", "10"))

# Redis keys
QUEUE_KEY = "ownpod:queue"
JOB_KEY = lambda jid: f"ownpod:job:{jid}"
