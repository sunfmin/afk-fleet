# One home for config: `afk config` validates, fills defaults, and emits canonical JSON

**Status:** accepted — applies [ADR-0004](0004-deterministic-mechanics-as-tools.md) to config
handling; completes the convention change [ADR-0008](0008-rebuild-as-one-observation-tool.md)
deliberately deferred ("flags now; one uniform convention change if/when `afk config` lands").

## Context

Defaults lived in three homes that disagreed about omission. The template's yaml comments promised
"anything omitted uses the default shown"; argparse carried its own literals (`--retry default=2`,
`--force-after default=6`, label strings); `afk_decide` had `.get(…, default)` fallbacks. They
disagreed in behaviour, not just location: omitting `busy_interval_seconds` crashed `pace` with a
KeyError, omitting `claim_lease_ttl_seconds` **silently disabled the ttl/2 pacing cap** (the guard
ADR-0003 calls load-bearing), and `concurrency: 3` existed only in prose with no code backstop at
all. On top of that, the config-key→flag rename table (`claim_lease_ttl_seconds`→`--ttl`,
`force_tick_after_skips`→`--force-after`, YAML list→csv) was written down nowhere — pure LLM-head
knowledge, re-derived by every fresh tick.

## Decision

- **One table.** `CONFIG_DEFAULTS` in `afk_decide.py` is the single source of truth for every key,
  default, and (via the default's type) shape — `concurrency` included. `authorize` and the
  instance id are deliberately absent: per-run, launcher-held facts, never file-armed.
- **`afk config --file docs/agents/afk-fleet.md` → canonical JSON.** It extracts the ```yaml block,
  parses it with a **schema-aware zero-dep reader** (the dialect this schema uses: scalars, one
  inline list, one-level `gate:`/`merge:` sections), validates every key and type against the table
  — **unknown key or wrong shape is an error**, raised at bootstrap with the human present — and
  emits the complete config. `--defaults` prints the pure table. Missing file → error; the
  launcher's existing offer-template-and-stop flow handles it.
- **The canonical JSON travels verbatim.** Launcher → tick → every config-consuming subcommand
  (`rebuild`, `fingerprint`, `heartbeat`, `next-attempt`, `pace`) via `--config`. Argparse literal
  defaults are deleted; individual flags remain as test/debug **overrides**. Resolution is one
  order everywhere: flag → `--config` → `CONFIG_DEFAULTS`. The rename table is dead — nothing
  re-derives flags from key names.
- **Uniform omission semantics.** `pace` resolves partial config through the table, so omission
  defaults instead of crashing and the ttl/2 cap can no longer be silently disabled. The template's
  "default shown" promise is now true for every key.
- **The template is gated, not generated.** `references/config-template.md` keeps its hand-written
  comments; a fixture test parses it with the same reader and fails if any value drifts from
  `CONFIG_DEFAULTS` (or if a schema key is missing from it) — the one unavoidable hand-sync,
  gated.

## Considered and rejected

- **PyYAML.** Full YAML for ~40 fewer lines, but the fleet's first third-party dependency, needed
  on every machine a fleet runs on. Our schema is deliberately shallow; a reader that refuses
  anything outside it is *more* correct for this job, not less. Rejected.
- **Switch the config format to TOML** (`tomllib` is stdlib). Requires Python ≥ 3.11, migrates
  existing target-repo config files, and re-teaches the template for no behavioural gain. Rejected.
- **Warn-and-ignore unknown keys.** Forgiving on version skew, but a typo'd key silently ignored is
  a config that lies to its author — the fleet then runs unattended for days on a default the human
  believes they overrode. Config is loaded once at bootstrap with the human present; that is where
  strictness is free. Rejected.
- **Flags-only (`afk config` serves just the launcher).** The rename table survives inside the
  tick's prose — the drift moves house instead of dying. Rejected; ADR-0008 pre-authorized the
  flip.
- **Generate the template from the table.** Kills the drift equally, but loses the template's
  hand-written per-key commentary (its real value) or forces comments into code strings. The gated
  hand-sync keeps both. Rejected.

## Consequences

- New: `CONFIG_DEFAULTS`, `parse_config_yaml`, `resolve_config` in `afk_decide.py`
  (fixture-tested, including the template drift gate); `cmd_config` + the `_cfg` resolution helper
  in `afk.py`; `--config` on `rebuild` / `fingerprint` / `heartbeat` / `next-attempt` /
  `classify-claims` / `frontier`.
- Deleted: every argparse literal default that shadowed the table; `pace`'s `.get` fallbacks (it
  now resolves through the table).
- Behaviour changes, all deliberate: omitted `busy_interval_seconds`/`idle_interval_seconds`
  default instead of crashing; the ttl/2 cap always applies; a config file with an unknown key
  refuses to load instead of half-loading.
- Validation errors surface at bootstrap — the one moment a human is present by design (ADR-0002).
- Runtime deps unchanged: python3 + git + gh, still nothing else.
