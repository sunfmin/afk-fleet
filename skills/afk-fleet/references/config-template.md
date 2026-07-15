# afk-fleet config (per-repo)

Copy this into the **target repo** at `docs/agents/afk-fleet.md`. The fleet reads it on startup.
Everything is repo-specific here; the skill core is repo-agnostic. Anything omitted uses the
default shown. `authorize` is intentionally NOT a config key — push+auto-merge is confirmed
interactively at launcher startup for the whole run (each tick inherits it), never pre-armed in a file.

```yaml
# --- dispatch contract ---
ready_label: ready-for-agent          # a child issue is dispatchable when it carries this
                                      #   (a human reserves an issue by REMOVING this label)
epic_labels: [epic, prd, wayfinder:map]   # never dispatched (a PRD is not a worker task)
claim: ref                            # atomic lock ref refs/afk/claim/<n> marks an issue as taken —
                                      #   replaces assignee; required for cooperating multi-fleet (ADR-0003)
dependencies: native                   # GitHub native blocked_by (open blockers gate dispatch)

# --- workers ---
base_branch: main
branch_pattern: "issue-{number}-{slug}"   # worktree-NAME hint passed to `orca worktree create --name`;
                                      #   orca sets the real branch (prefixed <user>/…) — ADR-0005
worker: orca                           # the only supported backend: orca creates the worktree + branch
                                      #   and spawns a real Claude Code in it, in one step (ADR-0005)
concurrency: 3                         # max workers running at once
worktree_cleanup: true                 # after merge/escalate, remove via `orca worktree rm issue:<n>`

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
escalate_label: ready-for-human        # applied (with ready_label removed, claim ref deleted) on give-up
escalate_comment: true                 # comment the stuck-point + PR/log links

# --- loop (launcher pacing) ---
busy_interval_seconds: 90              # re-tick soon (~1.5 min) when the last tick had work / in-flight PRs
idle_interval_seconds: 1500            # slow re-tick (~25 min) when idle
idle_ticks_before_sleep: 3             # this many empty ticks (frontier empty + no in-flight) → idle cadence
claim_lease_ttl_seconds: 4500          # a claim is live while its owner's heartbeat is this fresh (~75 min,
                                      #   3× idle). A peer may reclaim only a staler claim; while holding a
                                      #   claim the launcher never sleeps past ttl/2 so the lease can't lapse.
```

## Notes

- **Progressive gate.** Before the repo has a build/test/CI, the gate degrades to "the issue's own
  acceptance criteria + whatever build/test exists." `gate.ci: required` starts enforcing once the
  earliest issues have stood up CI.
- **Adversarial verify** is the pluggable, domain-specific half. Leave it `false` for plain software
  repos; turn it on for content/correctness repos where a machine gate can't catch a wrong answer.
- **Deploy is out of scope.** The fleet's mandate ends at a green merge to `merge.target`. Deploying
  (secrets, live infra) is never done by the fleet.
- **Reserved labels & refs.** The fleet manages, durably in GitHub, the `afk-attempt/<n>` labels
  (retry count) and the hidden `refs/afk/*` ref namespace — `afk-claim/<n>` (the claim, one per owned
  issue) and `afk-heartbeat/<id>` (per-instance liveness). This is what keeps ticks stateless and lets
  fleets cooperate (see the skill's "Why it runs forever" and ADR-0003). Don't hand-edit them or reuse
  the `afk-attempt/*` or `refs/afk/*` prefixes. If an org ruleset forbids non-branch refs, the fleet
  falls back to `refs/heads/afk-claim/*` at bootstrap and warns that `on: push` CI will then fire.
