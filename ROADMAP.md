# Review and roadmap

A review of Fleet Studio against six goals: multiple agent backends, local and
remote agents, GitHub and GitLab, workflow specification, review, and monitoring.
Part 1 records what this change generalised. Part 2 lists the defects found. Part 3
gives the suggested next steps, highest value first.

## 1. What changed

| Goal | Before | Now |
| --- | --- | --- |
| Multiple backends | Codex calls were spread across the server, controls, goals and activity | `studio_agents.AgentBackend` with `CodexBackend` and `ClaudeCodeBackend`. Each agent records a `backend`. Capabilities (queue, rename, goals, activity, tokens) let the UI and controls explain gaps instead of failing |
| Local and remote | Single Windows machine | `fleet_node.py` exposes observation and explicit controls over HTTPS or an SSH tunnel. Sessions are scoped by host, backend and native ID. Managed remote deployment is blocked pending a node assignment/reporting protocol |
| Platforms | Windows Terminal + PowerShell, Windows-only liveness | tmux launcher and `/proc`/`lsof` lock-owner detection on Linux and macOS. The existing PowerShell launcher is kept for local Codex on Windows |
| GitHub and GitLab | `glab` calls in backlog and metrics | `studio_trackers` adapters share one issue/history/pipeline shape. GitHub `open` maps to `opened`, PRs are excluded, Actions runs map onto pipelines. Issue-URL checks use `issue_prefix` |
| Workflow specification | Roles had no runtime requirements | Optional role `runtime` (`backends`, `location`). Deployment is refused while a bound agent breaks it. The field is omitted when unset, so existing release digests are unchanged |
| Safety | Release disabled agents whose session or folder changed | A changed backend or host also counts as a rebinding |

Compatibility: existing `fleet-studio.json`, releases, entry points and GitLab
configurations load unchanged. Generated agent text for GitLab fleets is
byte-identical, so installed entry points are not flagged as stale.

## 2. Defects found

Fixed in this change:

1. **Launcher path used the data root.** `Start-CodexFleet.ps1` was resolved
   from `--data-root` instead of the install folder, so a data root outside the
   checkout (which the README recommends) could not launch agents.
2. **Backlog settings dropped `legacyReferences`.** The settings form rebuilt the
   backlog object from scratch on every save.
3. **The test suite failed on non-Windows** (`WINDIR` lookup). Launch tests now
   pin the platform explicitly.
4. **Native IDs collided across hosts/backends.** Observation keys, token totals,
   session pickers and workflow binding checks now include host and backend.
5. **Controls could follow a changed binding after preview.** Dispatch keeps the
   reviewed target and host endpoint, rejecting later changes.
6. **Claude could start competing turns or report unknown ownership as idle.**
   Unknown ownership holds sends/launches, Studio serializes its own sends, and
   receipts distinguish process start from completion. Token usage includes cache
   reads and deduplicates streaming records.
7. **Remote redirects could forward credentials.** Redirects are refused; server
   errors after mutations retain an uncertain outcome. Nodes recheck ownership
   before terminal launch.
8. **Tracker changes could reuse old evidence.** Backlog and CI caches include
   provider identity. GitHub label history reads beyond its oldest API page and
   neutral CI conclusions remain neutral.

Observed, not changed:

- `GET /api/bootstrap` returns the write token to any local process that can
  reach the loopback port. That is acceptable on a single-user machine. On
  shared hosts, bind the token to a cookie set on first page load, or use a
  per-user Unix socket or named pipe.
- With no deployed ticket states, `stage_of` and the backlog role mapping
  hard-code `AI Ready`/`AI Claimed`/… labels and the `coordinator`/`writer`/
  `integrator` role keys. Move these defaults into a built-in workflow spec so
  every mapping is data.
- A backlog refresh reads up to 5,000 issues every 90 seconds. Use
  `updated_after`/`since` incremental reads, with a full sweep hourly.
- `fleet_studio.Handler` routes through long `if` chains, and most modules use
  very long single-line expressions. A route table, and adopting `ruff format`
  file by file, would make review much easier.
- Generated entry points show PowerShell syntax (`(Get-Location).Path`) to every
  agent. Render a POSIX variant for agents on Linux/macOS hosts.
- There is no CI configuration. Run the unit suite on Windows and Linux and the
  Chromium UI suite on Linux. The suite needs no network access.
- `package-lock.json` resolves from a private registry, so `npm ci` fails
  outside that network. Generate the lockfile against the public registry, or
  document the registry requirement.

## 3. Suggestions

### Agent backends
- **Claude Agent SDK / Managed Agents backend.** Cloud-hosted Claude agents
  with an event stream would remove transcript polling and give a real
  queue-like `send`.
- **Codex cloud, Gemini CLI, Aider, OpenHands.** Each needs only `observe`,
  `send` and `resume_command`. Add capability flags rather than special cases.
- **Model and cost metadata.** Record a per-model price table and show an
  *estimate* next to token counts, keeping the existing "not billing" label.
  Claude counts include cache reads; expose cache hit rate separately.

### Remote agents
- Deploy assignment files to nodes and provide authenticated reporting/adoption
  routes before enabling remote managed workflows. Local file paths and loopback
  endpoints cannot serve a remote agent.
- Show each host's health in the UI (last contact, node version, backends
  present) and warn when a node's version differs from Studio's.
- Let nodes push observations to Studio (long-poll or Server-Sent Events)
  instead of five-second polling across the network.
- Offer mutual TLS as an alternative to bearer tokens. Tighten the node with
  per-host allow-lists of folders and sessions.

### Trackers
- Read GitHub Projects (v2) status fields as an alternative to labels.
- Ingest PR/MR review state (approvals, requested changes, unresolved threads)
  and link it to tickets. Today review is visible only as agent-reported text.
- Use webhooks where Studio is reachable; keep polling as the fallback.

### Workflow specification
- **Checkable guards.** Prose guards are agent obligations today. Add an
  optional machine-checkable form (`ciStatus: success`, `labels: [...]`,
  `reviewApprovals: >= 1`). Studio could then hold a handoff until the guard is
  observably true, without ever executing scripts.
- **Cross-backend review.** A handoff option such as
  `reviewer.backend: different-from-source`, so Claude reviews Codex output
  and vice versa. Diverse reviewers catch more defects than a model reviewing
  its own work.
- **State SLAs.** Per-state time budgets (for example "Awaiting merge > 24 h")
  feeding the existing attention list.
- **Schema v2.** Collect these additions into `schemaVersion: 2`, keeping a v1
  reader for immutable releases.

### Review and monitoring
- Per-agent throughput: tickets closed, handoffs received and handoff latency
  by role and backend, so backends can be compared on the same workflow.
- Alert sinks (desktop notification, Slack or email webhook) for blockers,
  failed deliveries and stale goals. These are already computed; only delivery
  is missing.
- Export observations as OpenTelemetry metrics for teams with existing
  dashboards.
- A daily digest: what moved, what is blocked and on whom, tokens per ticket.
