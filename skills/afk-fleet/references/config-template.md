# afk-fleet config (per-repo)

Copy this into the **target repo** at `docs/agents/afk-fleet.md`. The fleet reads it on startup.
Everything is repo-specific here; the skill core is repo-agnostic. Anything omitted uses the
default shown. `authorize` is intentionally NOT a config key — push+auto-merge is confirmed
interactively at launch, never pre-armed in a file.

```yaml
# --- dispatch contract ---
ready_label: ready-for-agent          # a child issue is dispatchable when it carries this
epic_labels: [epic, prd, wayfinder:map]   # never dispatched (a PRD is not a worker task)
claim: assignee                        # add-assignee @me marks an issue as taken
dependencies: native                   # GitHub native blocked_by (open blockers gate dispatch)

# --- workers ---
base_branch: main
branch_pattern: "issue-{number}-{slug}"
worker: orca                           # orca-cli spawns a real Claude Code in a git worktree
concurrency: 3                         # max workers running at once
worktree_cleanup: true                 # remove the worktree after merge/escalate

# --- completion gate ---
gate:
  ci: required                         # wait for GitHub checks green on the PR
  local_command: ""                    # optional pre-PR local gate the worker runs (e.g. "pnpm build && pnpm test")
  adversarial_verify: false            # set true for content repos: an independent agent re-derives
                                       # the result and refutes wrong output before merge (refute-first)
  adversarial_verify_prompt: ""        # what the verifier checks (e.g. "re-solve; assert final == official answer:")

# --- merge ---
merge:
  strategy: squash                     # squash | merge | rebase
  target: main                         # fleet stops here; deploy is a separate human-gated step
  rebase_before_merge: true            # serialized: rebase onto latest target, re-gate, then merge
  delete_branch: true

# --- failure handling ---
retry: 2                               # per-issue retries; count tracked via an afk-attempt/<n> label on the issue
escalate_label: ready-for-human        # applied (with ready_label removed, assignee cleared) on give-up
escalate_comment: true                 # comment the stuck-point + PR/log links

# --- loop ---
poll_interval_seconds: 1500            # idle re-poll cadence (~25 min) via ScheduleWakeup
```

## Notes

- **Progressive gate.** Before the repo has a build/test/CI, the gate degrades to "the issue's own
  acceptance criteria + whatever build/test exists." `gate.ci: required` starts enforcing once the
  earliest issues have stood up CI.
- **Adversarial verify** is the pluggable, domain-specific half. Leave it `false` for plain software
  repos; turn it on for content/correctness repos where a machine gate can't catch a wrong answer.
- **Deploy is out of scope.** The fleet's mandate ends at a green merge to `merge.target`. Deploying
  (secrets, live infra) is never done by the fleet.
- **Reserved labels.** The fleet manages `afk-attempt/<n>` labels itself to track each issue's retry
  count durably in GitHub — this is what keeps the coordinator stateless (see the skill's "Bounded
  coordinator context"). Don't hand-edit them or reuse the `afk-attempt/*` prefix for anything else.
