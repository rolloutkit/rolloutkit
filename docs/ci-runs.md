# GitHub Actions runs, recorded before the history was deleted

`docs/field-notes.md` cites a workflow run by its ID, and every batch under
`docs/measurements/ci/` came out of one. Those citations point at a run history
that lives only in GitHub's database — it is not in the repository, no clone
carries it, and deleting the repository deletes it. This is the same failure as
the rewritten commit SHAs recorded in `docs/commit-map.md`: an argument resting
on an identifier whose registry can vanish. The fix is the same. Copy what the
identifier resolves to into the repository, while it still resolves.

Every run this repository ever produced is below, oldest first: 25 of them,
captured on 2026-08-26 immediately before the repository was deleted and
recreated. Nothing is filtered — the five early failures are part of the record.

**The commit column reads old → new.** Actions stores the SHA a run was
triggered on, and the rewrite of 2026-08-26 changed every SHA that existed
before it. The left value is what Actions recorded and no longer exists; the
right is the same tree in the current history, from `docs/commit-map.txt`. The
four runs marked *already post-rewrite* were triggered after the rewrite and
need no translation.

## Every run

| run | started (UTC) | workflow | event | result | commit | jobs |
| --- | --- | --- | --- | --- | --- | --- |
| `32820209254` | 2026-08-25 07:09 | CI | push | **failure** | `23b78d7234c8` → `80e0cb1875ba` | Linux lifecycle suite failure 11s |
| `32834931214` | 2026-08-25 10:00 | CI | push | **failure** | `768ce9fb565b` → `02d475c7078e` | Linux lifecycle suite failure 4m11s |
| `32836198746` | 2026-08-25 10:14 | CI | push | **failure** | `0e9626aeab42` → `16aad9d669c7` | Linux lifecycle suite failure 4m00s |
| `32836678859` | 2026-08-25 10:20 | CI | push | **failure** | `43290730f546` → `df61217e4d5d` | Linux lifecycle suite failure 4m05s |
| `32837004754` | 2026-08-25 10:23 | CI | push | **failure** | `a06c56f128e5` → `7dc87f10b46f` | Linux lifecycle suite failure 4m17s |
| `32837533489` | 2026-08-25 10:30 | CI | push | **success** | `a67c135261f1` → `f1a2113c88f3` | Linux lifecycle suite success 4m03s |
| `32876867613` | 2026-08-25 17:15 | CI | push | **success** | `2180131f2d15` → `a0b7b7564552` | Linux lifecycle suite success 6m50s |
| `32948024175` | 2026-08-26 08:29 | CI | push | **success** | `7b55cdba0bda` → `6256b24545ea` | Fast suite success 16s; Docker matrix success 8m06s |
| `32948629060` | 2026-08-26 08:36 | CI | push | **success** | `5aad8d6cded2` → `4c6f51b4443c` | Fast suite success 13s; Docker matrix success 6m58s |
| `32948639054` | 2026-08-26 08:36 | Measurement set | workflow_dispatch | **cancelled** | `5aad8d6cded2` → `4c6f51b4443c` | Measurement set cancelled 1m52s |
| `32948789008` | 2026-08-26 08:38 | Measurement set | workflow_dispatch | **cancelled** | `25594333c645` → `345b37fb2097` | Measurement set cancelled 2m36s |
| `32948788721` | 2026-08-26 08:38 | CI | push | **success** | `25594333c645` → `345b37fb2097` | Fast suite success 18s; Docker matrix success 6m53s |
| `32948999768` | 2026-08-26 08:40 | CI | push | **success** | `648891e92f9e` → `b855cefb9936` | Fast suite success 15s; Docker matrix success 7m07s |
| `32949002801` | 2026-08-26 08:40 | Measurement set | workflow_dispatch | **success** | `648891e92f9e` → `b855cefb9936` | Measurement set success 9m53s |
| `32950685024` | 2026-08-26 09:00 | CI | push | **success** | `31a8b1a37586` → `af2ccd3c3456` | Fast suite success 14s; Docker matrix success 7m09s |
| `32952910840` | 2026-08-26 09:25 | CI | push | **success** | `22ed2e768075` → `c48820cd6b7c` | Fast suite success 18s; Docker matrix success 8m36s |
| `32956292590` | 2026-08-26 10:03 | CI | push | **success** | `eb65b3227653` → `a1c5cb8f9794` | Fast suite success 18s; Docker matrix success 7m09s |
| `32957667502` | 2026-08-26 10:19 | CI | push | **success** | `db5059487837` → `d3f906877f1e` | Fast suite success 13s; Docker matrix success 7m05s |
| `32957680796` | 2026-08-26 10:19 | Measurement set | workflow_dispatch | **success** | `db5059487837` → `d3f906877f1e` | Measurement set success 11m22s |
| `32958520009` | 2026-08-26 10:29 | CI | push | **success** | `df5cc814ff32` → `51032e3f7878` | Fast suite success 20s; Docker matrix success 7m09s |
| `32959396429` | 2026-08-26 10:40 | CI | push | **success** | `a61795461e3d` → `56ddd5106986` | Fast suite success 12s; Docker matrix success 7m27s |
| `32964637483` | 2026-08-26 11:41 | CI | push | **success** | `4dde27cc8c15` (already post-rewrite) | Fast suite success 16s; Docker matrix success 8m12s |
| `32966282423` | 2026-08-26 12:00 | CI | push | **success** | `2dd2bb555cfd` (already post-rewrite) | Fast suite success 15s; Docker matrix success 7m04s |
| `32966487503` | 2026-08-26 12:03 | CI | push | **success** | `98630c03034c` (already post-rewrite) | Fast suite success 14s; Docker matrix success 7m05s |
| `32967504051` | 2026-08-26 12:14 | CI | push | **success** | `cc8d5d969623` (already post-rewrite) | Fast suite success 16s; Docker matrix success 8m28s |

## The two runs that produced the measurement corpus

`.github/workflows/measure.yml` is `workflow_dispatch` only. It ran the same
batch set `scripts/measure-set.sh` runs locally, on a GitHub `ubuntu-latest`
runner, and uploaded the JSON as an artifact. Two dispatches succeeded, and
between them they are the whole Linux leg of the campaign:

| run | tool commit | what it produced | where the summaries are |
| --- | --- | --- | --- |
| `32949002801` | `648891e92f9e` → `b855cefb9936` | the ten-batch set, ratio-only rule | `docs/measurements/ci/set-linux-x86_64/` |
| `32957680796` | `db5059487837` → `d3f906877f1e` | the two row fixtures, after the 3ms floor landed | `docs/measurements/ci/set-linux-x86_64-rows/` |

Two earlier dispatches, `32948639054` and `32948789008`, were cancelled before
they produced anything and are in the table above for completeness only.

The artifacts themselves expire on GitHub's own schedule and are not preserved
here — but they were never the evidence. The summaries drawn from them are in
`docs/measurements/`, and the host each batch ran on is recorded in the batch
file rather than inferred from the run that produced it. That was the point of
having the host name itself into the report.

## What is not preserved

Logs. A job's step-by-step output is not copied here, only its name,
conclusion and duration. The five failures of 2026-08-25 are recorded as
failures with the commit that caused each one; what they printed is gone. They
are five consecutive runs on 2026-08-25 working one Linux CI problem, and
`32837533489` is the first green run after them; the commits that got there are
in the history.

## What this file could not answer, and what now can

Everything above is recorded per job. `Docker matrix success 7m09s` is one bit
for 42 rows, and the question that actually comes up when a row goes red is not
answered by it: *was this row green before, and on what?* On 2026-08-26 the row
`in-app-readiness-never-changes` failed on `32b3936` and there was no way to
tell from this repository whether it had ever passed — the run before it was
recorded as green, which says only that no row failed, not that this row ran.

So the matrix now writes what each row observed. `tests/conftest.py` appends one
JSON Lines record per row — the expectation, the status, branch and summary of
every contract the run produced, the exit code, the duration, and the identity
of the run (commit, host, CPU count, Docker version, and the Actions run ID and
attempt). `.github/workflows/ci.yml` uploads it as `matrix-results-<run>-<attempt>`
with `if: always()`, which is the part that matters: the record worth keeping is
the one from the red run, and a step that only runs on success would drop it.

Each contract also carries `measured`: the numbers it judged, verbatim from the
report's `actual`. The verdict says which side of a boundary a row fell on and
never how far from it, and that distance is the whole question when a row goes
red on a slow host — a measurement 930ms from its FAIL line and one 4.9s from it
are the same branch and a different fixture. Two rows failed on 2026-09-03 and
neither margin was in the record: `slow-shutdown` had to be reconstructed from
the fixture's budget and a duration parsed out of an English sentence, and
`startup-within-resolution` could not be reconstructed at all, because the band
it is judged against is measured per run and published nowhere else. Contract
`evidence` is deliberately not kept — it holds per-request lists that grow with
the traffic a row drives, and one bounded line per row is what makes these
artifacts concatenable.

The file is one session's evidence — the run truncates it at session start — and
records are appended as rows finish, so a cancelled or timed-out job still
leaves the rows it got through. Artifacts are self-contained, so several can be
concatenated:

```
jq -r 'select(.row == "in-app-readiness-never-changes")
       | [.run.commit[0:12], .run.github_run_attempt, .outcome,
          .actual.SP004.status, .actual.SP004.branch] | @tsv' matrix-results*.jsonl
```

The same shape reads a margin across runs, which is the form a calibration
argument takes — how close the row has been running, on which host, and whether
the distance moved:

```
jq -r 'select(.row == "slow-shutdown")
       | [.run.commit[0:12], .run.os, .outcome,
          .actual.SP006.measured.shutdown_duration_ms,
          .actual.SP006.measured.margin_ms] | @tsv' matrix-results*.jsonl
```

Artifacts expire on GitHub's schedule (90 days here). What survives past that is
whatever gets written down, the same rule as everywhere else in this file.
