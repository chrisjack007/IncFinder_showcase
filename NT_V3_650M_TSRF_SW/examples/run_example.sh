#!/usr/bin/env bash
set -euo pipefail
# Activate a compatible environment and install the software before running.
# Defaults use the example folder; explicit arguments use the caller CWD.
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
input="${1:-$root/examples/sample.fasta}"
output="${2:-$root/results/bash_demo}"
models="${3:-$root/models}"
exec incfinder predict "$input" --output "$output" --model-dir "$models" --device cpu --allow-encoder-drift
