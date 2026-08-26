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
- `--version` output containing both package version and source commit.
- Phase-by-phase progress on stderr, numbered per run under `--repeat`.

### Changed

- Missing probe and target images are pulled once with progress on stderr.
- `explain` documents every verdict branch by name, with SP004 grouped by the
  drain strategy that can reach it; the branch catalogue is now enforced against
  the contracts by test.
- Readiness is used as the SP005 fallback target when no in-flight path is set.

### Fixed

- In-flight requests are confirmed on the socket before SIGTERM is sent.
- Completion evidence includes both counts and `completion_rate`.
