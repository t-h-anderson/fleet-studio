# Fleet Studio

A local application for designing, deploying and monitoring supervised
multi-agent workflows. The external tracker (GitLab or GitHub) owns tasks and
acceptance; Studio owns workflow definitions, agent bindings and communication
receipts. Monitor Codex CLI or Claude Code sessions on this computer or remote
machines running a fleet node. Managed workflow deployment currently supports
local agents; remote assignment delivery is still pending.

Studio is built around three ideas:

- **Agents pursue goals, not process.** Each role has a goal, success criteria
  and a done condition. Each agent gets a brief of about 300 words with only
  its own goal, scope and hand-offs. Agents report only *done*, *blocked* and
  *hand-off*.
- **Studio observes instead of asking.** Turn telemetry shows what each agent
  is doing, and health detectors flag agents that are looping, stalled,
  admin-heavy or burning tokens.
- **Recovery is automatic and self-limiting.** Automations reprompt with
  evidence, hand work to a healthy agent, pause or escalate, then stop after a
  limit. Throttles cap concurrency and token spend.

## Agent health, automations and throughput

**Agent health** is the default view. Each agent shows:

- its state, how long it has been in that state, and why;
- fresh tokens per hour;
- its call mix (work, explore, admin, waiting);
- estimated waste.

Expand a row to see the recent turns and their tool calls. **Nudge** sends a
message now. **Tweak role** opens the role's goal and brief.

| State | Detected when (defaults, per fleet `health` settings) |
| --- | --- |
| Looping | Any one of: the same failing command runs 4+ times within an hour; a 1–4 step cycle repeats; the same reply is given in 3 of the last 6 turns; or 3 Studio prompts in a row produced no work |
| Stalled | Idle for 20+ minutes with assigned open work, or a working turn with no new calls or tokens for 10+ minutes |
| Admin-heavy | 40%+ of recent tool calls are reporting, polling or tracker admin |
| Burning | 400k+ fresh tokens in an hour without any edit, build or test |
| Waiting | A blocked report or tracker blocker is in force (it stays until cleared) |

**Telemetry.** Studio parses every turn in local Codex rollouts and Claude Code
transcripts. For each tool call it keeps:

- the tool name;
- a short signature: the first line of the command, or a file path or
  pattern, up to 160 characters;
- a class;
- an error flag.

It also keeps token components and hashes of replies. Tool outputs, file
contents, reasoning and message text are **not** stored. Remote agents keep
their transcripts on their node, so they show session status only.

**Automations** act on health signals, and changes apply on the next check
without a deployment. Each rule has these parts:

- the role to watch;
- the signal and how long it must last;
- an action: reprompt with evidence, hand off to a healthy agent, stop a
  Studio-started turn, pause, or escalate;
- a cooldown;
- a limit on how often it may fire per agent in a rolling window;
- one fallback action once that limit is reached.

**Add recommended** creates rules for loops, stalls, admin-heavy agents and
token burn, scoped to a role you choose. They start switched off, each with a
**Backtest** that replays recorded health history to show how often it would
have acted. Each rule card shows its last action and why it did or did not
deliver. Each automatic action records its outcome: recovered (the agent did
real work afterwards), changed or no change. A recipient that is not ready
yet (mid-turn, open in a client, throttled) leaves the action waiting rather
than failed, and a hand-off with nobody available escalates to you.

**Throttles** set a maximum number of working agents and token budgets per
hour, for the whole fleet and per role. They apply to every automatic send.
Throttled time is recorded. Escalations appear in Progress and can be posted
to a Slack-compatible webhook.

**Throughput** shows:

- tickets closed;
- fresh tokens;
- estimated waste by cause (repeated steps, admin, polling, abandoned turns,
  unproductive reprompts);
- tokens per closed ticket;
- throttled time;
- which automations helped;
- a per-role table of stuck agents, queues, tokens and call mix.

The **Design board** marks roles with stuck agents and colours each agent chip
by health.

Automatic delivery no longer waits for adoption acknowledgements.

- **Codex:** messages are queued while the session is open.
- **Claude Code:** a closed session receives an automatic headless turn. Studio
  still never starts a turn while one is running or while a native client
  holds the session.

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

Claude agents read their brief with `workflow_client.py instructions` once per
session; Studio tells them when it changes. Automatic entry-point
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

### Typed assignment contracts

Formal events may carry an optional version-1 JSON assignment envelope. Start
from [sample.assignment.json](workflows/sample.assignment.json), fill the current
deployed agent/release/session/folder/role binding, and consult
[assignment.schema.json](workflows/assignment.schema.json) for the format.
Specify relative files/directories, the deliverable, named completion
requirements, dependencies and canonical evidence links in the configured
project. The envelope documents scope; it cannot grant source permissions,
enforce filesystem custody, run scripts or change acceptance policy.

Use `fleet_event.py --contract-file assignment.json --preview` with the usual
event, recipient, ticket, artifact and context arguments to validate and inspect
the message without sending or reserving anything. Omit `--preview` to record
the event through its existing configured delivery mode and stop/adoption
guards. Contract delivery rechecks bindings and optional `baseRevision` (the
recipient checkout's exact local HEAD) immediately before the native effect.
An optional `candidateRevision` binds subsequent submission/test evidence to
that exact SHA. Neither field runs a test or infers correctness.

Milestones for typed assignments supply `fleet_progress.py --contract-id <id>`.
Submission (`work-finished` or `--phase submitted`) also supplies
`--completion-evidence-file evidence.json`, an object mapping every requirement
ID to a project evidence URL, and `--revision <full-SHA>` when the contract names
a candidate. Progress and blocker reporting do not require completion evidence.
Evidence presence is checked; technical acceptance stays in the tracker.
Progress cards show outstanding requirements before submission.

An envelope replacement uses a new ID and names the current ID in `supersedes`.
Changed candidates require new evidence; old IDs cannot be reused for different
events. Original retries and earlier handoff receipts remain idempotent after
replacement or restart. Typed envelopes and their compact identities survive
ordinary history retention, including withdrawal; this never rolls back files
or external processes. Legacy handoffs keep their existing digests and behavior
when no typed assignment is active. Module/substantial-part acceptance remains
the reporting boundary.

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

Agents report only what Studio cannot observe:

```shell
python workflow_client.py report done --fleet <fleet-id> --agent <agent-id> --ticket <ticket-url> --note "What changed and where"
python workflow_client.py report blocked --fleet <fleet-id> --agent <agent-id> --note "What I need, and from whom"
python workflow_client.py report handoff --fleet <fleet-id> --agent <agent-id> --to Integrator --note "What to pick up"
```

Studio fills in the agent's identity, release, current ticket and event IDs.

- **Blocked** stays in force until the agent reports done or hand-off, so it is
  never repeated.
- **Hand-off** first looks for an enabled event trigger from the agent's role.
  Otherwise it goes directly to the healthiest agent in the named role, or to
  the named agent.
- **Receipts** are observed: a hand-off counts as received when the recipient
  starts its next turn.
- **Done and blocked** reports are also recorded as tracker-linked milestones
  when a release and ticket exist.

`fleet_progress.py` and `fleet_event.py` remain available for existing scripts
and typed assignment contracts.

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
FLEET_PYTHON=python3 npm run test:ui
python -B workflow_spec.py workflows/sample.workflow.json --output workflows/SAMPLE_WORKFLOW.md --check
```

Browser tests use Playwright's Chromium; set `FLEET_BROWSER_CHANNEL=msedge` to
use installed Microsoft Edge. They create synthetic
fleets in temporary directories. Real agent launches/controls and live tracker
reads are disabled. Backend tests mock native sends. Screenshots and generated
test results are local artifacts and are not committed.


### Authorized dependency continuations and recovery

An optional assignment-contract `continuation` records bounded permission to notify the same recipient when all dependencies satisfy the deployed tracker acceptance rules:

```json
"continuation": {
  "authorized": true,
  "when": "dependencies-accepted",
  "message": "Recheck the dependency evidence and continue the assigned module if current stops allow."
}
```

It requires nonempty dependencies, a durable receipt for the original assignment, fresh complete tracker observations for the task and every dependency, and current adoption/session/queue/goal eligibility. Dependencies are included in AI-filtered reads. Closure without an explicit `acceptsDelivery` rule remains insufficient. A blocked work report must explicitly use `blockerCategory: "dependency"` and exactly the authorized dependency set; approval, restriction, host and unknown blockers remain held. A notification never resumes a goal, changes ownership or accepts work.

Periodic reconciliation also handles dependencies already accepted when the original receipt arrives. One stable intent is reserved per immutable contract. Repeated snapshots, restart and display-history retention cannot create another. Review-mode handoffs retain review mode. Eligibility is checked again immediately before the native effect. A continuation has its own receipt and `continuationOf` link; it never becomes a new assignment or resets original milestones. Paused/blocked/limited goals and busy queues remain held. Superseded, withdrawn, submitted or changed-release continuations are invalidated. Compact reservations distinguish unsent, held, sending, delivered, uncertain and invalidated outcomes.

Handoff details now join dispatch receipts, recipient receipts and matching work milestones into a recovery checkpoint. A received or submitted obligation suppresses a prepared send at its final effect boundary. The submitted marker survives subsequent reports and restart; changed work needs a new explicit contract. Target queue acknowledgement is retained even if aggregate receipt finalization was interrupted. Unknown outcomes always require evidence reconciliation and are never blindly replayed. Checkpoints describe observed facts; they cannot restore processes, file handles, checkout state or guarantee exactly-once external execution.

Reviewed policy-only release revalidation preserves the original envelope, digest and event identity. An explicit proof allows reporting under the new release after unchanged routing/scope/bindings are checked. Received typed assignments can be revalidated without resending; unsent work still passes current adoption and delivery guards. Compact event identity remains usable after display-history pruning. A changed binding or work contract requires a new assignment.


### Current context and resolved notices

Open an agent's Progress details and choose **Current context**, or run:

```powershell
python fleet_context.py --fleet <fleet-id> --agent <agent-id>
```

The default CLI prints bounded text including its pagination metadata (default 12000 characters, `--max-chars` between 2000 and 16000). `--json` is an explicit full structured inspection mode; its text field is bounded, while metadata and duplicate structured items add output. Context supplements the full immutable deployed policy document; read that document before acting. Every page repeats release/binding and pause/goal observations, and preserves explicit-stop and source-permission requirements. It includes current contracts, unresolved obligations, matching milestones and canonical evidence instead of copying transcripts. Earlier superseded handoff instructions are replaced by reconciliation references. No context request sends a message or starts work.

Follow `nextCursor` using `--cursor`; changed assignment/stop/evidence snapshots reject old cursors. Poll-only timestamps do not invalidate a page. `--since <fingerprint>` returns the safety header and an explicit unchanged result when facts match. Oversized obligations produce an overflow reference and required size; retrieve the complete record with `--handoff <id>` before acting. An oversized header explicitly points to the full policy document. Unresolved work is never silently dropped to meet the bound.

**Preview notice retirement** shows up to 100 qualifying notices before applying a display-only change. A withdrawal must be proven unsent. Completed/superseded work requires receipt and submission evidence; current accepted work also needs fresh tracker acceptance under the deployed rules. Current withdrawal constraints and unresolved/uncertain effects remain visible. Apply rechecks the selected records and current binding/stop/evidence; changed facts require a new preview. Retired notices are hidden only while the retirement evidence remains valid. Reopened tasks, removed acceptance, later reports or stale acceptance observations restore their visibility for reconciliation. Their canonical records, receipt evidence, submission markers, supersession tombstones and deduplication IDs remain available through `/api/context/handoff`. No branch, file or canonical record is deleted.

### Reliability observations and scenarios

Monitoring includes durable counts of duplicate events/reports actually suppressed and manual reconciliations actually observed, with a counter-start timestamp. Retained delivery states and current blocker intervals are qualified separately; unavailable time stays unknown. These are neither lifetime totals nor estimates of saved time, cost or technical correctness.

The existing `python -m unittest discover -s tests` suite includes composed scenarios for accepted dependencies during a stop, repeated/out-of-order reports, restart around dispatch, uncertain native acknowledgement, changed assignments, policy changes, evidence retention and bounded-context pagination. `node tests/ui.mjs` uses the installed Playwright/Edge tooling for browser verification. All fixtures are synthetic and native boundaries are mocked; no model calls, tracker mutations, package downloads or MATLAB processes are needed. Acceptance remains at completed module/substantial-part boundaries.

### Opt-in native goal recovery

In Automations, select **Blocked by a transient model error** and **Resume the
same native goal**. No default rule enables this action. Profiles preserve it
with the other automation rules. Fleet and agent pauses remain in force.

Recovery requires a linked local Codex goal whose transition to `blocked`
coincides with a structured 502 or `rate_limit_exceeded` error in its latest
settled failed turn. The existing session must be open with an empty queue.
Reported blockers, paused/limited goals, missing evidence, unmatched tool calls
and any tools in the failed turn require operator review. Transcripts larger
than 8 MB also require review; recovery never guesses from a partial tail.

Studio activates the same goal through the existing native goal RPC, verifies
its objective, creation time, usage and budget, rechecks safety, then queues one
continuation message. A queued message confirms delivery, not resumed work.
The configured cooldown is a minimum; gateway errors wait at least one minute,
rate limits at least fifteen minutes, with exponential backoff. At most three
attempts are allowed per goal (or the configured smaller limit), across restarts
and rule edits, then Studio escalates locally for operator review. The rolling
window setting does not reset this per-goal cap. Unknown outcomes are retained
and never automatically replayed. Codex offers no atomic expected-goal write;
external clients can still race the final check. Closed sessions are not launched.

This feature does not deploy a workflow, enable a rule, or change live sessions.

### Subagent supervision

Local Codex parents now show their native descendants, including nested children,
in the expanded Agent health row. A parent with active children is shown as
**Subagents working**; silence while delegating does not trigger parent stall,
loop or token-burn interventions. The parent still owns delegation and integration.
Studio never assigns fleet roles to children or starts, stops or messages them.

Automatic messages, reassignment, interruption and native goal changes are held
while descendants are active, failed or have unsettled tool outcomes. Missing,
stale, cyclic or oversized relationship metadata is uncertain and also holds
intervention. Completed children restore ordinary parent supervision. Child
token usage is a separate lifetime total; it is not added to the parent's fresh
token or waste estimates. Raw child turn status remains visible alongside lifecycle
state. Native metadata is read only; prompts and child tool outputs are not retained.

This requires Codex's native `thread_spawn_edges` and turn/item metadata. A local
Codex without that table (or without a state database) is shown as "subagent
tracking unavailable": health detection, reprompts, hand-off deliveries and the
operator's Stop work as before, while automatic stops and native goal recovery stay
held because they need known descendant state. Read errors, cycles and truncated
graphs on a Codex that does record spawn edges remain uncertain and hold every
intervention. Remote nodes without descendant metadata keep their existing message
behavior. Graph observations are bounded to 500 descendants per parent;
larger graphs require review. No transcript tail is used to guess that children
have finished.
