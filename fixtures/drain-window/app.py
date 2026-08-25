"""Listener timing fixture for SP004."""

import os
import signal
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8000
DRAIN_SECONDS = float(os.environ.get("DRAIN_SECONDS", "0"))
EXIT_SECONDS = float(os.environ.get("EXIT_SECONDS", str(DRAIN_SECONDS + 0.2)))
RESET_AFTER_SIGTERM = os.environ.get("RESET_AFTER_SIGTERM") == "1"
READINESS_DROPS = os.environ.get("READINESS_DROPS", "1") == "1"

draining = False
server: ThreadingHTTPServer
drain_elapsed = threading.Event()
listener_closing = threading.Event()
sigterm_at: float | None = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        if draining and RESET_AFTER_SIGTERM:
            self.connection.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_LINGER,
                struct.pack("ii", 1, 0),
            )
            self.connection.close()
            return

        status = 503 if self.path == "/ready" and draining and READINESS_DROPS else 200
        body = b'{"ready":false}' if status == 503 else b'{"ready":true}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

        # Close between probes, not from a wall-clock timer in the middle of a
        # successful handshake. This fixture's normal drain path is meant to
        # model a listener that finishes the connection it just accepted. The
        # RESET_AFTER_SIGTERM path above deliberately keeps the opposite
        # behavior for the universal accept_then_reset failure branch.
        if (
            draining
            and drain_elapsed.is_set()
            and self.headers.get("Connection", "").lower() == "close"
            and not listener_closing.is_set()
        ):
            listener_closing.set()
            # The SP004 accept probe identifies itself with Connection: close.
            # Close the listening socket synchronously after flushing that
            # response, before the probe can begin its next 50ms cycle. Leaving
            # this to the cleanup thread creates a scheduler-dependent backlog
            # handshake which is then correctly classified as accept_then_reset.
            server.socket.close()
            threading.Thread(target=close_and_exit, daemon=True).start()

    def log_message(self, *args: object) -> None:
        pass


def close_and_exit() -> None:
    server.shutdown()
    server.server_close()
    elapsed = 0.0 if sigterm_at is None else time.monotonic() - sigterm_at
    time.sleep(max(0.0, EXIT_SECONDS - elapsed))
    os._exit(0)


def finish_shutdown() -> None:
    time.sleep(DRAIN_SECONDS)
    if DRAIN_SECONDS == 0 or RESET_AFTER_SIGTERM:
        listener_closing.set()
        close_and_exit()
        return
    drain_elapsed.set()


def begin_shutdown(signum: int, frame: object) -> None:
    global draining, sigterm_at
    draining = True
    sigterm_at = time.monotonic()
    if DRAIN_SECONDS == 0 and not RESET_AFTER_SIGTERM:
        # The immediate-close fixture must close before the signal handler
        # returns. Handing this zero-delay action to a thread leaves one
        # scheduler-sized backlog window where a new handshake can succeed and
        # reset, nondeterministically selecting accept_then_reset instead of the
        # listener_closed_early branch this fixture owns.
        listener_closing.set()
        server.socket.close()
        threading.Thread(target=close_and_exit, daemon=True).start()
        return
    threading.Thread(target=finish_shutdown, daemon=True).start()


signal.signal(signal.SIGTERM, begin_shutdown)
server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
server.serve_forever(poll_interval=0.01)
threading.Event().wait()
