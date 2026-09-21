#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SKILL_NAME="jobsearch-agent"
SKILL_DIR="$HERMES_HOME/skills/$SKILL_NAME"

mkdir -p "$SKILL_DIR"
cp "$ROOT/hermes/SKILL.md" "$SKILL_DIR/SKILL.md"
printf 'Installed %s skill at %s\n' "$SKILL_NAME" "$SKILL_DIR"

