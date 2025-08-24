# OwnPod

Self-hosted **RunPod-style queued endpoints** that dispatch to your **existing RunPod worker containers** over HTTP.
OwnPod exposes the **RunPod v2** API locally (`/v2/{endpoint_id}/…`) and schedules jobs across any number of worker URLs you provide.

---

## What this is

* **RunPod v2-compatible API**: `/run`, `/runsync`, `/status/{id}`, `/stream/{id}`, `/cancel/{id}`, `/retry/{id}`, `/purge-queue`, `/health`. Plus optional extensions (Idempotency-Key, ETag caching, `workerId`).
* **Central queue + dispatcher**: Redis-backed queue. OwnPod assigns jobs to idle workers and tracks their job IDs.
* **Workers are black boxes**: any container that serves the RunPod local API (`--rp_serve_api`) works. No code changes to your handlers.
* **Scales by list**: give OwnPod a comma-separated list of worker base URLs; it will fan out. Works with 1, 2, or N workers.

---

## Architecture

```
Client ──HTTP──> OwnPod API (/v2/endpoint/*)
                    │
                    ├── enqueue job (Redis list)
                    │
                    └── dispatcher → picks idle worker URL
                                ├─ POST {worker}/run      → returns worker_job_id
                                ├─ GET  {worker}/status   → proxy status
                                ├─ GET  {worker}/stream   → proxy SSE
                                └─ POST {worker}/cancel   → cancel in-flight job
```

* Queue: `ownpod:queue` (Redis list).
* Job records: `ownpod:job:{id}` (Redis hash).
* Dispatcher: single lightweight thread; one job per worker at a time (default capacity=1 per worker).

---

## Project Layout

The project follows a modular architecture with clear separation of concerns:

```
app/
├── config/           # Configuration settings
│   └── settings.py   # Environment variables and configuration
├── models/           # Data models for API validation
│   └── schema.py     # Pydantic models
├── services/         # Business logic services
│   ├── job_service.py     # Job management
│   ├── queue_service.py   # Queue operations
│   └── worker_service.py  # Worker availability
├── api/              # API endpoints and routing
│   └── router.py     # API endpoints
├── utils/            # Utility functions and helpers
│   ├── logging_setup.py   # Logging configuration
│   ├── http_client.py     # HTTP session pooling and retries
│   └── redis_client.py    # Redis client reuse
├── Dockerfile        # Container definition for API service
├── requirements.txt  # Python dependencies
└── main.py           # Application entry point

examples/
└── worker-echo/      # Example worker implementation
    ├── Dockerfile
    ├── requirements.txt
    └── handler.py
```

---

## Prerequisites

* Docker + Docker Compose v2
* NVIDIA Container Toolkit (if your workers need GPUs)
* `curl` and `jq` for the examples

Verify GPU access (optional):

```bash
docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi
```

---

## Quick start (with the included example workers)

### 1) Build and run two worker containers (ports 18000, 18001)

```bash
# Build example RunPod-style worker (echoes and reverses text)
docker build -t ownpod/worker-echo:latest ./examples/worker-echo

# Worker A on port 18000 (CPU); add --gpus "device=0" if needed
docker run --rm -p 18000:8000 \
  ownpod/worker-echo:latest \
  python handler.py --rp_serve_api --rp_api_host 0.0.0.0 --rp_api_port 8000

# Worker B on port 18001 (CPU); add --gpus "device=1" if needed
docker run --rm -p 18001:8000 \
  ownpod/worker-echo:latest \
  python handler.py --rp_serve_api --rp_api_host 0.0.0.0 --rp_api_port 8000
```

> Keep both terminals open. These are standard RunPod local-API servers.

### 2) Start OwnPod

```bash
cd ownpod
export OWNPOD_WORKERS="http://localhost:18000,http://localhost:18001"
docker-compose up
```

Expected: `redis` and `api` start; dispatcher sees two workers.

### 3) Run a job end-to-end

```bash
# Submit async job
jid="$(curl -s -X POST http://localhost:8000/v2/local-ep/run \
  -H "Content-Type: application/json" \
  -d '{"input":{"text":"ownpod demo run"}}' | jq -r .id)"

# Poll status
curl -s "http://localhost:8000/v2/local-ep/status/$jid" | jq .

# Stream progress/events (SSE)
curl -N "http://localhost:8000/v2/local-ep/stream/$jid"

# Final status with output
curl -s "http://localhost:8000/v2/local-ep/status/$jid" | jq .
```

---

## Use your own workers (local or remote, no code changes)

Run **each** of your existing RunPod worker images with the local API flags:

```bash
# Example: your worker on GPU 0, serving the RunPod local API on port 8000
docker run --rm --gpus "device=0" -p 18000:8000 \
  yourorg/your-runpod-worker:latest \
  python handler.py --rp_serve_api --rp_api_host 0.0.0.0 --rp_api_port 8000

# Another worker on GPU 1
docker run --rm --gpus "device=1" -p 18001:8000 \
  yourorg/your-runpod-worker:latest \
  python handler.py --rp_serve_api --rp_api_host 0.0.0.0 --rp_api_port 8000
```

Then point OwnPod at them:

```bash
export OWNPOD_WORKERS="http://localhost:18000,http://localhost:18001"
docker-compose --profile demo up
```

> OwnPod does **not** require Redis in worker containers. Workers remain unmodified RunPod handlers started with `--rp_serve_api`.

---

## API (RunPod v2 parity)

All routes are under `/v2/{endpoint_id}`. Default `endpoint_id` is `local-ep`.

* `POST /v2/local-ep/run`
  Body:

  ```json
  { "input": { "text": "process this" },
    "policy": { "executionTimeout": 600000, "ttl": 1800000, "lowPriority": false },
    "webhook": "http://localhost:9000/receive",
    "s3Config": { "url": "http://minio", "bucket": "b", "accessId": "a", "accessSecret": "s" } }
  ```

  → `{ "id": "JOB_ID", "status": "IN_QUEUE" }`

  Extensions (optional):
  - Send `Idempotency-Key: <token>` header to dedupe identical submissions within TTL (see config). First call enqueues and caches the response; subsequent calls with the same key return the cached response without enqueuing a duplicate.

* `POST /v2/local-ep/runsync`
  Same body. Waits up to `OWNPOD_RUNSYNC_TIMEOUT_MS` and returns current/terminal status.

* `GET /v2/local-ep/status/{JOB_ID}`
  → `{ "id": "...", "status": "IN_PROGRESS|COMPLETED|FAILED|TIMED_OUT|CANCELLED", "workerId": "...", "delayTime": ms, "executionTime": ms, "output": {...}, "error": "..." }`

  Notes:
  - While a job is executing, OwnPod maps internal `RUNNING` to `IN_PROGRESS` for client compatibility.
  - `workerId` identifies the worker handling the job (stable per worker URL).
  - `delayTime` measures enqueue-to-start delay. `executionTime` is provided by the worker if available; otherwise OwnPod computes it at completion.
  - Caching (optional): OwnPod returns an `ETag` header; clients may send `If-None-Match` to receive `304 Not Modified` when the status has not changed.

  Examples:

  Mid-execution:

  ```json
  {
    "delayTime": 21225,
    "id": "b9ba5b0c-a4a1-4ab9-9455-6a4ddcb17362-u1",
    "status": "IN_PROGRESS",
    "workerId": "t7zclng6mb5jwy"
  }
  ```

  Completed:

  ```json
  {
    "delayTime": 21225,
    "executionTime": 5273,
    "id": "b9ba5b0c-a4a1-4ab9-9455-6a4ddcb17362-u1",
    "output": {
      "filename": "test.webp",
      "image_b64": "data"
    },
    "status": "COMPLETED",
    "workerId": "t7zclng6mb5jwy"
  }
  ```

* `GET /v2/local-ep/stream/{JOB_ID}`
  Server-Sent Events relay from the assigned worker.

* `POST /v2/local-ep/cancel/{JOB_ID}`
  Cancels queued or running jobs (cooperative cancel for running jobs via worker API).

* `POST /v2/local-ep/retry/{JOB_ID}`
  Only for `FAILED` / `TIMED_OUT`. Returns a new job id.

* `POST /v2/local-ep/purge-queue`
  Drops all queued jobs.

* `GET /v2/local-ep/health`
  Queue length and worker states.

**Auth**: Authorization header is accepted and ignored by default. Add enforcement in API router if you expose beyond localhost.

---

## Configuration

Set via environment (see `docker-compose.yml`):

| Variable                                | Default                            | Purpose                                               |
| --------------------------------------- | ---------------------------------- | ----------------------------------------------------- |
| `OWNPOD_WORKERS`                        | `http://localhost:18000`           | Comma-separated list of worker base URLs              |
| `REDIS_URL`                             | `redis://redis:6379/0`             | Redis connection string                               |
| `OWNPOD_RESULT_TTL_SEC`                 | `1800`                             | TTL (s) for finished/failed job records               |
| `OWNPOD_RUNSYNC_TIMEOUT_MS`             | `30000`                            | Max wait for `/runsync` before returning              |
| `OWNPOD_DESCRIPTION_PREFIX`             | `ownpod`                           | Description prefix in job metadata                    |
| `OWNPOD_WORKER_VERIFY_TLS`              | `true`                             | Verify HTTPS worker certificates                       |
| `OWNPOD_WORKER_CA_BUNDLE`               | ``                                 | Path to CA bundle for self-signed certs               |
| `OWNPOD_WORKER_HEADERS`                 | ``                                 | JSON map of extra headers to send to workers          |
| `OWNPOD_WORKER_CONNECT_TIMEOUT_S`       | `5`                                | TCP connect timeout for worker HTTP                   |
| `OWNPOD_WORKER_RUN_READ_TIMEOUT_S`      | `30`                               | Read timeout for `/run` requests                      |
| `OWNPOD_WORKER_STATUS_TIMEOUT_S`        | `15`                               | Read timeout for `/status` polling                    |
| `OWNPOD_WORKER_CANCEL_TIMEOUT_S`        | `10`                               | Read timeout for `/cancel` requests                    |
| `OWNPOD_IDEMPOTENCY_TTL_SEC`            | `86400`                            | TTL (s) for Idempotency-Key cache on `/run`            |
### Remote worker examples

Remote over HTTPS with custom header and self-signed certs:

```bash
export OWNPOD_WORKERS="https://worker-a.example.com,https://worker-b.example.com"
export OWNPOD_WORKER_HEADERS='{"Authorization":"Bearer YOUR_TOKEN"}'
export OWNPOD_WORKER_VERIFY_TLS=false
# or better: point to your CA bundle and keep verify on
# export OWNPOD_WORKER_CA_BUNDLE=/etc/ssl/certs/your-ca.pem
# export OWNPOD_WORKER_VERIFY_TLS=true
docker compose up -d
```

Running only the API/Redis and using remote workers (no local demo workers):

```bash
export OWNPOD_WORKERS="http://10.0.0.11:8000,http://10.0.0.12:8000"
docker compose up -d
```

**Capacity**: default 1 in-flight job per worker URL. To support concurrency>1 for a worker, extend the dispatcher to track per-worker capacity.

---

## Example worker (included)

A minimal RunPod-compatible worker is provided at `examples/worker-echo/`.

**`examples/worker-echo/handler.py`**

```python
import runpod

def handler(event):
    data = event.get("input", {}) or {}
    text = str(data.get("text", ""))
    # Simulate some work and return a deterministic result
    return {"echo": text, "reversed": text[::-1]}

# Start RunPod serverless runtime; with --rp_serve_api it exposes /run, /runsync, /status, /stream, /cancel
runpod.serverless.start({"handler": handler})
```

**`examples/worker-echo/requirements.txt`**

```
runpod==1.7.0
```

**`examples/worker-echo/Dockerfile`**

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY handler.py .
EXPOSE 8000
CMD ["python","handler.py","--rp_serve_api","--rp_api_host","0.0.0.0","--rp_api_port","8000"]
```

Build and run:

```bash
docker build -t ownpod/worker-echo:latest ./examples/worker-echo
docker run --rm -p 18000:8000 ownpod/worker-echo:latest
```

---

## Operational notes

* **Durability**: Redis AOF enabled. Queued jobs survive OwnPod restarts.
* **In-flight jobs**: If OwnPod restarts, running jobs continue in workers; status reconciles on next poll.
* **Cancel**: Queued jobs are dropped immediately; running jobs are cancelled by forwarding to the worker's `/cancel/{id}` (if supported).
* **Backpressure**: Queue grows under load; dispatcher only assigns to idle workers.
* **Security**: Add token checks at the API or run behind a reverse proxy on localhost only.
* **Performance**: Uses HTTP connection pooling with retries for GETs (status/stream handshakes), no automatic retries for POSTs to avoid double submission. Runs with uvloop + httptools by default.

---

## Compatibility and discrepancies vs RunPod

OwnPod aims for client-level parity with RunPod v2 while adding safe extensions.

**Parity**
- Endpoints: `/run`, `/runsync`, `/status/{id}`, `/stream/{id}`, `/cancel/{id}` (+ `/retry/{id}`, `/health`, `/purge-queue`).
- Request body shape with `input`, optional `policy`, `webhook`, `s3Config`.
- Status semantics including `COMPLETED`, `FAILED`, `TIMED_OUT`, `CANCELLED`.

**Extensions (opt-in)**
- `Idempotency-Key` on `/run` to dedupe submissions within `OWNPOD_IDEMPOTENCY_TTL_SEC`.
- `ETag` on `/status/{id}` to enable conditional GETs (`If-None-Match`).
- `workerId` included in status responses.
- `delayTime` and `executionTime` computed when workers do not provide them.

**Minor differences**
- While executing, status is reported as `IN_PROGRESS` (OwnPod maps internal `RUNNING` → `IN_PROGRESS`). Some RunPod responses use `RUNNING`; most clients treat these as equivalent.
- Job IDs are opaque strings; format may differ from RunPod but is functionally interchangeable for clients.

---

## Troubleshooting

* *Workers never receive jobs*: `OWNPOD_WORKERS` not set or wrong; `/health` shows zero workers.
* *Jobs stuck in IN\_QUEUE*: all workers busy or unreachable; check worker logs and URLs; verify ports.
* *`/stream` stalls*: ensure the worker implements streaming; use `curl -N` to disable buffering.
* *Sync always returns RUNNING*: increase `OWNPOD_RUNSYNC_TIMEOUT_MS` or reduce worker runtime.
* *GPU not used*: run workers with `--gpus` and confirm `nvidia-smi` in the worker container.

---

## Roadmap

* Optional **auth** (Bearer token).

---

## License

MIT.