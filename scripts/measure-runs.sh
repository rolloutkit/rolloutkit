#!/usr/bin/env bash
#
# Run one prediction N times and put the readings in a table.
#
# Two constants in preflightkit are floors that describe the machine rather than
# the target: MIN_JITTER_RATIO, which decides whether a readiness window can be
# told apart from measurement noise, and the teardown stddev multiplier, which
# decides whether a shutdown budget can be told apart from the daemon's own
# overhead. Both were chosen on one macOS laptop. Neither can be defended from
# one machine, and the only way to find out is to run the same prediction on a
# Linux server, a macOS laptop and a CI runner and compare.
#
# Every run already measures those numbers and writes them into its JSON report
# under `resolution_calibration`, next to `phase_durations_ms`. This script does
# nothing clever: it runs the prediction N times, keeps every document, and
# prints the two blocks as a table with a median row, so the three hosts produce
# comparable output without anyone transcribing anything.
#
# The host names itself. `host_id` comes out of the report — OS and release,
# Docker server version, CPU count — so there is no flag to get wrong and no way
# for a batch to end up filed under the wrong machine. Load average is recorded
# per run rather than per batch, because it describes the run.
#
# Configuration files live outside this repo. Point at one with `-c`, or pass
# the image and its flags after `--`.
#
# Usage:
#   scripts/measure-runs.sh -c ../service-a/preflightkit.yaml -n 5
#   scripts/measure-runs.sh -n 5 -- pfk-fixture-good --port 8000 --ready-url /ready
#   scripts/measure-runs.sh -c ../service-b/preflightkit.yaml -l loaded -o /tmp/b
#
# Options:
#   -c FILE    config file, passed to `preflightkit test --config`
#   -n N       number of runs (default 5)
#   -o DIR     output directory (default measurements/<host>-<label>-<stamp>)
#   -l LABEL   free-text tag for the batch, e.g. idle / loaded / ci
#   -k         keep going after a failed run (default: stop)
#   -h         this help
#
# Anything after `--` is appended to the `preflightkit test` command line.
#
# Environment:
#   PFK        the command to invoke (default: `uv run preflightkit` inside a
#              checkout, `preflightkit` otherwise)

set -euo pipefail

usage() { sed -n '3,45p' "$0" | sed 's/^#\{1,2\} \{0,1\}//'; }

runs=5
config=""
outdir=""
label=""
keep_going=0

while getopts ":c:n:o:l:kh" opt; do
  case "$opt" in
    c) config=$OPTARG ;;
    n) runs=$OPTARG ;;
    o) outdir=$OPTARG ;;
    l) label=$OPTARG ;;
    k) keep_going=1 ;;
    h) usage; exit 0 ;;
    :) echo "measure-runs: -$OPTARG needs a value" >&2; exit 2 ;;
    \?) echo "measure-runs: unknown option -$OPTARG" >&2; exit 2 ;;
  esac
done
shift $((OPTIND - 1))
passthrough=("$@")

case "$runs" in
  ''|*[!0-9]*|0) echo "measure-runs: -n takes a positive integer, got '$runs'" >&2; exit 2 ;;
esac

if [ -n "$config" ] && [ ! -f "$config" ]; then
  echo "measure-runs: no such config: $config" >&2
  exit 2
fi

if [ -z "$config" ] && [ ${#passthrough[@]} -eq 0 ]; then
  echo "measure-runs: nothing to run — pass -c CONFIG, or the image after --" >&2
  exit 2
fi

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

if [ -n "${PFK:-}" ]; then
  # Deliberately word-split: PFK is a command line, not a single binary.
  # shellcheck disable=SC2206
  pfk=($PFK)
elif [ -f "$repo_root/pyproject.toml" ] && command -v uv >/dev/null 2>&1; then
  pfk=(uv run --project "$repo_root" preflightkit)
elif command -v preflightkit >/dev/null 2>&1; then
  pfk=(preflightkit)
else
  echo "measure-runs: no preflightkit on PATH and no uv to run it with; set PFK" >&2
  exit 2
fi

command -v python3 >/dev/null 2>&1 || { echo "measure-runs: python3 required" >&2; exit 2; }

summariser="$repo_root/scripts/summarise_runs.py"
# Checked before the batch, not after it. The two files travel together; finding
# out that only one of them was copied to the measurement host, after sitting
# through N runs, means running them again.
[ -f "$summariser" ] || {
  echo "measure-runs: missing $summariser — copy it alongside this script" >&2
  exit 2
}

now_ms() { python3 -c 'import time; print(int(time.time() * 1000))'; }
stamp=$(python3 -c 'import datetime; print(datetime.datetime.now().strftime("%Y%m%dT%H%M%S"))')

# Only for the directory name. The authoritative host identity is `host_id` in
# each document, which names the Docker server the measurement actually used.
host_slug=$(printf '%s-%s' "$(uname -s)" "$(uname -m)" | tr '[:upper:] /' '[:lower:]--')
if [ -z "$outdir" ]; then
  outdir="$repo_root/measurements/${host_slug}${label:+-$label}-$stamp"
fi
mkdir -p "$outdir"

cmd=("${pfk[@]}" test --format json)
[ -n "$config" ] && cmd+=(--config "$config")
cmd+=("${passthrough[@]+"${passthrough[@]}"}")

{
  printf 'command: %s\n' "$(printf '%q ' "${cmd[@]}")"
  printf 'runs: %s\nlabel: %s\nstarted: %s\nuname: %s\n' \
    "$runs" "${label:-none}" "$stamp" "$(uname -a)"
} > "$outdir/batch.txt"

echo "measure-runs: $runs run(s) -> $outdir" >&2
printf 'measure-runs: %s\n' "$(printf '%q ' "${cmd[@]}")" >&2

failed=0
for i in $(seq 1 "$runs"); do
  document="$outdir/run-$(printf '%02d' "$i").json"
  echo "measure-runs: run $i/$runs" >&2
  started=$(now_ms)
  status=0
  "${cmd[@]}" > "$document" 2> "$outdir/run-$(printf '%02d' "$i").stderr" || status=$?
  finished=$(now_ms)
  printf '%s\t%s\t%s\n' "$i" "$status" "$((finished - started))" >> "$outdir/wall.tsv"
  # Exit 1 is a contract verdict, not a tool failure: the document is complete
  # and its readings count. 2 and 3 mean no measurement was taken.
  if [ "$status" -gt 1 ]; then
    echo "measure-runs: run $i exited $status; see $outdir/run-$(printf '%02d' "$i").stderr" >&2
    failed=$((failed + 1))
    [ "$keep_going" -eq 1 ] || break
  fi
done

python3 "$summariser" "$outdir" | tee "$outdir/summary.txt"

# A batch that measured nothing is not a successful batch, and a caller that
# chains this into a comparison has to be able to tell. Exit 1 is the tool's own
# code for "ran, verdict was not clean", which is the same shape of answer.
if [ "$failed" -ne 0 ]; then
  echo "measure-runs: $failed of $runs run(s) took no measurement" >&2
  exit 1
fi
exit 0
