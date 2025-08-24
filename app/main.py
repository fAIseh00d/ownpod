import threading
from contextlib import asynccontextmanager
from fastapi import FastAPI

from api.router import router
from services.queue_service import init_queue, dispatch_loop
from services.worker_service import start_health_monitor
from utils.logging_setup import logger

# Create FastAPI app
app = FastAPI(title="OwnPod — RunPod v2 API (local clone with external workers)")

# Include API router
app.include_router(router)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    init_queue()
    start_health_monitor()
    threading.Thread(target=dispatch_loop, daemon=True).start()
    logger.info("OwnPod service started")
    try:
        yield
    finally:
        from services.worker_service import get_stop_event
        stop_event = get_stop_event()
        stop_event.set()
        logger.info("OwnPod service stopping")

# Recreate app with lifespan for proper startup/shutdown handling
app = FastAPI(title="OwnPod — RunPod v2 API (local clone with external workers)", lifespan=lifespan)
app.include_router(router)
