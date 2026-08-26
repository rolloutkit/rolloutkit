"""A server that keeps its in-app promise and still resets a late connection.

Every other SP004 fixture is written to avoid this: closing a listening socket
destroys whatever the kernel has already handshaken into its accept queue, and
`drain-window/app.py` sequences its close against the probe's request so that
the queue is provably empty. This one does the opposite on purpose, because the
population that lands there is not a defect and SP004 has to be able to say so.

The story it tells is an ordinary one. The window the application declared has
elapsed; a worker is busy and has not returned to `accept()`; the kernel keeps
completing handshakes into the backlog anyway; the process then closes the
listener and every one of them is reset. The connections destroyed that way
were opened after the application had already served the interval it promised,
and in a real rollout the endpoint was withdrawn before they were made. Nothing
here is aware that it is being probed.

Three timers, all measured from SIGTERM, and the gaps between them are what
makes the run deterministic rather than lucky:

  STOP_ACCEPT_AFTER_MS  the accept loop stops; the listening socket stays open,
                        so the next probe connection completes its handshake
                        and waits in the backlog. Set this clear of the
                        declared window: it is what the probe measures as the
                        last accepted connection.
  CLOSE_AFTER_MS        the listener closes and the waiting connection is
                        reset. Land it in the middle of a probe attempt, not
                        near either edge -- the probe is serial with a 500ms
                        timeout and a 50ms interval, so a close at
                        STOP_ACCEPT + 275ms sits about 225ms from both the
                        attempt's start and its timeout.
  EXIT_AFTER_MS         the process exits.
"""

import os
import select
import signal
import socket
import threading
import time

PORT = 8000
STOP_ACCEPT_AFTER_MS = float(os.environ.get("STOP_ACCEPT_AFTER_MS", "1800"))
CLOSE_AFTER_MS = float(os.environ.get("CLOSE_AFTER_MS", "2075"))
EXIT_AFTER_MS = float(os.environ.get("EXIT_AFTER_MS", "2400"))

draining = False
stopped_accepting = threading.Event()

listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("0.0.0.0", PORT))
listener.listen(128)


def serve(conn: socket.socket) -> None:
    try:
        conn.settimeout(2.0)
        try:
            conn.recv(65536)
        except OSError:
            return
        status = b"503 Service Unavailable" if draining else b"200 OK"
        body = b'{"ready":false}' if draining else b'{"ready":true}'
        head = (
            b"HTTP/1.1 " + status + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n"
        )
        conn.sendall(head + body)
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def accept_loop() -> None:
    """Accept until told to stop, then leave the socket listening.

    The wait is a select rather than a blocking accept so that the stop is
    taken between two connections instead of interrupting one. Leaving the
    socket bound and listening is the whole point: the kernel goes on
    completing handshakes into a queue nobody is draining.
    """
    while not stopped_accepting.is_set():
        readable, _, _ = select.select([listener], [], [], 0.01)
        if not readable:
            continue
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        threading.Thread(target=serve, args=(conn,), daemon=True).start()


def shutdown_timeline() -> None:
    start = time.monotonic()

    def at(offset_ms: float) -> None:
        time.sleep(max(0.0, offset_ms / 1000 - (time.monotonic() - start)))

    at(STOP_ACCEPT_AFTER_MS)
    stopped_accepting.set()
    at(CLOSE_AFTER_MS)
    listener.close()
    at(EXIT_AFTER_MS)
    os._exit(0)


def on_sigterm(signum: int, frame: object) -> None:
    global draining
    draining = True
    threading.Thread(target=shutdown_timeline, daemon=True).start()


signal.signal(signal.SIGTERM, on_sigterm)
accept_loop()
threading.Event().wait()
