---
name: Enso audit
schedule: "0 3 * * *"
command: enso doctor --attention --notify --quiet
enabled: true
catch_up: true
---

Run Enso's health audit and send a concise report to the configured notification target
when attention is needed. Healthy checks stay quiet. This job does not use an LLM or
repair findings.
