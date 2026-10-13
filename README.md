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

The headline blocker count covers active execution evidence: a current blocked
agent report or a claimed task marked blocked. Restricted work (`AI Banned`,
`Restriction::…`, `Access::AI read-only`), deferred/sign-off holds and unclaimed
dependency holds are listed separately. These display qualifications do not
change tracker labels or authorize work. Closure cards show their actual
observation windows; zero observed closures does not mean zero delivered work.
Idle sessions with blocked, paused or limited goals remain held. Inspect the
agent's current blocker report/activity before considering availability.

Monitoring settings default to 30 minutes without a handoff receipt, 60 minutes
without a meaningful task report, and 90 days of observation history. These are
attention thresholds: they do not stop agents, reassign work or infer failure.
Durations begin with observations. Initial stage-entry times and transitions
during observation gaps remain unknown. Closure counts cover observed tracker
transitions, not an imported historical total or a claim of technical acceptance.

### Monitoring

Open **Progress → Monitoring** for open tickets by workflow state, hourly backlog
samples for the last 30 days, unassigned tickets, session availability, the oldest
pending handoff receipt, token counters and test evidence. Select a ticket in
**Tasks** for its accumulated observed time in each state, including revisits.
Only consecutive matching observations up to 180 seconds apart contribute to
state time. Initial history, transition intervals and collection gaps are
excluded; these durations are not logged work effort. Timing starts afresh when
the project or deployed workflow changes. No historical state transitions are
inferred from a ticket's `updated_at` timestamp.

Native session token counters are read from Codex metadata without reading
transcripts. Session totals include earlier work; the separately displayed
observed usage counts positive increments since the first sample. Counter resets
are flagged and shared sessions counted once. Increments across downtime are
included when the counter remains monotonic. Unavailable/stale sessions are
excluded from totals with coverage shown. Goal usage/budgets come from the native
goal record. These are token counts, not billing costs or ticket attribution.

GitLab CI refreshes in the background about every 90 seconds, showing the latest
10 project pipelines across branches, their commit, ref and status, plus duration,
queue time and published test totals for the latest pipeline when available.
Missing reports and failed reads are labelled explicitly. A successful pipeline
does not imply tests were published or establish task acceptance.

Agents can report existing local test evidence without running tests or changing
a task's reported work/blocker state:

```powershell
python fleet_progress.py --fleet <fleet-id> --agent <agent-id> --release <release-id> --id <stable-test-run-id> --kind test-result --task <ticket-URL> --evidence <evidence-URL> --suite "Focused unit tests" --test-status passed --revision <full-Git-commit-SHA> --passed 12 --failed 0 --skipped 1 --duration-seconds 2.5
```

Test statuses are `passed`, `failed`, `blocked`, `running` and `skipped`. Counts
and duration are optional, never assumed to be zero. Use a new event ID for each
new run/status report; identical retries reuse the original ID. Evidence must
state the test scope and any uncommitted changes relative to the commit. The
latest result per suite, reporter, ticket and commit is shown (up to 100 from the
latest 1,000 retained reports). Earlier commits remain identified. Installing the
application does not deploy new reporting policies or notify agents. Add the
`progress-reporting` policy through a reviewed workflow deployment when ready to
adopt reporting instructions; existing runtime custody and test stops still apply.

Metrics are returned in the `metrics` member of `GET /api/progress`, with per-task
`stateSeconds` and `timingStartedAt`. They share `.runtime/progress.sqlite` and its
configured history retention. See [MONITORING.md](MONITORING.md) for definitions
and the next useful measurements.

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

If reporting is missing, **Progress → Set up reporting** previews a release that
adds only the progress-reporting policy (and increments a numeric patch version).
It refuses unrelated draft changes and preserves roles, bindings, enabled-trigger
settings and any explicit automation pause. Deployment does not send adoption
messages or acknowledge on behalf of agents. Existing installed folder entry
points resolve the new active release at the next safe boundary.

### Reconcile existing handoffs

Open a handoff in Progress and choose **Reconcile receipt or source**. Link the
actual recipient acknowledgement on the original ticket, optionally selecting
a public message from the original recipient session. Preview the effect before
applying. The record is labelled **operator reconciliation**, retaining the
evidence, previous delivery state and original event identity. It does not claim
technical acceptance, resume a goal or resend instructions. A missing source is
recovered only from the exact retained event and its namespaced identity.

Alternatively repair the source on a proven unsent event; it remains awaiting
delivery review. An active dispatch must settle first. Reconciliation rejects
stale previews and keeps existing event deduplication. The local authenticated
API exposes `POST /api/handoffs/reconcile/preview` (`actionId`, `mode`,
`evidenceUrl`, `note`, optional `receiptMessageId`) and
`POST /api/handoffs/reconcile/apply` (`previewId`).

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
