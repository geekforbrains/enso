#!/usr/bin/env bash
set -uo pipefail
if [[ -z "${ENSO_RUN_ID:-}" ]]; then
  echo "ENSO_ERROR: memory preparation requires ENSO_RUN_ID" >&2
  exit 2
fi
enso memory prepare --batch "$ENSO_RUN_ID" --json
status=$?
if [[ "$status" -gt 1 ]]; then
  echo "ENSO_ERROR: memory preparation failed" >&2
fi
exit "$status"
