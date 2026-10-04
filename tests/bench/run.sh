#!/usr/bin/env bash
# Benchmark runner for the Spotify -> bridge -> engine -> presence -> Discord pipeline.
#
#   tests/bench/run.sh                 # every benchmark, in order (about 12 minutes)
#   tests/bench/run.sh a_track_change  # one benchmark (name after "bench_")
#   BENCH_BRIDGE_JS=/path/to/other-bridge.js tests/bench/run.sh js_e2e   # measure another bridge
#
# What it does: builds the crate's test binary in a PRIVATE target dir and runs
# the #[ignore]d bench_* tests from src-tauri/src/bench_bridge.rs one at a time.
# Each prints a BENCH_RESULT {json} line; all of them are collected in
# $STATUSIFY_BENCH_OUT (default: tests/bench/results/<timestamp>.jsonl).
#
# Safe next to a live Statusify: no Tauri window, no port 8765, no Discord pipe
# `discord-ipc-N` (the fake Discord listens on \\.\pipe\statusify-bench-*), no
# DISCORD_APP_ID, no spicetify/Spotify access. The live exe's target dir is
# refused as a build dir.
#
# Release speed without a 10+ minute LTO link: opt-level 3 is kept, LTO off and
# 16 codegen units (CARGO_PROFILE_RELEASE_* env, no Cargo.toml change). The
# unit-cost numbers (f_unit_costs) are therefore a little pessimistic against
# the shipped LTO build; the latency numbers are I/O- and timer-bound and
# unaffected.
set -euo pipefail

here="$(cd "$(dirname "$0")/../.." && pwd)"
export PATH="$PATH:/c/Users/KurepaBoss/.cargo/bin"
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-C:/Users/KurepaBoss/Desktop/statusify-work/targets/bridge}"
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-4}"
export CARGO_PROFILE_RELEASE_LTO=false CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 CARGO_PROFILE_RELEASE_STRIP=false
unset DISCORD_APP_ID STATUSIFY_PORT STATUSIFY_DATA_DIR

case "${CARGO_TARGET_DIR//\\//}" in
  */statusify-rs/src-tauri/target*) echo "refusing to build into the live instance's target dir" >&2; exit 2;;
esac

out="${STATUSIFY_BENCH_OUT:-$here/tests/bench/results/$(date +%Y%m%d-%H%M%S).jsonl}"
mkdir -p "$(dirname "$out")"
export STATUSIFY_BENCH_OUT="$out"

if [ "$#" -gt 0 ]; then benches=("$@"); else
  benches=(f_unit_costs a_track_change c_pause_resume_seek d_discord_reconnect e_bridge_drop b_line_jitter_and_waste f_idle_cpu js_e2e js_lyrics_hang)
fi

cd "$here/src-tauri"
cargo test --release --lib --no-run 2>&1 | tail -3
for b in "${benches[@]}"; do
  echo "== bench_$b" >&2
  cargo test --release --lib "bench_$b" -- --ignored --nocapture --test-threads=1 2>>"${out%.jsonl}.stderr.log" | grep '^BENCH_RESULT ' || true
done
echo "results: $out"
