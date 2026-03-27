#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

if ! command -v adb >/dev/null 2>&1; then
  echo "Error: adb not found in PATH" >&2
  exit 1
fi

if ! command -v scrcpy >/dev/null 2>&1; then
  echo "Error: scrcpy not found in PATH" >&2
  exit 1
fi

# Optional: allow passing a device serial as first argument.
serial="${1:-}"

if [[ -z "$serial" ]]; then
  serial="$(adb devices | awk 'NR>1 && $2=="device" {print $1; exit}')"

  if [[ -z "$serial" ]]; then
    echo "Error: no online Android device found. Run: adb devices" >&2
    exit 1
  fi
fi

echo "Launching scrcpy on device: $serial"
exec scrcpy -s "$serial" \
  --window-title="note" \
  --turn-screen-off \
  --power-off-on-close \
# opt+o = turn off device screen