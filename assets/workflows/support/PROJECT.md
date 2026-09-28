---
name: Support
workflow: 2
enabled: false
stages:
  - name: diagnose
    command: printf '%s\n' '{"message":"Diagnostic complete","output":{"data":{"severity":"low"}}}'
  - name: classify
    inputs: [request, diagnose]
    routes: [simple, complex]
  - name: investigate
    inputs: [request, diagnose, classify]
    output: Diagnosis and recommended resolution.
  - name: approve
    human: true
    inputs: [investigate]
    return_to: investigate
  - name: resolve
    inputs: [diagnose, "investigate?", "approve?"]
    command: printf '%s\n' '{"message":"Resolution recorded","output":{"text":"Resolved"}}'
  - name: respond
    inputs: [request, resolve]
    output: Response to the requester.
paths:
  simple: [diagnose, classify, resolve, respond]
  complex: [diagnose, classify, investigate, approve, resolve, respond]
---

The diagnostic and resolution scripts run locally without a model. Classification and
response use independently configured agent jobs. The complex path includes investigation
and approval; the simple path skips both without recording completion for either.
