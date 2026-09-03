# Traffic probe container

Rolloutkit sends startup, readiness, accept-window, and in-flight traffic from
a run-scoped probe container on the target's bridge network. This is the primary
measurement path on every supported platform.

## Configuration

The probe uses the public `python:3.12-slim` image by default. On the first run,
Rolloutkit pulls this base image if it is not already available and reports
`pulling probe image (~50MB, once)...` on stderr. Configure another Python image
when your environment mirrors base images under a different name:

```yaml
probe:
  image: registry.internal.example/python:3.12-slim
```

Rolloutkit doesn't publish a custom probe image. It copies the probe program
and its exact traffic dependencies into a temporary in-memory filesystem when
the container starts. This avoids a Rolloutkit registry dependency and keeps
the CLI and probe code on the same version. In an air-gapped environment, load
the configured base image into Docker before running Rolloutkit; no pull is
attempted when that image is already present.

If Docker can't start the probe or attach it to the run network, Rolloutkit
uses host traffic. JSON evidence then reports `probe_location: host_fallback`
and `probe_fallback_reason`. `port_proxy_likely` applies only to this fallback.

## Dependency gates

`services.<name>.wait_for.tcp` holds the run until that dependency accepts a
connection on the port it declares. The wait runs from the probe container,
because a dependency publishes no port: on the run network it is reachable by
its alias and nowhere else.

The port has to be written down because nothing else in the run knows it. The
target declares one; a dependency does not, and `depends_on` in a compose file
carries no port either.

Without the gate the target is started the moment its dependency's container
exists. A database that is still initialising is not a database yet, so the
target gives up on it and fails its own readiness probe — and the run reports a
readiness failure naming the target, which is accurate about what happened and
wrong about what to go and read. A dependency that never accepts ends the run
with exit 3, names itself, and prints its own log tail.

On the host fallback the gate depends on the host. Where container addresses
are routable it waits from the host directly. Where they are not — Docker
Desktop, where the daemon lives in a VM — a connection to the dependency's
address times out whether or not it is listening, so the gate is skipped rather
than allowed to abort working configurations. JSON reports every gate under
`dependency_waits`, a skipped one with the reason it was skipped, so a run whose
gates did nothing is distinguishable from one whose gates all passed.

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
- Rolloutkit force-removes it before deleting the run network, including after
  measurement errors.

The host publishes the probe's control port only on `127.0.0.1`. Application
traffic never uses that port; it travels directly from the probe namespace to
the target's `target` network alias.
