# afk-fleet config (per-repo)

Copy this into the **target repo** at `docs/agents/afk-fleet.md`. The fleet reads it at bootstrap
through `afk config --file …`, which validates every key against the schema (an unknown key or
wrong shape is an **error**, caught with the human present) and emits the canonical JSON every tick
and tool consumes (ADR-0009). Everything is repo-specific here; the skill core is repo-agnostic.
Anything omitted uses the default shown. Two things are intentionally NOT config keys, and the
validator refuses both by construction:

- **`authorize`** — push+auto-merge is confirmed interactively at launcher startup for the whole run
  (each tick inherits it), never pre-armed in a file.
- **the worker launch command** — the string workers are started with (`ckimi`, `direnv exec . claude`,
  …) is *machine-local*, while this file is checked into the target repo and shared with the team: on a
  teammate's machine it is `command not found`, or worse, a same-named alias pointing at a different
  provider. It is settled at bootstrap by `afk worker-command` + one confirmation, and held only by the
  launcher (ADR-0010).

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
                                      #   (ADR-0005), then the fleet starts a real Claude Code in it with
                                      #   the run's worker launch command — NOT a key here (ADR-0010)
concurrency: 3                         # max workers running at once
worktree_cleanup: true                 # after merge/escalate, remove via `orca worktree rm issue:<n>`
worker_idle_grace_seconds: 300         # a no-PR worker that went idle is judged "finished" only after
                                      #   this much quiet (no commits, clean tree, no recent file activity);
                                      #   inside the window it's assumed still working between steps, so a
                                      #   finished-and-idle worker is never mistaken for one still coding

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

# --- progress (human-facing) ---
progress_comment: true                 # upsert ONE "status board" comment per issue — a progress checklist
                                       #   rendered from fleet state (claim + PR + checks + afk-attempt) so a
                                       #   human reading the issue sees how far along it is, including the
                                       #   otherwise-invisible "claimed, coding, no PR yet" phase. Edited in
                                       #   place, never appended; human-read only, never a tick input (ADR-0006).

# --- loop (launcher pacing) ---
busy_interval_seconds: 90              # re-tick soon (~1.5 min) when the last tick had work / in-flight PRs
idle_interval_seconds: 1500            # slow re-tick (~25 min) when idle
idle_ticks_before_sleep: 3             # this many empty ticks (frontier empty + no in-flight) → idle cadence
claim_lease_ttl_seconds: 4500          # a claim is live while its owner's heartbeat is this fresh (~75 min,
                                      #   3× idle). A peer may reclaim only a staler claim; while holding a
                                      #   claim the launcher never sleeps past ttl/2 so the lease can't lapse.
fingerprint_gate: true                 # each wake-up the launcher runs `afk fingerprint` (code, zero LLM
                                      #   tokens) and spawns a tick only when the digest of observable
                                      #   state (issues+labels, PRs+checks, claim refs) moved (ADR-0007)
force_tick_after_skips: 6              # safety net: a full tick at least every N skipped cycles — time-
                                      #   driven events (a peer's lease expiring) are invisible to any
                                      #   state hash. 1 disables skipping entirely.
```

## Notes

- **This template is pinned to the code.** The defaults table in `afk_decide.py`
  (`CONFIG_DEFAULTS`) is the single source of truth; a fixture test parses this file's yaml block
  and fails if any value here drifts from that table (ADR-0009). Edit defaults there, then mirror
  them here.
- **Progressive gate.** Before the repo has a build/test/CI, the gate degrades to "the issue's own
  acceptance criteria + whatever build/test exists." `gate.ci: required` starts enforcing once the
  earliest issues have stood up CI.
- **Adversarial verify** is the pluggable, domain-specific half. Leave it `false` for plain software
  repos; turn it on for content/correctness repos where a machine gate can't catch a wrong answer.
- **Set `gate.local_command` as soon as the repo can build.** The fleet's most expensive failure is
  a retry: a red CI gate tears the worker down and a *fresh* worker re-reads the issue, the docs,
  and the failure from scratch. A local `build && test` gate catches most failures inside the same
  worker session — a few fix-up edits instead of a full re-spawn plus a CI round-trip.
- **Fingerprint gate.** On a skipped cycle the launcher spawns no tick — its only cost is the tool
  call — and, while holding claims, refreshes the lease itself (`afk heartbeat`), so skipping never
  lapses a lease. Correctness never depends on the gate: a missed change waits at most
  `force_tick_after_skips` cycles (ADR-0007).
- **Deploy is out of scope.** The fleet's mandate ends at a green merge to `merge.target`. Deploying
  (secrets, live infra) is never done by the fleet.
- **Reserved labels, refs & the status comment.** The fleet manages, durably in GitHub, the
  `afk-attempt/<n>` labels (retry count), the hidden `refs/afk/*` ref namespace — `afk-claim/<n>` (the
  claim, one per owned issue) and `afk-heartbeat/<id>` (per-instance liveness) — and, when
  `progress_comment` is on, the single status-board comment tagged `<!--afk:status-->` (found and
  overwritten by that marker each tick). This is what keeps ticks stateless and lets fleets cooperate
  (see the skill's "Why it runs forever" and ADR-0003). Don't hand-edit them or reuse the
  `afk-attempt/*` / `refs/afk/*` prefixes or the `<!--afk:status-->` marker. If an org ruleset forbids
  non-branch refs, the fleet falls back to `refs/heads/afk-claim/*` at bootstrap and warns that `on:
  push` CI will then fire.
