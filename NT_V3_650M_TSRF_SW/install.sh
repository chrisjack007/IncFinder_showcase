#!/usr/bin/env bash
# Install into the activated Conda environment from this local release.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
fail() { printf '%s\n' "ERROR: $*" >&2; exit 2; }
[[ -n "${CONDA_PREFIX:-}" ]] || fail 'Activate the target Conda environment first.'
case "$(uname -s)" in
  MINGW*|MSYS*) platform=win-64 ;;
  Linux*) [[ "$(uname -m)" == x86_64 ]] || fail 'Supported architecture: Linux x86_64'; platform=linux-64 ;;
  *) fail 'Supported platforms: Windows Git Bash x86_64 / Linux x86_64' ;;
esac
conda_command=()
if command -v conda >/dev/null 2>&1; then
  conda_command=(conda)
elif [[ -n "${CONDA_EXE:-}" ]]; then
  conda_executable="$CONDA_EXE"
  if [[ "$platform" == win-64 ]]; then conda_executable="$(cygpath -u -- "$conda_executable")"; fi
  [[ -x "$conda_executable" ]] || fail 'CONDA_EXE is not executable; reactivate Conda.'
  conda_command=("$conda_executable")
else
  fail 'Conda is unavailable; reactivate the target environment.'
fi
models=''
while (($#)); do
  case "$1" in
    --model-dir) (($# >= 2)) || fail '--model-dir requires a path'; models="$2"; shift 2 ;;
    -h|--help) printf '%s\n' 'Usage: bash install.sh [--model-dir MODELS]'; exit 0 ;;
    *) fail "Unknown argument: $1" ;;
  esac
done
if [[ -n "$models" ]]; then
  if [[ "$platform" == win-64 ]]; then models="$(cygpath -u -- "$models")"; fi
  [[ -f "$models/ntv3/model.safetensors" && -f "$models/rf/stage1.joblib" && -f "$models/rf/stage2.joblib" ]] || fail 'Incomplete directory supplied to --model-dir.'
fi
[[ -f "$root/dist/channel/noarch/repodata.json" ]] || fail 'Local release channel is missing; use packaging/build_release.py to build the distribution.'
# The standard Conda transaction displays changes and retains its confirmation.
"${conda_command[@]}" install --offline --prefix "$CONDA_PREFIX" "$root/dist/channel/noarch/incfinder-0.5.0rc2-py_0.conda"
if [[ -z "$models" ]]; then incfinder version; exit 0; fi
python - "$models" <<'PY'
import sys
from ntv3_tsrf.resources import bind_models
print('Model binding:', bind_models(sys.argv[1]))
PY
incfinder doctor
