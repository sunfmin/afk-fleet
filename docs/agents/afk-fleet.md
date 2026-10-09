# afk-fleet config (sunfmin/afk-fleet)

Per-repo config consumed by `/afk-fleet` at bootstrap via `afk config --file <this file>`, which
validates every key against the one schema (unknown key or wrong shape → error) and emits the
canonical JSON every tick and tool consumes (ADR-0009). Anything omitted uses the default — so this
file sets only the keys where this repo differs, and a default that moves is followed. The base
branch (ADR-0042) and the worker launch command (ADR-0010) are intentionally NOT here: both are
settled at bootstrap.

```yaml
# Only what differs from the defaults (`afk config --defaults` prints those; the
# annotated list is skills/afk-fleet/references/config-template.md).

# --- workers ---
concurrency: 10

# --- completion gate ---
gate:
  ci: local
  local_command: "uvx ruff@0.16.10 check && uvx ty@0.0.85 check && uv run --with pytest --with pytest-xdist pytest skills/afk-fleet/scripts -q -n auto"
```

## Notes

- **The local gate is the gate.** This repo has no GitHub Actions and requires no status check on
  its merge target, so `gate.ci: local` (ADR-0012): `gate.local_command` is run by the worker before its PR
  and by its landing on the head that lands — no `no_checks` judgment per PR, and nothing lands
  ungated. A landing whose sync moved nothing does not run the gate a second time: the worker's
  green run is on record for that tree (ADR-0030).
- **Types first.** Before the tests, `gate.local_command` holds the production scripts to their
  types (ADR-0039): `ruff` that every function states them, `ty` that they agree. Seconds, against
  the suite's minute and a half, so a type error is red at once. `ruff.toml` and `ty.toml` say what
  is checked; the versions are pinned here, since a new checker finds new errors and nothing should
  turn the gate red but a change to the repo.
- **Local gate.** `gate.local_command` then runs the skill's tests via `uv`, per the repo's uv-only Python
  rule, in parallel: 71 s against 488 s serial, measured on 2026-10-05 on a 14-core Apple M4 Pro
  (Mac16,8). That is a snapshot, not a promise — the suite grows. It is a real gate for the code-touching issues and a no-op pass for
  prompt/docs-only issues.
- **Sync, not rebase.** A landing merges `base_branch` into the branch before its re-gate
  (ADR-0012, ADR-0027) — always: there is no key for it (ADR-0038).
- **Reserved surfaces.** The fleet manages the `afk-attempt/<n>` labels, the `refs/afk/*` ref
  namespace (`refs/afk/claim/*`, `refs/afk/heartbeat/*`, `refs/afk/base`), the single `<!--afk:status-->` status-board
  comment, and the `<!--afk:turn …-->` landing-turn comment on a PR. Don't hand-edit them.
