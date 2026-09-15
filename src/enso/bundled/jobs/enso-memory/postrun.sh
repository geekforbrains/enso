#!/usr/bin/env bash
set -uo pipefail
# Failed providers and closed gates keep their existing outcome; they cannot repair a batch.
[[ "${ENSO_RUN_STATUS:-}" == "ok" ]] || exit 0
if [[ -z "${ENSO_RUN_ID:-}" ]]; then
  echo "ENSO_ERROR: memory checking requires ENSO_RUN_ID" >&2
  exit 2
fi
enso memory check --batch "$ENSO_RUN_ID"
