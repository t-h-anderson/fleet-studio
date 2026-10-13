# Fleet Studio

A local Windows application for designing, deploying and monitoring supervised
multi-agent workflows. The external tracker owns tasks and acceptance; Studio
owns workflow definitions, local agent bindings and communication receipts.

## Start

Requires Python 3.11+, Windows, and Codex CLI for agent controls. GitLab reads use
an existing `glab` login. Node is needed only for browser/layout tests.

```powershell
python fleet_studio.py --open
```

Alternatively run `Start-FleetStudio.ps1`. The loopback server normally uses
http://127.0.0.1:8765. A new installation starts with an empty fleet. Add agents
and link their existing sessions and folders through the roster/session library.
The browser displays activity and queues instructions; interactive sessions run
in Windows Terminal.

Use `--data-root <folder>` to keep configuration and observations outside the
checkout. Stop the existing Studio server through App settings before starting
another version against the same data directory. Stopping Studio leaves agent
sessions running.

## Progress

Progress is the default view for new browser sessions. Existing saved view
preferences are preserved. It combines:

- Tracker tasks and stage queues, including source timestamps and partial/stale data.
- Agent-reported work, blocker reasons, dependencies and next actions.
- Session observations, which establish activity rather than task completion.
- Handoff delivery, agent receipt and workflow adoption as separate facts.
- Observed closures over 24 hours and seven days, and attention signals for waits.

Select a task, agent or handoff to inspect evidence and observation history.
The deployed workflow graph shows role queues and pending receipts. Its detail
panels link to the corresponding draft editors. Draft changes affect routing
only after a reviewed deployment.

Monitoring settings default to 30 minutes without a handoff receipt, 60 minutes
without a meaningful task report, and 90 days of observation history. These are
attention thresholds: they do not stop agents, reassign work or infer failure.
Durations begin with observations. Initial stage-entry times and transitions
during observation gaps remain unknown. Closure counts cover observed tracker
transitions, not an imported historical total or a claim of technical acceptance.

Connect a project through **Backlog → Project & links**. GitLab refreshes about
every 90 seconds in the server, including while the browser is closed. Session
observations and delivery checks run approximately every five seconds. The
tracker adapter exposes `snapshot(config)` and `history(config, iid)`; GitLab is
the initial implementation. Optional `backlog.legacyReferences` maps agent IDs
to local status files; those files supply display references only.

## Design and deploy

Import `workflows/sample.workflow.json` or build roles, handoffs, ticket-state
mapping, policies and triggers in the editors. The sample is a generic reviewed
delivery process with structured progress reporting.

1. Edit the draft, role assignments and communication contracts.
2. Review deployment: inspect definition changes, recipient bindings, enabled
   triggers and pending handoffs.
3. Deploy an immutable local release.
4. Review/install agent entry points and review/send adoption notifications.
5. Agents read their assignments and acknowledge the release at a safe boundary.

Automatic event delivery requires both reporting agent and recipient to have
acknowledged their current deployed bindings. Timer/idle delivery requires the
recipient's acknowledgement. Paused automation, excluded or rebound agents,
unavailable observations, stopped/limited goals and queued work continue to
hold delivery. Unknown send outcomes are never automatically resent. Earlier
unsent handoffs require review when their deployment changes.

Policies and handoff conditions are agent obligations. Studio does not execute
arbitrary workflow scripts or accept/close tracker tasks. Project-specific
source restrictions, task ownership and explicit stops remain applicable.

## Agent reports

Record substantive evidence in the tracker, then report meaningful boundaries:

```powershell
python fleet_progress.py --fleet <fleet-id> --agent <agent-id> --release <release-id> --id ticket-123-start --kind task-started --task https://gitlab.example/team/project/-/issues/123 --evidence https://gitlab.example/team/project/-/issues/123#note_456 --summary "Started the assigned change"
```

Supported kinds: `task-started`, `progress-updated`, `blocked`, `unblocked`,
`handoff-received`, `work-finished`. Blocked reports require `--reason` and
`--next-action`. A receipt requires `--handoff <delivery-action-id>` and must
match the original recipient and task. `--depends-on` adds an explicit dependency
link. Use `--data-root` when Studio stores its runtime elsewhere.

Reuse the same event ID and identical details when retrying. Reports are
idempotent across restarts and history retention. A work-finished report does
not close the tracker task. Receipt does not grant technical acceptance.
If Studio is unavailable, maintain the tracker record and retry at the next
safe boundary; reporting does not require an agent polling loop.

Formal handoff dispatch continues through `fleet_event.py`. Progress reporting
never dispatches instructions. Newly deployed policies with ID
`progress-reporting` include reporting guidance in generated agent instructions.

## Local API and storage

- `GET /api/progress?fleetId=...`: combined projection, freshness, attention,
  roles, reports, handoffs and adoption.
- `GET /api/progress/history?fleetId=...`: newest observations; optional
  `taskUrl`, `agentId` and `before` cursor. Follow `nextBefore` for further results.
- `POST /api/progress/report`: `fleetId`, `agentId`, `sessionId`, `releaseId`,
  `eventId`, `kind`, `taskUrl`, `evidenceUrl`; optional `summary`, `reason`,
  `nextAction`, `handoffId`, `dependsOn`.

Writes use the same instance token and local-origin checks as existing controls.
The CLI verifies the saved server instance before reporting. Raw task bodies,
tool output and private reasoning are not copied into progress observations.

`fleet-studio.json` stores local drafts/bindings and `fleet.json` stores launcher
settings. Immutable releases live in `workflow-releases/`. The new
`.runtime/progress.sqlite` stores derived observations and the last tracker
projection, separately from native Codex databases. Unresolved handoffs and
compact deduplication identities survive observation retention. This is not an
editable task store. All runtime/configuration files are ignored by Git.

Existing workflow format version 1 remains supported. Importing the sample does
not replace an active release until explicitly deployed. Existing local
configurations, releases, acknowledgement records and delivery identities remain
in their data directory; none are distributed with this repository.

## Validation

```powershell
python -B -m unittest discover -s tests -p 'test_*.py'
npm ci --ignore-scripts
npm run test:layout
npm run test:ui
python -B workflow_spec.py workflows/sample.workflow.json --output workflows/SAMPLE_WORKFLOW.md --check
```

Browser tests use Playwright with installed Microsoft Edge and create synthetic
fleets in temporary directories. Real agent launches/controls and live tracker
reads are disabled. Backend tests mock native sends. Screenshots and generated
test results are local artifacts and are not committed.
