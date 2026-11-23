#!/usr/bin/env bash
# Windows Git Bash / Linux; MSYS conversions are disabled only for Docker.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
image="${INCFINDER_IMAGE:-incfinder-ntv3-tsrf:0.5.0rc2}"
fail() { printf '%s\n' "ERROR: $*" >&2; exit 2; }
is_msys=false
case "$(uname -s)" in MINGW*|MSYS*) is_msys=true ;; esac
absolute_path() {
  local value="$1"
  if "$is_msys"; then value="$(cygpath -u -- "$value")"; fi
  realpath -m -- "$value"
}
host_path() {
  case "$1" in *,*) fail "Docker mount paths cannot contain commas: $1" ;; esac
  if "$is_msys"; then cygpath -m -- "$1"; else printf '%s\n' "$1"; fi
}
command -v docker >/dev/null 2>&1 || fail 'Docker is unavailable.'
command=predict
case "${1:-}" in
  predict|doctor|check|version|help) command="$1"; shift ;;
  --help|-h|--version) command="$1"; shift ;;
  '') command=help ;;
esac
models="${INCFINDER_MODEL_DIR:-$root/models}"
input='' output='' features=''
forward=()
while (($#)); do
  case "$1" in
    --model-dir) (($# >= 2)) || fail '--model-dir requires a path'; models="$2"; shift 2 ;;
    --model-dir=*) models="${1#*=}"; shift ;;
    --input|--i|-i) (($# >= 2)) || fail "$1 requires a path"; [[ -z "$input" ]] || fail 'Duplicate input'; input="$2"; shift 2 ;;
    --input=*|--i=*) [[ -z "$input" ]] || fail 'Duplicate input'; input="${1#*=}"; shift ;;
    --output|--o|-o) (($# >= 2)) || fail "$1 requires a path"; output="$2"; shift 2 ;;
    --output=*|--o=*) output="${1#*=}"; shift ;;
    --features-dir) (($# >= 2)) || fail '--features-dir requires a path'; features="$2"; shift 2 ;;
    --features-dir=*) features="${1#*=}"; shift ;;
    --device) (($# >= 2)) || fail '--device requires cpu'; [[ "$2" == cpu ]] || fail 'This image supports CPU only.'; forward+=("$1" "$2"); shift 2 ;;
    --device=*) [[ "${1#*=}" == cpu ]] || fail 'This image supports CPU only.'; forward+=("$1"); shift ;;
    --threads|--jobs|--batch-size|--max-length|--threshold-mode|--parent-run-id) (($# >= 2)) || fail "$1 requires a value"; forward+=("$1" "$2"); shift 2 ;;
    -*) forward+=("$1"); shift ;;
    *) if [[ "$command" == predict ]]; then [[ -z "$input" ]] || fail 'Duplicate input'; input="$1"; else forward+=("$1"); fi; shift ;;
  esac
done
mounts=()
help_only=false
for arg in "${forward[@]}"; do [[ "$arg" != --help && "$arg" != -h ]] || help_only=true; done
if ! "$help_only" && [[ "$command" == predict || "$command" == doctor || "$command" == check ]]; then
  models="$(absolute_path "$models")"
  [[ -d "$models/ntv3" && -d "$models/rf" ]] || fail 'Specify a complete model directory using --model-dir.'
  mounts+=(--mount "type=bind,source=$(host_path "$models"),target=/models,readonly")
fi
if [[ "$command" == predict ]]; then
  # Help does not require input/output or loading a model.
  if ! "$help_only"; then
    [[ -n "$input" && -n "$output" ]] || fail 'Usage: bash docker/run.sh predict INPUT -o OUTPUT --allow-encoder-drift'
    input="$(absolute_path "$input")"; output="$(absolute_path "$output")"
    [[ -f "$input" ]] || fail "Missing input: $input"
    [[ ! -e "$output" ]] || fail "Output already exists: $output"
    case "$output" in "$models"|"$models/"*) fail 'Output cannot be inside models.' ;; esac
    case "$output" in "$root/results/"*) ;; "$root"|"$root/"*) fail 'Outputs inside the software folder must be under results/.' ;; esac
    parent="$(dirname -- "$output")"; mkdir -p -- "$parent"
    container_input="/input/$(basename -- "$input")"
    mounts+=(--mount "type=bind,source=$(host_path "$input"),target=$container_input,readonly")
    mounts+=(--mount "type=bind,source=$(host_path "$parent"),target=/output"
             --env INCFINDER_RUN_DIR=/output/.incfinder-runs)
    forward=("$container_input" -o "/output/$(basename -- "$output")" "${forward[@]}")
    if [[ -n "$features" ]]; then
      features="$(absolute_path "$features")"; [[ -d "$features" ]] || fail "Missing feature directory: $features"
      mounts+=(--mount "type=bind,source=$(host_path "$features"),target=/features,readonly")
      forward+=(--features-dir /features)
    fi
  fi
fi
identity=()
if ! "$is_msys"; then identity=(--user "$(id -u):$(id -g)"); fi
if "$is_msys"; then
  MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' docker run --rm --pull=never --network=none "${identity[@]}" "${mounts[@]}" "$image" "$command" "${forward[@]}"
else
  docker run --rm --pull=never --network=none "${identity[@]}" "${mounts[@]}" "$image" "$command" "${forward[@]}"
fi
