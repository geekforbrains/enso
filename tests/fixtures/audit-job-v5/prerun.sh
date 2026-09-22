#!/usr/bin/env bash
# The gate for enso-audit: open only when `enso doctor` found something worth reporting.
# --attention widens the doctor's exit 1 past health problems to the installation-hygiene
# findings (unexpected entries, dangling links, world-readable credentials, stale files),
# which are warnings and would otherwise never reach the operator. Plain `enso doctor`
# keeps its ordinary meaning: exit 1 for a problem, 0 for warnings.
# The gate contract reads 0 and 1 the other way round, so this script inverts it:
# doctor 0 -> exit 1 (no work, nothing spent or sent); doctor 1 -> the JSON report on
# stdout and exit 0, so it lands in {{gate_output}}; anything else is the doctor itself
# failing (not on PATH, a crash) -> exit 2, alerted.
# A crash exits 1 as well (Python's default for an uncaught exception), with a traceback on
# stderr and nothing on stdout, so exit 1 opens the gate only when the JSON actually arrived.
# No `set -e`: the doctor's exit 1 has to reach the case below.
set -uo pipefail
REPORT=$(enso doctor --json --attention)
STATUS=$?
case "$STATUS" in
  0) exit 1 ;;
  1)
    if [[ $REPORT == \{* ]]; then
      printf '%s\n' "$REPORT"
      exit 0
    fi
    echo "ENSO_ERROR: enso doctor exited with status 1 without a report" >&2
    exit 2
    ;;
  *) echo "ENSO_ERROR: enso doctor exited with status $STATUS" >&2; exit 2 ;;
esac
