"""A service that exits cleanly and destroys every in-flight request doing it.

The point of this fixture is the combination: `os._exit(0)` produces exit code 0,
so a check that only looks at the exit code sees a graceful shutdown. Meanwhile
every request still being served is torn down mid-response.

Exiting cleanly is not the same as exiting correctly, and SP003 alone cannot tell
the difference — SP005 can.
"""

import asyncio
import os
import signal
from contextlib import asynccontextmanager

from fastapi import FastAPI


def _die_immediately(signum: int, frame: object) -> None:
    os._exit(0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Installed here, not at import time: uvicorn installs its own handlers when
    # `serve()` starts, which runs before lifespan startup. Registering later wins.
    signal.signal(signal.SIGTERM, _die_immediately)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/ready")
async def ready() -> dict[str, str]:
    return {"status": "ready"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/slow")
async def slow() -> dict[str, object]:
    await asyncio.sleep(5)
    return {"done": True, "took_seconds": 5}
