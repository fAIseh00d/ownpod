import time
import uuid
import json
import math
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from config.settings import DESCRIPTION_PREFIX, QUEUE_KEY, RUNSYNC_TIMEOUT_MS, JOB_KEY, IDEMPOTENCY_TTL_SEC
from models.schema import RunPayload
from services.job_service import set_fields, get_job, expire, response_status, get_stream_generator, _now_iso
from services.worker_service import get_worker_status, has_healthy_worker
from services.queue_service import purge_queue
from utils.logging_setup import logger
from utils.redis_client import get_redis
from utils.http_client import get_session, get_verify_param, timeouts_for

# Redis connection
r = get_redis()

# Create router
router = APIRouter(prefix="/v2")

@router.get("/{endpoint_id}/health")
def health(endpoint_id: str):
    try:
        pong = r.ping()
        qlen = r.llen(QUEUE_KEY)
        workers = get_worker_status()
        return {"status": "OK", "redis": pong, "queue_length": qlen, "workers": workers}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"redis error: {e}")

@router.post("/{endpoint_id}/run")
def run_async(endpoint_id: str, req: RunPayload, request: Request):
    _ = request.headers.get("Authorization")  # accepted/ignored locally
    idem = request.headers.get("Idempotency-Key")
    # Fail fast if no workers are reachable
    # Fast-fail if no healthy workers detected by background monitor
    if not has_healthy_worker():
        raise HTTPException(status_code=503, detail="no healthy workers available")
    if idem:
        key = f"ownpod:idemp:{idem}"
        # Fast path: existing
        cached = r.get(key)
        if cached:
            try:
                return json.loads(cached)
            except Exception:
                pass
        # Reserve atomically if not present
        # NX ensures only first caller creates it; EX sets TTL
        if not r.set(key, "__pending__", nx=True, ex=IDEMPOTENCY_TTL_SEC):
            # another request won the race; return its value when set or 409 if still pending
            cached = r.get(key)
            if cached and cached != "__pending__":
                try:
                    return json.loads(cached)
                except Exception:
                    pass
            # pending -> treat as already accepted
            return {"status": "IN_QUEUE"}

    jid = uuid.uuid4().hex
    enq = _now_iso()
    enq_ms = int(time.time() * 1000)
    # store job record
    with r.pipeline() as p:
        p.hset(JOB_KEY(jid), mapping={
            "id": jid,
            "status": "IN_QUEUE",
            "input_envelope": req.model_dump_json(),
            "enqueued_at": enq,
            "enqueued_ms": enq_ms,
            "description": f"{DESCRIPTION_PREFIX}:{str(req.input)[:80]}",
        })
        # push to queue
        p.lpush(QUEUE_KEY, jid)
        p.execute()
    resp = {"id": jid, "status": "IN_QUEUE"}
    if idem:
        try:
            r.setex(f"ownpod:idemp:{idem}", IDEMPOTENCY_TTL_SEC, json.dumps(resp))
        except Exception:
            pass
    return resp

@router.post("/{endpoint_id}/runsync")
def run_sync(endpoint_id: str, req: RunPayload, request: Request):
    _ = request.headers.get("Authorization")
    logger.debug(f"runsync recv keys={list((req.input or {}).keys())}")

    timeout_ms = RUNSYNC_TIMEOUT_MS

    # Enqueue the job via async path
    resp = run_async(endpoint_id, req, request)
    jid = resp["id"]

    logger.info(f"runsync wait jid={jid} timeout_ms={timeout_ms}")
    deadline = time.time() + (timeout_ms / 1000.0)

    # Poll status until terminal or timeout
    check_interval = 0.2
    while time.time() < deadline:
        job = get_job(jid)
        status = job.get("status", "UNKNOWN")
        if status in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
            return response_status(job)
        time.sleep(check_interval)

    # Timed out: return current status snapshot
    return response_status(get_job(jid))

@router.get("/{endpoint_id}/status/{job_id}")
def status(endpoint_id: str, job_id: str, ttl: Optional[int] = None, request: Request = None):
    job = get_job(job_id)
    if not job: raise HTTPException(status_code=404, detail="job not found")
    if ttl:
        try: r.expire(JOB_KEY(job_id), max(1, int(math.ceil(ttl/1000.0))))
        except Exception: pass
    body = response_status(job)
    # ETag over status + exec/delay time + completed_at
    etag_src = json.dumps({k: body.get(k) for k in ("status","executionTime","delayTime","id","workerId")}, sort_keys=True)
    import hashlib
    etag = hashlib.sha256(etag_src.encode()).hexdigest()
    inm = request.headers.get("If-None-Match") if request else None
    from fastapi import Response
    if inm and inm == etag:
        return Response(status_code=304)
    resp = Response(content=json.dumps(body), media_type="application/json")
    resp.headers["ETag"] = etag
    return resp

@router.get("/{endpoint_id}/stream/{job_id}")
def stream(endpoint_id: str, job_id: str):
    job = get_job(job_id)
    if not job: raise HTTPException(status_code=404, detail="job not found")
    wurl = job.get("worker_url"); wid = job.get("worker_job_id")
    if not (wurl and wid): raise HTTPException(status_code=409, detail="job not assigned yet")
    
    return StreamingResponse(get_stream_generator(wurl, wid, job_id), media_type="text/event-stream")

@router.post("/{endpoint_id}/cancel/{job_id}")
def cancel(endpoint_id: str, job_id: str):
    job = get_job(job_id)
    if not job: raise HTTPException(status_code=404, detail="job not found")

    state = job.get("status")
    if state == "IN_QUEUE":
        # Mark as cancelled; dispatcher will skip if it ever pops it
        set_fields(job_id, {"status": "CANCELLED", "completed_at": _now_iso()}); expire(job_id)
        return {"id": job_id, "status": "CANCELLED"}
    if state == "RUNNING":
        wurl, wid = job.get("worker_url"), job.get("worker_job_id")
        if wurl and wid:
            try:
                session = get_session()
                session.post(
                    f"{wurl}/cancel/{wid}",
                    timeout=timeouts_for("cancel"),
                    verify=get_verify_param(),
                )
            except Exception:
                pass
        set_fields(job_id, {"status": "CANCELLED", "completed_at": _now_iso()}); expire(job_id)
        return {"id": job_id, "status": "CANCELLED"}
    return {"id": job_id, "status": state}

@router.post("/{endpoint_id}/retry/{job_id}")
def retry(endpoint_id: str, job_id: str):
    job = get_job(job_id)
    if not job: raise HTTPException(status_code=404, detail="job not found")
    if job.get("status") not in ("FAILED", "TIMED_OUT"):
        raise HTTPException(status_code=400, detail="only FAILED or TIMED_OUT can be retried")
    payload = json.loads(job["input_envelope"])
    return run_async(endpoint_id, RunPayload(**payload), Request({"type":"http"}))

@router.post("/{endpoint_id}/purge-queue")
def purge_queue_endpoint(endpoint_id: str):
    try:
        return purge_queue()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"purge failed: {e}")
