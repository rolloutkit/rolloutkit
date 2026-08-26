# preflightkit

Run a backend container, reproduce what a deploy does to it, and **measure** how
it actually behaves. Every verdict comes with evidence.

> Measure, don't guess.

preflightkit does not tell you that "graceful shutdown may be misconfigured". It
shows you a timeline and tells you which requests were destroyed.

## Quick start

You can get a useful lifecycle report without creating a configuration file:

```console
preflightkit test my-api:latest --port 8000 --ready-url /ready
```

Without a deployment profile, preflightkit uses `platform: kubernetes`, a
30-second grace period, `pre_stop: none`, and `drain: none`. The missing drain
mechanism makes SP004 `WARN`; this is expected because nothing covers routing
propagation during shutdown. Without `contracts.inflight`, required contract
SP005 uses the readiness endpoint as a fallback. It returns a real verdict when
the readiness latency is distinguishable from host jitter; otherwise, it
returns `INCONCLUSIVE` with the measured values and tells you to pass
`--inflight-path` for a slower endpoint. Set `contracts.inflight: null` only
when you intend to disable SP005 and receive `SKIP`.

Use `--fail-on error` to gate on failures. Required `SKIP` and `INCONCLUSIVE`
contracts also block that gate unless you pass `--allow-inconclusive`. CLI
flags override `PREFLIGHTKIT_*` environment variables, which override
`preflightkit.yaml`; model defaults apply last.

The remaining commands don't require AI or hidden configuration. `measure`
prints measurements and a timeline without contract verdicts or gating,
`validate` checks configuration without contacting Docker, `explain SPXXX`
prints static contract documentation, and `list-contracts` lists the available
contracts. Use `--format json` or `--format junit` when another tool consumes a
`test` report.

```
preflightkit v0.1   run pfk_01J8C4XK
Target: my-api:latest
Profile: kubernetes, grace 30s, preStop sleep 5s -> shutdown budget 25s

SHUTDOWN TIMELINE
  T0  SIGTERM -> PID 1              +0ms
  T1  readiness -> 503              +87ms
  T4  process exit (code 143)       +1440ms

  SP005 in-flight completion        FAIL   8/10 completed, 2 reset
```

## Status

Early development — a vertical slice. Working today: SP001 startup, SP002
readiness stability, SP003 signal handling, SP004 drain window, SP005 in-flight
completion, and SP006 shutdown deadline, against a Docker runtime. Run-scoped
dependency services and single-file Compose config import are available.

Contracts whose measurement preconditions do not hold report `INCONCLUSIVE`.
This is distinct from `SKIP`: `SKIP` means a contract was not configured, while
`INCONCLUSIVE` means the run could not measure it. Report-only mode remains exit
0. When you enable `--fail-on`, a required contract that returns `SKIP` or
`INCONCLUSIVE` blocks the gate. Use `--allow-inconclusive` only when you intend
to accept an unmeasured required contract.

## Scope

The only thing in scope is **measurable process behaviour inside a container
during startup and shutdown**.

It is not a SAST tool, a vulnerability scanner, a Dockerfile linter, a Kubernetes
YAML linter, a secret scanner, an APM, or a load tester. It does not duplicate
Trivy, Checkov, KubeLinter, Polaris, or Semgrep.

## Known limitations

- **Service mesh.** With Istio or Linkerd the sidecar receives its own SIGTERM
  and the shutdown ordering changes. Not modelled.
- **Multi-worker.** `wait` observes PID 1 exiting. That matches Kubernetes
  semantics, but it does not show the distribution inside a worker group.
- **HTTP/1.1 only.** gRPC, WebSocket, and queue workers are out — a worker has no
  externally observable "request in progress" signal, which needs its own design.
- **macOS.** Every run still gets a user-defined bridge, but Docker Desktop's
  container IPs are not host-routable. Traffic therefore falls back to a
  published port and reports `port_proxy_likely: true`. SP001's TCP-open
  sub-measurement is `INCONCLUSIVE`. SP004 is also `INCONCLUSIVE` when an
  `in_app` strategy requires direct listener timing; `none` still warns, and
  `prestop` remains applicable without that timing. Readiness remains
  measurable. Linux sends traffic directly to the unpublished container IP.
- **Readiness fallback.** Without `--inflight-path`, SP005 falls back to the
  readiness endpoint as its in-flight target. A service whose readiness is fast
  can leave the endpoint's p50 too close to the measurement jitter floor to
  separate the two, and SP005 then reports `readiness_fallback_below_resolution`
  rather than guessing. Because the jitter floor is a property of the host, the
  same service can resolve on one machine and not on another: a 2.5-5.3ms
  readiness p50 against a 0.23-1.66ms floor was measured at 1.9x to 15.3x on a
  single machine, against a required 10x. Pass `--inflight-path` with a slower
  representative endpoint for a result that does not depend on where it ran.
- **Windows** is not supported.

## Security

The target image is treated as untrusted: temporary network, resource limits, no
host network, no privileged mode, no docker socket mount, no host filesystem
mount, bounded log collection, and env values redacted from all output.

Running an untrusted image through your local Docker daemon carries risk on its
own. That is a property of Docker, not of this tool.

## License

Apache-2.0
