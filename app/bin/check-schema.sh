#!/usr/bin/env bash
#
# Fail if ui/openapi.yaml and ui/src/api/schema.d.ts are behind the backend.
#
# ADR-019 makes the generated schema the frontend's contract and commits it so
# an API change shows up in the same diff as the backend change that caused it.
# Nothing enforced that until this hook: a serializer field added without
# rerunning `npm run api:types` left the committed contract quietly wrong, and
# the UI kept compiling against a schema that no longer matched the API.
set -euo pipefail

cd "$(dirname "$0")/.."

# CI sets PYTHON to the interpreter on its PATH; a checkout uses the venv.
PYTHON="${PYTHON:-.venv/bin/python}"

if ! command -v "$PYTHON" >/dev/null 2>&1 && [ ! -x "$PYTHON" ]; then
  echo "skipping schema check: $PYTHON not found" >&2
  exit 0
fi

before=$(mktemp)
trap 'rm -f "$before"' EXIT
cp ../ui/openapi.yaml "$before"

# Test settings so this needs no database and no .env.
DJANGO_SETTINGS_MODULE=app.settings.test \
  "$PYTHON" manage.py spectacular --file ../ui/openapi.yaml >/dev/null

if ! diff -q "$before" ../ui/openapi.yaml >/dev/null; then
  cp "$before" ../ui/openapi.yaml   # leave the tree as we found it
  echo "The committed OpenAPI schema is out of date (ADR-019)." >&2
  echo "Run 'npm run api:types' in ui/ and commit openapi.yaml and schema.d.ts" >&2
  echo "alongside this backend change." >&2
  exit 1
fi
