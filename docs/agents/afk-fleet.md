# afk-fleet config (sunfmin/afk-fleet)

Per-repo config consumed by `/afk-fleet` at bootstrap via `afk config --file <this file>`, which
validates every key against the one schema (unknown key or wrong shape → error) and emits the
canonical JSON every tick and tool consumes (ADR-0009). Anything omitted uses the default. This repo's
trunk is not `main`, so `base_branch` and `merge.target` are set accordingly. The worker
launch command is intentionally NOT here (settled at bootstrap; ADR-0010).

```yaml
# --- dispatch contract ---
ready_label: ready-for-agent
epic_labels: [epic, prd, wayfinder:map]
claim: ref
dependencies: native

# --- workers ---
base_branch: master
branch_pattern: "issue-{number}-{slug}"
worker: orca
concurrency: 10
worktree_cleanup: true
worker_idle_grace_seconds: 300

# --- completion gate ---
gate:
  ci: local
  local_command: "uv run --with pytest --with pytest-xdist pytest skills/afk-fleet/scripts -q -n auto"
  adversarial_verify: false
  adversarial_verify_prompt: ""

# --- merge ---
merge:
  strategy: squash
  target: master
  sync_before_merge: true
  delete_branch: true

# --- failure handling ---
retry: 2
escalate_label: ready-for-human
escalate_comment: true

# --- progress (human-facing) ---
progress_comment: true

# --- loop (launcher pacing) ---
busy_interval_seconds: 90
idle_interval_seconds: 1500
idle_ticks_before_sleep: 3
claim_lease_ttl_seconds: 4500
fingerprint_gate: true
force_tick_after_skips: 6
```

## Notes

- **The local gate is the gate.** This repo has no GitHub Actions and requires no status check on
  its merge target, so `gate.ci: local` (ADR-0012): `gate.local_command` is run by the worker before its PR
  and by its landing on the head that lands — no `no_checks` judgment per PR, and nothing lands
  ungated. A landing whose sync moved nothing does not run the gate a second time: the worker's
  green run is on record for that tree (ADR-0030).
- **Local gate.** `gate.local_command` runs the skill's tests via `uv`, per the repo's uv-only Python
  rule, in parallel: 71 s against 488 s serial, measured on 2026-10-05 on a 14-core Apple M4 Pro
  (Mac16,8). That is a snapshot, not a promise — the suite grows. It is a real gate for the code-touching issues and a no-op pass for
  prompt/docs-only issues.
- **Sync, not rebase.** `merge.sync_before_merge` merges the merge target into the branch before the
  landing's re-gate (ADR-0012, ADR-0027); the retired `rebase_before_merge` key is now a load-time error.
- **Reserved surfaces.** The fleet manages the `afk-attempt/<n>` labels, the `refs/afk/*` ref
  namespace (`afk-claim/*`, `afk-heartbeat/*`), the single `<!--afk:status-->` status-board
  comment, and the `<!--afk:turn …-->` landing-turn comment on a PR. Don't hand-edit them.
