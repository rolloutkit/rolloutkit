# preflightkit

Run a backend container, reproduce what a deploy does to it, and **measure** how
it actually behaves. Every verdict comes with evidence.

> Measure, don't guess.

preflightkit does not tell you that "graceful shutdown may be misconfigured". It
shows you a timeline and tells you which requests were destroyed.

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
dependency services are available. Compose import and the remaining fixtures
are next.

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
  published port, reports `port_proxy_likely: true`, and marks SP001's TCP-open
  sub-measurement and SP004 `INCONCLUSIVE`. Readiness remains measurable. Linux
  sends traffic directly to the unpublished container IP.
- **Windows** is not supported.

## Security

The target image is treated as untrusted: temporary network, resource limits, no
host network, no privileged mode, no docker socket mount, no host filesystem
mount, bounded log collection, and env values redacted from all output.

Running an untrusted image through your local Docker daemon carries risk on its
own. That is a property of Docker, not of this tool.

## License

Apache-2.0
