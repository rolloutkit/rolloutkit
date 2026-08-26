# README material

Raw material for a README that has not been written yet. Nothing here is prose
for a reader: these are captured outputs, kept verbatim, so that whoever writes
the README quotes a real run instead of typing a plausible-looking one.

Every `.txt` file starts with the command that produced it. All of them were
captured on 2026-08-26 from preflightkit 0.1.0, at `COLUMNS=100`, with stdout
and stderr merged in the order a terminal would show them — the five progress
lines come from stderr, the report from stdout.

| file | what it is |
| --- | --- |
| [`service-b-in-app.txt`](service-b-in-app.txt) | a real service judged under the `in_app` drain profile |
| [`service-b-prestop.txt`](service-b-prestop.txt) | the same image, same run shape, judged under `prestop` |
| [`zero-config.txt`](zero-config.txt) | the one-line form, no configuration file at all |
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
| SP004 drain-window | **FAIL** — `listener closed -217ms after T0, but must remain open for 5000ms` | **PASS** — `prestop_not_applicable`, the hook owns routing removal |
| SP005 inflight-completion | FAIL — 4/66 completed, 62 destroyed | FAIL — 4/54 completed, 50 destroyed |

SP004 is the row that moves, and the negative number is the point. The window
is measured from SIGTERM to the last connection the accept probe got accepted,
so -217ms says no new connection was accepted at any moment after the signal —
the last one predates it. Claiming `in_app` is claiming the application keeps
accepting for the full 5s window; this one accepted for none of it. Under
`prestop` the identical behaviour is not a defect and the contract does not
treat it as one, because the platform hook rather than the application is what
stops traffic arriving.

Read the -217ms as a verdict about the window, not as a stopwatch on gunicorn's
listener. The probe was already being refused before T0: 100 concurrent
requests against two sync workers fill the backlog, and a connection that
cannot be accepted looks the same to the probe whether the cause is saturation
or shutdown. Both of those are reasons a pod under `in_app` will drop traffic
it promised to serve, which is what the contract is asking about.

SP005 fails either way, and that is the finding worth putting in front of a
reader: no drain profile saves requests the process destroys on its way out.

## The zero-config run

`preflightkit test IMAGE --port PORT --ready-url PATH` against the Go graceful
fixture, from an empty directory. The empty directory is deliberate — the tool
discovers `preflightkit.yaml` from the working directory, so a run started in a
project root is not zero-config even when no `-c` is passed.

It shows what the one-line form does and does not get you. Five contracts are
measured and answered. SP004 warns rather than fails, because with no
configuration there is no drain claim to hold the image to. SP005 comes back
INCONCLUSIVE: with no `--inflight-path`, readiness is the fallback target, and
this fixture's readiness answers in 0.3ms against 0.2ms of probe-path jitter —
a ratio of 1.7x where 10x is required. The output names the ratio and the fix.

That is the honest shape of the zero-config path: it is a real measurement of
most of the lifecycle, and it tells you precisely which part it could not
resolve rather than guessing.

## Sanitization

The configurations here are rewritten from the originals, which live in the
service's own repository and are not ours to publish:

- the header comment no longer names the client project;
- the image is retagged `service-b:latest`. The original local build tag
  named the framework rather than the client, so this is for consistency
  with the field notes rather than for concealment;
- `env_file: .env` is replaced by a single inline `ALLOWED_HOSTS: "*"`. The
  original file holds the service's real secrets. Nothing else in it affects
  what is measured — the database engine defaults to sqlite, so the image runs
  standalone, and what is under measurement is gunicorn's shutdown path and PID
  1's signal disposition.

The route names are real and are left alone: `/healthz/` and `/api/subjects/`
identify nothing. The runs are real runs of the real image under these
configurations, not edited transcripts.
