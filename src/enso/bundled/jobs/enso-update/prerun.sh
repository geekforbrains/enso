#!/usr/bin/env bash
# Sending is handled by the CLI, which records only successfully delivered notices.
# An offline check remains quiet and retries at the next nightly slot.
enso update check --notify --quiet
exit 1
