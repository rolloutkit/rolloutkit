# README material

Raw material for a README that has not been written yet. Nothing here is prose
for a reader: these are captured outputs, kept verbatim, so that whoever writes
the README quotes a real run instead of typing a plausible-looking one.

Every `.txt` file starts with the command that produced it. All of them were
captured on 2026-08-28 from rolloutkit 0.1.0, at `COLUMNS=100`, with stdout
and stderr merged in the order a terminal would show them — the five progress
lines come from stderr, the report from stdout.

| file | what it is |
| --- | --- |
| [`service-b-in-app.txt`](service-b-in-app.txt) | a real service judged under the `in_app` drain profile |
| [`service-b-prestop.txt`](service-b-prestop.txt) | the same image, same run shape, judged under `prestop` |
| [`zero-config-service-b.txt`](zero-config-service-b.txt) | the one-line form against that same real service, no configuration file |
| [`zero-config.txt`](zero-config.txt) | the one-line form against a fixture that shuts down correctly |
| [`explain-sp004.txt`](explain-sp004.txt) | `explain SP004`, complete |
| [`explain-sp005.txt`](explain-sp005.txt) | `explain SP005`, complete |
| [`service-b-in-app.yaml`](service-b-in-app.yaml) | the configuration behind the first run |
| [`service-b-prestop.yaml`](service-b-prestop.yaml) | the configuration behind the second |

## The two profile runs

service-b is Django 5.0.6 behind `gunicorn --workers 2` with the sync worker
class — the image `docs/field-notes.md` measured on 2026-08-22. Two workers
means two requests are served at a time and everything behind them waits in the
listen backlog, so 100 concurrent requests produce a real queue rather than the
artificial one a slow endpoint fakes.

Both runs are from the macOS laptop, `Darwin 25.5.0 / docker 29.7.2 / 11cpu`.
The 2026-08-22 field run was on a native Linux server, so the timings here are
not comparable with the ones in that section; the verdicts are.

The pair is the argument for the `deployment` block existing at all. The image
is identical, the two configurations differ in five lines, and the verdicts do
not agree:

| contract | `in_app`, 5s window | `prestop`, 5s sleep |
| --- | --- | --- |
| SP004 drain-window | **INCONCLUSIVE** — `accept_window_unmeasured`, the window was never observed | **PASS** — `prestop_not_applicable`, the hook owns routing removal |
| SP005 inflight-completion | FAIL — 2/74 completed, 72 destroyed | FAIL — 3/62 completed, 59 destroyed |

SP004 is the row that moves, and what it moves between is the point. Declaring
`in_app` is claiming the application keeps accepting new connections for the
full 5s window, so the tool has to time when the listener stopped; declaring
`prestop` hands that job to the platform hook before SIGTERM, and the same
listener behaviour stops being a question.

Under `in_app` the answer is that there is no answer. `T2 last new connection
accepted -202ms` is a negative window: the last connection the probe got
accepted predates the signal, so no accept after T0 was ever observed. Rather
than report that as a listener that "closed -202ms after T0" — a stopwatch
reading nobody took — SP004 declines, keeps the raw number in evidence, and
names the mechanism the evidence supports:

```
SP004 drain-window  INCONCLUSIVE  accept window not measured: the probe was still busy
on an earlier connection for the 202ms before T0 (`explain SP004` for why, --format
json for the attempts)
```

One sentence, naming the mechanism and the interval nothing sampled. The probe
interval, the rule that classified it and the attempt list are a command and a
flag away rather than in the summary line.

That mechanism is worth a README paragraph of its own, because it is this
configuration doing it to itself. The accept probe is serial: it opens one
connection, waits for the response, then opens the next 50ms later. 100
concurrent requests against two sync workers is exactly the queue that makes
that response take longer than the gap to the signal, so the probe spends the
whole run-up to T0 blocked inside one attempt. SP005's load is what costs SP004
its measurement. Turn the concurrency down and the window comes back; leave it
up and SP005 gets the queue it was written for. The two contracts are asking
the same target for different things.

SP005 fails either way, and that is the finding worth putting in front of a
reader: no drain profile saves requests the process destroys on its way out.

## The zero-config runs

`rolloutkit test IMAGE --port PORT --ready-url PATH`, from an empty
directory. The empty directory is deliberate — the tool discovers
`rolloutkit.yaml` from the working directory, so a run started in a project
root is not zero-config even when no `-c` is passed.

`zero-config-service-b.txt` is the one to quote. It is the same real image as
the two profile runs, with no configuration file anywhere, and one line finds a
production defect the configured runs cannot:

```
PID 1 signal disposition   sh, no SIGTERM handler - the kernel will discard it
SP003 signal-handling  FAIL   shutdown never started: PID 1 (sh) showed no reaction to SIGTERM
SP006 shutdown-deadline  FAIL  killed by SIGKILL at the end of the 30s budget (exit 137);
the process never shut itself down
```

The image's own `CMD` is `/bin/sh -c "… && gunicorn …"` — no `exec`, so the
shell stays PID 1, and the kernel discards a SIGTERM sent to a PID 1 whose
disposition is still the default. The two `.yaml` files here each add `exec` to
that command line, which is why their runs show `gunicorn, SIGTERM handler
installed` and a shutdown finished inside 300ms. Both readings are true of the same image: what
the container does as shipped, and what it does once the entrypoint is fixed.

That run needs one flag, and it is the reason `--env` exists:

```
$ rolloutkit test service-b:latest --port 8000 --ready-url /healthz/
infrastructure error
sidecar could not observe readiness /healthz/: traffic probe /startup failed: HTTP 408:
{"last":{"ok":false,"status":400, …

--- container output (last lines) ---
… Invalid HTTP_HOST header: 'target:8000'. You may need to add 'target' to ALLOWED_HOSTS.
```

Two stretches of one run, with the 400 page's headers and Django's traceback cut
at the `…` — that failure is not kept as a file here, only the shape of it.

Exit 3, nothing measured. The container is reached by its name on the run's own
network, which Django's host check rejects, and before `--env` the one-line path
had no way to say otherwise — every Django service with a host check was outside
the happy path. `--env ALLOWED_HOSTS='*'` closes it, and the value goes through
the same name-based redaction as anything written in a config file.

`zero-config.txt` is the contrast: the same one-line form against a Go fixture
that shuts down correctly, where the only findings are the two the zero-config
path structurally cannot answer. SP004 warns rather than fails, because with no
configuration there is no drain claim to hold the image to. SP005 comes back
INCONCLUSIVE: with no `--inflight-path`, readiness is the fallback target, and
this fixture's readiness answers in 0.3ms against 0.1ms of probe-path jitter —
a ratio of 1.8x where 10x is required. Both readings are per-run properties of
the host, so the ratio in the file is the one that run measured, not a constant.
The output names it and the fix.

That is the honest shape of the zero-config path: a real measurement of most of
the lifecycle, findings that do not need a configuration file to be real, and a
precise statement of which part it could not resolve rather than a guess.

## Sanitization

The configurations here are rewritten from the originals, which live in the
service's own repository and are not ours to publish:

- the header comment no longer names the client project;
- the image is retagged `service-b:latest`. The original local build tag
  named the framework rather than the client, so this is for consistency
  with the field notes rather than for concealment;
- `env_file: .env` is replaced by a single inline `ALLOWED_HOSTS: "*"` — passed
  as `--env ALLOWED_HOSTS='*'` in the zero-config run, which is the same value
  by another route. The original file holds the service's real secrets. Nothing
  else in it affects what is measured — the database engine defaults to sqlite,
  so the image runs standalone, and what is under measurement is gunicorn's
  shutdown path and PID 1's signal disposition.

The route names are real and are left alone: `/healthz/` and `/api/subjects/`
identify nothing. The runs are real runs of the real image under these
configurations, not edited transcripts.
