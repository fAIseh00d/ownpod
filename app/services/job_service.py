import json
import time
import datetime
import threading
import random
import math
from typing import Dict, Any, Generator

from config.settings import (
    RESULT_TTL_SEC,
    JOB_KEY,
)
from services.worker_service import mark_idle, set_worker_job
from utils.logging_setup import logger
from utils.redis_client import get_redis
from utils.http_client import get_session, get_verify_param, timeouts_for

# Connect to Redis
r = get_redis()

def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"

def set_fields(jid: str, fields: Dict[str, Any]):
    r.hset(JOB_KEY(jid), mapping=fields)

def get_job(jid: str) -> Dict[str, Any]:
    return r.hgetall(JOB_KEY(jid)) or {}

def expire(jid: str):
    r.expire(JOB_KEY(jid), RESULT_TTL_SEC)

def response_status(job: Dict[str, Any]) -> Dict[str, Any]:
    state = job.get("status", "UNKNOWN")
    # Map RUNNING -> IN_PROGRESS for client compatibility
    client_state = "IN_PROGRESS" if state == "RUNNING" else state
    resp: Dict[str, Any] = {"id": job["id"], "status": client_state}
    if "delay_ms" in job: resp["delayTime"] = int(job["delay_ms"])
    if "exec_ms" in job:  resp["executionTime"] = int(job["exec_ms"])
    if job.get("worker_id"):
        resp["workerId"] = job.get("worker_id")
    if job.get("status") == "COMPLETED" and "output_json" in job:
        try: resp["output"] = json.loads(job["output_json"])
        except Exception: resp["output"] = job["output_json"]
    if job.get("status") in ("FAILED", "TIMED_OUT", "CANCELLED") and "error" in job:
        resp["error"] = job["error"]
    return resp

def state_map_from_worker(status: str) -> str:
    # Worker local API uses same states; keep a defensive map
    m = {
        "IN_QUEUE": "IN_QUEUE",
        "RUNNING": "RUNNING",
        "COMPLETED": "COMPLETED",
        "FAILED": "FAILED",
        "CANCELLED": "CANCELLED",
        "CANCELED": "CANCELLED",
        "TIMED_OUT": "TIMED_OUT",
    }
    return m.get(status, status)

def monitor_job(job_id: str, worker_url: str, worker_job_id: str):
    try:
        logger.debug(f"monitor start job={job_id} worker={worker_url} worker_job_id={worker_job_id}")
        max_retries = 600
        retry_count = 0
        backoff = 0.2
        last_state = None
        
        while retry_count < max_retries:
            try:
                if retry_count % 10 == 0:
                    logger.debug(f"monitor poll job={job_id} attempt={retry_count+1}")
                
                # First try POST - most RunPod workers implement this
                session = get_session()
                try:
                    resp = session.post(
                        f"{worker_url}/status/{worker_job_id}",
                        timeout=timeouts_for("status"),
                        verify=get_verify_param(),
                    )
                    resp.raise_for_status()
                except Exception as e:
                    # Fallback to GET if POST fails
                    resp = session.get(
                        f"{worker_url}/status/{worker_job_id}",
                        timeout=timeouts_for("status"),
                        verify=get_verify_param(),
                    )
                
                w = resp.json()
                state = state_map_from_worker(w.get("status", "UNKNOWN"))
                if state != last_state:
                    logger.info(f"job {job_id} state {state}")
                    last_state = state
                fields: Dict[str, Any] = {}
                
                if state == "COMPLETED":
                    # keep output JSON as-is
                    if "output" in w:
                        fields["output_json"] = json.dumps(w["output"])
                    fields["status"] = "COMPLETED"
                    fields["completed_at"] = _now_iso()
                    # carry over exec time if provided
                    if "executionTime" in w:
                        fields["exec_ms"] = int(w["executionTime"])
                    elif "started_ms" in get_job(job_id):
                        try:
                            started_ms = int(get_job(job_id).get("started_ms", 0))
                            fields["exec_ms"] = max(0, int(time.time() * 1000) - started_ms)
                        except Exception:
                            pass
                    set_fields(job_id, fields); expire(job_id)
                    break
                elif state in ("FAILED", "TIMED_OUT", "CANCELLED"):
                    fields["status"] = state
                    if "error" in w: fields["error"] = str(w["error"])
                    fields["completed_at"] = _now_iso()
                    set_fields(job_id, fields); expire(job_id)
                    break
                
                retry_count += 1
                # exponential backoff with jitter, cap at 2s
                backoff = min(backoff * 1.5, 2.0)
                time.sleep(backoff + random.uniform(0, 0.1))
            except Exception as e:
                logger.warning(f"monitor error job={job_id}: {e}")
                retry_count += 1
                time.sleep(min(backoff * 2, 2.0) + random.uniform(0, 0.2))
    except Exception as e:
        set_fields(job_id, {"status": "FAILED", "error": f"monitor error: {e}", "completed_at": _now_iso()})
        expire(job_id)
    finally:
        mark_idle(worker_url)

def get_stream_generator(worker_url: str, worker_job_id: str, job_id: str) -> Generator[bytes, None, None]:
    logger.info(f"stream relay start jid={job_id} worker={worker_url}")
    session = get_session()
    with session.get(
        f"{worker_url}/stream/{worker_job_id}",
        stream=True,
        timeout=(timeouts_for("run")[0], None),
        verify=get_verify_param(),
    ) as rstream:
        rstream.raise_for_status()
        for chunk in rstream.iter_lines():
            if chunk:
                yield chunk + b"\n"
