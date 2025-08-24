import threading
import time
import hashlib
import base64
from typing import Dict, Any, Optional

from config.settings import WORKER_URLS
from utils.http_client import get_session, get_verify_param, timeouts_for
from utils.logging_setup import logger

# Worker state management
_worker_lock = threading.Lock()
def _gen_worker_id(url: str) -> str:
    d = hashlib.sha1(url.encode()).digest()
    b32 = base64.b32encode(d).decode().lower().rstrip("=")
    return b32[:14]

_workers: Dict[str, Dict[str, Any]] = {
    u: {"busy": False, "job": None, "last": 0.0, "id": _gen_worker_id(u), "healthy": True, "last_probe": 0.0} for u in WORKER_URLS
}
_stop = threading.Event()
_rr_index = 0  # round-robin pointer

def claim_idle_worker() -> Optional[str]:
    global _rr_index
    with _worker_lock:
        if not WORKER_URLS:
            return None
        n = len(WORKER_URLS)
        start = _rr_index % n
        for i in range(n):
            idx = (start + i) % n
            url = WORKER_URLS[idx]
            st = _workers.get(url)
            if st and not st["busy"] and st.get("healthy", True):
                st["busy"] = True
                st["job"] = None
                st["last"] = time.time()
                _rr_index = (idx + 1) % n
                return url
    return None

def mark_idle(url: str):
    with _worker_lock:
        if url in _workers:
            _workers[url]["busy"] = False
            _workers[url]["job"] = None
            _workers[url]["last"] = time.time()

def get_worker_status():
    with _worker_lock:
        return {u: {"busy": st["busy"], "job": st["job"], "id": st.get("id"), "healthy": st.get("healthy", True)} for u, st in _workers.items()}

def set_worker_job(worker_url: str, job_id: str):
    with _worker_lock:
        if worker_url in _workers:
            _workers[worker_url]["job"] = job_id

def get_stop_event():
    return _stop

def get_worker_id(worker_url: str) -> Optional[str]:
    with _worker_lock:
        st = _workers.get(worker_url)
        return st.get("id") if st else None

def _probe_worker(url: str) -> bool:
    try:
        session = get_session()
        # Any response (even 404) means host is reachable; we only care about transport
        resp = session.get(f"{url}/status/ping", timeout=timeouts_for("status"), verify=get_verify_param())
        return True
    except Exception:
        return False

def has_healthy_worker() -> bool:
    with _worker_lock:
        for st in _workers.values():
            if st.get("healthy", True):
                return True
    return False

def _health_loop(interval_s: float = 3.0):
    logger.info("worker health monitor start")
    while not _stop.is_set():
        for url in WORKER_URLS:
            try:
                ok = _probe_worker(url)
                with _worker_lock:
                    st = _workers.get(url)
                    if st is None:
                        continue
                    prev = st.get("healthy", True)
                    st["healthy"] = bool(ok)
                    st["last_probe"] = time.time()
                if ok != prev:
                    logger.info(f"worker {url} healthy={ok}")
            except Exception as e:
                logger.warning(f"health probe error {url}: {e}")
        _stop.wait(interval_s)

def start_health_monitor():
    threading.Thread(target=_health_loop, daemon=True).start()
