import threading
from typing import Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config.settings import (
    WORKER_HEADERS,
    WORKER_VERIFY_TLS,
    WORKER_CA_BUNDLE,
    CONNECT_TIMEOUT_S,
    RUN_READ_TIMEOUT_S,
    STATUS_TIMEOUT_S,
    CANCEL_TIMEOUT_S,
)


_thread_local = threading.local()


def _create_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=2,
        backoff_factor=0.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(pool_connections=16, pool_maxsize=64, max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    if WORKER_HEADERS:
        session.headers.update(WORKER_HEADERS)
    return session


def get_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = _create_session()
        _thread_local.session = session
    return session


def get_verify_param():
    return WORKER_CA_BUNDLE or WORKER_VERIFY_TLS


def timeouts_for(kind: str) -> Tuple[float, float | None]:
    if kind == "run":
        return (CONNECT_TIMEOUT_S, RUN_READ_TIMEOUT_S)
    if kind == "status":
        return (CONNECT_TIMEOUT_S, STATUS_TIMEOUT_S)
    if kind == "cancel":
        return (CONNECT_TIMEOUT_S, CANCEL_TIMEOUT_S)
    return (CONNECT_TIMEOUT_S, RUN_READ_TIMEOUT_S)


