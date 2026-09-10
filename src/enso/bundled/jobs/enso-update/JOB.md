---
name: Enso release check
schedule: "30 3 * * *"
provider: "{{provider}}"
model: "{{model}}"
effort: "{{effort}}"
workspace: default
enabled: true
prerun: prerun.sh
catch_up: true
---

The prerun performs a deterministic release check and delivers a notification only
when a newer release has not already been announced. It never opens the agent gate.
Do not install updates from this job. The operator requests an upgrade in chat or
with `enso update apply` when ready.
