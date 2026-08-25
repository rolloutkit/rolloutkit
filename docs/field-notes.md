# Field notes

Acceptance #4b: real images, run by hand, once each. The output of this file is a
list of problems, not a pass/fail. It is the input to the decision at the end of
M2 about whether the model holds up.

For each image: how long it took to reach a measurable state, what was missing,
which config field turned out to be necessary.

Historical sections that predate JSON provenance are marked with
`preflightkit commit: unknown (pre-provenance)`. New measurements must name the
exact harness commit in their section heading.

---

## service-a (2026-08-21; preflightkit commit: unknown, pre-provenance)

FastAPI behind `gunicorn -k uvicorn.workers.UvicornWorker --workers 4`,
`--graceful-timeout 30`. The application uses several external dependencies.

**Time to first measurement: ~11 minutes**, nearly all of it spent finding out
why the container would not boot.

### What was missing

1. **The image did not exist.** `docker compose build` had never been run for
   this service. preflightkit reported it as an *internal error* (exit 4) rather
   than an infrastructure error (exit 3) — a DockerError escaped `run_session`
   uncaught. **Fixed.**

2. **Startup failures were undiagnosable.** The container died during boot,
   preflightkit removed it, and the only message was
   `readiness /health/ never returned 200 (ConnectError)`. The actual reason was
   in the container's own log output, which was collected into `report.logs_tail`
   and then thrown away with the report. Reproducing it required running
   `docker run` by hand three times. **Fixed**: `StartupFailure` now carries the
   log tail (120 lines, redacted, markup disabled) and the CLI prints it.

3. **The error named the symptom, not the cause.** Docker Desktop's port proxy
   accepts connections even with nothing behind it, so `wait_for_tcp` succeeded
   against a container that had already exited, and the failure surfaced as a
   readiness problem. **Fixed**: the readiness path now inspects container state
   first and reports `container exited during startup with code 1` instead.

4. **The dependency environment was not runnable as supplied.** Multiple
   dependencies were absent or initialized differently from the target's
   configuration. These were target-environment failures, not preflightkit
   verdicts, but the tool had to preserve enough startup evidence to distinguish
   them from readiness failures.

### Which config fields were needed

- `env_file` plus dependency endpoint overrides were needed before run-scoped
  service networking existed. This became the main argument for `services:`.
- The exact readiness route, including its trailing slash, had to be configured;
  the slashless route redirected instead of returning the expected status.
- `contracts.startup.budget: 30s` covered worker and dependency startup, which
  measured 4.34s.

### What was measured

| Contract | Verdict | Evidence |
|---|---|---|
| SP001 startup | PASS | ready in 4.34s |
| SP003 signal-handling | PASS | exit 0, 460ms |
| SP005 in-flight | SKIP | not configured — see below |
| SP006 shutdown-deadline | PASS | 460ms of a 25s budget |

Readiness stopped answering 104ms after SIGTERM — gunicorn closes its listener
almost immediately. Under a `prestop` profile that is fine, because the 5s sleep
covers the Endpoints removal. Under `drain: none` it would be a hole.

### Open problem: SP005 could not be run

The core contract needs an endpoint that is still executing when SIGTERM
arrives. This service has no obviously slow endpoint, so `contracts.inflight`
was left out and SP005 reported SKIP.

This is the first real limitation of the model: **it assumes the user can name a
slow endpoint.** Options worth considering — a `warmup` request mode, driving
load against a normal endpoint with high concurrency, or accepting that SP005
simply does not apply to services without long requests.

Worth noting for later: `--graceful-timeout 30` exceeds the 25s shutdown budget
in this profile. Nothing in flight means nothing waited for, so it never showed.
With SP005 configured, it might.

---

## service-a, second session (2026-08-21; preflightkit commit: unknown, pre-provenance)

The first session left SP005 unmeasured. This one measured it, and in doing so
answered the "assumes a slow endpoint" problem and turned up three reporting
defects.

### Bringing it up, again

The dependency environment had drifted since the first session and one data
store had no schema. The crash log exposed both problems. Until initialization
completed, every measured application request returned 500. A measurement
taken against that state would have been technically valid and practically
worthless: the responses completed, but the service was not working. This
became the reason SP005 requires an all-2xx steady-state baseline.

### SP005: the slow endpoint was not needed after all

The slowest public endpoint on this image is ~85ms. The first attempt used
`concurrent: 200` on the theory that 200 requests against 4 gunicorn workers
would build a queue deep enough to hold requests open. It did not: all 200
finished within 66ms of each other, 41–59ms apiece. Uvicorn workers are async,
so concurrency does not queue — it fans out.

What worked was to stop trying to lengthen the request and instead shorten the
window: `sigterm_after: 30ms` against a ~50ms request. 200/200 in flight, 200/200
completed.

**Reliability note (2026-08-25):** this exact 200/200 count was measured before
the harness waited for an in-flight request to reach the socket. It is an
unversioned, pre-fix measurement and is unreliable; it is retained here rather
than silently replaced. The verdict was rechecked below.

So the limitation recorded last time is narrower than it looked. SP005 does not
need a slow endpoint; it needs `sigterm_after` to land inside the request. The
honest constraint is a **ratio**: the in-flight window has to stay well clear of
`measurement_jitter_ms` (1.4–1.8ms here, so a 30ms window is ~20x margin). That
is a documentable rule, not a dead end — but nothing in the tool computes it or
says it out loud. The ERROR note still only suggests "point the contract at a
genuinely slow endpoint", which is the advice that does not work here.

### What was measured, realistic profile

grace 30s, preStop sleep 5s, drain prestop -> 25s budget. Three repeats.

| Contract | Verdict | Evidence |
|---|---|---|
| SP001 startup | PASS | 4.32s median (4.32 / 4.35 / 4.41), 30s budget |
| SP003 signal-handling | PASS | exit 0 in 461ms, no SIGKILL |
| SP005 in-flight | PASS | 200/200 accepted before T0, 200/200 completed |
| SP006 shutdown-deadline | PASS | 461ms of 25s, 24.5s margin |

Readiness went unreachable 48–122ms after SIGTERM, mode `unreachable` rather
than `status_change` — the app does not answer 503 while draining, it stops
answering. Under `prestop` the 5s sleep covers it. Under `drain: in_app` this
would be the finding.

**The one real defect this found in the application**: of 200 responses
delivered after SIGTERM, `announced_connection_close: 0`. The app finishes every
in-flight request correctly and never tells the client the connection is
closing. Behind a load balancer that pools keep-alive connections, that is the
shape of a 502 — the pool holds a connection to a process that is about to
vanish. SP005 passes and the evidence still says something is wrong, which is
the behaviour the evidence block exists for.

### Profile sensitivity on a real image

Same image, `termination_grace_period: 250ms`, no preStop, `drain: none`:
SP003 FAIL, SP006 WARN, exit 1. Acceptance #3 — one image, two profiles,
opposite verdicts — now holds outside the fixtures.

But the numbers at that budget describe the harness as much as the app. SIGKILL
goes out at +275ms and the daemon reports the exit at +346ms; the exit code is
**0**, three runs running, so the process ended on its own and our SIGKILL
changed nothing. `sigkill_sent` records what we did, not what killed it, and
SP003's message ("SIGKILL was required — the process did not exit on SIGTERM")
asserts the second while measuring the first. At a 25s budget this never comes
up. At 250ms it is the whole result.

One earlier run at the same profile did land differently — exit 137, SP005 FAIL
with 134 of 200 destroyed, one caught mid-body at 0/55 bytes. That is a genuine
sub-100ms race, and it is the case `--repeat` exists for. Three repeats
afterwards were unanimous, so no FLAKY was raised: the flake is rarer than 1-in-3
and the tool would report a clean FAIL to CI on most days.

### Defects found in preflightkit

1. **SP006 returns WARN when the budget was blown.** `margin_ms: -107.5`,
   `sigkill_required: true`, status WARN, summary "only -107ms under the 250ms
   budget". The FAIL branch is guarded on `duration is None`, which cannot happen
   once SIGKILL is sent, because the process still exits and the duration is
   still recorded. A profile that violates only SP006 passes CI. This is the
   false negative the product exists to prevent.
   **Fixed**: the guard is now `margin < 0 or sigkill_effective`. The condition
   that made it unreachable is gone, and `fixtures/ignores-sigterm` holds the
   branch open — see the coverage rule below.

2. **The evidence block has no cap.** The timeline correctly prints 5 events and
   says "... and 129 more, all listed under CONTRACTS" — and then CONTRACTS
   prints all 134, one per line. The cap was applied where the output was already
   readable and skipped where it was not.
   **Fixed**: `_MAX_EVIDENCE_ROWS = 5`, and both blocks now point at
   `--format json`, which is the only output that really does hold every event.

3. **Redaction hid a dependency hostname in a crash log.** `Cannot connect to
   host ***redacted***` removed the fact needed to diagnose the failure. Values
   that are hostnames are not secrets in the way passwords are.
   **Fixed**: only env vars whose *name* matches `KEY|TOKEN|SECRET|PASSWORD` have
   their values masked. Request headers keep blanket masking — `Authorization`
   matches none of those four words and is a credential anyway.

4. **`sigkill_sent` was read as a verdict.** SP003 announced "SIGKILL was
   required" over an exit code of 0, because it checked what preflightkit did
   rather than what ended the process. At a 25s budget the two never diverge; at
   250ms the difference is the whole result.
   **Fixed**: `sigkill_effective = (exit_code == 137)` decides, `sigkill_sent`
   stays in the evidence, and when they disagree the report says so in a note.

### Closed since

- **Nothing checked that the service was functional before judging SP005.**
  There is now a baseline phase: 25 concurrent requests at the ready,
  unsignalled service before anything is shut down. If they are not all 2xx,
  SP005 is **INCONCLUSIVE** rather than a clean sweep of meaningless 500s. The
  independent lifecycle contracts still publish their verdicts. Because SP005
  is required, any enabled `--fail-on` gate blocks on that inconclusive result
  unless the caller explicitly adds `--allow-inconclusive`. The baseline also
  removed the guess from `sigterm_after`, which is now derived from the
  measured p50 unless the config pins it.
- **`sigterm_after` versus `measurement_jitter_ms`** is recorded rather than
  ruled on: SP005's evidence carries both plus their ratio, with a note when the
  window is within an order of magnitude of the noise floor. Making it a verdict
  would be a threshold invented rather than measured.

### Still open

- `--graceful-timeout 30` still exceeds the 25s budget and still never shows,
  now for a measured reason: nothing in this service takes long enough to make
  gunicorn use its graceful window.

---

## Fixture matrix, real containers (2026-08-21; preflightkit commit: unknown, pre-provenance)

Nine rows, five images, every declared verdict branch of every contract either
covered by a fixture or listed with a reason it cannot be reached — and
`tests/test_coverage.py` fails the build if that stops being true. Seven rows
matched what the matrix claimed on the first run. The other two are the notes
below.

### PID 1 does not die of SIGTERM, and that is not an edge case

The `no-signal-handler` row expected exit 143: no handler, default disposition,
kernel kills the process. It hung instead, burned the whole grace period and left
on SIGKILL.

The kernel discards signals whose disposition is the default for the init process
of a PID namespace, and PID 1 in a container is exactly that. So the reasoning
"we don't need a SIGTERM handler, the default is to exit" is wrong in every
container it has ever been applied to — and Kubernetes will spend the full
`terminationGracePeriodSeconds` on it at every single rollout.

Two fixtures now, because they are different findings:

- `pid1-discards-sigterm` — no handler, PID 1, SIGKILL. SP003 FAIL/killed,
  SP006 FAIL/past_deadline.
- `default-disposition` — the same app behind a signal-forwarding shell wrapper.
  The shell holds PID 1 and traps SIGTERM, so the signal is delivered and the app
  dies of it as an ordinary child: exit 143, SP003 WARN. This is the only honest
  route to 143 in a container, and it is why SP003 warns rather than fails — the
  exit code cannot tell it apart from an app that re-raises SIGTERM after
  cleaning up.

### Defects found in preflightkit (continued)

5. **A grace period of 30 seconds or more produced an internal error.**
   `wait()` long-polls `POST /containers/{id}/wait` and passed `timeout=None` to
   mean "no limit". `_request` read `None` as "unspecified" and applied the 30
   second client default, so the poll died of `httpx.ReadTimeout` before the
   container did. Exit 4 with `ExceptionGroup: unhandled errors in a TaskGroup`,
   for the Kubernetes default grace period, on exactly the shutdown SP006 exists
   to measure. Only the short-budget fixtures had ever exercised it.
   **Fixed**: a `_CLIENT_DEFAULT` sentinel now separates "unspecified" from "no
   timeout at all"; `anyio.fail_after` remains the only clock on the wait.
   `tests/test_docker_timeouts.py` holds both meanings apart without a daemon.

6. **The internal-error message named the wrapper, not the failure.** Everything
   runs in anyio task groups, so an error from a background task arrives wrapped;
   the CLI printed `ExceptionGroup: unhandled errors in a TaskGroup (1
   sub-exception)`, which says neither what failed nor where. Finding defect 5
   meant re-running the session by hand to get a traceback.
   **Fixed**: groups are flattened to their leaves for the message, and a group
   whose leaves are all Docker failures is now classified as infrastructure
   (exit 3) rather than as a bug in preflightkit (exit 4).

---

## go-http: nosignal / graceful (2026-08-21; preflightkit commit: unknown, pre-provenance)

Two purpose-built Go fixtures, not images found in the wild: identical
`net/http` servers on `:8000` (`GET /ready` → 200, `GET /work` → 50ms then 200),
multi-stage build, `CGO_ENABLED=0`, `FROM scratch`, exec-form
`ENTRYPOINT ["/server"]`. 4.92MB each, no shell, no libc, nothing between the
application and the signal. The only difference is twelve lines:
`signal.Notify(stop, syscall.SIGTERM)` and `srv.Shutdown(ctx)`.

The profile is the realistic one rather than the fixture-sized one:
`termination_grace_period: 30s`, `pre_stop: sleep 5s`, `drain: prestop` — a 25s
shutdown budget. `contracts.inflight` aims 10 concurrent requests at `/work`
with `sigterm_after` left unset.

Saved output: `docs/runs/go-nosignal.{txt,json}`, `docs/runs/go-graceful.{txt,json}`.
Terminal and JSON are separate invocations; both agreed on every verdict.

**Time to first measurement: ~4 minutes**, almost all of it the `golang:1.23-alpine`
pull. This number does not mean what the service-a one means: those images were
someone else's and the clock measured discovery. Here the clock measures the
config surface, and the config surface held — see below.

### What was missing

1. **`preflightkit measure` does not exist.** The spike ships one command,
   `test`. `test --format json` produced everything needed, so nothing was
   blocked, but the command named in the task is not there.

2. **Nothing else.** No new config field, no schema change, no flag. A Go binary
   on scratch with no interpreter, no package manager and no shell went through
   the same config as the FastAPI fixtures unmodified. That is the first evidence
   that the config model is describing container lifecycle rather than Python.

3. **`sigterm_after` could not have been guessed here.** A 50ms endpoint gives a
   window of a few tens of milliseconds; picked by hand it is a coin flip. The
   baseline measured p50 = 56.6ms / 57.8ms and derived 28ms / 29ms. Both runs put
   ten requests in flight at T0 on the first attempt. The jitter ratio came out
   15.4 and 27.0 — the window is real, but only 15–27x the daemon's own noise
   floor, which is the narrowest this has been exercised at.

### Prediction versus measurement

Six predictions were written down before anything ran. **Three held.**

| # | Prediction | Verdict |
|---|---|---|
| 1 | nosignal: SP003 FAIL, no reaction to SIGTERM, SIGKILL required, exit 137, shutdown consumes the full 25s | **Wrong on all four** |
| 2 | nosignal: `pid1: true`, `sigkill_effective: true` | **Wrong**, and `pid1` does not exist |
| 3 | nosignal: SP005 in-flight requests destroyed | **Right**, for the wrong reason |
| 4 | nosignal: SP006 FAIL, budget exceeded | **Wrong** |
| 5 | graceful: SP003 PASS, exit 0, within ~100ms | **Right** |
| 6 | graceful: SP005 PASS, `announced_connection_close > 0` | **Right** |

**1 and 4 — nosignal does react to SIGTERM.** It exits **2** in 84ms, having
destroyed all ten in-flight requests at +7ms. No SIGKILL was sent
(`sigkill_sent: false`), the 25s budget was never approached, SP003 PASSed and
SP006 PASSed. The prediction was carried over from yesterday's Python fixture,
where a handlerless PID 1 ignores SIGTERM completely and has to be killed. Go
does not behave that way, and the reason is worth stating precisely.

The kernel discards a signal aimed at the init process of a PID namespace only
when its disposition is the **default**. The Go runtime installs a real handler
for SIGTERM at startup whether or not anyone calls `signal.Notify`, because
`os/signal` has to be able to deliver it later. So the signal *is* delivered.
With no `os/signal` consumer registered, the runtime takes the die path: reset
the handler to `SIG_DFL`, re-raise — and *that* raise is discarded, because now
the disposition is the default and the process is PID 1. The runtime falls
through to `exit(2)`.

The same binary confirms it from the other side. Run behind `docker --init`,
where tini holds PID 1 and the server is an ordinary child:

```
PID 1 = /server        SIGTERM -> exit 2
PID 1 = docker-init    SIGTERM -> exit 143
```

Same image, same signal, different exit code, and the only variable is which
process is PID 1. This is also the sharpest justification yet for never enabling
`HostConfig.Init`: it does not merely perturb the timing, it changes the measured
exit code from 2 to 143. A tool that quietly enabled `--init` would have reported
the wrong number with total confidence.

**3 — right answer, wrong mechanism.** The requests were destroyed, but not by a
SIGKILL at the end of a 25s budget. They died at **+7ms**, when the runtime's
`exit(2)` tore the sockets down mid-request. "Requests destroyed" was predicted
from a 25-second cause and measured from a 7-millisecond one. A status-only check
would have called this a correct prediction.

**5 and 6 — held exactly.** graceful exits 0 in 106ms / 119ms, ten of ten
in-flight requests complete, and `announced_connection_close: 10` —
`srv.Shutdown` sets `Connection: close` on every response it finishes on the way
out, on all ten. `responses_after_sigterm: 10`. This is the first time that
evidence field has been produced by anything other than uvicorn.

### Are `pid1` and `sigkill_effective` correct on a non-Python image?

This was meant to be their first independent test. Only one of them exists.

**There is no `pid1` field.** Not in the JSON, not in any contract's evidence,
not anywhere in the source. The PID-1 signal-discard mechanism was written up in
this file yesterday as a major finding and never became a measurement. The fact
itself is true here and was verified outside the tool — `ps` in the container's
PID namespace shows `1 server` — but preflightkit does not report it, cannot key
a verdict on it, and gave no hint of it in either run. Given that this same
mechanism produces exit 2 on Go, silence on Python, and 143 behind an init
process, a report that never names it is leaving out the variable that explains
all three.

**`sigkill_effective` is correct.** `false` on both runs, matching `exit_code` 2
and 0, with `sigkill_sent: false` alongside it. Nothing about it is
Python-specific — it reads the exit code the daemon reported, and on an image
with no interpreter at all it still says the right thing. It also did the job it
was added for in the negative direction: with the old `sigkill_required` logic
there was no SIGKILL to misreport here, and the field correctly stayed quiet
rather than inventing a story about a signal nobody sent.

### Defects found in preflightkit

7. **SP003 reports exit 2 as a clean exit.** `SP003 PASS/clean_exit — "exit 2
   after 84ms"`. The branch order is `never_exited` → `killed` (137) →
   `default_disposition` (143) → `stopsignal_mismatch` → `clean_exit`, so every
   exit code that is not 137 or 143 lands in `clean_exit`, including non-zero
   ones. Exit 2 is the Go runtime announcing that it could not die properly. The
   report calls it clean and passes the contract.

   This is the same family as the SP006 false negative fixed yesterday — a
   verdict reached by falling off the end of a chain of specific checks rather
   than by testing the thing being claimed. `clean_exit` currently means "not one
   of the codes we named", where it should mean "exit 0". Left unfixed
   deliberately: this run was a measurement, and changing contract logic
   mid-measurement would invalidate what it measured.

8. **T4 includes container teardown, and at this timescale it shows.** nosignal's
   sockets die at +7ms and readiness goes unreachable at +20ms, but `T4 process
   exit` is reported at +84ms. The process was gone long before the daemon said
   so. `measurement_jitter_ms` is 1.8ms, so ~60ms of that gap is not daemon RTT —
   it is container teardown being counted as application shutdown. Against a 25s
   budget it is a rounding error. Against the 250ms budget in the service-a
   notes it would be a quarter of the result. Not a wrong number, but
   `shutdown_duration_ms` is measuring more than its name claims.

### Still open, from this run

- No `pid1` evidence, per above.
- `jitter_ratio` at 15.4 is the lowest observed so far and still produced a clean
  ten-of-ten window. The threshold at which the derived `sigterm_after` stops
  being reliable has not been found, only approached.

---

## SP003 redesign and defect 8 (2026-08-22; preflightkit commit: unknown, pre-provenance)

Not a new image. This is the session where defects 7 and 8 from the go-http run
were fixed, and the rule was that neither fix would be designed before the thing
it claimed to fix had been measured.

### Defect 7: the verdict now comes from behaviour

`clean_exit` meant "not one of the codes we named", so a Go binary that destroyed
ten in-flight requests and exited 2 was published as a clean shutdown. The fix
was not to add exit 2 to the list. It was to stop asking the exit code what
happened.

Three behaviours are separable, and only the first is legible in the exit code:

| Behaviour | How it is established |
|---|---|
| the process ended itself | `exit_code is not None and not sigkill_effective` |
| SIGKILL ended it | `exit_code == 137` |
| SIGTERM never reached it | `/proc/1/status`, read **before** the signal |

`killed` split into `signal_discarded` (FAIL — PID 1 had no handler, the kernel
dropped the signal) and `killed` (FAIL — it had the signal and did not act), and
a new `unclean_exit` (WARN) catches the codes that used to fall through. The exit
code moved into `evidence`, where it is a fact rather than a verdict.

The same fixture now reads:

```
SP003 WARN  the process stopped itself after 72ms and reported failure (exit 2)
```

### The PID-1 measurement that yesterday's note asked for

`/proc/1/status` cannot be read with `docker cp` — that reads the layered
rootfs, and `/proc` is not in it. It needs a sidecar sharing the target's PID
namespace: `HostConfig.PidMode = "container:<id>"`, `busybox cat /proc/1/status`,
`NetworkMode: none`, read-only rootfs, 64MB, half a CPU. Never pulled; if
`busybox:latest` is absent the field degrades to `None` and SP003 says so instead
of guessing.

`SigCgt` is a bitmask; SIGTERM is signal 15, so bit 14, so `0x4000`. Measured on
the three images already in the repo:

| PID 1 | `SigCgt` | SIGTERM handler |
|---|---|---|
| CPython | `0000000000000002` | **no** — SIGINT only |
| Go runtime | `fffffffd7fc1feff` | yes — everything, asked for or not |
| uvicorn | `0000000100004002` | yes |

The middle row is the whole go-http finding, now readable from a bitmask
**before** the experiment rather than inferred from an exit code after it. The
top row predicts the Python fixture's SIGKILL before a signal is sent.

### Defect 8: T4, resolved by measuring three clocks

The gap was real — sockets died at +7ms, the daemon reported exit at +84ms — but
"container teardown" was a label, not a diagnosis. Three clocks, three runs, four
images:

| Clock | What it contains |
|---|---|
| observed | our kill request out, our `wait` back — a round trip at each end |
| daemon `kill`→`die` | dockerd's own stamps, one clock, no round trip of ours |
| `wait` return | earliest, and anchored to nothing in particular |

The daemon's `kill` frame lands **6–13ms after** we issue the kill, so our T0 was
systematically early by that much. The `die` frame arrives 1.3–2.1ms *after*
`wait` returns — later, but better anchored. `daemon ≈ observed − 6ms` held on
every image. T4 now comes from `die.timeNano − kill.timeNano`; `observed` is kept
as evidence with `observation_lag_ms` beside it.

That removed our round trip. It did not remove the gap, so the gap was measured
directly: SIGKILL a `sleep` — which cannot catch, block or delay it — and time
the daemon's own `kill`→`die`. Everything in that number is the daemon.

| Container shape | kill→die (3 runs) |
|---|---|
| no network | 12.8 / 12.4 / 12.3 ms |
| bridge, no published port | 51.0 / 43.1 / 52.0 ms |
| bridge + published port | 82.8 / 84.4 / 94.1 ms |

**Publishing a port costs 70–80ms of teardown on Docker Desktop.** That is
defect 8, in full: not "container teardown" generally, but the port forwarder in
front of the container being dismantled before dockerd calls it dead. The Go
binary's 84ms was ~6ms of application and ~78ms of plumbing.

This also caught a defect in the fix itself. The floor probe was first written
with `NetworkMode: none` — the cheapest shape — which reports ~12ms where the
honest floor for a target that always publishes a port is ~85ms. A calibration
that understates by sevenfold is worse than none, because it looks rigorous. The
probe now runs in the same shape as the target.

The number is reported, never subtracted. A measurement minus an estimate is a
figure nothing observed. When the floor exceeds the measured duration — as it
does on the Go fixture, 79ms floor against 73ms measured — the report says the
application's shutdown is not resolvable on this host rather than crediting it
with a speed nothing measured:

```
The 73ms is at or below this host's floor of 79ms ... the application's own
shutdown is not resolvable here; all that can be said is that it cost nothing
this measurement could see.
```

On the uvicorn fixture (3.13s) the note stays silent: 79ms inside a real drain is
noise, and saying so would be noise too.

### What this cost, in honesty

Three verdicts changed, all in the direction of claiming less:

- Go nosignal: `PASS clean_exit` → `WARN unclean_exit`
- Python handlerless: `FAIL killed` → `FAIL signal_discarded`, naming the cause
- Every fast shutdown: a bare duration → a duration with its floor beside it

None of these are new capabilities. They are the same runs, reported without the
parts that were being asserted rather than measured.

---

## service-b (2026-08-22; preflightkit commit: unknown, pre-provenance)

Django 5.0.6 behind `gunicorn core.wsgi:application --workers 2 --timeout 300`.
No `-k`, so the **sync** worker class: two requests are served at a time and
everything else waits in the kernel's listen backlog. This is the first image
where concurrency produces a real queue rather than one faked by a slow endpoint.

**Time to first measurement: ~7 minutes**, most of it the image build. Faster
than service-a because this image could boot standalone.

### What was missing

1. **No external dependency was required for this measurement.** The image has
   a local fallback and tolerates an unavailable cache, so it booted standalone.

2. **No slow endpoint was needed.** A normal public endpoint measured about
   41ms unloaded. Behind two sync workers, 100 concurrent requests pushed p50 to
   about 190ms and the tail past a second. The queue became the load generator.

### Which config fields were needed

`env_file`, a liveness-only readiness route, `startup.budget: 60s` (startup
management commands run before gunicorn binds; ~6.5s observed),
`inflight.concurrent: 100`,
`sigterm_after: 300ms`. Also `target.command`, to build the counterfactual below
without editing the user's Dockerfile.

### The result, in one table

The image was run twice. The only difference is one word — `exec` before
`gunicorn` in the shell-form CMD, the standard fix every container guide gives.

| | SP003 | SP005 | SP006 |
|---|---|---|---|
| as shipped | **FAIL** signal_discarded | **PASS** 56/56 | **FAIL** SIGKILL at 30s |
| `exec` added | **PASS** clean_exit, 336ms | **FAIL** 4/60 | **PASS** 336ms |

**Reliability note (2026-08-25):** the `exec` row's exact SP005 count was
measured before the harness waited for an in-flight request to reach the
socket. It is an unversioned, pre-fix measurement and is unreliable; it is
retained here rather than deleted. The verdict was rechecked below.

**Adding `exec` turned a passing in-flight contract into a failing one.** Not
because the fix is wrong — it is the correct fix — but because the passing
version passed by never shutting down at all.

### Prediction versus measurement

Nine predictions written down before the first run. **Eight held.**

| # | Prediction | Verdict |
|---|---|---|
| 1 | PID 1 is `sh`, no SIGTERM handler | **Right** |
| 2 | SIGTERM discarded, SP003 FAIL/signal_discarded | **Right** |
| 3 | SIGKILL ends it, full budget consumed | **Right** |
| 4 | SP005 FAIL, 0–2 of N completed | **Wrong** — SP005 PASSed, 56/56 |
| 5 | destroyed requests are a mix the tool cannot separate | **Right**, see below |
| 6 | gunicorn's graceful path never runs | **Right** |
| 7 | with `exec`: handler present, SP003 PASS | **Right** |
| 8 | with `exec`: shutdown well inside 30s | **Right** — 336ms |
| 9 | with `exec`: ~2 of ~60 complete | **Right** — 4/60, and 2/72 on a rerun |

**1 and 2 — the bitmask said so before the signal was sent.** PID 1's
`/proc/1/status` reads `SigCgt: 0000000000010002`: signals 2 and 17, SIGINT and
SIGCHLD, and nothing else. Bit 14 is clear, so SIGTERM's disposition is still the
default, so the kernel discards it — dash installs no trap for a non-interactive
`-c`, and it cannot exec-optimise a `&&` chain away. With `exec` added, PID 1
becomes gunicorn and the mask reads `0000000008314a07`: signals 1, 2, 3, 10, 12,
**15**, 17, 21, 22, 28 — precisely gunicorn's documented arbiter signal set.

This is the measurement added yesterday doing, on its first real image, the exact
job it was added for: naming the cause before the experiment rather than
inferring it from an exit code afterwards. The report says
`SIGTERM was discarded: PID 1 (sh) had no handler installed for it`, and it is
right.

**4 — wrong, and wrong in the direction that matters.** As shipped, SP005
reported `PASS — 56/56 in-flight requests completed`. Every request finished
because the application never learned it was dying: it kept serving normally for
thirty seconds and was then SIGKILLed with nothing in flight. The contract's own
claim is true and its window contained no shutdown at all.

**9 — gunicorn sync workers abandon the queue, as predicted.** With `exec`, the
arbiter closes the listener, forwards SIGTERM to both workers, each finishes the
one request it is holding, and the master is gone in 336ms — nowhere near the 30s
`--graceful-timeout`, because a sync worker's graceful path waits for the request
in hand and nothing else. Four completed; fifty-six died at **+66ms** with zero
bytes received. `--graceful-timeout` is not what governs this. Worker count is.

**5 — the destroyed population is two populations.** All 56 are reported
identically: `reset_before_response` / `awaiting_response`, detail
`peer closed the connection (FIN)`. But two of them were being processed by a
worker when the listener closed, and fifty-four had been accepted by the kernel's
backlog and never seen by Django at all. Those need different fixes — more
workers versus a preStop drain — and the report cannot tell them apart. This is
the T2 semantics problem from the plan, met in the field for the first time.

Also worth noting: the close is a **FIN**, not an RST. From a load balancer's
side these look like orderly closes carrying no response, which is a quieter
failure than a connection error and easier to miss in upstream metrics.

### Defects found in preflightkit

9. **SP005 passes without noticing that no shutdown occurred.** In the as-shipped
   run SP003 reported the signal discarded and SP005 reported `PASS — 56/56`,
   with an empty `notes` list. Nothing in SP005's output says its measurement
   window did not contain a shutdown. A reader taking the contracts one at a time
   — which is how a CI summary presents them — would conclude that in-flight work
   is handled correctly on termination. It is not; it was never tested. SP005
   should say so when `runtime_handler_installed` is false or SIGKILL was what
   ended the process.

   Note this is the mirror image of the property the plan deliberately designed
   for (`kills-inflight` exits 0, so SP003 passes while SP005 fails). Contract
   independence is right. Reporting a contract whose precondition never held as
   a plain PASS is not.

10. **`announced_connection_close` is not evidence of anything on this server.**
    The as-shipped run — where the application never received SIGTERM — reports
    `announced_connection_close: 56`, all 56 responses. Gunicorn's sync worker
    sends `Connection: close` on **every** response unconditionally; it has no
    keep-alive. The field is presented under `keepalive_closed_cleanly`, which
    reads as "the server announced it was going away", and here it means only
    "the server is a sync worker". On uvicorn the field is meaningful. It needs
    either a baseline comparison (did this header appear *before* T0 too?) or a
    plainer name.

### Closed in M1: preconditions

The two findings above are now contract preconditions rather than after-the-fact
notes:

- SP003 publishes the observed `shutdown_started` fact after the experiment.
  Traffic still runs before that fact is known. SP005 returns `INCONCLUSIVE /
  shutdown_never_started` when readiness never changes, accepts do not stop,
  and the process does not exit voluntarily. Its candidate request counts and
  evidence remain in the report. The shipped shell-PID1 configuration is a real
  fixture and can no longer produce SP005 PASS.
- SP005 separately requires an all-2xx steady-state baseline. A 500 response
  that completes is no longer treated like a correct response that completes.
- SP006 calibrates five teardown samples in the target's network shape. It uses
  the median floor plus three sample standard deviations as the host-local
  resolution threshold. The samples, median, spread, coefficient, and computed
  threshold are evidence; budgets inside that envelope return `INCONCLUSIVE`.
  Budgets above 2s skip the five-cycle calibration and report
  `not_calibrated`, because calibration cannot change their measurability.
- Shutdown-time `announced_connection_close` is `not_applicable` when the
  steady-state baseline never established keep-alive, as with sync gunicorn.

SP003 now selects its verdict from observed reaction, deadline, and effective
SIGKILL. Exit 0, 2, and 143 remain evidence, but do not select different verdict
branches.

All built-in contracts declare whether they are required. SP005 is required, so
both `SKIP` and `INCONCLUSIVE` block whenever `--fail-on` is enabled. The only
escape hatch is the explicit `--allow-inconclusive` flag; report-only mode keeps
exit 0.

### Closed in M1: run-scoped bridge and honest TCP evidence (2026-08-24)

Each experiment now creates one user-defined bridge, attaches dependency
`services:` and the target, and removes the containers before removing the
network. Dependency keys are installed as network aliases. A resolver container
on the runtime-created bridge resolved a generic service alias to the dependency
container IP; the test is separate from the verdict fixture matrix.

The service-a compose configuration was checked directly. Its internal service
aliases now remain unchanged in a preflightkit `services:` configuration; host
gateway rewrites are no longer needed for DNS. This is not compose import:
volumes, compose healthchecks, and `depends_on` conditions still have to be
represented or prepared explicitly until `init --from-compose`.

On Linux, the target port is not published and traffic goes to the container IP.
The delayed-bind fixture therefore measures the application's bind, about 3s.
On this macOS Docker Desktop host the same real fixture reported:

- `port_proxy_likely: true`;
- traffic through `127.0.0.1:<published>` while retaining the custom bridge;
- raw proxy TCP observation at 1.27ms, exposed only as evidence;
- SP001 `tcp_open_status: INCONCLUSIVE`, with readiness at 3.20s;
- `teardown_calibration_status: not_calibrated` for its 30s budget;
- no owned network left after cleanup.

Teardown probes use the same run network name and the same published/unpublished
shape as the target when the budget is 2s or less. This keeps the floor
comparable on Linux direct-IP runs and Docker Desktop fallback runs.

### Native Linux acceptance (2026-08-24)

Both bridge checks were repeated manually on `Linux 6.8.0-134-generic`, Docker
29.6.1/amd64. The host process used `/var/run/docker.sock`; this was not Docker
Desktop's Linux VM.

The three-second delayed-bind fixture produced `port_proxy_likely: false`, a
direct private container endpoint, and `published_port: null`. SP001's TCP
sub-measurement was `MEASURED` at 3752.51ms and readiness passed at 3796.23ms.
The cold image/process overhead explains the distance from exactly 3000ms; the
important distinction held: the TCP timestamp followed the application bind
instead of appearing near zero through a proxy.

The service-a image was then run against fresh ephemeral dependencies on another
owned bridge. No existing network, volume, credential, or dependency container
was used. Readiness returned 200 through a direct private container endpoint,
with `published_port: null` and `port_proxy_likely: false`. TCP opened at
2383.60ms; full readiness took 26180.60ms on the cold ephemeral dependencies.
This closes the DNS question: service-name resolution works without host-gateway
rewrites.

Both runs removed their owned containers and networks. The direct Linux
connection path required by SP004 is now verified.

### Closed in M1: SP004 drain window (2026-08-24)

SP004 now runs its own 50ms stream of fresh TCP connections before T0 and keeps
probing until process exit or three consecutive connection refusals. It does not
reuse SP005 keep-alive traffic and does not require a 2xx baseline: any HTTP
response proves that the new connection reached the listener. The report always
records T0, T1 and its `status_change | unreachable | never` mode, T2, T4,
`accept_window_ms`, `accept_window_resolution_ms`, post-T0 outcomes, and the
refused, timeout, or reset outcomes after T2.

The verdict is resolved only after the run. Missing `shutdown_started`, a
published-port proxy, a shutdown budget inside the measured teardown envelope,
or an in-app window no larger than twenty probe intervals produces
`INCONCLUSIVE` while retaining the candidate verdict and traffic evidence.
Connections accepted after T0 and then reset without any response override all
drain strategies with `FAIL / accept_then_reset`.

The native Linux fixture matrix ran on the same host used for bridge acceptance.
All ten integration rows matched their declared status and branch:

- immediate listener close: `in_app_listener_closed_early` FAIL at about 0ms,
  but `prestop_covered` PASS for the identical behavior;
- 1.8s in-app drain: `in_app_covered` PASS at 1864.88ms;
- thin reserve: `in_app_thin_margin` WARN at 1528.80ms for a 1300ms window;
- undeclared strategy: `none_uncovered` WARN;
- deliberate response-less RST: `accept_then_reset` FAIL under `in_app`,
  `prestop`, and `none`;
- preStop exit near its 2100ms budget: `prestop_near_deadline` WARN;
- a 1000ms in-app window at 50ms resolution:
  `in_app_window_below_probe_resolution` INCONCLUSIVE.

Every terminal refusal streak was classified as `connection_refused`; the RST
fixture was the only source of `accept_then_reset`. The readiness warning now
names the observable property: only an explicit readiness status change is a
drain signal. A readiness endpoint that stays healthy until it becomes
unreachable follows the same `in_app_readiness_not_signaled` WARN branch; the
process-exit race no longer changes the verdict.

The same 1.8s drain fixture was also rerun on Docker Desktop. SP004 returned
`INCONCLUSIVE / port_proxy_likely`. Its evidence retained an
`unresolved_candidate` of `PASS / in_app_covered`; no candidate status appears
beside the contract verdict. The experiment was not skipped: evidence retained
a 2092.31ms apparent accept window, 50ms resolution, T1 `status_change` at
29.90ms, T4 at 2140.45ms, and 41 post-T0 connection outcomes. Those numbers
describe the published-port path and therefore do not become a verdict.

### SP004 real-image predictions, recorded before measurement (2026-08-24)

These predictions were written before either image was run through SP004. Both
images previously showed gunicorn closing its listener near T0. The profile,
not the image, must therefore decide whether that behavior is acceptable.

| Image | Profile | Predicted SP004 result |
|---|---|---|
| service-a | `prestop` | PASS: preStop owns routing removal, so a short accept window is evidence, not a defect |
| service-a | `in_app`, 5s window | FAIL: the accept window is much shorter than 5s |
| service-b with `exec` | `prestop` | PASS: preStop owns routing removal, so immediate listener closure is acceptable |
| service-b with `exec` | `in_app`, 5s window | FAIL: sync gunicorn closes the listener much earlier than 5s |

The comparison will use native Linux direct container-IP traffic. A Docker
Desktop published-port run cannot test these predictions because SP004 must
return `INCONCLUSIVE / port_proxy_likely` there.

### SP004 real-image results

The two in-app predictions held at the status level. Neither preStop prediction
held cleanly because the universal reset rule found a stronger condition than
the strategy-specific table.

| Image | Profile | Predicted | First measured result | Accept window |
|---|---|---|---|---|
| service-a | `prestop` | PASS | **FAIL / accept_then_reset** | 107.75ms (±50ms) |
| service-a | `in_app`, 5s | FAIL / early close | **FAIL / in_app_listener_closed_early** | 82.54ms (±50ms) |
| service-b with `exec` | `prestop` | PASS | **FAIL / accept_then_reset** | 76.89ms (±50ms) |
| service-b with `exec` | `in_app`, 5s | FAIL / early close | **FAIL / accept_then_reset** | 95.10ms (±50ms) |

The service-a preStop difference is intermittent. Two additional independent
runs produced `PASS / prestop_covered` at 99.76ms and 64.72ms, with three clean
`connection_refused` outcomes after T2 and no reset. The first run completed the
TCP handshake at +107.75ms and received `ECONNRESET` at +110.16ms. The measured
population is therefore two PASS and one FAIL, not a stable PASS. A combined
repeat set must report this as FLAKY rather than choose the majority.

The service-b preStop difference repeated. Its second run again completed a
post-T0 handshake at +109.97ms and received a response-less RST at +392.51ms.
The in-app run had the same shape. Sync gunicorn closes its listener while an
established connection can still be queued in the kernel backlog; the client
has completed `connect()`, but no worker returns an HTTP response before the
socket is reset. Under SP004's universal rule this is FAIL under every strategy.
The 5s window comparison never gets to override that stronger finding.

service-a uses asynchronous uvicorn workers, so a worker usually consumes the
last established connection before shutdown and returns a response. One of
three preStop runs landed inside the smaller close/dispatch race and reset it.
This explains why its profile result varied while service-b's two sync-worker runs
did not.

Both images were measured on native Linux with `port_proxy_likely: false` and
direct container-IP traffic. service-a used an existing anonymized image
snapshot with fresh ephemeral dependencies. service-b was built natively for
amd64 from the measured source snapshot.
Its first direct-IP startup returned 400 because the container IP was absent
from `ALLOWED_HOSTS`; the measurement profile added `ALLOWED_HOSTS=*` and then
reached readiness. That failed startup produced no contract verdict.

### Startup and T4 evidence from the same runs

SP001 now separates Docker's target-container create/start/inspect call from
application startup as `container_start_overhead_ms`. service-a measured
564–848ms across four runs; service-b measured 505–921ms across three successful
runs. The earlier 752ms delayed-bind forecast is inside the observed Linux
range. TCP-open and readiness durations remain relative to the returned,
inspected container endpoint, so the new value is evidence and is not silently
added to either application timing.

Defect 8 is closed for SP006: every successful real-image report selected
`shutdown_duration_source: daemon_events`, using the target container's first
Docker `kill.timeNano` through `die.timeNano`. The observed `wait()` wall-time
remains separate evidence. SP004 previously labeled that observed timestamp as
T4; it now records daemon T4 as `t4_exit_offset_ms`, plus
`t4_exit_source: daemon_events` and `t4_observed_exit_offset_ms`.

The distinction is visible in these runs. service-a's first preStop run had
daemon T4 760.04ms versus observed 793.68ms. service-b's first preStop run had
1156.39ms versus 1179.50ms. One repeat reversed the ordering: daemon T4
1375.19ms versus a 1284.23ms `wait()` return. That is not an HTTP round trip in
the selected duration; it is Docker reaching `not-running` before emitting the
network-teardown-complete `die` event. Teardown calibration measures this
component in the same network shape. It remains evidence and is never
subtracted from a target duration.

### Still open, from this run

- `jitter_ratio` reached **172.4** here, the highest recorded, on a clean window.
  The threshold at which the derived `sigterm_after` becomes unreliable is still
  only being approached from above.
- The in-flight denominator moves between runs (56, 60, 72 out of 100 requested)
  because it counts what was still open at T0, and p50 sits near `sigterm_after`.
  Correct, but the report never explains why the number it names is not the
  number in the config.

### SP004 preStop specification correction and rerun (2026-08-24)

The original SP004 table made `accept_then_reset` a strategy-independent FAIL.
That rule confused a completed TCP handshake with an application-level
`accept()` call. The kernel can place a connection in the listener backlog and
reset it when the listener closes without the application ever owning the
socket. A 20-per-second probe kept creating that race after T0 even though a
real preStop rollout has already removed the pod from routing before SIGTERM.
The service-a one-in-three failure above was the signature of this measurement
error, not an intermittent application defect.

The corrected preStop model stops the fresh-connection probe at T0. SP004 emits
`PASS / prestop_not_applicable`: the summary and evidence state that listener
timing is not applicable to this strategy, while the required contract remains
configured rather than becoming a blocking `SKIP`. `accept_window_ms`, the
50ms resolution, and rejection classes remain evidence. The `in_app` and
`none` streams continue after T0, and connections that start after T0 and reset
without a response still produce `FAIL / accept_then_reset`. Connections that
started before T0 remain SP005's responsibility.

The corrected code was measured on native Linux with direct container-IP
traffic and `port_proxy_likely: false`. Each image/profile pair ran three times.

| Image | Profile | Three-run result | Accept windows, each ±50ms |
|---|---|---|---|
| service-a | `prestop` | 3/3 `PASS / prestop_not_applicable`; no post-T0 handshake or reset | -21.08ms, -3.86ms, -6.74ms |
| service-a | `in_app`, 5s | 3/3 `FAIL / in_app_listener_closed_early` | 67.63ms, 116.90ms, 76.30ms |
| service-b with `exec` | `prestop` | 3/3 `PASS / prestop_not_applicable`; no post-T0 handshake or reset | -8.85ms, -5.98ms, -4.01ms |
| service-b with `exec` | `in_app`, 5s | 3/3 FAIL; `accept_then_reset` remained the stronger in-app finding | 71.55ms, 107.77ms, 124.58ms |

The service-a in-app prediction was approximately 82ms. Its 76.30ms median
and 67.63–116.90ms range agree at the probe's stated ±50ms resolution. service-b's
in-app status also matched the prediction. Its branch was
`accept_then_reset`, not the weaker early-listener-close branch, because the
post-T0 probe is intentionally retained for `in_app` and reproduced the sync
gunicorn backlog reset.

Both runs used anonymized image snapshots and fresh, isolated dependencies.
The first service-b attempt stopped before verdict evaluation because the minimal
measurement environment selected production settings without the required CORS
origin list. The rerun used the same development-settings mode as the previous
Django measurement; this changed startup configuration, not gunicorn's signal
or listener behavior.

The same reports expose `startup_resolution_ms` as the target container's
create/start/inspect overhead: 546.34–727.26ms for service-a and
500.09–714.33ms for service-b. SP001 now treats a nominal budget overrun inside
that per-run resolution as `PASS / within_resolution`; only an overrun larger
than the measured resolution reaches `WARN / over_budget`. JSON includes the
field both in SP001 actual values and every run summary, and the terminal labels
the value as startup resolution.

---

## SP005 socket-race rerun (2026-08-25; preflightkit commit: 768ce9fb565b91a494334ae5e01cf371790e2b76)

The three suspect SP005 measurements were repeated three times each after the
harness began waiting for at least one request to reach its socket before
sending SIGTERM. All runs used native Linux 6.8.0-134-generic and Docker
29.6.1/amd64. Traffic used the target's private container address;
`port_proxy_likely` was false.

service-b was built from source commit
`efa24f341b5806915782ff9a360b70480e3bdebf` with the `exec gunicorn`
counterfactual. service-a used the same anonymized image snapshot as the prior
field run and fresh isolated Postgres, Redis, and MinIO dependencies. The Go
graceful fixture was built from the preflightkit commit named in this heading.

| Target | Run | SP005 | Completed / in flight at T0 | Issued | Baseline p50 | Jitter |
|---|---:|---|---:|---:|---:|---:|
| service-a, uvicorn workers | 1 | PASS / `all_completed` | 200/200 | 200 | 3012.646ms | 4.367ms |
| service-a, uvicorn workers | 2 | PASS / `all_completed` | 94/94 | 200 | 1186.456ms | 1.765ms |
| service-a, uvicorn workers | 3 | PASS / `all_completed` | 181/181 | 200 | 1960.554ms | 1.856ms |
| service-b, sync workers + `exec` | 1 | FAIL / `requests_destroyed` | 5/89 | 100 | 503.437ms | 2.198ms |
| service-b, sync workers + `exec` | 2 | FAIL / `requests_destroyed` | 3/93 | 100 | 858.215ms | 1.612ms |
| service-b, sync workers + `exec` | 3 | FAIL / `requests_destroyed` | 4/82 | 100 | 327.723ms | 2.566ms |
| Go graceful | 1 | PASS / `all_completed` | 197/197 | 200 | 66.803ms | 1.839ms |
| Go graceful | 2 | PASS / `all_completed` | 121/121 | 200 | 71.359ms | 2.360ms |
| Go graceful | 3 | PASS / `all_completed` | 154/154 | 200 | 77.139ms | 2.448ms |

The verdicts did not change: service-a and Go were 3/3 PASS, and service-b was
3/3 FAIL. The exact populations did change. Therefore the race did not affect
the three qualitative findings, but it did affect which requests were honestly
counted as in flight. The old service-a 200/200, service-b 4/60, and unversioned
Go 200/200 stress readings are measurements from before the harness fix and are
unreliable as exact counts. They remain in the historical record with that
label.

The new denominator is intentionally not the configured concurrency. `issued`
records how many requests the harness launched; `in_flight_at_sigterm` records
only requests that had reached the socket and were still open at T0. Waiting for
one socket write removes the original race without pretending all concurrent
tasks crossed that boundary simultaneously.
