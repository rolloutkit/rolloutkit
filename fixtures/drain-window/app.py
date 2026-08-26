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
SLOW_SECONDS = float(os.environ.get("SLOW_SECONDS", "0"))
# Milliseconds after the shutdown phase is recognised at which this process
# stops accepting, while it is still running normally and still serving the
# request it already accepted. Zero leaves the accept loop alone.
STOP_ACCEPT_AFTER_MS = float(os.environ.get("STOP_ACCEPT_AFTER_MS", "0"))

draining = False
server: ThreadingHTTPServer
drain_elapsed = threading.Event()
listener_closing = threading.Event()
accept_stopped = threading.Event()
stop_accept_armed = threading.Event()
slow_lock = threading.Lock()
slow_in_flight = 0
sigterm_at: float | None = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def is_accept_probe(self) -> bool:
        """True for SP004's accept probe, false for the readiness watcher.

        Both hit the readiness path with `Connection: close` after T0, so that
        header alone names neither of them. The probe writes its request by
        hand and sends exactly `Host` and `Connection`; the watcher goes
        through `http.client`, which always adds `Accept-Encoding: identity`.

        Telling them apart is what makes the close below safe rather than
        lucky: only the probe is serial, so only while *its* request is being
        served is the accept queue guaranteed to hold nothing SP004 counts.
        """
        return (
            self.headers.get("Connection", "").lower() == "close"
            and self.headers.get("Accept-Encoding") is None
        )

    def do_GET(self) -> None:  # noqa: N802
        if (
            STOP_ACCEPT_AFTER_MS
            and slow_in_flight
            and self.headers.get("Connection", "").lower() == "close"
            and not stop_accept_armed.is_set()
        ):
            # `Connection: close` alone does not mean the accept probe: the
            # control sidecar sends it on every readiness sample too, and those
            # start well before the run reaches shutdown. What only happens in
            # the shutdown phase is the overlap — a probe connection arriving
            # while the long request is open. The readiness samples, the health
            # check and the baseline are each run to completion in turn, so no
            # earlier phase can produce it.
            stop_accept_armed.set()
            threading.Thread(target=stop_accepting, daemon=True).start()

        if draining and RESET_AFTER_SIGTERM:
            self.connection.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_LINGER,
                struct.pack("ii", 1, 0),
            )
            self.connection.close()
            return

        if self.path == "/slow":
            self.do_GET_slow()

        # Decided before the response is written, and acted on in two halves
        # around it. Closing a listening socket destroys whatever is sitting in
        # its accept queue: the peer's handshake already succeeded, so it is
        # told it is connected and then reset without a reply. SP004 is right
        # to call that a failure — this fixture is simply not the specimen for
        # it, and `accept-then-reset-in-app` is.
        #
        # The safe moment is exactly this one. The accept probe is serial and
        # is blocked on the reply to this very request, so it has no handshake
        # anywhere and the queue holds nothing SP004 counts. Answering first
        # gives it the interval between the flush and the close to open its
        # next connection into a queue about to be destroyed — a scheduler
        # slice wide, which is why it was a run in eight on an idle laptop and
        # a whole CI job on a loaded two-core runner.
        finishing = (
            draining
            and drain_elapsed.is_set()
            and self.is_accept_probe()
            and not listener_closing.is_set()
        )
        if finishing:
            listener_closing.set()
            stop_listening()

        status = 503 if self.path == "/ready" and draining and READINESS_DROPS else 200
        body = b'{"ready":false}' if status == 503 else b'{"ready":true}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

        # The connection this reply went out on was accepted long before the
        # listening socket closed, so it is unaffected by that close and is
        # answered normally. This fixture's drain path models a listener that
        # finishes the connection it just accepted; the RESET_AFTER_SIGTERM
        # path above deliberately keeps the opposite behaviour, for the
        # universal accept_then_reset failure branch.
        if finishing:
            threading.Thread(target=exit_after_budget, daemon=True).start()

    def do_GET_slow(self) -> None:  # noqa: N802
        """Hold the connection open long enough to still be open at T0."""
        global slow_in_flight
        with slow_lock:
            slow_in_flight += 1
        try:
            time.sleep(SLOW_SECONDS)
        finally:
            with slow_lock:
                slow_in_flight -= 1

    def log_message(self, *args: object) -> None:
        pass


def exit_after_budget() -> None:
    elapsed = 0.0 if sigterm_at is None else time.monotonic() - sigterm_at
    time.sleep(max(0.0, EXIT_SECONDS - elapsed))
    os._exit(0)


def stop_listening() -> None:
    """Stop accepting: the accept loop first, then the socket it selects on.

    `serve_forever` is polling this socket in the main thread. Closing it from
    under that poll is how a fixture ends as a traceback instead of a verdict,
    so the loop is stopped first; `shutdown` is documented as safe from another
    thread and returns within one poll interval.
    """
    server.shutdown()
    server.socket.close()


def close_and_exit() -> None:
    server.shutdown()
    server.server_close()
    exit_after_budget()


def stop_accepting() -> None:
    """Stop accepting while the process keeps serving, well before any signal.

    A worker that has closed its listening socket but is still finishing the
    requests it holds. Nothing here touches the connections already accepted:
    the socket the in-flight request arrived on stays open and its response is
    written normally.
    """
    time.sleep(STOP_ACCEPT_AFTER_MS / 1000)
    accept_stopped.set()
    server.socket.close()
    threading.Thread(target=server.shutdown, daemon=True).start()


def finish_shutdown() -> None:
    time.sleep(DRAIN_SECONDS)
    if accept_stopped.is_set():
        # No probe can arrive to trigger the close-on-response path, because
        # there is no listening socket left to arrive on. Exit on the timer
        # instead of waiting for a connection that cannot happen, which would
        # otherwise leave SIGKILL as the only way out.
        exit_after_budget()
        return
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
