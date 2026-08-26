# Field notes

Acceptance #4b: real images, run by hand, once each. The output of this file is a
list of problems, not a pass/fail. It is the input to the decision at the end of
M2 about whether the model holds up.

For each image: how long it took to reach a measurable state, what was missing,
which config field turned out to be necessary.

Historical sections that predate JSON provenance are marked with
`preflightkit commit: unknown (pre-provenance)`. New measurements must name the
exact harness commit in their section heading.

Unless a historical row explicitly says `probe_location: sidecar`, every
measurement before the product-sidecar rerun used
`probe_location: host_direct`. Those rows remain unchanged as the historical
baseline.

**Every commit SHA in this file that predates 2026-08-26 names a commit that no
longer exists.** The repository's history was rewritten on that date to remove a
work email address from the author and committer fields of all 48 commits, which
`git grep` could not reach because an identity lives in the commit header rather
than in a blob. Rewriting changes every SHA. The trees did not change — only the
identity did — so each section still describes the code it always described, but
the name it uses for that code has to be translated.

The old-to-new table is `docs/commit-map.md`, with the machine-readable two-column
form in `docs/commit-map.txt`. The same applies to `preflightkit_commit` in every
JSON report written before that date, including all 208 documents in the
measurement corpus.

Two SHAs here are not preflightkit commits and are unaffected: service-b's source
commit `efa24f341b58`, and the sidecar-probe spike harness `ece4949723f0`, which
was never part of this repository.

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

---

## Sidecar probe spike (2026-08-25; measurement commit: ece4949723f0352182f949c489185c332a83dc4e)

This section keeps the pre-measurement predictions and the resulting 36 runs in
one record. The spike keeps contract evaluation on the host and changes only
where raw traffic and Docker timing observations originate.

<!-- prettier-ignore -->
> [!NOTE]
> This is an experimental measurement spike. It does not define product
> behavior or move any contract into a sidecar.

### Predictions recorded before measurement

These predictions were committed at `d05b2e8` before the first sidecar container
started. The ranges below are unchanged from that commit.

The matrix uses the same four fixture configurations in each environment. Each
row will run three times. `delayed-bind` isolates SP001 TCP timing,
`immediate-in-app` and `drains-in-app` bound SP004, and `kills-inflight` checks
that the sidecar preserves SP005 request classification.

| Environment | Probe location | Fixture | Prediction before measurement |
|---|---|---|---|
| Linux | host, direct IP | `delayed-bind` | TCP opens 3.0–4.0s after target start; the timestamp is measurable |
| Linux | host, direct IP | `immediate-in-app` | SP004 candidate is FAIL / early close; accept window -50–100ms |
| Linux | host, direct IP | `drains-in-app` | SP004 candidate is PASS / covered; accept window 1.75–1.95s |
| Linux | host, direct IP | `kills-inflight` | SP005 is FAIL; completion rate 0.0–0.2 with 10 issued |
| Linux | sidecar | `delayed-bind` | TCP opens within 100ms of the Linux host result |
| Linux | sidecar | `immediate-in-app` | Same FAIL branch as Linux host; window differs by no more than 100ms |
| Linux | sidecar | `drains-in-app` | Same PASS branch as Linux host; window differs by no more than 100ms |
| Linux | sidecar | `kills-inflight` | Same FAIL classification and completion-rate band as Linux host |
| macOS | sidecar | `delayed-bind` | TCP opens within 100ms of Linux sidecar and remains measurable |
| macOS | sidecar | `immediate-in-app` | FAIL / early close, not proxy-driven INCONCLUSIVE; -50–100ms window |
| macOS | sidecar | `drains-in-app` | PASS / covered; window differs from Linux sidecar by no more than 100ms |
| macOS | sidecar | `kills-inflight` | Same FAIL classification and completion-rate band as Linux sidecar |

Across the matrix, the predicted Linux-sidecar median
`measurement_jitter_ms` differs from the Linux-host median by at most 2ms. The
predicted macOS-sidecar median differs from the Linux-sidecar median by at most
2ms. The predicted teardown floor is 10–60ms on Linux and 35–70ms inside Docker
Desktop's unpublished bridge. The predicted sidecar startup cost is 150–1200ms
on Linux and 200–1500ms on macOS.

### Measured environments

The measurement used spike commit
`ece4949723f0352182f949c489185c332a83dc4e`. Each cell below is the median of
three independent runs, followed by the observed range. The Linux machine ran
Linux 6.8.0-134-generic and Docker 29.6.1/amd64. The macOS machine used Docker
Desktop's Linux VM.

| Probe placement | Jitter | Teardown floor | Probe startup cost |
|---|---:|---:|---:|
| Linux host, direct IP | 1.14ms (0.61–2.07) | 237.07ms (201.90–307.39) | 220.77ms (181.33–262.13) |
| Linux sidecar | 1.48ms (0.97–2.61) | 250.02ms (215.32–431.97) | 1085.39ms (878.38–1905.05) |
| macOS sidecar | 0.95ms (0.88–1.00) | 50.73ms (47.86–56.35) | 177.76ms (165.37–202.23) |

Probe startup cost is wall time from the host starting the `docker run` command
to the Python probe process entering its measurement function. It includes
container create, namespace attachment, and interpreter startup; it excludes
image build and pull time.

The Linux sidecar jitter was 0.34ms above the Linux-host baseline, a 1.30x
ratio. The macOS sidecar was 0.54ms below the Linux sidecar. Both differences
were inside the predicted 2ms band.

The Linux teardown-floor prediction did not hold. During measurement, that host
reported load averages 4.88 / 3.95 / 2.89 on six CPUs. Its sidecar floor was
12.94ms above the host floor, but both were 177–199ms above the predicted upper
bound. The macOS host reported 3.28 / 3.23 / 2.74 on 11 Docker CPUs, and its
50.73ms sidecar floor stayed inside the predicted 35–70ms band.

### TCP and accept-window measurements

The same target image and environment values were used in all three probe
placements. TCP timing starts when Docker returns from starting the target. The
accept window is the last completed handshake relative to sidecar-recorded T0.

| Fixture | Linux host | Linux sidecar | macOS sidecar |
|---|---:|---:|---:|
| `delayed-bind`, TCP open | 3602.48ms (3557.49–3643.83) | 3594.44ms (3545.21–3643.22) | 3173.42ms (3160.07–3179.70) |
| `immediate-in-app`, accept window | 2.29ms (1.48–51.40) | 7.08ms (3.91–57.89) | 0.68ms (0.65–0.88) |
| `drains-in-app`, accept window | 1836.31ms (1832.81–1846.22) | 1878.05ms (1842.30–1898.54) | 1836.91ms (1832.66–1847.53) |

Linux host and Linux sidecar differed by 8.05ms on delayed bind, 4.79ms on
immediate close, and 41.74ms on the 1.8s drain. macOS sidecar and Linux sidecar
differed by 421.02ms on delayed bind, 6.39ms on immediate close, and 41.14ms on
the 1.8s drain.

The 421.02ms delayed-bind difference repeated on the fast-start fixtures. Linux
host and Linux sidecar needed 519–759ms to observe the drain-window target's TCP
listener; macOS sidecar needed 159–170ms. The Linux target-start path therefore
carried 350–590ms more host scheduling and process-start latency before either
probe placement measured the listener. That difference was present between
hosts, not between Linux host and Linux sidecar.

The unchanged SP004 table maps the macOS sidecar immediate-close evidence to
`FAIL / in_app_listener_closed_early`: all three windows were 0.65–0.88ms
against a required 1200ms, and no run used a published host port. The 1.8s drain
covered the same 1200ms requirement in all nine runs. No `drains-in-app` run
recorded an accepted connection that reset without a response.

### In-flight traffic measurements

The `kills-inflight` fixture issued ten `/slow` requests per run. The same
product traffic generator executed inside the sidecar image; contract code was
not imported or evaluated there.

| Probe placement | Three-run completed / in flight | Completion rate | Raw classification |
|---|---:|---:|---|
| Linux host, direct IP | 0/10, 0/10, 0/10 | 0.0, 0.0, 0.0 | 10 destroyed in every run |
| Linux sidecar | 0/10, 0/10, 0/10 | 0.0, 0.0, 0.0 | 10 destroyed in every run |
| macOS sidecar | 0/10, 0/10, 0/10 | 0.0, 0.0, 0.0 | 10 destroyed in every run |

All three placements retained the same SP005 request population and failure
classification.

### Prediction comparison

The table compares every pre-recorded row with its three-run measurement. A
match means the measured value stayed inside the numeric band written before
the run.

| Environment | Fixture | Measured result | Prediction |
|---|---|---|---|
| Linux host | `delayed-bind` | TCP 3557–3644ms | matched |
| Linux host | `immediate-in-app` | early-close FAIL, 1–51ms | matched |
| Linux host | `drains-in-app` | covered, 1833–1846ms | matched |
| Linux host | `kills-inflight` | FAIL, rate 0.0, 10 issued | matched |
| Linux sidecar | `delayed-bind` | median 8ms from Linux host | matched |
| Linux sidecar | `immediate-in-app` | same FAIL, median 5ms from host | matched |
| Linux sidecar | `drains-in-app` | same covered result, median 42ms from host | matched |
| Linux sidecar | `kills-inflight` | same FAIL and rate 0.0 | matched |
| macOS sidecar | `delayed-bind` | median 421ms below Linux sidecar | did not match 100ms band |
| macOS sidecar | `immediate-in-app` | early-close FAIL, 0.65–0.88ms | matched |
| macOS sidecar | `drains-in-app` | covered, median 41ms from Linux sidecar | matched |
| macOS sidecar | `kills-inflight` | same FAIL and rate 0.0 | matched |

The aggregate jitter prediction matched in both comparisons. The Linux
teardown-floor prediction and the Linux sidecar startup upper bound did not:
the sidecar startup range reached 1905.05ms instead of stopping at 1200ms. The
macOS floor and startup predictions matched.

### Numeric answer

For SP004 placement, macOS sidecar and Linux were equivalent at the probe's
50ms resolution: the immediate-close medians differed by 1.61ms from Linux host
and 6.39ms from Linux sidecar; the drain medians differed by 0.60ms and 41.14ms.
macOS produced the same early-close FAIL evidence instead of a proxy-driven
INCONCLUSIVE. SP001 TCP became measurable through the sidecar, while the
delayed-bind medians remained 421.02ms apart across the two hosts.

Across every measured dimension, the environments were not numerically equal:
jitter differed by 0.54ms, teardown floor by 199.28ms, delayed-bind TCP by
421.02ms, and sidecar startup cost by 907.63ms. The sidecar removed the host-port
proxy difference from TCP and accept-window traffic; it did not remove the
Docker-host timing differences recorded by the same probe.

---

## Product sidecar rerun (2026-08-25; preflightkit commit: ee2ab03b8078e7e9442d0f9937b82b2a363a6764)

These are new product runs, not the spike harness. Every row used
`probe_location: sidecar` with the default `python:3.12-slim` probe image. The
Linux host ran Linux 6.8.0-134-generic and Docker 29.6.1/amd64. service-b was
built from source commit `efa24f341b5806915782ff9a360b70480e3bdebf` and used
the `exec gunicorn` counterfactual. service-a used the same anonymized image
snapshot and isolated Postgres, Redis, and MinIO dependencies as its prior
field run.

### Real-image profiles

Each image/profile cell was a fresh, independent three-run prediction. The
completion fraction is followed by `completion_rate`; it is not inferred from
configured concurrency.

| Run | SP004 | Accept window | SP005 | Completed / in flight (rate) | Baseline p50 | Jitter |
|---|---|---:|---|---:|---:|---:|
| service-a, `in_app`, 1 | FAIL / `in_app_listener_closed_early` | 97.98ms | PASS / `all_completed` | 124/124 (1.000) | 2006.91ms | 1.31ms |
| service-a, `in_app`, 2 | FAIL / `in_app_listener_closed_early` | 31.96ms | PASS / `all_completed` | 137/137 (1.000) | 1454.47ms | 0.90ms |
| service-a, `in_app`, 3 | FAIL / `in_app_listener_closed_early` | -595.55ms | PASS / `all_completed` | 7/7 (1.000) | 2255.01ms | 2.14ms |
| service-a, `prestop`, 1 | PASS / `prestop_not_applicable` | -63.47ms | FAIL / `requests_destroyed` | 161/174 (0.925) | 1745.85ms | 1.42ms |
| service-a, `prestop`, 2 | PASS / `prestop_not_applicable` | -47.66ms | FAIL / `requests_destroyed` | 88/163 (0.540) | 1947.63ms | 1.07ms |
| service-a, `prestop`, 3 | PASS / `prestop_not_applicable` | 88.09ms | PASS / `all_completed` | 164/164 (1.000) | 2715.04ms | 1.40ms |
| service-b + `exec`, `in_app`, 1 | FAIL / `accept_then_reset` | 30111.21ms | FAIL / `requests_destroyed` | 2/100 (0.020) | 697.95ms | 1.19ms |
| service-b + `exec`, `in_app`, 2 | FAIL / `accept_then_reset` | 305.27ms | FAIL / `requests_destroyed` | 4/100 (0.040) | 408.98ms | 1.34ms |
| service-b + `exec`, `in_app`, 3 | FAIL / `accept_then_reset` | 241.53ms | FAIL / `requests_destroyed` | 5/96 (0.052) | 592.94ms | 1.14ms |
| service-b + `exec`, `prestop`, 1 | PASS / `prestop_not_applicable` | -21.07ms | FAIL / `requests_destroyed` | 4/100 (0.040) | 531.71ms | 3.07ms |
| service-b + `exec`, `prestop`, 2 | PASS / `prestop_not_applicable` | -13.59ms | FAIL / `requests_destroyed` | 3/98 (0.031) | 689.17ms | 2.21ms |
| service-b + `exec`, `prestop`, 3 | PASS / `prestop_not_applicable` | -0.32ms | FAIL / `requests_destroyed` | 4/100 (0.040) | 415.65ms | 0.99ms |

The service-a SP005 result is profile-sensitive in these runs: `in_app` was
3/3 PASS, while `prestop` was 1/3 PASS and 2/3 FAIL. service-b remained 6/6
FAIL across both profiles. The table retains the raw populations because a
single aggregate would hide that spread.

Added 2026-08-26: one row above no longer describes what the tool would say.
`service-a, in_app, 3` reports `FAIL / in_app_listener_closed_early` on a
-595.55ms accept window, and SP004 stopped reading a negative window as a
listener-close time that day. A last accept further before T0 than one probe
interval now resolves to `INCONCLUSIVE / accept_window_unmeasured`, with the
raw -595.55ms kept in evidence. The other two service-a `in_app` rows are
positive windows and are unaffected, and the `prestop` rows never depended on
the window at all. The table is left as it was measured; this is what the same
measurement would be judged as now.

### Go fixtures

| Fixture | Run | SP003 | SP005 | Completed / in flight (rate) | Jitter |
|---|---:|---|---|---:|---:|
| graceful | 1 | PASS / `shutdown_observed` | PASS / `all_completed` | 10/10 (1.000) | 1.52ms |
| graceful | 2 | PASS / `shutdown_observed` | PASS / `all_completed` | 10/10 (1.000) | 3.76ms |
| graceful | 3 | PASS / `shutdown_observed` | PASS / `all_completed` | 10/10 (1.000) | 1.75ms |
| no signal handler | 1 | PASS / `shutdown_observed` | FAIL / `requests_destroyed` | 0/10 (0.000) | 0.87ms |
| no signal handler | 2 | PASS / `shutdown_observed` | FAIL / `requests_destroyed` | 0/10 (0.000) | 1.08ms |
| no signal handler | 3 | PASS / `shutdown_observed` | FAIL / `requests_destroyed` | 0/10 (0.000) | 1.91ms |

The no-signal fixture uses a fixed one-second handler and a fixture-owned 100ms
signal point. A 50ms handler plus a baseline-derived midpoint stopped reaching
the destruction branch on a loaded Linux runner because Docker API signal
delivery arrived after the handler completed. The longer handler changes only
the fixture's reachability margin.

### macOS and Linux accept windows

Both platforms used the same product commit and `probe_location: sidecar`.
Each cell is the median of three runs followed by the range.

| Fixture | Linux sidecar | macOS sidecar | Verdicts |
|---|---:|---:|---|
| `immediate-in-app` | 44.64ms (-1.31–93.82) | 5.76ms (-22.11–7.39) | FAIL in all six runs |
| `drains-in-app` | 1930.38ms (1862.65–2002.36) | 1858.63ms (1841.84–1868.53) | PASS in all six runs |

The immediate-close medians differed by 38.88ms, inside the 50ms accept-probe
interval. The drain medians differed by 71.75ms on an approximately 1.9s
window; their ranges overlapped and every run covered the declared 1200ms
window. macOS therefore published SP004 FAIL for the immediate-close fixture,
not INCONCLUSIVE.

### Explicit fallback

On macOS, configuring the deliberately absent image
`preflightkit-probe-intentionally-missing:never` selected
`probe_location: host_fallback`. The report retained the exact local-image
error in `probe_fallback_reason`, set `port_proxy_likely: true`, and published
SP004 as `INCONCLUSIVE / port_proxy_likely`. This run used the commit in this
section heading.

### Product fixture matrix

The final matrix ran at preflightkit commit
`75366ef1cfc850814288aa4afd928fe4b6e9efde`. Native Linux completed with
`232 passed, 1 skipped`; macOS completed with `233 passed`. The branch coverage
check reported no uncovered contract branches.

The one Linux skip is deliberately not a sidecar exception. It is the
`docker-desktop-host-fallback-port-proxy` row, whose configured missing probe
image forces `host_fallback`; Docker Desktop then supplies the host-port proxy
that row measures, while native Linux fallback uses the target's direct bridge
address. All primary-path matrix rows ran through the sidecar on Linux, including
SP004 and the teardown-floor branches.

Two narrow timing fixtures were widened after the first Linux pass exposed
runner-dependent branch changes: SP006 thin margin now uses a 25s exit inside a
30s budget, and SP004 thin margin uses an 11s drain against a 10s requirement.
The immediate-close fixture also closes its listener synchronously in the
SIGTERM handler. These changes preserve the named branches while moving their
boundaries well outside host scheduling jitter.

## Prediction duration and phase distribution (2026-08-26; preflightkit commit: 68816b8ecf7e3feda529dcda605fdb8ee4f52dd4 plus the uncommitted phase-progress change)

The spec tolerates a pipeline step of roughly +40s and refuses +5 minutes. That
number had never been measured since the sidecar, the teardown calibration and
the traffic baseline were added, so this section measures it and takes the
phase distribution apart. Timing changes nothing about the verdicts; the
uncommitted change in the tree adds stderr progress lines and does not touch
the measurement path.

### What this run could and could not cover

Two parts of the requested set are missing and are not estimated here.

Native Linux was not reachable from this session. Every number below is Docker
Desktop 29.7.2 on Darwin 25.5.0, arm64, 11 CPUs — the same host class as the
macOS column in the sidecar sections above, not the `Linux 6.8.0-134-generic`
machine those sections used. Container create/start and teardown are the phases
most likely to move on native Linux, and both are small here; the phase that
dominates is request duration, which is a property of the target.

The service-a and service-b measurement configs are not in this repository —
`git status --ignored` shows only `__pycache__` and `spikes/` — so the two real
images were replaced by the fixture that has the same shape: a 5s endpoint with
ten concurrent in-flight requests. Their runs are still owed.

### Phase distribution

Medians of three runs each, milliseconds, warm image cache. `fastest` is the
lightest fixture in the tree: readiness-only, `contracts.inflight: null`. The
last row is one invocation with `--repeat 3`, not three invocations.

| Case | Total | Probe | Deps | Target start | Calib | Baseline | Experiment | Teardown |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `identical-readiness-health` (fastest) | 1848 | 854 | 0 | 232 | 80 | 8 | 467 | 208 |
| `kills-inflight` (5s endpoint) | 18961 | 858 | 0 | 306 | 73 | 15118 | 2407 | 200 |
| `good-fastapi-prestop` (5s endpoint) | 22103 | 856 | 0 | 307 | 75 | 15092 | 5551 | 214 |
| `good-fastapi-prestop --repeat 3` | 65979 | 2440 | 0 | 901 | 217 | 45335 | 16443 | 633 |
| `django-shipped` (1s grace, calibrating) | 7988 | 842 | 0 | 234 | 2912 | 1723 | 2046 | 208 |
| two cached dependencies | 2167 | 855 | 149 | 246 | 104 | 7 | 479 | 377 |

Ranges over the three runs were tight: 1811–1855, 18934–18976, 22062–22106,
7447–8724, 2161–2226. Only the calibrating fixture varied by more than 50ms,
and its variance is in the calibration phase itself (2874–3690).

Two cold-cache cases, one run each. A missing probe image costs
`probe_image_preparation` 3768 against a warm 854. A missing target image costs
`target_start` 3872 against a warm 232; the pull is inside that phase, not a
phase of its own.

### The number that matters

A single prediction against these images is **1.8s to 22.1s** — well inside the
+40s tolerance and nowhere near five minutes. The threshold is crossed only by
`--repeat 3`, which measured **66.0s**.

The distribution is not evenly spread and does not scale with the tool. Fixed
per-run overhead — probe, target start, teardown, and the uncalibrated pid1
probe — is about 1.4s and barely moves between cases. Everything above that is
the target's own request duration:

    baseline_ms ~= 3 x request_duration

25 baseline samples are issued concurrently, so they cost one request duration,
not 25. The other two come from `verify_keep_alive`, which performs two
*sequential* requests on one connection. For the 5s endpoint that predicts
15.0s and the measurement is 15.09s across nine runs, spread 32ms. On the
`/work` fixture, whose observed duration is about 0.57s, it predicts 1.71s and
measures 1.72s.

That relation is what decides whether the spec threshold is ever at risk: a
target whose in-flight endpoint takes longer than about 13s pushes a single
prediction past 40s, regardless of anything preflightkit does.

### Shortening options, priced but not implemented

Each saving below is measured, not estimated. None of them is implemented.

**Caching the teardown calibration.** Calibration only runs when the shutdown
budget is 2s or less; above that it short-circuits and the phase contains just
the pid1 probe. The two states are visible directly: 2912ms median when it
calibrates, 73–80ms when it does not. So caching saves **~2.9s per run, and
only for short-budget configurations** — none of the real-image profiles
measured here. On a `--repeat 3` of a short-budget config it would save two of
the three calibrations, about 5.8s. Its correctness cost is the harder half: the
floor is a property of host and Docker version, both of which a TTL can only
approximate.

**Reducing the baseline probe count.** This one does not pay. The 25 samples are
concurrent, so cutting them to 10 removes concurrency, not wall time — the
predicted and measured saving is **~0ms**. The reducible cost is elsewhere in
the same phase: `verify_keep_alive`'s two sequential requests are
**10.06s of the 15.09s baseline (67%), and 46% of the whole 22.1s run**.
Proving keep-alive with one request instead of two would save one request
duration (5.0s here); overlapping the check with the concurrent burst would save
both (10.1s), taking the run from 22.1s to about 12.0s. This is the largest
saving available anywhere in the pipeline and it was not on the list.

**Starting the probe container in parallel with dependencies.** Bounded by the
smaller of the two phases. Warm probe preparation is 855ms; two cached
dependency containers cost **149ms**, because `services:` currently starts
containers without waiting for anything. The saving today is therefore **~149ms
on a 2.2s run**. It becomes worth revisiting when `wait_for.tcp` lands and the
dependencies phase starts costing seconds — at that point the ceiling rises to
the full 855ms of probe preparation.

**Not on the list, but measured:** `--repeat N` repeats the whole per-run
envelope. Across the 66.0s repeat-3 run that is 2440ms of probe preparation and
217ms of calibration for work whose result is identical each time. Reusing one
sidecar across repeats would save about **1.6s of 66.0s (2.4%)** — small, and
it would couple runs that are currently independent.

Ranked by measured saving: the keep-alive check (10.1s) is worth more than
everything else on the list combined; calibration caching (2.9s) applies to a
minority of configurations; parallel probe start (0.15s) is noise until
dependencies wait for readiness.

### First-run experience

Before this session the only progress line in the tool was the probe-image pull,
so the *warm* path — the common one — printed nothing at all between the command
and the report: 22.5s of an apparently hung terminal on the 5s fixture. Phases
are now announced on stderr as they begin, with stdout left machine-clean, and
`--repeat` numbers them. A cold first run reads:

    starting the traffic probe
    pulling probe image (~50MB, once): python:3.12-slim
    starting the target and waiting for readiness: traefik/whoami
    pulling target image (once): traefik/whoami
    measuring the baseline: 25 requests plus keep-alive
    sending SIGTERM and observing the shutdown
    removing the containers and the network

Calibration announces itself only when it will actually run, so the line never
names a phase that short-circuits.

---

## SP005 in-flight window audit (2026-08-26; preflightkit commit: 7d1cc89d11b1e2c9344989162d6334758718cdd5)

Triggered by a single `nothing_in_flight` ERROR on `go-runtime-exit-2` during a
full-suite run (258 tests, 335s). The row expects FAIL / `requests_destroyed`,
which is reached by counting; an empty window turns it into an ERROR that reads
as a defect in the image rather than in the fixture.

### Not reproducible under CPU load

16 `yes` processes on an 11-core box, four consecutive runs of the fixture as it
then stood (1s handler, `sigterm_after: 100ms`):

| run | verdict | in flight | jitter | window/jitter |
|-----|---------|-----------|--------|---------------|
| 1 | FAIL requests_destroyed | 10/10 | 0.312ms | 320x |
| 2 | FAIL requests_destroyed | 10/10 | 0.235ms | 426x |
| 3 | FAIL requests_destroyed | 10/10 | 1.774ms | 56x |
| 4 | FAIL requests_destroyed | 10/10 | 0.239ms | 418x |

CPU starvation does not empty the window. On the tool's own published rule —
`sigterm_after / measurement_jitter >= MIN_JITTER_RATIO` (10) — this fixture was
never near the boundary; it measured 56-426x.

### The whole matrix, by window

Of 28 rows carrying an SP005 expectation, 5 reach a counted branch
(`all_completed`, `requests_destroyed`). The rest are decided by a precondition
before anything is counted, so no window can change them.

| row | signal aimed at | source | in flight | window/jitter |
|-----|-----------------|--------|-----------|---------------|
| readiness-fallback-slow | 102ms | readiness_fallback | 10/10 | 383x |
| good-fastapi-prestop | 2515ms | baseline | 10/10 | 2067x |
| startup-over-budget | 2533ms | baseline | 10/10 | 9953x |
| kills-inflight | 2000ms | config | 10/10 | 8077x |
| go-runtime-exit-2 | 100ms | config | 10/10 | 417x |

Every row populates its window completely, and every row clears the tool's rule
by two to three orders of magnitude.

### Why `expected_duration / sigterm_after` is not the rule to gate on

That ratio is not what preflightkit measures against anything. Gating the matrix
on it at 10x would fail `kills-inflight` (5000/2000 = 2.5x, 10/10 in flight,
never observed to flake) and cannot be evaluated at all for the three rows that
leave `sigterm_after` unset — those get half the measured p50, so the ratio is
2x *by construction of the tool's own default*. A 10x gate would condemn the
default derivation.

### What the evidence does point at

T0 is an absolute wall-clock deadline, set before the sidecar is told to start:

    lead_ms = report.sigterm_after_ms
    t0_unix_ns = time.time_ns() + (lead_ms + 300) * 1_000_000

The sidecar therefore has `lead_ms + 300ms` to receive the POST and connect
every request, and that allowance does not stretch under load. `_was_in_flight`
keys on `connected_ns`, so a late connect — not an early finish — empties the
window. The two rows with a small lead are `go-runtime-exit-2` (100ms) and
`readiness-fallback-slow` (102ms); every other row has 2000ms or more. The row
that flaked is one of the two.

Consequence worth stating plainly: **lengthening the handler does not address
this mechanism.** The window survives T0 either way; what is scarce is the
setup allowance before it. The handler was widened anyway (1s to 5s, surviving
window 900ms to 4900ms) because it costs 4s of suite time and removes the other
way the window can empty, but the 300ms constant is where the remaining risk
lives. Making that allowance proportional to the lead rather than fixed is the
change that would close it; not implemented, not measured.

### A second row, much closer to the boundary

The Docker matrix run that verified the above failed a different row:
`readiness-fallback-fast` expected INCONCLUSIVE / `readiness_fallback_below_resolution`
and returned PASS / `all_completed`. Six consecutive runs:

| run | verdict | readiness p50 | jitter | ratio |
|-----|---------|---------------|--------|-------|
| 1 | INCONCLUSIVE | 5.33ms | 1.185ms | 4.50x |
| 2 | PASS | 3.62ms | 0.237ms | 15.28x |
| 3 | INCONCLUSIVE | 2.64ms | 1.163ms | 2.27x |
| 4 | PASS | 3.59ms | 0.234ms | 15.34x |
| 5 | INCONCLUSIVE | 2.54ms | 1.336ms | 1.90x |
| 6 | INCONCLUSIVE | 4.95ms | 1.662ms | 2.98x |

Two of six flip the verdict: a ~33% flake rate, an order of magnitude worse
than the row that started this audit. The measured ratio straddles the 10x rule
from 1.9x to 15.3x.

p50 is stable at 2.5-5.3ms; the swing is entirely in the jitter, which is
bimodal at roughly 0.235ms or 1.2-1.7ms. When the quiet mode wins, a 3.6ms
readiness endpoint clears 10x and the fallback becomes resolvable — so the row
that exists to demonstrate *unresolvable* fallback demonstrates the opposite.

This row cannot be fixed by widening a margin, because it is the one row whose
expectation requires sitting *below* the rule, and it has no lever left.

Both fallback rows drive the same knob, `READINESS_DELAY_SECONDS` on the shared
`pfk-fixture-good` image. `readiness-fallback-slow` sets it to 0.2, which puts
p50 at ~200ms against at most 1.7ms of jitter — about 118x, and it can be moved
further out at will. `readiness-fallback-fast` sets it to nothing, so p50 is the
container's own floor. The knob only turns one way, and that way is toward the
threshold, not away from it.

The margin it would need is not available: against the observed 0.234ms jitter
floor, holding the ratio under 10x requires a p50 under 2.34ms, and the fastest
p50 measured across six runs was 2.54ms. It misses by roughly 200us of Docker
port-forward round trip, and no fixture setting buys that back. The other term,
jitter, is measurement noise — not a fixture input at all.

So the asymmetry is structural. The slow row proves its branch by moving away
from the rule; the fast row can only prove its branch by sitting on it. Two ways
out, neither implemented, because both change something deliberate:

- pin the inputs, so the branch is proven against fixed p50 and jitter rather
  than against whatever the runner produces;
- let `tests/test_coverage.py` accept a unit test as proof for this one branch.
  `tests/test_preconditions.py::test_readiness_fallback_below_jitter_resolution_is_inconclusive`
  already proves it with p50 8.0ms and jitter 1.8ms, deterministically, in the
  fast suite. The live row adds no coverage over it — only the die roll.

The second is the smaller change but touches the repo's central guarantee, that
every declared branch is reached by a real image. That guarantee exists because
SP006 once declared a FAIL no code could reach and CI stayed green.

### Resolved: classified, not excused

The second was taken, as a classification rather than an exemption. Branches are
now declared in the catalog as one of two kinds. `live_image` branches assert
something about the target — the process exited, the connection was reset,
readiness changed — and only a real image can show the tool read that correctly;
they keep the guarantee unchanged. `decision_unit` branches compare numbers
already measured and say whether the comparison resolves at all; the image is
not an input, so a container supplies the comparison with whatever the host
produced and the verdict tracks the machine instead of the target.

`readiness_fallback_below_resolution` is the only branch classified
`decision_unit` so far. Its proof is a named pytest node id that the coverage
gate runs, so the branch is only covered while that test is green, and a rename
fails rather than silently uncovering it. The classification is repeated
independently in `tests/test_coverage.py::REVIEWED_DECISION_UNIT`; the two must
agree, which is what keeps this from becoming the exception list it replaced.

### The real finding is not about tests

The numbers above describe the tool's default path, not just a fixture. With no
`--inflight-path`, SP005 falls back to the readiness endpoint, and readiness is
usually the fastest endpoint a service has. On this machine that put p50 at
2.5-5.3ms against a 0.23-1.66ms jitter floor: 1.9x to 15.3x against a required
10x, straddling it. The jitter floor belongs to the host, not the image, so the
zero-config one-line prediction can resolve on one machine and decline on
another for the same service.

The tool's behaviour is correct — declining to guess is the right answer, and
the summary already names the fix. What was missing is that the limitation was
not written down anywhere a user would meet it. It now appears in README under
Known limitations and in `explain SP005`'s precondition text, both with the
measured numbers.

### Open question: is 10x the right number for the fallback path?

`MIN_JITTER_RATIO = 10` is one constant serving two different jobs, and the
evidence for it comes from only one of them. Its recorded basis is a configured
in-flight path: 1.4-1.8ms of daemon jitter against a 30ms window, about 20x,
and that window worked. On a configured path the ratio is a sanity check on a
window the user chose, and a user who wants more margin lengthens the endpoint.

On the fallback path the same constant decides whether the default experience
runs at all, against a window nobody chose — half the readiness p50, which is
whatever the service happens to be. There the constant is not a sanity check,
it is the gate on the zero-config path, and the measurements say it sits right
where real services land.

Two things argue for a separate, higher fallback constant rather than a lower
one. First, the failure modes are not symmetric: on a configured path a
marginal ratio yields a noisy count the user can discount, while on the fallback
path it decides between measuring and not measuring, so being wrong near the
boundary costs more. Second, the numbers show the boundary is exactly where the
default lands — 1.9x to 15.3x on one machine for one service — which means the
current constant makes host identity, not service behaviour, the deciding input.
Raising the fallback constant would convert an unstable answer into a stable
"point --inflight-path at something slower", which is the action the user has to
take either way.

What argues against acting yet: all of this is one machine. The claim that the
default lands on the boundary needs the same measurement on Linux and on CI
before a constant is chosen, because the jitter floor is exactly the term that
varies by host, and picking a number from one laptop's floor would repeat the
mistake this note is about. Not changed. The readings the choice needs are now
kept on every run — see "What is now recorded on every run" below.

Since corrected in part. Thirty-two runs across four load conditions say the
ratio barely moves with how busy the machine is, because the jitter probe and
the readiness probe pay the same costs and the ratio divides them out. The term
that decides the fallback verdict on this host is the endpoint's own speed, plus
a sampling artefact on a cold Docker VM. "Host identity is the deciding input"
is therefore too strong; what remains open is whether the jitter *floor* differs
enough between Linux, macOS and CI to move the threshold. See "What the fallback
ratio tracks, measured four ways" at the end of this file.

Now closed. The floor differs by 3.4x — 0.154ms on macOS against 0.516ms on a
native Linux CI daemon — and it moves the verdict on three of ten
configurations. "Host identity is the deciding input" was not too strong after
all; it was measured on the wrong axis. See "Three hosts, pipeline cost, and the
fallback decision" at the end of this file.

## Watch list and measurement record (2026-08-26; preflightkit commit: 394996b0bd0afb74d7021b3d51db92a3302293ff)

### Watch item: SP001 `within_resolution` is the same one-way knob

The classification split does not cover this row, and it should not be made to.
`within_resolution` is `live_image` and correctly so: readiness time is a real
property of the target, and no arithmetic over already-measured numbers can
stand in for the image becoming ready. What it shares with
`readiness-fallback-fast` is not the type — it is the shape of the fixture that
has to reach it.

The branch is entered when

    budget < startup_duration_ms <= budget + startup_resolution_ms

and `startup_resolution_ms` is `container_start_overhead_ms` plus one 100ms
readiness poll. That makes the admissible window exactly as wide as the
resolution, wherever the budget is put.

Neither knob the fixture has can widen it. Moving `contracts.startup.budget`
slides the window; the width is a property of the host's Docker daemon and this
harness's poll interval, not of anything the fixture declares. A faster image
does not widen it either — it only moves `startup_duration_ms` within the same
window, and toward the lower edge. What the image knob does have is a stop: the
row already runs on `pfk-fixture-stdlib`, `python:3.12-slim` with one stdlib
module and no framework import, which is as fast as anything in this repository
that is still a Python server. If a host pushes readiness past
`budget + resolution`, the answer that worked in `68c24bd` — pick a faster image
— is not available a second time.

Three runs today, `Darwin 25.5.0 / docker 29.7.2 / 11cpu`, host load average
43.5-49.6:

| Run | `startup_duration_ms` | `startup_resolution_ms` | Overrun | Overrun / resolution |
|---|---:|---:|---:|---:|
| 1 | 160.93 | 215.29 | 60.93 | 28.3% |
| 2 | 172.43 | 213.61 | 72.43 | 33.9% |
| 3 | 170.33 | 213.01 | 70.33 | 33.0% |

Both margins are comfortable here: 61-72ms above the 100ms budget on the low
side, and 141-154ms of unused resolution on the high side. That is the state of
one host under heavy load, not a guarantee.

The row's history is the reason it is on a watch list rather than left alone.
The budget has been 15s, then 200ms, then 1ms, then 100ms, and the image was
swapped from `pfk-fixture-good` to `pfk-fixture-stdlib` in `68c24bd`. Four
adjustments to one row, each one correct in isolation, each one a response to a
failure on a machine that was not the previous machine.

**Prediction.** This branch fails again on a host whose timing differs from this
laptop's, in one of two directions:

- On a fast host — native Linux, no virtual machine between the CLI and the
  daemon — `startup_duration_ms` falls to or below the 100ms budget. The verdict
  stays PASS but the branch becomes `within_budget`, and `fixtures/matrix.yaml`
  fails on the branch rather than on the status. Readiness has to lose only
  61-72ms of the 161-172ms it takes here for this to happen, and the fixture is
  already the fastest image available, so the only remaining response is to move
  the budget again.
- On a loaded runner where interpreter startup inflates more than the daemon's
  create/start round trip does, the overrun outgrows the resolution and the
  branch becomes `over_budget` (WARN). Today's overrun uses a third of the
  available resolution, so this needs roughly a threefold divergence between the
  two costs, not merely a slow machine.

Which direction arrives first is not predictable from here, and the earlier
Linux measurements in this file argue against assuming the obvious one: that
host's teardown floor was 237-250ms against this laptop's 50.73ms, so "native
Linux" did not mean "every daemon operation is cheaper". What is predictable is
that one of them arrives, because the row's margin is a host quantity that no
fixture setting controls.

**What a recurrence means.** Not another adjustment. `within_resolution` is a
verdict about the target whose *boundary* is a host measurement, which is
neither of the two types the catalog declares: it is not `decision_unit`,
because the image is genuinely an input, and it is not adequately served by
`live_image`, because which of two real branches a real image lands on is
decided by the machine underneath it. Two rows now show this shape. A third
would make it a category, and the answer would be to name the third type and
give it its own rule — not to move the budget a fifth time.

### The same toss was also hiding in a hand-written test

Running the Docker matrix after the classification work turned up a second
failure of the same kind, in a test the classification could not have covered.
`test_configless_one_line_cli_and_required_skip_gate` exercises the zero-config
one-liner — `test IMAGE --port PORT --ready-url PATH` — and asserted, among a
dozen host-independent facts about the CLI, that SP005 came back INCONCLUSIVE
with the fallback's explanatory summary. That assertion is the removed
`readiness-fallback-fast` row written a second time, in Python instead of YAML.

Three clean-directory runs of that exact command today measured ratios of 2.85,
4.07 and 6.61 — all declining. The matrix run reached `all_completed`, which the
fallback path can only reach above 10. The test failed on the run that resolved,
which is the one outcome it had no way to express.

`tests/test_coverage.py` did not and could not see this. Its gate reads
`fixtures/matrix.yaml`, so a branch that is classified `decision_unit` is kept
out of the matrix — but nothing stops a hand-written live test from asserting
the same decision through a real container. The classification closed the front
door.

The test now derives its expectations from the ratio the same invocation
recorded, and asserts both outcomes: declining publishes the summary and blocks
under `--fail-on error`; resolving publishes `all_completed` with a ratio that
permits it and does not block. What it no longer does is require a particular
side. The wording of the declining summary stays where it can be tested with
known numbers, in
`tests/test_preconditions.py::test_readiness_fallback_below_jitter_resolution_is_inconclusive`.

A gate that would have caught this is not obvious — it would have to notice that
a live test's assertion depends on a `decision_unit` outcome, which is a
statement about intent rather than about text. Recorded here as the second place
this particular toss was hiding, and the third fixture in this file whose
stability turned out to be a property of the machine rather than of the image.

### What is now recorded on every run

The open question above cannot be closed from this laptop, and the data that
would close it was being measured and discarded on every run. It is now kept.
Each run in the JSON report carries a `resolution_calibration` block next to its
`phase_durations_ms`:

- `host_id` — OS and release, Docker server version, CPU count. The grouping
  key. Load average is deliberately not in it, because it describes the run
  rather than the machine, and is recorded beside the numbers it explains.
- `measurement_jitter_ms`, with `measurement_jitter_source` and the sample
  count. The source matters: sidecar jitter times TCP round trips to the target
  and host-fallback jitter times the Docker daemon, so pooling the two would
  read a change of instrument as a change of host.
- `readiness_p50_ms`, `readiness_max_ms`, `readiness_samples`.
- `ratio` — readiness p50 over jitter — and `minimum_ratio`, the value of
  `MIN_JITTER_RATIO` in force when the row was written.
- `inflight_target`, because the block is written on both paths. A run that
  takes SP005's own advice and points `--inflight-path` at a slower endpoint
  still measures the readiness baseline, and its ratio is the same evidence
  about the same host. Recording it only on the fallback path would have left
  the question open on exactly the hosts best placed to close it.

The teardown floor is the other half and was already per-run in
`teardown_calibration`: samples, median, sample stddev, and the threshold.

The three runs in the table above are the first readings taken this way, and
they make the case for taking them unconditionally: that fixture sets
`contracts.inflight: null`, so SP005 never ran and no verdict depended on the
ratio — and the ratio still landed on both sides of the constant, on one
machine, minutes apart. 4.91, then 16.34, then 4.20, against a required 10. The
readiness p50 barely moved across them (4.75-6.18ms); the jitter floor moved
3.4-fold (0.37-1.26ms). The unstable term is the one that belongs to the host,
and three runs that were not about SP005 at all measured it anyway.

There is already one measured cross-host figure of the same kind in this file:
the teardown floor was 237-250ms on `Linux 6.8.0-134-generic` and 50.73ms on
this macOS host, roughly fivefold. A constant chosen from either one alone would
be calibrated to that one. `MIN_JITTER_RATIO` is unchanged and stays unchanged
until the same block exists for a Linux server, a macOS laptop and a CI runner.

## What the fallback ratio tracks, measured four ways (2026-08-26; preflightkit commit: a738ee65314ae27e399fe70a94484971a11e5eab)

Thirty-two predictions of the same zero-config command on one macOS host,
`Darwin 25.5.0 / docker 29.7.2 / 11cpu`, run with `scripts/measure-runs.sh` in
four batches of eight. The command is the one whose SP005 verdict has been
flipping all week:

    preflightkit test pfk-fixture-good --port 8000 --ready-url /ready --format json

This was set up to test a specific claim: that the ratio is driven by ambient
load, that a busy machine separates readiness from jitter better than an idle
one, and that the fallback path therefore declines most often on an empty
laptop — the first thing a new user does. Three of the four batches were chosen
to confirm or break that. It broke.

### The four conditions

| batch | machine state | jitter med | p50 med | ratio med | ratio range | cleared 10 |
|---|---|---|---|---|---|---|
| `ambient` | as found, 73% CPU idle | 0.847ms | 4.30ms | 4.68 | 1.22-16.08 | 1 of 8 |
| `cpu-loaded` | 16 spinners, ~0% idle | 1.155ms | 3.68ms | 3.59 | 1.64-14.75 | 1 of 8 |
| `docker-churn` | 4 concurrent container create/destroy loops, 0.8% idle | 0.312ms | 1.17ms | 3.62 | 1.99-7.41 | 0 of 8 |
| `warm-quiet` | churn run for 25s, then stopped; 60% idle | 0.145ms | 0.36ms | 2.52 | 2.08-3.20 | 0 of 8 |

Ambient load is not the variable. Saturating every core with work that never
touches Docker moved the median ratio from 4.68 to 3.59, which is nothing beside
a within-batch range of 1.22 to 16.08. Two runs out of thirty-two cleared the
constant, both on a jitter sample that happened to land low — 0.330ms and
0.241ms against batch medians of 0.847 and 1.155.

### The variable is how warm the Docker VM is, and it moves both terms together

The two batches that changed the numbers are the two that had the daemon busy
beforehand. Between `ambient` and `warm-quiet` the jitter floor fell 5.8-fold
and readiness p50 fell 11.9-fold. The duration table from the same runs says the
same thing from the other side: the `calibration` phase, which is where the
jitter floor is measured, took a median 640ms in the `ambient` batch and 91ms in
`warm-quiet` — seven times faster for identical work.

An idle Docker VM on macOS pays a wake-up on each probe. That cost lands on
whichever probe happens to catch it, which is why the cold batches are not just
slower but wildly noisier: `ambient` spans 13-fold, `warm-quiet` spans 1.5-fold.

Because the wake-up cost lands on both the jitter probe and the readiness probe,
the ratio between them is close to scale-invariant. That is what a ratio is for,
and it is the reassuring half of this result: `MIN_JITTER_RATIO` is not measuring
how busy the machine is.

### What it is measuring is the endpoint, and the answer does not improve

On the tightest, most repeatable batch — the one where every reading agrees with
every other reading to within 1.5-fold — this endpoint's ratio is 2.52. A
readiness endpoint that answers in 0.36ms is not distinguishable from a probe
floor of 0.145ms, and no amount of load makes it so. The tool declining here is
correct, and it is correct for a reason that has nothing to do with the host
being unusual.

The control makes the point without any argument. Two runs of the
`readiness-fallback-slow` fixture, same host, same afternoon, same image, same
`/ready` path. One environment variable differs — `READINESS_DELAY_SECONDS=0.2`
makes the handler sleep before answering:

| readiness handler | jitter | p50 | ratio |
|---|---|---|---|
| answers immediately (warm-quiet median) | 0.145ms | 0.36ms | 2.52 |
| sleeps 200ms first | 0.643ms | 205.90ms | 320.40 |

One env var moved the ratio 127-fold. Four machine states moved the median
1.9-fold. Whatever `MIN_JITTER_RATIO` is gating, it is not the host.

(The slow fixture also declares a longer grace period and startup budget, which
is why it is a fixture rather than a flag. Neither is an input to jitter or to
readiness p50.)

That reframes the open question. The reason to collect readings from a Linux
server and a CI runner is no longer "the constant might be calibrated to this
laptop". It is to find out whether the jitter *floor* differs enough across
hosts to move the threshold at all — because on this host, the thing being
compared against it is a property of the endpoint, and a lower coefficient would
not make a 2.5-to-1 separation real. It would only make the tool claim it.

`MIN_JITTER_RATIO` is unchanged.

### Two corrections to what was believed before this

**The clean-versus-matrix comparison was cold-versus-warm, not idle-versus-busy.**
The three clean-directory runs recorded in the previous section — 2.85, 4.07,
6.61 — were taken on a cold VM and sit inside the `ambient` batch's range. The
matrix run that reached `all_completed` came after thirty-nine other Docker
tests had been warming the daemon. Both observations are accounted for without
any appeal to load.

**The first-run experience is not reliably bad; it is unrepeatable, which is
worse.** The prediction was that a new user on an empty laptop gets the
INCONCLUSIVE. What the numbers say is that a new user on a cold machine gets
whichever of the two the jitter sample happens to produce — the `ambient` batch
contains both a 1.22 and a 16.08, eight runs apart, same command, same image,
same minute. A user who runs it twice can see it decline and then resolve, which
reads as the tool being unreliable rather than as the endpoint being too fast to
measure. The wording SP005 prints already gives the numbers and the fix; that is
the right response and it does not change here.

### `load_average` is provenance, not explanation, at least on macOS

Worth recording next to the `resolution_calibration` block that carries it. It
read 43.8 during `ambient` while the CPU was 73% idle, and 23-28 during
`docker-churn` while four container loops held the CPU at 99%. It rose when
sixteen spinners were added and then kept falling regardless of what the machine
was doing — 9-11 by `warm-quiet`, 3.6 fifteen minutes later — because it was decaying
from something that had run before any of this started. Short-lived container
processes never accumulate the run-queue depth it counts.

So across these four batches it tracked its own decay, not the pressure the runs
were under, and it cannot be used to order them. It stays in the record because
it costs nothing and may behave better on Linux, where the batches would want to
be re-read against it rather than assumed. The two figures that did track the
machine's state are the jitter floor and the `calibration` phase duration, both
already recorded per run.

### How these were taken

`scripts/measure-runs.sh -n 8 -l <label> -- pfk-fixture-good --port 8000
--ready-url /ready`, from an empty directory so that no `preflightkit.yaml` is
discovered and SP005 takes the readiness fallback. Every document is kept;
`scripts/summarise_runs.py` prints the duration, resolution and teardown blocks
with a median row. The host names itself out of `host_id`, so a batch cannot be
filed under the wrong machine. Configuration files for real services live
outside this repo and are passed with `-c`.

The harness is POSIX shell and standard-library Python, and its parsing is
covered by `tests/test_measurement_scripts.py` against documents built in the
test rather than measured — including a run that died before writing one, which
must not enter the medians as a fast run. It has not yet been executed on Linux.
That is the next thing to find out, and it is cheap to find out first: a crash
on the second host costs the trip, not the table.

Teardown was `not_calibrated` in all thirty-two runs: the profile's budget is far
from the floor, so nothing needed measuring. The cross-host teardown figure this
file already carries (237-250ms on Linux, 50.73ms here) still comes from the
runs that did calibrate.

## Three hosts, pipeline cost, and the fallback decision (2026-08-26; preflightkit commit: 74982e8ca26689947f3142a8bafa53fc91d1c842)

Eight runs of each batch on each of three conditions, taken with
`scripts/measure-set.sh` so that every condition ran the same set in the same
order: ten batches on macOS warm, ten on Linux CI, and six on macOS loaded,
where only `fallback` and the five-point sweep were repeated under load. 26
batches, **208 documents**, and because `repeat3` writes three runs into one
document, **240 runs**, 160 of which took the readiness fallback.

The per-batch tables are in `docs/measurements/`, one file per batch with the
host, the count, the median and the range; the index there lists every batch in
one table, these 26 together with the four later row-fixture batches taken after
the window floor landed. The raw JSON is not committed.

The question the previous note left open was whether the jitter floor differs
enough between Linux, macOS and CI to move the fallback threshold. It does: by
3.4x, and it moves the verdict on three of ten configurations.

### What these three are, and what is still missing

| condition | host_id | daemon | what it is |
|---|---|---|---|
| macOS warm | `Darwin 25.5.0 / docker 29.7.2 / 11cpu` | Docker Desktop, aarch64, in a VM | the laptop the constants were chosen on, idle |
| macOS loaded | same | same | the same laptop with `hw.ncpu` spinners pinning every core |
| Linux CI | `Linux 6.17.0-1022-azure / docker 28.0.4 / 2cpu` | native daemon, amd64, 7938 MB | GitHub `ubuntu-latest`, workflow run 32949002801, recorded in `docs/ci-runs.md` |

The third row is the control this file has wanted since the sidecar spike: a
native Linux daemon with no VM between the measurement and the kernel. It is not
the native Linux *server* the brief asked for, and the difference matters — two
cores and a shared cloud host is a weaker machine than a real server, so it
bounds the jitter floor from the pessimistic side rather than the realistic one.
The server leg and the two real service configurations were not run; see "What
this could not cover".

The second row is a machine *state*, not a fourth machine. It is here because
the instability that prompted this whole question turned out to live between
states rather than within a batch, and without it every disagreement figure in
this note would have read 0.00 and said nothing.

### The three-host table

Median of eight runs per cell. Jitter and p50 in ms.

| batch | readiness delay | macOS warm jitter / p50 / ratio | macOS loaded jitter / p50 / ratio | Linux CI jitter / p50 / ratio |
|---|---|---|---|---|
| `fallback` | none | 0.150 / 0.39 / 2.62 | 0.406 / 3.71 / 9.74 | 0.513 / 1.12 / 2.18 |
| `sweep-1ms` | 1ms | 0.146 / 1.68 / 11.64 | 0.749 / 5.77 / 11.59 | 0.502 / 2.15 / 4.32 |
| `sweep-2ms` | 2ms | 0.144 / 2.89 / 19.58 | 1.105 / 7.53 / 6.85 | 0.457 / 3.17 / 6.92 |
| `sweep-3ms` | 3ms | 0.137 / 4.06 / 28.81 | 0.417 / 7.72 / 25.52 | 0.503 / 4.15 / 8.29 |
| `sweep-5ms` | 5ms | 0.157 / 6.55 / 41.96 | 0.266 / 10.15 / 36.05 | 0.516 / 6.18 / 12.37 |
| `sweep-10ms` | 10ms | 0.142 / 11.58 / 81.59 | 0.264 / 16.01 / 60.70 | 0.508 / 11.30 / 22.32 |
| `slow` | 200ms | 0.560 / 205.02 / 366.86 | — | 0.518 / 201.16 / 388.20 |
| `fast` | none, SP005 disabled | 0.166 / 0.43 / 2.58 | — | 0.556 / 1.10 / 2.01 |
| `full` | none, in-flight configured | 0.147 / 0.36 / 2.45 | — | 0.508 / 1.09 / 2.20 |

The jitter floors, over every run of every batch:

| condition | n | min | median | max |
|---|---|---|---|---|
| macOS warm | 96 | 0.124 | 0.154 | 0.718 |
| macOS loaded | 48 | 0.223 | 0.370 | 2.798 |
| Linux CI | 96 | 0.378 | 0.516 | 0.635 |

Two things in that table were not expected. The native Linux daemon has a
*higher* jitter floor than the Docker VM — 0.516 against 0.154, a factor of 3.4
— so the VM is not the source of the noise the ratio divides by; core count is
the better explanation, two against eleven. And the Linux floor is the tightest
of the three: 0.378 to 0.635 across 96 runs, a range narrower than macOS
manages while idle. A weaker machine, measured more repeatably.

The consequence is the finding. `sweep-1ms`, `sweep-2ms` and `sweep-3ms` are the
same image with the same readiness delay, and the ratio rule resolves all three
on macOS and refuses all three on Linux. The resolution floor is below 1ms on the
laptop and between 3ms and 5ms on the runner.

### Pipeline cost

Total of `phase_durations_ms`, median of eight runs.

| batch | macOS warm | Linux CI | what dominates |
|---|---|---|---|
| `fast` | 1.88s | 2.91s | nothing; this is the floor |
| `fallback` | 2.17s | 3.48s | sidecar start |
| `slow` | 5.29s | 6.43s | baseline, 200ms readiness x 10 samples |
| `full` | 22.08s | 23.37s | baseline 15.1s, experiment 5.5s |
| `repeat3` (one document, 3 runs) | 5.47s | 8.73s | three sidecar starts |

`full` is the realistic Kubernetes profile: a 5s in-flight endpoint, a 5s preStop
and a 30s grace period. It is the worst case in the set and it costs 23.37s on
the slowest host measured. The spec allows +40s. There is nothing to cut.

The tool's own fixed cost — everything that is not waiting on a duration the
user configured — is 1.46s on macOS and 2.68s on CI:

| phase | macOS warm | Linux CI |
|---|---|---|
| `probe_image_preparation` (network + traffic sidecar) | 864ms | 1636ms |
| `calibration` | 86ms | 153ms |
| `target_start` | 293ms | 705ms |
| `teardown` | 215ms | 188ms |

`probe_image_preparation` is the single largest fixed item on both hosts and
doubles on the two-core runner. `teardown` is the one phase that is *faster* on
Linux, 183-192ms against 199-215ms, which is the native daemon showing.

The +40s limit is reached only when the configured in-flight endpoint runs longer
than about 11s: `total ~ F + 10R + 3D + P + S`, and with F = 2.7s and a 5s preStop
that puts D at 10.8s. +5 minutes is not reachable by any configuration a person
would write. The model was checked against the runs rather than assumed —
predicted baseline against measured, macOS: `full` 15004/15107, `slow` 2665/2800,
`sweep-3ms` 139/147, `fallback` 105/96.

Since the budget is not exceeded, the shortening options stay priced and
unimplemented. Recorded so the price is known if the profile ever grows:

1. Run the keep-alive proof concurrently with the 25 baseline samples. The proof
   is itself two sequential requests, so this takes the in-flight cost from 3D to
   2D, not to 1D: 22.08s to 17.1s on macOS, 23.37s to 18.3s on CI. Semantically
   free — nothing about the proof depends on running it alone.
2. Move the keep-alive proof to the readiness path: 3D becomes D + 2R, 22.08s to
   12.1s, 23.37s to 13.3s. Not free — a route that answers `Connection: close`
   on the in-flight path but not on `/ready` would stop being caught.
3. Reuse the network and sidecar across `--repeat`. Saves one
   `probe_image_preparation` per extra run: on macOS 5.47s to about 3.9s, on CI
   8.73s to about 5.5s, and the saving grows with N. Confirmed to be real —
   inside one `repeat3` document the three per-run figures were 776.6, 793.3 and
   790.9ms, so nothing is amortised today.
4. Not a candidate: SP002's ten sequential readiness probes. Sequential agreement
   is what SP002 measures; running them concurrently would delete the contract.

### The fallback decision, as a stability question

The question was moved from "what should the coefficient be" to "after how many
samples is the decision stable", which is the right move: the 1.22-16.08 spread
that prompted it is a spread of *single* readings, and no coefficient repairs a
reading that cannot be repeated.

Stability is measured as disagreement — the chance that two independent
applications of a rule to the same configuration on the same host reach opposite
answers, bootstrapped over 20,000 resamples with a fixed seed. There is no
ground truth here and inventing one would mean assuming the answer, so the
metric asks only whether the rule repeats itself. `scripts/analyse_resolution.py`
prints it. When these batches were taken, `current` and `ratio-1` were the same
rule and agreed exactly, which is how the script was checked against the tool.
They have since diverged: `current` now applies the absolute floor adopted
below, while the `ratio-k` and `p50-T` columns deliberately do not, because
those columns exist to compare candidate rules and folding a shipped constant
into a candidate would score it on a decision it did not make.

**The instability is not where it looked.** Within a single machine state the
current rule is already settled: disagreement 0.00 on all ten macOS-warm batches
and all ten Linux CI batches. Every non-zero figure in the whole dataset comes
from the loaded state:

| batch (loaded) | current, as measured (= ratio-1) | ratio-3 | ratio-5 | ratio-9 | p50>=5ms | p50>=10ms |
|---|---|---|---|---|---|---|
| `fallback` | 0.50 | 0.22 | 0.06 | 0.00 | 0.22 | 0.00 |
| `sweep-1ms` | 0.50 | 0.22 | 0.06 | 0.00 | 0.00 | 0.00 |
| `sweep-2ms` | 0.38 | 0.03 | 0.00 | 0.00 | 0.00 | 0.22 |
| `sweep-3ms` | 0.47 | 0.37 | 0.17 | 0.03 | 0.00 | 0.38 |
| `sweep-5ms` | 0.22 | 0.44 | 0.50 | 0.42 | 0.00 | 0.50 |
| `sweep-10ms` | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |

0.50 is a coin toss, and the current rule reaches it twice.

### Variant A: more samples, and the lower edge must clear the threshold

Resolve only if the *minimum* of k readings clears the ratio; if the readings
straddle it, INCONCLUSIVE.

It works where it was asked to. `fallback` and `sweep-1ms` go from a coin toss
to settled by k=9. But three things came out of the numbers that the design did
not anticipate:

**It buys stability by declining to answer.** Resolve rate on `sweep-5ms-loaded`
falls 0.88 → 0.67 → 0.51 → 0.30 as k goes 1 → 3 → 5 → 9. The disagreement that
disappears is not converted into a decision, it is converted into INCONCLUSIVE.
For the fallback path specifically, INCONCLUSIVE and "below" produce the same
user experience — SP005 does not run — so on the branch that matters this is a
strictly worse answer delivered more confidently.

**At the boundary it gets less stable before it gets more stable.** `sweep-5ms`
loaded goes 0.22 → 0.44 → 0.50 → 0.42. A minimum over k draws down as k grows,
so a configuration sitting just above the threshold is dragged across it by the
very samples meant to settle it. This is exactly the near-threshold case the
brief asked about, and it is the one case where more evidence makes the rule
worse.

**Priced as written, it is unaffordable.** k in the table means k *runs*, because
the ratio is a per-run figure. k=9 on the realistic profile is 9 x 23.37s = 3.5
minutes, against a +40s budget. That kills the variant in the form stated — but
not the idea, because the run already takes ten readiness samples
(`readiness_samples: 10`) and five jitter samples, and throws away everything but
p50 and max. A lower bound computed from the samples a single run already has
costs nothing in wall-clock and needs one field added to the report. That is a
different change from the one proposed, and it is the affordable shape of it.

### Variant B: an absolute ms floor on readiness p50

Drop the ratio, resolve if `readiness_p50 >= T`.

Its case is one number. Across the ten configurations run on both idle hosts,
the two hosts agree on **10 of 10** under `p50 >= 5ms` and on **7 of 10** under
the ratio. The three it fixes are `sweep-1ms`, `sweep-2ms` and `sweep-3ms` — the
same image getting opposite verdicts on two machines.

Calibration is not free-handed. On Linux CI the ratio resolves at p50 6.18ms and
refuses at 4.15ms, so any T in (4.15, 6.18] reproduces the most conservative
host's verdicts exactly on all ten configurations; 5ms is the middle of that
band. Against the transport floors it is 4.5x the worst *median* jitter observed
anywhere (1.105ms, loaded) and 1.8x the worst single jitter reading (2.798ms,
also loaded). Disagreement is 0.00 on 15 of the 16 batches; the exception is
`fallback-loaded` at 0.22.

Its case against is the other half of the same table. Across machine states on
one host, the ratio agrees 5/6 and the absolute threshold agrees **3/6**:

| batch | jitter warm → loaded | p50 warm → loaded | ratio rule | p50>=5ms rule |
|---|---|---|---|---|
| `fallback` | 0.150 → 0.406 | 0.39 → 3.71 | below/below | below/below |
| `sweep-1ms` | 0.146 → 0.749 | 1.68 → 5.77 | resolve/resolve | **below/resolve** |
| `sweep-2ms` | 0.144 → 1.105 | 2.89 → 7.53 | **resolve/below** | **below/resolve** |
| `sweep-3ms` | 0.137 → 0.417 | 4.06 → 7.72 | resolve/resolve | **below/resolve** |
| `sweep-5ms` | 0.157 → 0.266 | 6.55 → 10.15 | resolve/resolve | resolve/resolve |
| `sweep-10ms` | 0.142 → 0.264 | 11.58 → 16.01 | resolve/resolve | resolve/resolve |

Under load a 1ms readiness window measures 5.77ms, and the absolute rule reports
a resolvable window for a service whose behaviour did not change. The ratio does
not make that mistake, because load inflates the jitter probe and the readiness
probe together and the division cancels it — which is what the four-condition
note found and what the loaded column confirms on a third axis.

So the two rules do not fail in the same direction. The ratio's failure is that
its floor is a property of the machine, so the same service answers differently
on two machines. The absolute threshold's failure is that it reads the measuring
machine's load as the service's behaviour. Neither is a bug in the sense of
being wrong about what it measures; they measure different questions, and the
tool has been treating one as if it answered the other.

### Recommendation

Not implemented, and `MIN_JITTER_RATIO` is untouched at 10.

**Keep the ratio as the decision.** It is the only one of the two that is stable
under load, and load is the condition a CI runner is actually in. Every
disagreement figure above comes from the loaded state, so a rule that is fooled
by load is disqualified for the environment this tool runs in.

**Do not raise the coefficient.** The evidence against is direct rather than
argued: making the rule stricter made the near-threshold case *less* repeatable,
0.22 to 0.50 on `sweep-5ms-loaded`. Raising 10 to 20 moves the boundary, it does
not remove one, and the configurations that then sit on the new boundary will be
exactly as unstable as the ones on the old one.

**Add an absolute floor as a guard, not as the decision.** Refuse to resolve when
`readiness_p50` is below the floor regardless of ratio. That closes the one class
of error the ratio cannot see — the laptop resolving a 1.68ms window at ratio
11.64 while the runner calls the same configuration unresolvable — without
importing the absolute rule's load sensitivity, because a guard can only ever
turn a "yes" into a "no", and the load failure mode is a false yes. From the
band above, 5ms is the calibrated value; it changes no verdict on Linux CI and
removes three on macOS, all three of which Linux already refused.

Adopted at **3ms**, below that band rather than inside it, on the argument that
a floor taken from (4.15, 6.18] stops being a guard: for a service with fast
readiness it becomes the deciding input, which is the role the ratio is supposed
to hold. The guard's job is the pathological case — a ratio cleared by a quiet
probe path rather than by a wide window — and three measured hosts is a thin
basis for a constant, so it errs low. What that costs is one of the three
cross-host disagreements: at 3ms the guard overturns `sweep-1ms` and `sweep-2ms`
on macOS, and `sweep-3ms` at p50 4.06ms stays above any floor in the 3-4ms range
and keeps disagreeing with Linux, which refuses it at ratio 8.29. Cross-host
agreement goes 7/10 to 9/10, not to 10/10. The tenth is not reachable by a guard
placed below the band, and reaching it was not worth making the guard the rule.
`MIN_JITTER_RATIO` unchanged at 10. Implemented as
`MIN_READINESS_WINDOW_MS = 3.0` in `src/preflightkit/contracts/inflight.py`,
where the ratio and the floor are one function that all three call sites share.

Replayed against all 208 documents, the guard moves **14 of 160** fallback runs,
every one of them resolve to refuse and none the other way — 8 of 8 on macOS
`sweep-1ms` and 6 of 8 on macOS `sweep-2ms`. Nothing else in the campaign is
touched.

That 6-of-8 is the cost, and it should be recorded next to the benefit rather
than under it. `sweep-2ms` has a readiness p50 of 2.89ms, which puts it on the
new boundary the way `sweep-5ms-loaded` sat on the old one: its disagreement
under the shipped rule goes from 0.00 to **0.38**, a configuration that used to
answer the same way every time and now does not. The guard did not remove the
boundary problem, it moved the boundary — which is exactly the objection raised
above against raising `MIN_JITTER_RATIO`, and it applies here with equal force.

What makes the trade worth taking anyway is where the boundary now sits. At 10x
the boundary tracked the *host*: `sweep-1ms` was a stable resolve on one machine
and a stable refusal on another, and no amount of repetition on either machine
would have revealed the disagreement. At 3ms the boundary tracks the *service*:
a service whose readiness p50 is near 3ms is unstable everywhere, identically,
and repeating the run shows it. An unstable answer that repeats its instability
is one a user can act on; a stable answer that is stable for the wrong reason is
not. Neither is free, and this note is not claiming the second problem was
solved.

**Take the cheap half of variant A, not the stated one.** Record the readiness
samples the run already collects, or a lower quantile of them, in
`resolution_calibration`. Then a lower bound is available for free and the
INCONCLUSIVE branch can be defended from within a single run. Nine runs cannot.

Adopted. `resolution_calibration` now carries `readiness_latencies_ms` and
`measurement_jitter_latencies_ms` — every sample, not a summary. The jitter
samples matter more than the readiness ones: across these 240 runs the ratio's
volatility was almost entirely in its denominator, 0.14 against 0.01 on the
200ms fixture, so recording only the numerator would have widened the block
without making the question answerable. No decision reads them yet; they exist
so that the next revision of this rule can be argued from readings that are
already in hand rather than from a fresh campaign.

**Report the floor, whatever is decided.** The most useful output of this whole
exercise for a user is not a verdict but the sentence "this host cannot resolve
windows below about 4ms" — which macOS and Linux CI answer differently by a
factor of four, and which the run already has every number it needs to say.

### What this could not cover

The native Linux *server* leg was not run. The brief named no host and the SSH
configuration on this machine lists nine, several of them production. Starting
container workloads on an unnamed production server is not a call to make by
inference, so the CI runner stands in as the native-Linux control and the server
leg is still open. The `service-a` and `service-b` configurations were to be
supplied separately and did not arrive, so both real-service legs are also open
— which matters, because every configuration in this note is a fixture whose
readiness delay was chosen to bracket the threshold, and a real service lands
where it lands.

### Teardown calibration did not run once, and why

`teardown_calibration` was null in **all 240 runs on all three conditions**.
Not sparse, not noisy — absent. Anyone reading this file later should not go
looking for cross-host teardown-floor numbers in these batches, because there
are none, and the `teardown` *phase* durations in the cost table above are not
them: that is the phase's own wall-clock, a different quantity measured for a
different reason, and reading 183-215ms as a daemon characteristic would be
reading a stopwatch on a code path.

The reason is not bad luck. Calibration is gated on
`TEARDOWN_CALIBRATION_MAX_BUDGET_MS = 2_000`: a run measures the floor only when
the configured shutdown budget is 2s or under, on the argument that a budget far
from the floor can be judged without it. That is sound laziness and is not being
questioned here. What it means in practice is that the gate is closed for every
configuration in this set — the four hand-written batches derive budgets of
25000, 30000, 30000 and 25000ms, and the five generated sweep configs are all
30000ms. Not one of the ten comes within an order of magnitude of the cutoff, so
the gate was never once open across 240 runs.

The set is at fault here, not the tool. Three fixtures in `fixtures/matrix.yaml`
*do* qualify — `stdlib-http/django-shipped.yaml` at 1000ms, and
`ignores-sigterm/preflightkit.yaml` and `stdlib-http/baseline-500.yaml` at
2000ms — and the Docker matrix runs all three. The floor is therefore measurable
and is being measured; it is simply not measured by anything in
`scripts/measure-set.sh`, which was built around the resolution question and
inherited profiles chosen for it. A cross-host teardown campaign needs one of
those three added to the set. That is a one-line change and was not made here,
because the brief asked for readings and changing the harness mid-campaign would
have made the ten batches incomparable with the ones already taken.

There is a second-order consequence worth stating. Unlike the jitter ratio, the
teardown stddev multiplier cannot have its evidence accumulate as a side effect
of ordinary use: `resolution_calibration` is written on every run, whereas the
block that would justify the teardown constant is null on any run with a
realistic grace period — which is every run a person would actually make. The
fix that worked for the ratio, write the readings down on every run, does not
transfer. Whatever defends that constant will have to be a deliberate campaign
against fixtures chosen for it.

The cross-host teardown-floor figures this file carries elsewhere — 237-250ms on
Linux, 50.73ms on macOS — still come from the earlier runs that did calibrate,
and remain the only measurements of it that exist.

### How these were taken

`scripts/measure-set.sh -n 8`, which runs the same ten batches in the same order
on any host: `fast`, `full`, `fallback`, `slow`, `repeat3`, and a five-point
sweep at 1, 2, 3, 5 and 10ms of readiness delay. The sweep range was chosen after
a smoke probe at 10ms returned a ratio of 72.6 on macOS, which showed the upper
half of the range only ever gives one answer.

The `fallback` batch runs from an empty directory. This is not tidiness: an
earlier ad-hoc batch labelled "fallback" was found to have
`inflight_target: configured` in its documents, because the repository root holds
a sample configuration with a configured in-flight path and
`DEFAULT_CONFIG_NAMES` discovers it from the working directory. The batch had
measured the wrong branch.

`scripts/build_fixture_images.py` reads the image list out of
`fixtures/matrix.yaml` rather than a hand-written copy, so a host can reach the
starting line without the five-minute Docker matrix.
`.github/workflows/measure.yml` is `workflow_dispatch` only and uploads
`measurements/` as an artifact; the raw documents stay out of the repository,
and the per-batch summaries drawn from them are committed under
`docs/measurements/`. `scripts/analyse_resolution.py` is standard
library only and repeats `CURRENT_RATIO = 10.0` deliberately rather than
importing it, so that re-running the analysis after a threshold change does not
silently re-score old batches under the new one.

## More jitter samples, and two rows on the default path (2026-08-26; preflightkit commit: 22ed2e768075ba553481e43aa293a242532ba3f9)

Two questions left over from the fallback rule landing. The first was raised from
the numbers in the section above: the ratio's denominator is measured from five
samples and the numerator from ten, and the denominator is the volatile one, so
raise the sample count and the decision should settle without touching either
threshold. It does not, and the reason is worth writing down. The second is
coverage: the default path had one live fixture, and it was on the resolve side.

### The samples the corpus does not contain

The documents record `measurement_jitter_ms` and `measurement_jitter_samples: 5`.
They do not record the five samples — that field landed in the same commit as the
window floor, so every document taken before it has the median and nothing under
it. The estimator cannot be studied from readings that already collapsed it.

What can be studied is the estimator itself. `TrafficProbe.jitter` was pointed at
201 samples instead of 5 and the ordinary prediction run twelve times warm and
twelve times under `hw.ncpu` spinners, so each run yields a pool of 201
consecutive connects taken by the same code at the same point in the lifecycle.
Sub-sampling a pool is then a model of "what if this run had taken n samples"
that assumes nothing about the distribution's shape.

Spread of median-of-n around the run's own centre, within one run — pure
sampling noise, drift excluded by construction:

| condition | n=5 | n=10 | n=15 | n=20 | n=30 |
| --- | --- | --- | --- | --- | --- |
| warm | 0.049 | 0.032 | 0.027 | 0.022 | 0.017 |
| loaded | 0.661 | 0.381 | 0.293 | 0.236 | 0.187 |

The estimator improves exactly as it should. Under load, twenty samples give a
median 2.8x tighter than five. The premise was right.

### The decision does not move

Run-to-run variation in the recorded jitter is that estimator noise plus real
drift in the host between runs. On a log scale the two add, so subtracting the
measured estimator variance from the observed variance leaves the drift, and a
simulation that draws a drift and then an estimator error reproduces the observed
spread at n=5 by construction. That reproduction is the calibration check: the
n=5 column below has to agree with the readings as taken, and it does — 0.12
against 0.12 averaged over the twenty fallback batches.

Chance two runs of one configuration answer differently:

| batch | observed | n=5 | n=15 | n=20 | n=30 |
| --- | --- | --- | --- | --- | --- |
| every Linux CI batch | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| macOS `sweep-2ms` | 0.38 | 0.38 | 0.37 | 0.37 | 0.38 |
| every other macOS warm batch | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| `fallback-loaded` | 0.50 | 0.44 | 0.44 | 0.45 | 0.45 |
| `sweep-1ms-loaded` | 0.50 | 0.49 | 0.49 | 0.49 | 0.49 |
| `sweep-2ms-loaded` | 0.38 | 0.48 | 0.49 | 0.49 | 0.49 |
| `sweep-3ms-loaded` | 0.47 | 0.44 | 0.42 | 0.42 | 0.42 |
| `sweep-5ms-loaded` | 0.22 | 0.08 | 0.02 | 0.02 | 0.01 |
| mean over 20 batches | 0.12 | 0.12 | 0.11 | 0.11 | 0.11 |

One batch out of twenty improves.

Asked as a count rather than as a rate, over every fallback run in the corpus —
160 of them, across both hosts and both load conditions, the batches listed in
`docs/measurements/README.md`. Each recorded run is
re-drawn through the estimator's own sampling distribution at each n, and the
draw is compared against the answer that run actually gave:

| jitter samples | runs expected to answer differently | of | share |
| --- | --- | --- | --- |
| 5 | 2.9 | 160 | 1.8% |
| 10 | 1.9 | 160 | 1.2% |
| 15 | 1.5 | 160 | 0.9% |
| 20 | 1.1 | 160 | 0.7% |
| 30 | 0.9 | 160 | 0.6% |

Quadrupling the sample count buys 1.8 runs out of 160. The floor is not zero and
never becomes zero, because 57 of the 160 sit within 5x to 20x of the ratio gate
and most of them are held there by where the service is, not by how the jitter
was estimated.

Cross-host agreement — a macOS run and a Linux CI run of the same configuration
reaching the same answer — does not move at all:

| batch | n=5 | n=10 | n=15 | n=20 | n=30 |
| --- | --- | --- | --- | --- | --- |
| `fallback`, `slow`, `sweep-1ms`, `sweep-5ms`, `sweep-10ms` | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| `sweep-2ms` | 0.77 | 0.75 | 0.75 | 0.75 | 0.76 |
| `sweep-3ms` | 0.01 | 0.00 | 0.00 | 0.00 | 0.00 |
| mean over 7 batches | 0.82 | 0.82 | 0.82 | 0.82 | 0.82 |

`sweep-3ms` is the clearest case in the whole study. The two hosts disagree about
it essentially always, at every sample count, and more samples make it slightly
worse rather than better. The hosts disagree because they are different: macOS
measures p50 4.14ms at 0.50ms of jitter, a ratio of 8.3 that the gate refuses,
while the Linux runner's own p50 for the same image clears it. Sharpening both
measurements sharpens the disagreement. That is the host dependence the window
floor documents, and no amount of sampling is going to remove it, because it is
not noise.

Splitting the variance of `log(ratio)` by source says the same thing from the
other end:

| | share of total |
| --- | --- |
| readiness p50 | 10% |
| jitter, host drift between runs | 62% |
| jitter, estimating a median from five samples | 28% |

28% is not nothing, and in the quiet batches it is often the majority — 78%,
80%, 100% on the Linux runner. But those are the batches whose verdict is
already settled at n=5, so there is nothing there to win. Where the verdict is
unsettled the reachable share is 19% to 24%, swamped by drift, and halving it
moves the disagreement by about 0.01.

The two unsettled cases are unsettled for reasons a sample count cannot touch:

- macOS `sweep-2ms` sits at p50 2.89ms against the 3ms floor. Some runs land
  above it, some below. The jitter measurement is not an input to that.
- The loaded batches move because the load itself moves between runs. Pool
  medians across twelve loaded runs ran 0.208ms to 0.761ms — a 3.7x span in the
  quantity being estimated, not in the estimate of it. The readiness p50 moves
  with it (cv 0.13 to 0.34), so the numerator is unstable too.

Cost, measured on the same pools — the sum of the draws, against the run they sit
in:

| condition | n=5 | n=20 | extra | run total |
| --- | --- | --- | --- | --- |
| warm | 0.59ms | 2.35ms | +1.76ms | 2153ms |
| loaded | 2.97ms | 11.90ms | +8.92ms | 4171ms |

Under a tenth of a percent of the run. The change is affordable and it is not
worth making: it buys a better number and the same decision. **Not implemented.**
The samples are recorded now, so the question can be reopened from readings
rather than from a re-run if the rule is ever revisited.

What this does not settle: every reading here is from two hosts, and the loaded
condition is eleven spinners on eleven cores, which is a deliberate pathology
rather than a busy machine. A host whose noise is genuinely heavy-tailed but
stable between runs — the shape a shared cloud runner might have — would put a
larger share in the estimator term, and is the case that would change the answer.

### The default path had one fixture and it was on the easy side

`readiness-fallback-slow` covers the resolve side at 200ms. The refusal side had
no live row at all: it was reclassified `decision_unit` after the fixture
covering it was found to be rolling for its verdict rather than proving it, at
1.9x to 15.3x across six runs on one machine.

That classification was correct for the rule as it then stood. The ratio compares
the service against the host, so the image was not what decided the branch —
a quieter machine raises the ratio with nothing about the service changing.

The window floor removes that. It is not a comparison against the host, so no
amount of quiet moves it. The branch went back to live-image proof,
`REVIEWED_DECISION_UNIT` lost its SP005 entry, and `readiness-fallback-below-ratio`
is the row.

Both rows were then measured properly rather than argued for — eight runs on each
of the two hosts, which is the same batch size the rest of this document uses, and
not the single CI pass that a matrix run gives.

`readiness-fallback-below-ratio`, the refusal. The verdict holds if *either*
clause refuses, so its margin is the larger of the two:

| host | readiness p50 | floor margin | ratio | ratio margin |
| --- | --- | --- | --- | --- |
| macOS | 0.33–0.79ms | **3.8x** | 1.76–4.09 | 2.4x |
| Linux CI | 1.01–1.19ms | 2.5x | 2.08–2.41 | **4.2x** |

The bolded clause is a different one on each host, and that is the whole
argument for the row. macOS has a quiet probe path, so the ratio climbs and the
absolute floor is what holds the verdict down. The Linux runner has 3x the
jitter, so the ratio stays low while its readiness p50 creeps to within 2.5x of
the floor. Either clause alone would be uncomfortable on one of the two
machines; the pair is 3.8x clear at worst. The old fixture for this branch had
only the ratio, and that is exactly why it was lost.

`readiness-fallback-25ms`, the resolve. This one needs *both* clauses to pass,
so its margin is the smaller:

| host | readiness p50 | floor margin | ratio | ratio margin |
| --- | --- | --- | --- | --- |
| macOS | 27.20–30.29ms | 9.1x | 118–251 | 11.8x |
| Linux CI | 26.45–26.82ms | 8.8x | 50–60 | **5.0x** |

5.0x is the number the row lives on. It is at 25ms rather than the 200ms of
`readiness-fallback-slow` because 200ms is far wider than a readiness probe
normally costs, and a resolve path proved only at that scale would look healthy
in the matrix and be useless in the field.

It was called `readiness-fallback-tight` when it was added, which reads as "near
the boundary" and is wrong by a factor of five. Named for the delay now, which
cannot go stale the way an adjective does.

### `cause: below_window` cannot be a live fixture, on arithmetic

The request was for a row pinned to the window clause: readiness p50 near 1ms,
three times under the floor, refused by the floor rather than by the ratio. It
does not exist. The cause is reported by whichever clause refuses first, and the
ratio is checked first, so reaching `below_window` needs the ratio to *clear*:

    p50 >= 10 * jitter   and   p50 < 3ms   ⟹   jitter < 0.3ms

The Linux runner measures 0.43–0.56ms. The interval is empty there. Every window
that clears 10x on that host is at least 5ms and therefore above the floor, so no
configuration of any image can produce `below_window` on it.

This is not a prediction. `sweep-1ms` is that configuration and it was run eight
times on each host: macOS reports `below_window` at p50 1.68ms and ratio 11.6,
the Linux runner reports `below_ratio` at p50 2.15ms and ratio 4.3. Same config,
same image, opposite cause. A row pinned to the cause would pass on the laptop
and fail in CI — which is the host dependence the guard exists to document, so
writing it into a fixture as an expectation would be backwards.

The branch is what the matrix asserts, and the branch is the same on both hosts.
`below_window` stays proved by `tests/test_preconditions.py`, which feeds the
decision function the two numbers directly and does not need a host that can
produce them.

### The reclassification had a third file in it, and only Docker found it

Moving the branch off `decision_unit` passed ruff and the whole non-Docker suite,
including the coverage gate that exists to police exactly this kind of edit. The
Docker matrix failed on `test_configless_one_line_cli_and_required_skip_gate`
with a bare `StopIteration`.

The cause is a helper in `tests/test_fixtures.py` that asked the catalog which
SP005 branch was classified `decision_unit` and used the answer as the name to
assert. The indirection was deliberate and its reasoning was sound while it held:
a test that starts a container may not name a `decision_unit` branch, because the
classification says the image is not what decides it, so the name had to come
from the registry rather than from the test. After the reclassification there was
no such branch to find, and `next()` on an empty generator raised.

Two things are worth keeping from this. The first is that the helper's premise
expired with the classification: the branch is registered by a matrix row now, so
naming it in a test is an ordinary claim and `test_no_test_names_a_branch_the_
registry_cannot_see` is what holds it. The literal name is back in the test with
that written next to it.

The second is where the gate does not reach. It checks that every claim is
registered and that the two classifications agree; it does not check that code
reading the classification still has something to read. That gap is only visible
from a run, and this one cost a five-minute matrix to find. It is narrow — one
helper, now gone — but the general shape is that the catalog is data other code
queries, and a query that returns nothing is not a coverage failure, so nothing
in the coverage file is looking for it.

## A flaky matrix row, and the accept queue it was racing (2026-08-26; preflightkit commit: e9ddd2b7dfb1046edd48548a217120c414dbf14c)

`in-app-readiness-never-changes` expects `SP004 WARN /
in_app_readiness_not_signaled`. On 2026-08-26 the Docker matrix reported `FAIL /
accept_then_reset — 1 connection(s) started after SIGTERM were reset without a
response (first connected at +2009ms)`. A rerun of the same commit was green,
which is the shape of a flake and says nothing about which side is wrong.

### The obvious hypothesis, and the number that killed it

The suspicion was that the fixture closed its window early: if the app stopped
holding the listener before the declared `in_app_window`, the reset would be a
real defect and SP004 should have failed on the window clause too, which would
make the matrix expectation wrong rather than the fixture.

It did not. The declared window is 1200ms. The listener is held for about 2000ms
on every run of both hosts, and the reset landed at +2015ms — 815ms *after* the
window closed. The window clause was right not to fire, and the app was never
early. Whatever produced the reset happened at the end of a window it had
already covered.

### n=8 on both hosts, before and after

H1 is a Darwin 25.5.0 laptop, 11 cpu, docker 29.7.2. H2 is a GitHub
`ubuntu-latest` runner, 2 cpu, docker 28.0.4.

| batch | n | WARN | FAIL | accept_window_ms min / median / max |
| --- | --- | --- | --- | --- |
| H1, before | 8 | 7 | 1 | 1968 / 2008 / 2034 |
| H2 alone, before | 8 | 8 | 0 | 1965 / 2019 / 2029 |
| H1, after | 8 | 8 | 0 | 2015 / 2029 / 2060 |
| H2 alone, after | 8 | 8 | 0 | 2010 / 2040 / 2171 |

The window itself does not move: eight numbers on each host, all within about
10% of 2000ms, on a machine with 11 cores and on one with 2. What moved was
whether a connection got caught, and the fourth reading is the one that names
the variable. H2 *alone* was 8/8 green before the fix, while H2 *inside the
Docker matrix* flipped one run in two on the same commit. It is load-dependent,
not host-dependent — which is why one host's clean batch was never evidence.

### The mechanism

Closing a listening socket resets every connection already handshook into its
accept queue. The fixture closed its listener from inside the reply it was
writing, so between the close and the probe's next `accept()` there was a window
the width of a scheduler slice — invisible on an idle machine, wide enough to
catch a connection on a loaded two-core runner.

SP004 is right to call that `accept_then_reset`: a connection the client already
believed it had, destroyed after T0, is exactly what the branch is for. What was
wrong is that this row is not the specimen for it. `accept-then-reset-in-app`
is, and it keeps the racing close on purpose.

### What the fix changes, and what it does not

The close is now ordered rather than raced. The listening socket is shut down
*before* the reply is written, while the accept probe is blocked waiting for
that very reply — and the probe is serial, so at that instant its accept queue
is provably empty. The connection being answered was accepted long before, so it
is unaffected.

Ordering it that way needs the probe told apart from the readiness watcher, and
after T0 both send `Connection: close` to the same readiness path. The
discriminator is that the watcher goes through `http.client`, which always adds
`Accept-Encoding: identity`, while the probe writes its request by hand and
sends only `Host` and `Connection`. Keying the close on `Connection: close`
alone is what let a *readiness sample* close the listener while an accept-probe
handshake sat in the queue.

The matrix expectation is unchanged, and deliberately so. Each SP004 in_app
branch has exactly one live row. Flipping this one to FAIL would have duplicated
`accept-then-reset-in-app` while leaving `in_app_readiness_not_signaled` with no
live coverage at all — and it would have made a row assert a verdict it produced
one time in eight.

### Two things this cost that were worth paying

The first is that `docs/ci-runs.md` could not answer "was this row green
before". It records one conclusion per job, and 34 rows share it. Per-row
records are now written by `tests/conftest.py` and uploaded as a CI artifact;
the reasoning is in that file and in `docs/ci-runs.md`.

The second is that SP004's in_app clauses were being asked in source order.
Under this fixture both `accept_then_reset` and `in_app_readiness_not_signaled`
held on the same run — a FAIL and a WARN disagreeing about the same shutdown —
and which one came out was decided by which `if` was written first. That order
is now declared as `IN_APP_PRECEDENCE`, printed by `explain SP004`, and pinned
by tests that fail if any neighbouring pair is swapped.

### How these were taken

`scripts/measure-runs.sh -c fixtures/drain-window/readiness-never.yaml -n 8` on
H1; the same batch on H2 through `.github/workflows/measure.yml` with
`standard_set=false`, which is what `-x` was added for. Before-readings on
`32b3936` and `840f3fa`, after-readings on `e9ddd2b` (H1) and `99ed5e5` (H2).
The H2 batches are Actions runs `32996171026` and `32998568732`.
