#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
browser_bin="${JOB_AGENT_V2_BROWSER_BIN:-/opt/google/chrome/google-chrome}"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}"
profile_dir="${JOB_AGENT_V2_CHROME_PROFILE:-$config_dir/job-agent-v2}"
extension_dir="$script_dir/extension"
url="${1:-https://job-boards.greenhouse.io/givedirectly/jobs/4738257005}"
extension_args=()
if [[ "${JOB_AGENT_V2_USE_COMMAND_LINE_EXTENSION:-0}" == "1" ]]; then
  extension_args=(
    "--disable-extensions-except=$extension_dir"
    "--load-extension=$extension_dir"
  )
fi
remote_debug_args=()
if [[ -n "${JOB_AGENT_V2_REMOTE_DEBUG_PORT:-}" ]]; then
  remote_debug_args=(
    "--remote-debugging-port=${JOB_AGENT_V2_REMOTE_DEBUG_PORT}"
    "--remote-debugging-address=127.0.0.1"
  )
fi

if [[ ! -x "$browser_bin" ]]; then
  browser_bin="$(command -v google-chrome || command -v google-chrome-stable || true)"
fi
if [[ -z "$browser_bin" || ! -x "$browser_bin" ]]; then
  echo "Chrome executable not found; set JOB_AGENT_V2_BROWSER_BIN" >&2
  exit 1
fi
if [[ ! -f "$extension_dir/manifest.json" ]]; then
  echo "extension/manifest.json not found in $script_dir" >&2
  exit 1
fi

mkdir -p "$profile_dir"
exec "$browser_bin" \
  --user-data-dir="$profile_dir" \
  "${extension_args[@]}" \
  --no-first-run \
  --no-default-browser-check \
  "${remote_debug_args[@]}" \
  "$url"
