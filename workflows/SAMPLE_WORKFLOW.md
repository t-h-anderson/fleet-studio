# Reviewed delivery

Workflow example-delivery / 1.0.0 / SHA-256 600cb7f69b468666ff55e750204b2ae3ea7c438669c2766e02d5fc215dd82f3e

Generated from workflow.json. Edit the workflow in Fleet Studio, then review and deploy a new release.

This deployed workflow replaces the workflow rules in its historical source documents. References below to former roles, backlog and handoff documents identify migration sources; the definitions in this release control workflow behavior. Continue following applicable engineering standards, source/publication restrictions and explicit ticket/host stops. Report conflicts before acting.

A supervised workflow for bounded implementation, review and delivery.

Prose guards and ticket transitions are agent responsibilities. Studio evaluates only the configured event, session-condition and timer triggers; a notification is not technical acceptance.

## Role: Coordinator [coordinator]

Kind: agent

Set bounded work, assign owners and resolve blockers.

Authority: Own scope and assignments.

## Role: Writer [writer]

Kind: agent

Implement assigned work, verify it and resolve review findings.

Authority: Own only assigned files and scope.

## Role: Subagent reviewers [subagent-reviewers]

Kind: subagent

Review changes independently and return concrete findings.

Authority: Read-only review; implementation stays with the parent.

## Role: Integrator [integrator]

Kind: agent

Integrate reviewed work and complete delivery.

Authority: Own assigned integration and delivery.

## Handoffs

### Assign scoped packet [assign-scoped-packet]

Coordinator -> Writer

When: An eligible, unclaimed packet is ready

Artifact: Ticket with source, file scope and unit acceptance

Required guard: One writer per owned file

### Request static review [request-static-review]

Writer -> Subagent reviewers

When: Implementation, tests and drafts are ready

Artifact: Exact diff, source basis, tests and draft documentation

Required guard: Read-only reviewers; no independent execution

### Return findings [return-findings]

Subagent reviewers -> Writer

When: Independent review is complete

Artifact: Concrete findings and coverage gaps

Required guard: Writer fixes findings and renews affected evidence

### Submit reviewed packet [submit-reviewed-packet]

Writer -> Integrator

When: Reviewed packet is AI Awaiting Merge

Artifact: Exact head, test and analysis evidence and usable drafts

Required guard: Integrator checks evidence and records bundle ownership

### Review integrated diff [review-integrated-diff]

Integrator -> Subagent reviewers

When: Substantive bundle changes are ready

Artifact: Integrated diff and evidence applicability

Required guard: Read-only; reuse valid completed review

### Return bundle findings [return-bundle-findings]

Subagent reviewers -> Integrator

When: Integration review is complete

Artifact: Findings, corrections and remaining limits

Required guard: Integrator renews checks invalidated by changes

### Escalate scope exception [escalate-scope-exception]

Integrator -> Coordinator

When: A cross-bundle or acceptance exception remains

Artifact: Concrete conflict, evidence and requested decision

Required guard: Ordinary acceptance and merge remain with Integrator

## Ticket states (first matching rule wins)

- closed: Closed; queue roles: ; closed / labels  / prefixes
- blocked: Blocked; queue roles: coordinator; opened / labels AI Blocked / prefixes
- integration: Awaiting merge; queue roles: integrator; opened / labels AI Awaiting Merge / prefixes
- active: In progress; queue roles: writer; opened / labels AI Claimed / prefixes
- ready: Ready; queue roles: writer; opened / labels AI Ready / prefixes
- backlog: Backlog; queue roles: coordinator; fallback

## Ticket transitions


## Agent policies

### Meaningful progress reports [progress-reporting]

At task start, meaningful progress, blocker changes, handoff receipt and work completion, report a structured milestone with the canonical tracker evidence link. Tracker records remain authoritative. Reported completion is not technical acceptance. Do not produce repeated no-change updates.

## Trigger templates

### Assign scoped packet [assign-scoped-packet]

event; event=handoff-9e32e493; condition=turn-completed; interval=30 minutes; delivery=automatic; recipients=single

Workflow action: Assign scoped packet
From: Coordinator
To: Writer
When: An eligible, unclaimed packet is ready
Required artifact: Ticket with source, file scope and unit acceptance
Required condition: One writer per owned file

Read the reported ticket and current handoff before acting. Confirm named custody, scope, evidence and explicit stops. A reported event is a notification, not proof of acceptance. Record work in the existing ticket/MR; do not invent missing assignments or transfer another agent's runtime.

### Submit reviewed packet [submit-reviewed-packet]

event; event=handoff-8253bbf2; condition=turn-completed; interval=30 minutes; delivery=automatic; recipients=single

Workflow action: Submit reviewed packet
From: Writer
To: Integrator
When: Reviewed packet is AI Awaiting Merge
Required artifact: Exact head, unit/Analyzer evidence and usable drafts
Required condition: Integrator checks evidence and records bundle ownership

Read the reported ticket and current handoff before acting. Confirm named custody, scope, evidence and explicit stops. A reported event is a notification, not proof of acceptance. Record work in the existing ticket/MR; do not invent missing assignments or transfer another agent's runtime.

### Escalate scope exception [escalate-scope-exception]

event; event=handoff-4709c619; condition=turn-completed; interval=30 minutes; delivery=automatic; recipients=single

Workflow action: Escalate scope exception
From: Integrator
To: Coordinator
When: A cross-bundle or acceptance exception remains
Required artifact: Concrete conflict, evidence and requested decision
Required condition: Ordinary acceptance and merge remain with Integrator

Read the reported ticket and current handoff before acting. Confirm named custody, scope, evidence and explicit stops. A reported event is a notification, not proof of acceptance. Record work in the existing ticket/MR; do not invent missing assignments or transfer another agent's runtime.

## Mapping gaps


## Sources


## Design diagram

```mermaid
flowchart LR
  n0["Coordinator"]
  n1["Writer"]
  n2["Subagent reviewers"]
  n3["Integrator"]
  n0 -->|"Assign scoped packet"| n1
  n1 -->|"Request static review"| n2
  n2 -->|"Return findings"| n1
  n1 -->|"Submit reviewed packet"| n3
  n3 -->|"Review integrated diff"| n2
  n2 -->|"Return bundle findings"| n3
  n3 -->|"Escalate scope exception"| n0
```
