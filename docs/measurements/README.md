# Measurement summaries

The tables `scripts/summarise_runs.py` prints, one file per batch, committed so
that the claims in `docs/field-notes.md` point at something a reader can open.

The raw JSON stays out of the repository. It is 240 documents across the 30
batches below — 272 runs, because `repeat3` writes three into one document — and
several megabytes of latency arrays. A reader who wants them can produce their
own: the command each batch ran is at the top of its summary.
What is committed is the part the argument rests on — which host, how many runs,
the median, the range, and what the tool decided.

Ranges matter more than medians here, which is why every batch is eight runs and
why a range is printed beside every median rather than a standard deviation. The
question is almost always whether a whole spread stays on one side of a
threshold, and a spread answers that where a summary statistic does not.

## Regenerating

    scripts/summarise_runs.py measurements/<set>/<batch> > docs/measurements/<set>/<batch>.txt

The checkout path is stripped from the recorded command line on the way out,
because these files are committed and a checkout path carries a username. The
batch file on disk keeps it. Nothing else is filtered.

## Hosts

- **H1** — `Darwin 25.5.0 / docker 29.7.2 / 11cpu`
- **H2** — `Linux 6.17.0-1022-azure / docker 28.0.4 / 2cpu`

## The rule each batch was measured under

The SP005 readiness fallback resolves when `readiness_p50 / jitter >= 10`. An
absolute clause, `readiness_p50 >= 3ms`, was added partway through this campaign.
Batches taken before it were decided by the ratio alone, so their recorded
verdict is not directly comparable with a later batch showing the same numbers —
the tool would answer differently today. The column says which rule was in force.

| batch | host | rule | n | wall s | p50 ms | jitter ms | ratio | SP005 as recorded |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| [`ci/set-linux-x86_64-rows/below-ratio`](ci/set-linux-x86_64-rows/below-ratio.txt) | H2 | ratio+floor | 8 | 3.64 (3.61–3.97) | 1.05 (1.01–1.19) | 0.478 (0.445–0.505) | 2.3 (2.1–2.4) | below_resolution |
| [`ci/set-linux-x86_64-rows/tight`](ci/set-linux-x86_64-rows/tight.txt) | H2 | ratio+floor | 8 | 3.99 (3.88–4.10) | 26.56 (26.45–26.82) | 0.489 (0.445–0.532) | 54.1 (49.9–59.7) | all_completed |
| [`ci/set-linux-x86_64/fallback`](ci/set-linux-x86_64/fallback.txt) | H2 | ratio only | 8 | 3.50 (3.40–3.64) | 1.12 (1.08–1.16) | 0.513 (0.486–0.557) | 2.2 (1.9–2.3) | below_resolution |
| [`ci/set-linux-x86_64/fast`](ci/set-linux-x86_64/fast.txt) | H2 | ratio only | 8 | 2.92 (2.86–3.07) | 1.10 (1.07–1.15) | 0.556 (0.492–0.619) | 2.0 (1.7–2.2) | SKIP disabled |
| [`ci/set-linux-x86_64/full`](ci/set-linux-x86_64/full.txt) | H2 | ratio only | 8 | 23.37 (23.32–23.45) | 1.09 (1.08–1.16) | 0.508 (0.445–0.573) | 2.2 (1.9–2.4) | all_completed |
| [`ci/set-linux-x86_64/repeat3`](ci/set-linux-x86_64/repeat3.txt) | H2 | ratio only | 8 | 8.74 (8.62–9.61) | 1.09 (1.04–1.17) | 0.540 (0.480–0.635) | 2.0 (1.7–2.3) | SKIP disabled |
| [`ci/set-linux-x86_64/slow`](ci/set-linux-x86_64/slow.txt) | H2 | ratio only | 8 | 6.45 (6.42–7.07) | 201.16 (201.08–201.48) | 0.518 (0.503–0.549) | 388.2 (366.6–400.2) | all_completed |
| [`ci/set-linux-x86_64/sweep-10ms`](ci/set-linux-x86_64/sweep-10ms.txt) | H2 | ratio only | 8 | 3.62 (3.54–3.70) | 11.30 (11.23–11.33) | 0.508 (0.425–0.534) | 22.3 (21.2–26.4) | all_completed |
| [`ci/set-linux-x86_64/sweep-1ms`](ci/set-linux-x86_64/sweep-1ms.txt) | H2 | ratio only | 8 | 3.54 (3.35–3.57) | 2.15 (2.12–2.28) | 0.502 (0.467–0.544) | 4.3 (3.9–4.6) | below_resolution |
| [`ci/set-linux-x86_64/sweep-2ms`](ci/set-linux-x86_64/sweep-2ms.txt) | H2 | ratio only | 8 | 3.50 (3.38–3.58) | 3.17 (3.15–3.23) | 0.457 (0.378–0.537) | 6.9 (5.9–8.4) | below_resolution |
| [`ci/set-linux-x86_64/sweep-3ms`](ci/set-linux-x86_64/sweep-3ms.txt) | H2 | ratio only | 8 | 3.55 (3.48–3.62) | 4.15 (4.13–4.21) | 0.503 (0.445–0.542) | 8.3 (7.6–9.3) | below_resolution |
| [`ci/set-linux-x86_64/sweep-5ms`](ci/set-linux-x86_64/sweep-5ms.txt) | H2 | ratio only | 8 | 3.52 (3.49–3.62) | 6.18 (6.16–6.85) | 0.516 (0.463–0.556) | 12.4 (11.1–13.4) | all_completed |
| [`set-darwin-arm64-loaded/fallback-loaded`](set-darwin-arm64-loaded/fallback-loaded.txt) | H1 | ratio only | 8 | 3.29 (2.89–3.53) | 3.71 (2.34–6.87) | 0.406 (0.252–1.915) | 9.7 (1.6–18.9) | all_completed ×4, below_resolution ×4 |
| [`set-darwin-arm64-loaded/sweep-10ms-loaded`](set-darwin-arm64-loaded/sweep-10ms-loaded.txt) | H1 | ratio only | 8 | 3.36 (2.95–3.65) | 16.01 (13.40–19.12) | 0.264 (0.238–0.774) | 60.7 (22.5–80.3) | all_completed |
| [`set-darwin-arm64-loaded/sweep-1ms-loaded`](set-darwin-arm64-loaded/sweep-1ms-loaded.txt) | H1 | ratio only | 8 | 3.46 (3.05–3.68) | 5.77 (5.10–7.25) | 0.749 (0.233–2.363) | 11.6 (2.2–25.3) | all_completed ×4, below_resolution ×4 |
| [`set-darwin-arm64-loaded/sweep-2ms-loaded`](set-darwin-arm64-loaded/sweep-2ms-loaded.txt) | H1 | ratio only | 8 | 3.48 (2.87–3.71) | 7.53 (5.73–11.21) | 1.105 (0.254–1.796) | 6.9 (5.4–24.1) | below_resolution ×6, all_completed ×2 |
| [`set-darwin-arm64-loaded/sweep-3ms-loaded`](set-darwin-arm64-loaded/sweep-3ms-loaded.txt) | H1 | ratio only | 8 | 3.61 (3.08–3.89) | 7.72 (6.73–12.79) | 0.417 (0.240–2.798) | 25.5 (3.3–33.6) | all_completed ×5, below_resolution ×3 |
| [`set-darwin-arm64-loaded/sweep-5ms-loaded`](set-darwin-arm64-loaded/sweep-5ms-loaded.txt) | H1 | ratio only | 8 | 3.27 (3.04–3.72) | 10.15 (7.84–13.02) | 0.266 (0.223–0.923) | 36.1 (9.7–56.6) | all_completed ×7, below_resolution ×1 |
| [`set-darwin-arm64-rows/below-ratio`](set-darwin-arm64-rows/below-ratio.txt) | H1 | ratio+floor | 8 | 2.18 (2.05–2.51) | 0.37 (0.33–0.79) | 0.157 (0.145–0.216) | 2.3 (1.8–4.1) | below_resolution |
| [`set-darwin-arm64-rows/tight`](set-darwin-arm64-rows/tight.txt) | H1 | ratio+floor | 8 | 2.57 (2.48–2.86) | 28.94 (27.20–30.29) | 0.154 (0.121–0.250) | 193.0 (118.1–250.6) | all_completed |
| [`set-darwin-arm64/fallback`](set-darwin-arm64/fallback.txt) | H1 | ratio only | 8 | 2.19 (2.05–2.41) | 0.39 (0.34–0.65) | 0.150 (0.142–0.184) | 2.6 (2.1–3.7) | below_resolution |
| [`set-darwin-arm64/fast`](set-darwin-arm64/fast.txt) | H1 | ratio only | 8 | 1.88 (1.80–2.44) | 0.43 (0.39–0.82) | 0.166 (0.154–0.193) | 2.6 (2.0–4.6) | SKIP disabled |
| [`set-darwin-arm64/full`](set-darwin-arm64/full.txt) | H1 | ratio only | 8 | 22.07 (22.01–22.30) | 0.36 (0.31–0.40) | 0.147 (0.133–0.159) | 2.4 (2.2–2.7) | all_completed |
| [`set-darwin-arm64/repeat3`](set-darwin-arm64/repeat3.txt) | H1 | ratio only | 8 | 5.48 (5.36–6.00) | 0.39 (0.36–0.82) | 0.160 (0.141–0.184) | 2.5 (2.1–5.8) | SKIP disabled |
| [`set-darwin-arm64/slow`](set-darwin-arm64/slow.txt) | H1 | ratio only | 8 | 5.26 (5.23–5.85) | 205.02 (204.34–207.25) | 0.560 (0.446–0.718) | 366.9 (287.9–463.1) | all_completed |
| [`set-darwin-arm64/sweep-10ms`](set-darwin-arm64/sweep-10ms.txt) | H1 | ratio only | 8 | 2.27 (2.21–2.38) | 11.58 (11.33–12.62) | 0.142 (0.124–0.163) | 81.6 (69.7–93.8) | all_completed |
| [`set-darwin-arm64/sweep-1ms`](set-darwin-arm64/sweep-1ms.txt) | H1 | ratio only | 8 | 2.15 (2.12–2.17) | 1.68 (1.64–1.79) | 0.146 (0.130–0.154) | 11.6 (11.0–12.9) | all_completed |
| [`set-darwin-arm64/sweep-2ms`](set-darwin-arm64/sweep-2ms.txt) | H1 | ratio only | 8 | 2.16 (2.10–2.18) | 2.89 (2.72–3.02) | 0.144 (0.137–0.168) | 19.6 (17.8–21.6) | all_completed |
| [`set-darwin-arm64/sweep-3ms`](set-darwin-arm64/sweep-3ms.txt) | H1 | ratio only | 8 | 2.18 (2.09–2.33) | 4.06 (3.75–4.50) | 0.137 (0.131–0.162) | 28.8 (26.5–31.5) | all_completed |
| [`set-darwin-arm64/sweep-5ms`](set-darwin-arm64/sweep-5ms.txt) | H1 | ratio only | 8 | 2.33 (2.24–2.40) | 6.55 (6.14–7.16) | 0.157 (0.134–0.185) | 42.0 (35.4–46.5) | all_completed |

240 runs in 30 batches, on 2 hosts.

`below_resolution` is `readiness_fallback_below_resolution`: the fallback refused
to name a window, so SP005 is INCONCLUSIVE. `all_completed` is the resolve side.
`SKIP disabled` is a batch that set `contracts.inflight` to null, so SP005 never
ran — the numbers in the row are still real, the verdict just is not SP005's.

A batch showing two verdicts crossed the threshold partway through. Those are the
interesting rows, not the broken ones: `set-darwin-arm64-loaded/*` is the machine
under load, and it is where the host, rather than the image, decides the answer.

## The commit each batch names

Every summary's `tool:` line carries the commit its harness was built from, and
all of them predate the 2026-08-26 history rewrite, so none resolves in this
repository any more:

| as printed | now | floor clause |
| --- | --- | --- |
| `648891e` | `b855cef` | no |
| `74982e8` | `31f40c8` | no |
| `db50594` | `d3f9068` | yes |
| `eb65b32` | `a1c5cb8` | yes |

`docs/commit-map.md` has the full table and says why the rewrite happened.
