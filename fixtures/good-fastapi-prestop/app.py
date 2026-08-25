"""A correctly behaving service.

uvicorn's default SIGTERM handling: stop accepting new connections, let in-flight
requests finish, then exit 0. Nothing clever here — this is what "correct" is
supposed to look like.
"""

import asyncio
import os
import time

from fastapi import FastAPI

time.sleep(float(os.environ.get("STARTUP_DELAY_SECONDS", "0")))

app = FastAPI()


@app.get("/ready")
async def ready() -> dict[str, str]:
    await asyncio.sleep(float(os.environ.get("READINESS_DELAY_SECONDS", "0")))
    return {"status": "ready"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/slow")
async def slow() -> dict[str, object]:
    await asyncio.sleep(5)
    return {"done": True, "took_seconds": 5}
