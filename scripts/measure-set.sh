#!/usr/bin/env bash
#
# Run the standard measurement set on one host.
#
# `measure-runs.sh` takes one batch. This takes the whole set, so that a Linux
# server, a macOS laptop and a CI runner are asked the same four questions in
# the same order and their tables line up without anyone reconciling flags.
#
# The four batches, and why each one is in the set:
#
#   fast      A config with no in-flight experiment and nothing to drain. This
#             is the floor: what the tool costs a pipeline when the target is
#             instant and every phase still runs.
#   full      A realistic Kubernetes profile — 5s preStop, a configured
#             in-flight path, a 30s grace period. Most of its wall clock is
#             waiting the user asked for, which is the distinction the pipeline
#             budget turns on.
#   fallback  The configless one-liner. This is the path whose resolution is in
#             question: with no in-flight path configured, SP005 falls back to
#             readiness, and whether that can be told apart from measurement
#             noise is what `MIN_JITTER_RATIO` decides. Readiness answers in
#             microseconds here, so the honest verdict is INCONCLUSIVE.
#   slow      The same fallback path against a readiness endpoint that sleeps
#             200ms. Same image, same route, one environment variable apart.
#             This is the half of the pair the rule is supposed to resolve, and
#             without it a batch only shows how often the rule says no — not
#             whether it says yes when it should.
#   repeat3   `fast` again with `--repeat 3`. Three predictions inside one
#             process separates the per-process cost (image preparation,
#             dependency resolution) from the per-prediction cost, which is the
#             only way to say what a second prediction would actually add.
#   sweep-*   The same fallback path at readiness delays of 1, 2, 3, 5 and
#             10ms. `fallback` and `slow` are the easy ends of the question and
#             both would come out the same under any threshold worth having.
#             What a threshold has to be judged on is the region where services
#             actually sit, and whether a service there gets the same answer
#             twice. These configurations are generated rather than committed:
#             they are one environment variable away from the fixture beside
#             them, and a checked-in copy is a second place for the image name
#             to go stale. The range is not arbitrary: measured jitter on
#             one macOS host ran from 0.145ms to 1.155ms, which puts the
#             ratio's own boundary anywhere between p50 1.5ms and 11.6ms.
#             A service in that band is decided by the machine.
#
# The fallback batch runs from an empty directory on purpose. `test` discovers
# `rolloutkit.yaml` from the working directory, and this repository has one at
# its root — a sample with a configured in-flight path. Run from the checkout,
# the batch meant to exercise the fallback would silently exercise the
# configured path instead and the rows would be mislabelled rather than wrong.
#
# Extra configurations join the set with `-c LABEL=PATH`, repeatable. Real
# service configurations live outside this repository, so they arrive that way
# rather than as fixtures.
#
# `-x` drops the standard set and runs only those extras. The set exists so the
# hosts answer the same questions in the same order; asking one new question on
# every host is the same need, and paying for ten unrelated batches to get one
# reading on a CI runner is how a host comparison stops being taken.
#
# Usage:
#   scripts/measure-set.sh
#   scripts/measure-set.sh -n 8 -c service-a=../a/rolloutkit.yaml -c service-b=../b/rolloutkit.yaml
#   scripts/measure-set.sh -x -n 8 -c readiness-never=fixtures/drain-window/readiness-never.yaml
#
# Options:
#   -n N            runs per batch (default 8)
#   -o DIR          output root (default measurements/set-<host>)
#   -c LABEL=PATH   an extra configuration to measure, repeatable
#   -x              run only the -c extras, not the standard set
#   -h              this help

set -u

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runs=8
outroot=""
extra_labels=()
extra_paths=()
extras_only=0

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while getopts ":n:o:c:xh" opt; do
  case "$opt" in
    n) runs="$OPTARG" ;;
    o) outroot="$OPTARG" ;;
    c)
      if [ "${OPTARG#*=}" = "$OPTARG" ]; then
        echo "measure-set: -c wants LABEL=PATH, got '$OPTARG'" >&2
        exit 2
      fi
      extra_labels+=("${OPTARG%%=*}")
      extra_paths+=("${OPTARG#*=}")
      ;;
    x) extras_only=1 ;;
    h) usage; exit 0 ;;
    :) echo "measure-set: -$OPTARG needs a value" >&2; exit 2 ;;
    \?) echo "measure-set: unknown option -$OPTARG" >&2; exit 2 ;;
  esac
done

case "$runs" in
  ''|*[!0-9]*) echo "measure-set: -n wants a positive integer, got '$runs'" >&2; exit 2 ;;
esac
[ "$runs" -gt 0 ] || { echo "measure-set: -n wants a positive integer, got '$runs'" >&2; exit 2; }

# Validate every extra config before the first container starts. The cost of
# this set is a trip to another machine, not the runtime.
for index in "${!extra_paths[@]}"; do
  path="${extra_paths[$index]}"
  [ -f "$path" ] || { echo "measure-set: no such config: $path" >&2; exit 2; }
  extra_paths[$index]="$(cd "$(dirname "$path")" && pwd)/$(basename "$path")"
done

if [ "$extras_only" -eq 1 ] && [ "${#extra_paths[@]}" -eq 0 ]; then
  echo "measure-set: -x runs only the -c extras, and none were given" >&2
  exit 2
fi

runner="$repo_root/scripts/measure-runs.sh"
[ -x "$runner" ] || { echo "measure-set: cannot execute $runner" >&2; exit 2; }

host_slug="$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)"
[ -n "$outroot" ] || outroot="$repo_root/measurements/set-$host_slug"
mkdir -p "$outroot" || exit 2
outroot="$(cd "$outroot" && pwd)"

echo "measure-set: $runs run(s) per batch -> $outroot"

failed=0
run_batch() {
  local label="$1" workdir="$2"; shift 2
  echo
  echo "======== batch: $label"
  ( cd "$workdir" && "$runner" -l "$label" -o "$outroot/$label" -n "$runs" "$@" )
  local status=$?
  [ "$status" -eq 0 ] || { echo "measure-set: batch '$label' reported failures" >&2; failed=$((failed + 1)); }
}

if [ "$extras_only" -eq 0 ]; then
  empty_dir="$outroot/.no-config"
  rm -rf "$empty_dir" && mkdir -p "$empty_dir" || exit 2

  run_batch fast     "$repo_root" -c "$repo_root/fixtures/stdlib-http/default-disposition.yaml"
  run_batch full     "$repo_root" -c "$repo_root/fixtures/good-fastapi-prestop/rolloutkit.yaml"
  run_batch fallback "$empty_dir" -- rk-fixture-good --port 8000 --ready-url /ready
  run_batch slow     "$repo_root" -c "$repo_root/fixtures/good-fastapi-prestop/readiness-fallback-slow.yaml"
  run_batch repeat3  "$repo_root" -c "$repo_root/fixtures/stdlib-http/default-disposition.yaml" -- --repeat 3

  # The boundary sweep. Written next to the readings they produce, so a batch
  # directory says which delay it was taken at without a lookup elsewhere.
  sweep_dir="$outroot/.sweep"
  rm -rf "$sweep_dir" && mkdir -p "$sweep_dir" || exit 2
  for ms in 1 2 3 5 10; do
    seconds="$(python3 -c "print($ms / 1000)")"
    cat > "$sweep_dir/$ms.yaml" <<YAML
version: 1
target:
  image: rk-fixture-good
  port: 8000
  env: {READINESS_DELAY_SECONDS: "$seconds"}
deployment:
  termination_grace_period: 30s
  drain: {strategy: none}
probes:
  readiness: {path: /ready, expected_status: 200}
contracts:
  startup: {budget: 15s}
YAML
    run_batch "sweep-${ms}ms" "$repo_root" -c "$sweep_dir/$ms.yaml"
  done

  rmdir "$empty_dir" 2>/dev/null
fi

for index in "${!extra_paths[@]}"; do
  run_batch "${extra_labels[$index]}" "$repo_root" -c "${extra_paths[$index]}"
done

echo
echo "measure-set: done -> $outroot"
[ "$failed" -eq 0 ] || { echo "measure-set: $failed batch(es) had runs that took no measurement" >&2; exit 1; }
exit 0
