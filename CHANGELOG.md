# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `--env KEY=VALUE` (repeatable) and `--env-file PATH` on the configless
  one-line path. They join the existing precedence — CLI over environment over
  YAML over default — and land in `target.env`, so a value passed on the command
  line is redacted by the same rule as one written in the file.
- SP004 branch `accept_window_unmeasured` (INCONCLUSIVE) behind a new
  `accept_window_measured` precondition. `accept_window_ms` is a drain
  measurement only while the probe is still being accepted when the signal
  lands; when it is not, the run now declines to date a listener close it never
  saw, and publishes the raw negative value as evidence.
- `IN_APP_PRECEDENCE` on SP004: the order its in_app clauses are asked in is
  declared worst-first and tested, rather than being whatever order the `if`
  statements happened to be written in.

### Changed

- The terminal report prints contract notes whatever the verdict says. They were
  suppressed on PASS and SKIP, which hid the one thing a green SP004 row has to
  say: that connections were reset after its window had already closed.
- SP004 scopes `accept_then_reset` to the declared `in_app_window`, keyed on
  when the caller asked rather than when the handshake completed. A reset asked
  for after the window is reported as a count and a note, not a failure —
  measurement across 176 runs put the two populations at +11.8..+1018.5ms and
  +1255.1ms or later against a 1200ms window, with no overlap.

### Fixed

- SP004 reads its unmeasured mechanism from the attempts between the last
  accepted connection and T0, not from every attempt after it. Refusals from a
  process that has already exited described the exit, and were reporting a
  listener as gone before a signal it was still serving through.

## [0.1.0] - 2026-08-26

First release. `Changed` and `Fixed` below are relative to the `0.1.0.dev0`
working version, not to anything previously published.

### Added

- Zero-configuration `test IMAGE --port PORT --ready-url PATH` workflow.
- Static `explain` and `list-contracts` commands, Compose configuration import,
  JSON/JUnit reporting, and fixture branch-coverage enforcement.
- Run-scoped sidecar traffic measurement with explicit host fallback evidence.
- Per-run and aggregate `phase_durations_ms` timing evidence.
- Per-run `resolution_calibration` in the JSON report: host identifier, load
  average, probe-path jitter with the location it was measured at, readiness
  p50, and the ratio between them. What preflightkit can resolve is a property
  of the host, so choosing a threshold takes readings from several.
- `--version` output containing both package version and source commit.
- Phase-by-phase progress on stderr, numbered per run under `--repeat`.
- `scripts/measure-runs.sh`, which runs one prediction N times, keeps every
  JSON document and prints the phase durations, resolution calibration and
  teardown floor as a table with a median row. The host names itself out of the
  report, so batches from a Linux server, a macOS laptop and a CI runner are
  comparable without transcription. Configuration files are passed with `-c`.
- `scripts/measure-set.sh`, which runs the same batches in the same order on
  every host, `scripts/build_fixture_images.py`, which builds the fixture images
  from `fixtures/matrix.yaml` so a measurement host does not need the Docker
  matrix, and `scripts/analyse_resolution.py`, which reports how often two runs
  of one configuration reach opposite resolution verdicts. A
  `workflow_dispatch` workflow runs the set on a CI runner and uploads the
  documents.

### Changed

- Missing probe and target images are pulled once with progress on stderr.
- `explain` documents every verdict branch by name, with SP004 grouped by the
  drain strategy that can reach it; the branch catalogue is now enforced against
  the contracts by test.
- Readiness is used as the SP005 fallback target when no in-flight path is set.
- The SP005 readiness fallback now also requires an absolute window of at
  least 3ms, not only 10x the probe-path jitter. The two clauses are equals: a
  window can clear the ratio because the probe path was quiet rather than
  because the window was wide, and measurement showed the floor, not the
  ratio, is what refuses such a window on a quiet host. Measured across three
  conditions, the jitter floor moved 3.4x between hosts, and the same image at
  1ms and 2ms of readiness delay resolved on a macOS laptop while a native
  Linux runner refused it. The verdict branch is unchanged; the precondition
  evidence carries a `cause` naming which clause refused. `explain SP005` and
  the README state the host dependence.
- `resolution_calibration` records every readiness and jitter sample the run
  takes, not only p50 and max. The samples were already paid for and discarded,
  and without them a revision of the fallback rule needs a fresh measurement
  campaign rather than the readings already in hand.
- The SP005 readiness fallback refusal is proved by a live fixture again. It
  was reclassified `decision_unit` when a fixture for it was found to be rolling
  for its verdict — the ratio compares the service against the host, so a
  quieter machine moved the answer with nothing about the image changing. The
  absolute window floor is not a comparison against the host, so a readiness
  endpoint with no work behind it fails both clauses on every machine measured.
  Only one has to fail, and a different one is the robust clause on each host:
  the floor is 3.8x clear on macOS and the ratio 4.2x clear on the Linux runner,
  over eight runs each. `readiness-fallback-below-ratio` is that row, and
  `readiness-fallback-25ms` covers the resolve side at 25ms rather than only at
  the 200ms of `readiness-fallback-slow`, 5.0x clear of the tighter of its two
  clauses on the noisier host.
- Branch coverage is enforced against Python tests as well as against
  `fixtures/matrix.yaml`. A test that names a verdict branch has to have that
  branch registered — by a matrix row, or by being the proof the catalog names
  for a `decision_unit` branch. The matrix could previously be cleaned while a
  hand-written copy of the same claim stayed behind in a test, unseen.
- An `in_app` drain window the accept probe cannot resolve is now a
  configuration error (exit code 2) from both `validate` and `test`, reported
  before any container starts. It was previously a contract verdict, which meant
  waiting out a full run to be told the configuration was unmeasurable.

### Fixed

- In-flight requests are confirmed on the socket before SIGTERM is sent.
- Completion evidence includes both counts and `completion_rate`.

[Unreleased]: https://github.com/preflightkit/preflightkit/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/preflightkit/preflightkit/releases/tag/v0.1.0
