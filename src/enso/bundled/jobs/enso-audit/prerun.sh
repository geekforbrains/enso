#!/usr/bin/env bash
# The gate for enso-audit: open only when `enso doctor` found a problem.
# The doctor exits 0 when healthy and 1 on a problem, and the prerun contract reads those the
# other way round, so this script inverts it: doctor 0 -> exit 1 (no work, nothing spent or
# sent); doctor 1 -> the JSON report on stdout and exit 0, so it lands in {{prerun_output}};
# anything else is the doctor itself failing (not on PATH, a crash) -> exit 2, alerted.
# A crash exits 1 as well (Python's default for an uncaught exception), with a traceback on
# stderr and nothing on stdout, so exit 1 opens the gate only when the JSON actually arrived.
# No `set -e`: the doctor's exit 1 has to reach the case below.
set -uo pipefail
REPORT=$(enso doctor --json)
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
