# afk-fleet config (sunfmin/afk-fleet)

Per-repo config consumed by `/afk-fleet` at bootstrap via `afk config --file <this file>`, which
validates every key against the one schema (unknown key or wrong shape → error) and emits the
canonical JSON every tick and tool consumes (ADR-0009). Anything omitted uses the default. This repo's
trunk is `master` (not `main`), so `base_branch` and `merge.target` are set accordingly. The worker
launch command and the run authorization are intentionally NOT here (settled interactively at
bootstrap; ADR-0010).

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
concurrency: 3
worktree_cleanup: true
worker_idle_grace_seconds: 300

# --- completion gate ---
gate:
  ci: required
  local_command: "uv run --with pytest pytest skills/afk-fleet/scripts -q"
  adversarial_verify: false
  adversarial_verify_prompt: ""

# --- merge ---
merge:
  strategy: squash
  target: master
  rebase_before_merge: true
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

- **No CI yet.** This repo has no GitHub Actions; `gate.ci: required` degrades progressively to the
  issue's acceptance criteria + the local gate below until CI stands up.
- **Local gate.** `gate.local_command` runs the skill's fixture tests (pure verdicts in
  `afk_decide.py`) via `uv`, per the repo's uv-only Python rule. It is a real gate for the
  code-touching issues and a no-op pass for prompt/docs-only issues.
- **Reserved surfaces.** The fleet manages the `afk-attempt/<n>` labels, the `refs/afk/*` ref
  namespace (`afk-claim/*`, `afk-heartbeat/*`), and the single `<!--afk:status-->` status-board
  comment. Don't hand-edit them.
