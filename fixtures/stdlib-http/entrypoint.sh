#!/bin/sh
# A signal-forwarding wrapper: the pattern that appears in thousands of images
# whose author knew a shell PID 1 swallows signals and wrote around it.
#
# The shell keeps PID 1 and installs a SIGTERM handler, so the kernel delivers
# the signal instead of discarding it. The application runs as a child, where a
# default disposition still means death: it dies of SIGTERM and the wrapper
# reports its 143 as its own exit code.
trap 'kill -TERM "$child" 2>/dev/null' TERM

python -u /app/app.py &
child=$!

wait "$child"
status=$?
if [ "$status" -gt 128 ]; then
    # `wait` returned because the trap ran, not because the child finished.
    wait "$child"
    later=$?
    [ "$later" -lt 128 ] || status=$later
fi
exit "$status"
