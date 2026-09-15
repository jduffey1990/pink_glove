#!/usr/bin/env bash
#
# Lint ui/ with a Node that can actually run the linter.
#
# eslint-config-vuetify's toolchain calls Object.groupBy, which landed in Node
# 21. On anything older eslint does not degrade -- it dies with
# "TypeError: Object.groupBy is not a function" from inside a dependency,
# which reads as a broken install rather than an unmet requirement.
#
# So: pick up the version in .nvmrc if nvm is available, and otherwise say
# plainly what is wrong.
set -euo pipefail

cd "$(dirname "$0")/.."

required=$(tr -d '[:space:]' < .nvmrc)

current_major() { node --version 2>/dev/null | sed 's/^v//; s/\..*//' || echo 0; }

if [ "$(current_major)" -lt "$required" ] && [ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]; then
  # shellcheck disable=SC1091
  . "${NVM_DIR:-$HOME/.nvm}/nvm.sh"
  nvm use >/dev/null 2>&1 || true
fi

if [ "$(current_major)" -lt "$required" ]; then
  echo "ui lint needs Node >= ${required}; found $(node --version 2>/dev/null || echo 'no node')." >&2
  echo "Run 'nvm use' in ui/ (it reads .nvmrc), or install Node ${required}." >&2
  exit 1
fi

exec npm run --silent lint:fix
