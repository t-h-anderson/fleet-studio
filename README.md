# Fleet Studio

A local application for designing, deploying and monitoring supervised
multi-agent workflows. The external tracker (GitLab or GitHub) owns tasks and
acceptance; Studio owns workflow definitions, agent bindings and communication
receipts. Monitor Codex CLI or Claude Code sessions on this computer or remote
machines running a fleet node. Managed workflow deployment currently supports
local agents; remote assignment delivery is still pending.

## Start

Requires Python 3.11+ and the CLI for each agent backend you use (Codex CLI,
Claude Code). GitLab reads use an existing `glab` login and GitHub reads an
existing `gh` login. Windows opens agents in Windows Terminal; Linux and macOS
use tmux. Node is needed only for browser/layout tests.

Tracker refresh errors distinguish authentication, project access, connectivity,
TLS, timeout, rate limits, missing CLI and unexpected responses. Raw CLI output
is never displayed. Failed reads retain the last successful snapshot with its
timestamp and a stale warning; counts remain unknown until a successful read.

In **Backlog → Project & links**, **Retry tracker reads without an unavailable
local proxy** is off by default. Enable it for launch environments that inherit
temporary loopback proxies. After a connectivity failure, Studio probes only
unauthenticated loopback proxy endpoints and retries a read-only GET once with
unreachable proxy entries omitted from that child process. Live, remote and
credentialed proxies, `NO_PROXY`, and the parent environment stay unchanged.
Recovery is logged without proxy values or credentials. This setting applies
to tickets, history and CI; it never retries a tracker mutation.

```powershell
python fleet_studio.py --open
```

Alternatively run `Start-FleetStudio.ps1`. The loopback server normally uses
http://127.0.0.1:8765. A new installation starts with an empty fleet. Add agents
and link their existing sessions and folders through the roster/session library.
The browser displays activity and previews instructions; interactive sessions run
in Windows Terminal or tmux.

Use `--data-root <folder>` to keep configuration and observations outside the
checkout. Stop the existing Studio server through App settings before starting
another version against the same data directory. Stopping Studio leaves agent
sessions running.

## Agent backends and hosts

Each agent has a **backend** and a **host**, chosen in the agent editor:

| Backend | Observation | Delivery | Rename | Native goals |
| --- | --- | --- | --- | --- |
| Codex CLI | Native metadata DBs and writer locks | `codex queue` (queued for the next turn) | Yes | Local only |
| Claude Code | Transcript in `~/.claude/projects` (or `CLAUDE_CONFIG_DIR`) | Explicit headless `claude -p --resume` turn on supported hosts | No | No |

Claude Code has no message queue, so automatic workflow delivery is held.
Explicit delivery previews explain that applying starts a headless turn. Studio
requires a saved transcript matching the assigned folder and refuses delivery
while it was written in the last 90 seconds, a Claude process may hold the
session, or process ownership is unknown. It prevents a second Studio-started
turn while the first process runs. Keep the native client closed during launch;
Studio cannot prevent a separate client being opened concurrently. A `started`
receipt confirms process creation, not turn completion or assignment adoption.
Headless output logs are kept in `.runtime/claude-turns/`. Activity displays only
public text blocks, excluding thinking, tool inputs and tool results. Token
counts include input, output, cache creation and cache reads; repeated streaming
usage records are deduplicated. These totals are not billing estimates.

Codex process liveness uses Windows Restart Manager, `/proc` on Linux or `lsof`
on macOS. Claude ownership detection currently requires Linux `/proc`; Windows
and macOS can monitor transcripts, but Studio refuses headless sends and terminal
launches when ownership cannot be established. Unknown ownership is never treated
as idle, and old unfinished turns are not inferred to have completed.

Claude assignments must be loaded explicitly with `workflow_client.py instructions`
at safe task boundaries and acknowledged by the agent. Automatic entry-point
installation currently creates Codex's `AGENTS.override.md` and rejects Claude
bindings; Claude does not automatically consume that file.

### Remote hosts

Run a fleet node on each remote machine:

```bash
export FLEET_NODE_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python fleet_node.py                         # loopback; reach it through an SSH tunnel
python fleet_node.py --bind 0.0.0.0 --cert node.pem --key node.key   # direct HTTPS
```

In Studio, open the fleet editor and list each node under **Remote fleet nodes**
as `<id> <url> <TOKEN_ENV_NAME>`, then start Studio with that environment
variable set to the node's token. Tokens are never written to Studio's files.
URLs must be HTTPS, or `http://127.0.0.1` for a tunnel such as
`ssh -L 8790:127.0.0.1:8790 build-box`. Select the host in each agent's editor;
its folder is validated by the node, not locally.

Nodes accept only Studio's own operations: observe, catalogue, public activity,
plain-text delivery (slash and shell commands are rejected), rename and open in
tmux. Unreachable hosts mark their sessions `unknown`, which holds automatic
delivery like any other unavailable observation. Native goal controls remain
local-Codex only. Redirects are refused so bearer credentials cannot follow them.
Server errors after a mutation are reported as uncertain and are not retried
automatically. Nodes recheck native session ownership before opening a terminal.

Remote nodes do not yet receive Studio's deployed assignment files or support
the local reporting/adoption endpoint. Deployment and entry-point installation
therefore reject enabled remote bindings. Remote observation and explicit
controls remain available; remote managed workflows require a future node
deployment/reporting protocol.

A deployed release records each agent's backend and host. Changing either
counts as a rebinding: the agent is excluded from automatic delivery until a
new release is reviewed and acknowledged.
Session identity includes host, backend and native ID, keeping observations and
token counters separate even when different hosts reuse the same native ID.
Controls retain the reviewed binding and host endpoint through dispatch.

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

### Assignment observations

**Progress → Agents** separates assignment observations from native session
activity. Cards distinguish queued assignments, receipts, implementation,
verification, waiting, submission and accepted delivery. They show the task,
reported wait owner, dependencies, next action and evidence time. Missing,
earlier-binding and conflicting evidence remain explicit. Unknown delivery
outcomes require reconciliation; inspecting a card never retries a send.

For module or substantial-part milestones, use `fleet_progress.py` with
`--kind progress-updated --phase verifying` (or `implementing` / `submitted`).
A `test-result` remains separate evidence and cannot advance the assignment.
`--blocker-owner` and `--resume-condition` also work without a native goal.
Observed waiting time sums only matching blocker reports at most three minutes
apart; longer gaps and time since the last report are excluded. Reports remain
milestone-based; no frequent heartbeat or per-function gate is required.

Tracker closure alone may mean cancelled or superseded work. A workflow state
can optionally set `acceptsDelivery: true` through the state editor or an
import, followed by a reviewed deployment. It must explicitly match closed
tickets and cannot be the fallback. Only fresh tracker evidence matching that
deployed rule and a current submitted assignment produces **Accepted in
tracker**. Partial/stale reads, earlier assignments and conflicting active
reports cannot establish acceptance. Omitted flags preserve existing v1
workflow digests.

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

Codex session token counters are read from native metadata; Claude counters come
from transcript usage records. Session totals include earlier work; the separately displayed
observed usage counts positive increments since the first sample. Counter resets
are flagged and shared sessions counted once. Increments across downtime are
included when the counter remains monotonic. Unavailable/stale sessions are
excluded from totals with coverage shown. Goal usage/budgets come from the native
goal record. These are token counts, not billing costs or ticket attribution.

GitLab CI or GitHub Actions refreshes in the background about every 90 seconds,
showing the latest 10 project pipelines/runs across branches, their commit, ref
and status, plus duration and queue time when available. GitLab supplies published
test totals for the latest pipeline when available; GitHub Actions does not expose
test totals through this adapter. Changing providers invalidates cached evidence.
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

Connect a project through **Backlog → Project & links**. The tracker is
detected from the URL (`github.com` is GitHub, anything else GitLab); choose
GitHub explicitly for GitHub Enterprise. The tracker refreshes about every 90
seconds in the server, including while the browser is closed. Session
observations and delivery checks run approximately every five seconds.

Adapters in `studio_trackers.py` expose `snapshot(config)`, `history(config, iid)`
and `pipelines(project)` with one normalised shape. GitHub issues use the same
label conventions, and `open` maps to `opened`, so workflow ticket states apply
unchanged. Pull requests are excluded from tickets. GitHub Actions runs map
onto the pipeline view; GitHub has no native test-report summary, so test
totals come from agent-reported evidence. Optional `backlog.legacyReferences` maps agent IDs
to local status files; those files supply display references only.

## Design and deploy

Import `workflows/sample.workflow.json` or build roles, handoffs, ticket-state
mapping, policies and triggers in the editors. The sample is a generic reviewed
delivery process with structured progress reporting.

1. Edit the draft, role assignments and communication contracts. A role may
   constrain its runtime (allowed backends, local or remote host); deployment
   is refused while an assigned agent breaks that constraint.
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

Existing workflow format version 1 remains supported. The optional role
`runtime` field (`{"backends": [...], "location": "any|local|remote"}`) is
omitted when unset, so earlier releases keep their digests. Importing the sample does
not replace an active release until explicitly deployed. Existing local
configurations, releases, acknowledgement records and delivery identities remain
in their data directory; none are distributed with this repository.

## Bounded goals

Progress → Goals shows observed native objectives, ticket links, completion
criteria, blocker owners/dependencies/resume conditions and token usage. A role
is a continuing workflow responsibility; a goal is one authorized work packet.
Goal age uses the meaningful-report threshold to flag review, never automatic
clearing. Missing native data is shown as unknown; removed/replaced goals remain
in history and are not inferred to be complete.

Link a goal in the UI, or add `--goal-id` and `--completion-criteria` to a
substantive `fleet_progress.py` report. Goal-linked blocked reports also require
`--blocker-category`, `--blocker-owner`, `--resume-condition`, `--reason` and
`--next-action`; `--depends-on` can be repeated. Receipt and test reports omit
goal fields. An `unblocked` report records dependency resolution without
resuming the native goal. Operator links are labelled separately from reports.

Complete, cancel and resume each require ticket evidence, an explicit decision,
a fresh preview and final apply. Close the session at a safe boundary and empty
its queue first. Complete/resume use supported Codex app-server metadata APIs
without setting an objective or budget, preserving usage. Cancel clears the
native goal and retains its evidence and usage snapshot in Studio. Resume is
available for blocked/paused goals; it does not override budget/usage limits or
launch a session. None of these actions changes tracker state or acceptance.

The installed Codex protocol lacks an atomic expected-goal check. Studio checks
identity, binding, release, evidence, queue and process state before apply, then
rechecks immediately before the native write. Keep other clients away from the
closed session during apply; that external race cannot be eliminated by Studio.
Intent and outcome are retained in `.runtime/goal-actions.json`. Timeouts or
interrupted actions remain unknown and are never retried automatically. Goal
records and action receipts survive observation retention.

`POST /api/goals/link` records an operator link; `/api/goals/preview` prepares a
decision and `/api/goals/apply` consumes its `previewId`. All writes require the
instance token and local-origin checks. `--no-launch` disables native apply.
The Goals view offers a policy-only `bounded-goals` deployment after reporting
is set up; it rejects unrelated draft changes and preserves existing stops,
roles, routes and automation pause state. Agents acknowledge the new release
at their next safe boundary. Deployment itself sends no messages.

The policy-only `coordination` deployment adds final-integration coordination:
the Integrator records a bounded reservation on the current ticket with owner,
candidate/base revisions, affected files/interfaces, tested revisions and a
review/expiry time. Overlapping changes, including externally managed work,
are sequenced with that owner. Changed candidates require an applicability
assessment and refreshed affected evidence. This is an agent workflow policy;
Studio does not enforce Git locks or assume an external contributor agreed.

For an unsent handoff held by a policy-only release, **Reconcile receipt or
source → Revalidate an unsent event** offers a separate evidence review. It
requires unchanged routing contracts and source/recipient bindings, a verified
original trigger and no prior dispatch attempt. Apply retains the event ID,
message and audit trail, then returns it to normal delivery with adoption and
availability checks. It neither creates a receipt nor repeats technical review.

## Validation

```powershell
python -B -m unittest discover -s tests -p 'test_*.py'
npm ci --ignore-scripts
npm run test:layout
npm run test:ui
python -B workflow_spec.py workflows/sample.workflow.json --output workflows/SAMPLE_WORKFLOW.md --check
```

Browser tests use Playwright with installed Microsoft Edge (remove the
`channel` option to use bundled Chromium elsewhere) and create synthetic
fleets in temporary directories. Real agent launches/controls and live tracker
reads are disabled. Backend tests mock native sends. Screenshots and generated
test results are local artifacts and are not committed.
