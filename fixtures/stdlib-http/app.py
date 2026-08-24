"""The smallest possible HTTP server, with and without a signal handler.

No framework, so nothing installs a SIGTERM handler behind our back — which is
the whole point. With `HANDLE_SIGTERM=1` the process exits 0; without it, SIGTERM
takes its default disposition and the kernel reports exit 143. Those are two
different SP003 verdicts, and no framework-based fixture can produce the second
one because every framework catches the signal.
"""

import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8000
_readiness_count = 0
_readiness_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        status = 200
        if self.path == "/slow":
            time.sleep(5)
            body = b'{"done": true}'
        elif self.path == "/work":
            time.sleep(0.2)
            body = b'{"done": true}'
        elif self.path == "/fail":
            time.sleep(0.2)
            status = 500
            body = b'{"error": true}'
        elif self.path == "/ready":
            global _readiness_count
            with _readiness_lock:
                probe_index = _readiness_count
                _readiness_count += 1
            if os.environ.get("FLAPPING_READINESS") == "1" and probe_index % 2:
                status = 503
            if (
                os.environ.get("READINESS_FAIL_AFTER_STARTUP") == "1"
                and probe_index > 0
            ):
                status = 503
            time.sleep(float(os.environ.get("READINESS_DELAY_SECONDS", "0")))
            body = b'{"status": "ready"}'
        elif self.path == "/health":
            body = b'{"status": "ready"}'
        else:
            self.send_error(404)
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def _exit_cleanly(signum: int, frame: object) -> None:
    # os._exit rather than sys.exit: SystemExit raised inside a handler has to
    # unwind through serve_forever and the worker threads, and what this fixture
    # needs to guarantee is the exit *code*, not the unwinding.
    os._exit(0)


if os.environ.get("HANDLE_SIGTERM") == "1":
    signal.signal(signal.SIGTERM, _exit_cleanly)

time.sleep(float(os.environ.get("STARTUP_DELAY_SECONDS", "0")))
ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
