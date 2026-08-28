# rolloutkit

Your service passes its tests. Does it survive a rollout?

`rolloutkit` runs your container the way a deployment does — starts it, puts
traffic on it, sends SIGTERM, and watches what happens to the requests that were
in flight. It reports what it measured, with timestamps.

```
$ rolloutkit test service-b:latest --port 8000 --ready-url /healthz/ --env ALLOWED_HOSTS='*'
```

```
STARTUP
  GET /healthz/ -> 200                        6.78s
  PID 1 signal disposition                    sh, no SIGTERM handler - the kernel will discard it

SHUTDOWN TIMELINE
  T2  last new connection accepted            +30009ms (±50ms)

CONTRACTS
  SP003 signal-handling        FAIL   shutdown never started: PID 1 (sh) showed no reaction to SIGTERM
        - PID 1 in a container is the init of a PID namespace, and the kernel silently discards
          signals whose disposition is still the default for it. The application was never woken;
          a longer grace period would only make the wait longer.
  SP006 shutdown-deadline      FAIL   killed by SIGKILL at the end of the 30s budget (exit 137);
                                      the process never shut itself down
        - In Kubernetes this is the point where every open connection is severed, whatever is
          still running.

Result: 2 FAIL, 1 WARN
```

This is a real Django service. Its Dockerfile uses a shell-form `CMD`, so `/bin/sh`
becomes PID 1 and the kernel drops SIGTERM on the floor. Every rollout spends the
full `terminationGracePeriodSeconds` waiting, then severs every open connection.
Nothing in the logs says so. The exit code is 137, which most dashboards render as
"OOMKilled or restarted".

## The part that is easy to miss

The fix is well known: use exec-form, or `exec` the process. Here is the same image
after that change:

```
  SP003 signal-handling        PASS   shutdown started and the process stopped within budget after 291ms
  SP005 inflight-completion    FAIL   2/74 completed, 72 destroyed
        request #17 reset_before_response during awaiting_response +61ms
        request #28 reset_before_response during awaiting_response +61ms
        request #29 reset_before_response during awaiting_response +61ms
        request #32 reset_before_response during awaiting_response +62ms
        request #33 reset_before_response during awaiting_response +61ms
        ... and 67 more destroyed requests (--format json lists every one)
  SP006 shutdown-deadline      PASS   exited in 291ms of 30s
```

Signal handling is fixed. Shutdown is fast and clean. And the service now destroys
94% of the requests it was serving, on every single rollout.

It was doing that before too. The difference is that before, shutdown never started,
so nothing was ever tested. A passing contract can mean the thing you wanted to test
did not happen — which is why `rolloutkit` refuses to report a verdict it could not
measure.

## Install

```
uvx rolloutkit test my-api:latest --port 8000 --ready-url /ready
```

or `pipx run rolloutkit`, or `pip install rolloutkit`.

Requires Python 3.12+ and a Docker daemon. Linux gives the most precise timing;
macOS with Docker Desktop works, and the report says when a measurement was limited
by the platform.

## What it checks

Six contracts, each of which either measures something or says it could not.

| | |
|---|---|
| **SP001** startup | time to TCP and to readiness, against a budget |
| **SP002** readiness-stability | is readiness correct and stable, or does it flap |
| **SP003** signal-handling | does SIGTERM reach the process and does it react |
| **SP004** drain-window | does the listener stay open while routing is still sending traffic |
| **SP005** inflight-completion | do accepted requests finish, or get reset |
| **SP006** shutdown-deadline | does the process exit inside the grace budget |

`rolloutkit explain SP004` prints what a contract measures, which preconditions it
needs, every verdict it can reach, and what to do first when it fails.

## Configuration

The one-line form covers a quick look. Anything real wants a config file, because
the verdicts depend on how you actually deploy:

```yaml
version: 1

target:
  image: my-api:latest
  port: 8000
  env_file: .env.rolloutkit

services:                     # dependencies, started on the same network
  db:
    image: postgres:16-alpine

deployment:                   # this is what SP004 and SP006 judge against
  platform: kubernetes
  termination_grace_period: 30s
  pre_stop: { type: sleep, duration: 5s }
  drain: { strategy: prestop }

probes:
  readiness: { path: /ready, expected_status: 200 }

contracts:
  inflight:
    request: { method: GET, path: /api/reports, expected_duration: 5s }
    concurrent: 10
```

`rolloutkit init --from-compose docker-compose.yml --service api` generates most of
this from a compose file. It reads the file; it never runs it.

The `deployment` block is not decoration. The same image is correct under one drain
strategy and broken under another, and SP004 says so:

- `prestop` — the platform hook removes the pod from routing before SIGTERM. The
  listener may close immediately; that is correct.
- `in_app` — the application owns the gap. It must keep accepting for the declared
  window after SIGTERM, then drain.
- `none` — nothing covers routing propagation, so rollouts will drop connections
  regardless of what the application does.

## In CI

```yaml
- run: docker build -t my-api:latest .
- run: uvx rolloutkit test -c rolloutkit.yaml --fail-on error
```

Exit codes: `0` pass, `1` a contract failed, `2` bad configuration, `3` the run could
not be performed, `4` an internal error. Without `--fail-on`, no contract verdict
blocks anything and the run reports exit 0 — start there, gate later. A configuration
or infrastructure error is still exit 2 or 3, because in neither case was anything
measured.

`--format json` emits the full evidence: every timestamp, every broken request, the
host and calibration the run was measured on. `--format junit` is for CI test panels.

The tool's own cost is a little over a second — probe preparation, calibration and
teardown — and the rest of a run is your service starting and the in-flight window
you asked for. End to end on the laptop these captures come from: 4.2s against a Go
fixture that starts instantly, 9.4s against the Django service above.

## What it will not tell you

- **Resolution depends on the host.** The measurement floor moved by 3.4x across the
  machines this was calibrated on — 0.154ms on an idle macOS laptop against 0.516ms
  on a native Linux CI daemon — and it moves the verdict on three of ten
  configurations. When a window is too small to separate from that floor, the verdict
  is `INCONCLUSIVE`, not a guess.
- **The zero-config path uses readiness as the in-flight target.** If your readiness
  endpoint is very fast, that window cannot be resolved and SP005 says so. Point
  `--inflight-path` at a slower, representative endpoint for a stable result.
- **HTTP/1.1 only** in this release. gRPC, WebSocket, and background workers are not
  covered.
- **It is not a chaos platform, a linter, or a scanner.** It exercises one narrow
  thing — the start and the stop — and tries to prove what it found.

## Design

Three rules the implementation is held to:

**Measure, don't guess.** Every FAIL carries evidence: offsets from T0, request
outcomes, exit codes, the host and its measured noise floor.

**A verdict requires a measurement.** Contracts declare their preconditions. If a
precondition does not hold — shutdown never started, the baseline was not healthy,
the window was below the resolution of the probe — the contract returns
`INCONCLUSIVE` and says which precondition failed. It does not return PASS.

**Runtime evidence beats static inference.** Exit codes are reported, not trusted:
the same Go binary exits 2 as PID 1 and 143 behind an init, so the code describes the
container, not the application. Verdicts come from observed behaviour.

## Security

The target image is treated as untrusted: a run-scoped network, resource limits, no
host network, no privileged mode, no Docker socket mount, no host filesystem mount,
bounded log collection, and env values redacted from every output format.

Running an untrusted image through your local Docker daemon carries risk on its own.
That is a property of Docker, not of this tool.

## Status

v0.1. Docker runtime, six contracts, terminal / JSON / JUnit output.

Planned: dependency fault injection, outbound timeout testing, a GitHub Action with
PR annotations, a Kubernetes runtime, and an MCP server for agents.

## License

Apache-2.0.
