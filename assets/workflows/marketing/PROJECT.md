---
name: Marketing
workflow: 2
enabled: false
stages:
  - name: research
    output: Audience facts and source references.
  - name: draft
    inputs: [request, research]
    output: Campaign copy ready for editorial review.
  - name: editorial
    human: true
    inputs: [research, draft]
    return_to: draft
    max_returns: 1
  - name: publish
    inputs: [draft, editorial]
    command: printf '%s\n' '{"message":"Published to local fixture","output":{"artifacts":[{"uri":"local:campaign","revision":"v1"}]}}'
paths:
  campaign: [research, draft, editorial, publish]
---

Bind research to claude/sonnet/high and draft to codex/sol/medium (or explicit configured
agents of your choice). Publication is a local demonstration command; replace it with a
real publishing script whose receipts identify the resulting revision.
