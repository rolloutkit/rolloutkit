"""A service that does not shut down when told to.

Two behaviours from one image, chosen by `SHUTDOWN_DELAY_MS`:

  unset / 0   the SIGTERM handler returns without doing anything. The process
              never exits, preflightkit's enforcer sends SIGKILL at the end of
              the budget, and the exit code is 137. This is the case SP003 and
              SP006 both have to catch — and the case the old SP006 reported as
              a WARN with a negative margin.

  5000        the handler blocks for five seconds and then exits 0. Nothing is
              violated; the shutdown simply eats most of the budget. That is a
              WARN, and it is the branch that separates "slow" from "late".
"""

import asyncio
import os
import signal
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

SHUTDOWN_DELAY_MS = int(os.environ.get("SHUTDOWN_DELAY_MS", "0"))
READINESS_DROPS_ON_SIGTERM = os.environ.get("READINESS_DROPS_ON_SIGTERM") == "1"
draining = False


def _on_sigterm(signum: int, frame: object) -> None:
    global draining
    if READINESS_DROPS_ON_SIGTERM:
        draining = True
    if SHUTDOWN_DELAY_MS <= 0:
        return  # Signal caught and deliberately ignored.
    # Blocking on purpose: a real service doing synchronous cleanup on the main
    # thread looks exactly like this from the outside.
    time.sleep(SHUTDOWN_DELAY_MS / 1000)
    os._exit(0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Installed here, not at import time: uvicorn installs its own handlers when
    # `serve()` starts, which runs before lifespan startup. Registering later wins.
    signal.signal(signal.SIGTERM, _on_sigterm)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/ready")
async def ready(response: Response) -> dict[str, str]:
    if draining:
        response.status_code = 503
        return {"status": "draining"}
    return {"status": "ready"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/slow")
async def slow() -> dict[str, object]:
    await asyncio.sleep(5)
    return {"done": True, "took_seconds": 5}
