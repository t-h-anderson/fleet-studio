# Fleet monitoring

The tracker owns task state and acceptance. CI publishes pipeline/test outcomes.
Agents provide local test evidence and meaningful progress reports. Native session
metadata provides activity and token counters. Each observation retains its source;
missing or stale information must remain visible.

## Available now

| Measurement | Source and interpretation |
| --- | --- |
| Open backlog and queues per state | Latest tracker projection, with configured scope and partial/stale indicators. |
| Backlog trend | Latest sample per UTC hour; up to 30 days, bounded by configured retention. The chart breaks across missing/partial samples. |
| Ticket time in each state | Sum of consecutive matching observation intervals; excludes initial history, gaps over 180 seconds and transition intervals. Includes revisits, separated by deployed workflow. Not recorded effort. |
| Unassigned open tickets | Missing tracker assignee; this does not infer agent ownership from session activity. |
| Closures in 24 hours / 7 days | Distinct tickets with observed tracker closures; initial closed tickets are not counted as newly completed. |
| Agent availability | Current working, idle, closed, attention or unknown session observations. Idle does not establish spare capacity or permission to assign work. |
| Token usage | Native cumulative session counters, increments since monitoring, and native goal usage/budget. Session counters are deduplicated and resets flagged. No cost estimates. |
| GitLab CI | Latest 10 project pipelines; status, branch and SHA, latest pipeline duration/queue time and published test summary. No pass inference from absent reports. |
| Local tests | Agent-reported suite, status, full commit SHA, optional counts/duration and evidence. Reporting never executes tests or clears a blocker. |
| Communication delay | Pending receipts, oldest pending handoff, overdue thresholds, queue delivery and acknowledgement shown separately. |
| Adoption and data freshness | Current binding acknowledgements, workflow drift, last successful tracker/CI refresh, errors and gaps. |
| Execution blockers versus other holds | Current structured blocker reports or claimed blocked tasks count as execution blockers; restrictions, deferred/sign-off work and unclaimed dependency holds are separate. No label or ownership changes. |
| Workflow availability | Distinct from session activity: an idle session with a blocked, paused or limited goal remains held, with its current reason or an explicit missing-reason notice. |
| Bounded goal evidence | Exact native goal identity, ticket, done criterion, blocker category/owner/dependencies/resume condition and usage. Quiet goals flag a review; stops are never automatically cleared. Missing or replaced goals are not assumed complete. |
| Goal recovery audit | Reviewed complete/cancel/resume intent, ticket evidence and verified or uncertain outcome. Native usage is preserved for status changes; cancellation retains a Studio snapshot. No automatic retry. |
| Receipt reconciliation | Operator-recorded acknowledgement evidence and source repair retain the original event identity and prior delivery state. They do not repeat delivery or confer technical acceptance. |

| Agent health | Turn telemetry classifies each agent as looping, stalled, admin-heavy, burning, waiting, productive, idle or offline, with the evidence. State changes are recorded as `health` observations and used for automation backtests. |
| Tool-call mix | Each tool call is classed as work, explore, admin, wait or other from its name and a short command or path signature. Outputs and contents are not stored. |
| Wasted tokens | Fresh tokens (input, output and cache writes) in repeated, admin and polling calls, aborted turns and Studio reprompts that produced no work. These are estimates, split by each turn's call mix. |
| Automation outcomes | Each automatic reprompt, hand-off, pause or escalation, with its delivery result and whether the agent recovered within 30 minutes. |
| Throttled time | Time automatic sends waited because of concurrency or token-per-hour limits. |

## Recommended next measurements

| Priority | Measurement | Decision it supports | Required evidence |
| --- | --- | --- | --- |
| 1 | Median / p90 lead and cycle time | Identify slow stages and estimate completion ranges. | Complete transition spans; separate ongoing work and incomplete histories. GitLab label/state history can backfill, with mapping version recorded. |
| 1 | Blocked-time share and reasons | Remove the constraints consuming the most time. | Structured blocked/unblocked reports or explicit tracker transitions; group by reason and dependency. |
| 1 | Review/verification queue age | Locate overloaded reviewers or scarce test runtimes. | Entry/exit timestamps and responsible role, including explicit resource reservations. |
| 1 | Test-evidence freshness | Find changes without relevant verification. | Exact candidate SHA, suite scope, clean/dirty worktree identity and linked local/CI results. Show missing required suites separately from failures. |
| 2 | Arrival rate versus throughput | See whether the backlog is growing faster than delivery. | Created/closed/reopened events, fixed time windows and stable scope. |
| 2 | Reopen / rework rate | Find repeated defects or unclear handoff requirements. | Closure/reopen and review-return transitions with reason codes. |
| 2 | Handoff acknowledgement p50 / p90 | Distinguish communication delay from execution delay. | Dispatch, queue acceptance, receipt and work-start timestamps; account for still-waiting handoffs. |
| 2 | Token burn rate and budget runway | Spot expensive runs and budget pressure early. | Timestamped token increments, stable sampling coverage and goal budgets. Ticket costs additionally need explicit work attribution and versioned pricing. |
| 2 | Test duration, failure and flake trends | Identify slow or unreliable verification. | Comparable suite identities and repeated runs at the same revision/environment. A rerun that passes alone does not prove flakiness. |
| 3 | Workflow changes versus outcomes | Evaluate whether routing or role changes help. | Release-tagged timing, throughput, rework and workload observations with comparable scope. |

Start with cycle time, blocked-time share, verification queue age and evidence
freshness. Use metrics to investigate bottlenecks; avoid ranking agents by tokens,
message count or activity alone. Alert rules should name a condition, a duration,
the evidence to inspect and an owner, with deduplication and acknowledgement.
