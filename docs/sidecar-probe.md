# Traffic probe container

Preflightkit sends startup, readiness, accept-window, and in-flight traffic from
a run-scoped probe container on the target's bridge network. This is the primary
measurement path on every supported platform.

## Configuration

The probe uses the public `python:3.12-slim` image by default. Configure another
locally available Python image when your environment mirrors base images under
a different name:

```yaml
probe:
  image: registry.internal.example/python:3.12-slim
```

Preflightkit doesn't publish or pull a custom probe image. It copies the probe
program and its exact traffic dependencies into a temporary in-memory filesystem
when the container starts. This avoids a Preflightkit registry dependency and
keeps the CLI and probe code on the same version. In an air-gapped environment,
load the configured base image into Docker before running Preflightkit.

If Docker can't start the probe or attach it to the run network, Preflightkit
uses host traffic. JSON evidence then reports `probe_location: host_fallback`
and `probe_fallback_reason`. `port_proxy_likely` applies only to this fallback.

## Calibration

The active traffic location owns its calibration. The sidecar measures
`measurement_jitter_ms` through fresh target TCP connections. For short shutdown
budgets, it also measures the teardown floor by observing a SIGKILLed calibration
listener disappear from the same bridge. Host fallback retains host-side Docker
round-trip and teardown calibration.

JSON reports `probe_location`, `measurement_jitter_ms`, and teardown calibration
together so the location and resolution can't be separated accidentally.

## Security

The probe has the following runtime restrictions:

- It isn't privileged and doesn't use the host network.
- It receives no Docker socket or host filesystem mount.
- It drops all Linux capabilities and enables `no-new-privileges`.
- It uses a read-only root filesystem and a bounded, in-memory payload directory.
- It has fixed CPU, memory, and process limits.
- It joins only the temporary bridge used by the target and declared services.
- Preflightkit force-removes it before deleting the run network, including after
  measurement errors.

The host publishes the probe's control port only on `127.0.0.1`. Application
traffic never uses that port; it travels directly from the probe namespace to
the target's `target` network alias.
