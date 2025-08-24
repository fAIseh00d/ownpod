import json
import threading
import time
import datetime
from typing import Dict, Any

from config.settings import (
    QUEUE_KEY,
)
from services.worker_service import claim_idle_worker, mark_idle, get_stop_event, set_worker_job, get_worker_id
from services.job_service import get_job, set_fields, expire, _now_iso, monitor_job
from utils.logging_setup import logger
from utils.redis_client import get_redis
from utils.http_client import get_session, get_verify_param, timeouts_for

# Connect to Redis
r = get_redis()

def dispatch_loop():
    logger.info(f"dispatcher start")
    stop_event = get_stop_event()
    
    while not stop_event.is_set():
        # only proceed if at least one worker is idle
        widle = claim_idle_worker()
        if not widle:
            time.sleep(0.1)
            continue

        # pop next job id (1s timeout to allow loop exit/heartbeat)
        item = r.brpop(QUEUE_KEY, timeout=1)
        if not item:
            mark_idle(widle)  # release the worker we claimed
            continue
            
        _, jid = item
        logger.debug(f"queue pop job={jid}")
        job = get_job(jid)
        if not job:
            logger.warning(f"job missing jid={jid}")
            mark_idle(widle)
            continue
            
        # cancel/drop if already terminal
        if job.get("status") not in (None, "", "IN_QUEUE"):
            logger.debug(f"skip terminal jid={jid} status={job.get('status')}")
            mark_idle(widle)
            continue

        # compute delay and mark RUNNING
        started = _now_iso()
        delay_ms = None
        # prefer enqueued_ms if present (set at enqueue time), fallback to ISO calc
        try:
            if "enqueued_ms" in job:
                delay_ms = int(time.time() * 1000) - int(job.get("enqueued_ms", 0))
            else:
                enq = job.get("enqueued_at")
                if enq:
                    t0 = datetime.datetime.fromisoformat(enq[:-1])
                    t1 = datetime.datetime.fromisoformat(started[:-1])
                    delay_ms = int((t1 - t0).total_seconds() * 1000)
        except Exception:
            delay_ms = None

        # mark job running with assigned worker
        fields = {"status": "RUNNING", "started_at": started, "worker_url": widle}
        if delay_ms is not None: fields["delay_ms"] = delay_ms
        # store worker id and start timestamp in ms for execution time
        try:
            fields["worker_id"] = get_worker_id(widle) or ""
            fields["started_ms"] = int(time.time() * 1000)
        except Exception:
            pass
        set_fields(jid, fields)

        # send to worker
        try:
            payload = json.loads(job["input_envelope"])
            logger.info(f"dispatch jid={jid} worker={widle}")

            # Send to worker's /run endpoint with the expected shape
            input_data = payload.get("input", {})
            logger.debug(f"dispatch input keys={list(input_data.keys())}")

            session = get_session()
            resp = session.post(
                f"{widle}/run",
                json={"input": input_data},
                timeout=timeouts_for("run"),
                verify=get_verify_param(),
            )
            resp.raise_for_status()

            wresp = resp.json()
            logger.debug(f"run resp keys={list(wresp.keys())}")
            worker_job_id = wresp.get("id") or wresp.get("job_id") or wresp.get("jobId")
            if not worker_job_id:
                raise RuntimeError(f"worker did not return job id: {wresp}")

            # update job with worker assignment
            assign_fields = {"worker_job_id": worker_job_id}
            set_fields(jid, assign_fields)
            set_worker_job(widle, jid)

            # start monitor thread to track completion
            threading.Thread(target=monitor_job, args=(jid, widle, worker_job_id), daemon=True).start()
        except Exception as e:
            set_fields(jid, {"status": "FAILED", "error": f"dispatch error: {e}", "completed_at": _now_iso()})
            expire(jid)
            mark_idle(widle)
            continue

# Initialize the queue (ensure queue key exists)
def init_queue():
    # Empty list without deleting key
    r.ltrim(QUEUE_KEY, 1, 0)

# Purge the queue
def purge_queue():
    r.delete(QUEUE_KEY)
    r.ltrim(QUEUE_KEY, 1, 0)  # recreate empty list
    return {"status": "OK", "queue_length": 0}
