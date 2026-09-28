---
name: Development
workflow: 2
enabled: false
stages:
  - name: classify
    routes: [direct, planned]
    instructions: Decide whether the request needs a written plan and approval.
  - name: plan
    output: An implementation plan with scope and acceptance conditions.
  - name: approve
    human: true
    inputs: [plan]
    return_to: plan
  - name: build
    inputs: [request, "plan?", "approve?"]
    output: The implementation and verification evidence.
  - name: review
    inputs: [build]
    return_to: build
    max_returns: 1
  - name: qa
    human: true
    inputs: [build, review]
    return_to: build
paths:
  direct: [classify, build, review]
  planned: [classify, plan, approve, build, review, qa]
---

This example demonstrates optional planning and QA without requiring a repository.
For Git integration, set repo/base and append an integrate stage to the applicable paths,
with the real project checks. The dev preset authors those repository settings.
