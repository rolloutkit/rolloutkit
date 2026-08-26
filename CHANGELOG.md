# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

### Changed

- Missing probe and target images are pulled once with progress on stderr.
- `explain` documents every verdict branch by name, with SP004 grouped by the
  drain strategy that can reach it; the branch catalogue is now enforced against
  the contracts by test.
- Readiness is used as the SP005 fallback target when no in-flight path is set.
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
