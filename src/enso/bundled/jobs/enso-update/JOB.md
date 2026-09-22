---
name: Enso release check
schedule: "30 3 * * *"
command: enso update check --notify --quiet
enabled: true
catch_up: true
---

Check for a newer release and notify the operator once per version. This job never
installs updates; the operator requests an upgrade in chat or with `enso update apply`
when ready.
