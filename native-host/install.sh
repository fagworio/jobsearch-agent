#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 EXTENSION_ID HOST_EXECUTABLE" >&2
  exit 2
fi

extension_id="$1"
host_executable="$2"
if [[ ! "$extension_id" =~ ^[a-z]{32}$ ]]; then
  echo "EXTENSION_ID must be a 32-character Chrome extension id" >&2
  exit 2
fi
if [[ ! -x "$host_executable" ]]; then
  echo "HOST_EXECUTABLE must point to an executable file" >&2
  exit 2
fi

config_root="${XDG_CONFIG_HOME:-$HOME/.config}"
browser_config_dir="${JOB_AGENT_V2_BROWSER_DIR:-google-chrome}"
case "$browser_config_dir" in
  google-chrome|chromium) ;;
  *) echo "JOB_AGENT_V2_BROWSER_DIR must be google-chrome or chromium" >&2; exit 2 ;;
esac
host_dir="$config_root/$browser_config_dir/NativeMessagingHosts"
mkdir -p "$host_dir"
sed \
  -e "s|__HOST_EXECUTABLE__|${host_executable//|/\\|}|g" \
  -e "s|__EXTENSION_ID__|$extension_id|g" \
  native-host/com.job_agent_v2.json.template > "$host_dir/com.job_agent_v2.json"
# Chrome reads the manifest before it starts the host. Keep the manifest
# readable by the browser process while the host executable remains separately
# protected by the filesystem.
chmod 644 "$host_dir/com.job_agent_v2.json"
echo "Installed com.job_agent_v2 at $host_dir/com.job_agent_v2.json"
