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
host_dir="$config_root/google-chrome/NativeMessagingHosts"
mkdir -p "$host_dir"
sed \
  -e "s|__HOST_EXECUTABLE__|${host_executable//|/\\|}|g" \
  -e "s|__EXTENSION_ID__|$extension_id|g" \
  native-host/com.job_agent_v2.json.template > "$host_dir/com.job_agent_v2.json"
chmod 600 "$host_dir/com.job_agent_v2.json"
echo "Installed com.job_agent_v2 at $host_dir/com.job_agent_v2.json"
