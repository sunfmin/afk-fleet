# One CLI seam: decisions in the core, one config carrier, errors that are not outcomes

**Status:** accepted — tightens [ADR-0004](0004-deterministic-mechanics-as-tools.md) and
[ADR-0009](0009-one-home-for-config.md); supersedes the "demoted, not deleted" clause of
[ADR-0008](0008-rebuild-as-one-observation-tool.md) for `frontier` / `subclassify`, the tick-side
gathering of [ADR-0013](0013-liveness-is-recency-not-accumulated-work.md)'s signals, and the flag
names in [ADR-0006](0006-progress-status-board-comment.md) and
[ADR-0011](0011-takeover-and-progress-preservation.md).

## Decision

The `afk` CLI is the only interface a tick has to the fleet's mechanics, and the tick is an LLM
reading prose. Four rules now hold at that seam:

1. **A failed push is an error, never a lost race.** `{"won": false}` means exactly one thing: a peer
   holds the claim (the result names the `owner`). `claim` raises when its push failed and no claim
   exists on the remote; `reclaim` / `takeover` raise when the push failed although the claim has not
   moved; `probe` falls back to `refs/heads` only on a push the *server rejected*, and raises on one
   that never reached a verdict. All exit 3 with `{"error": …}`.
2. **The config is the one carrier.** The claim namespace is a config key, `claim_namespace`
   (default `refs/afk`). `afk probe` returns the canonical config with the namespace that works, and
   the launcher holds *that* config. Every subcommand takes `--config` and resolves it in one
   function (`_cfg`): override flag → `--config` → `CONFIG_DEFAULTS`.
3. **A question the tick asks is one call.** `afk no-pr` gathers the worktree's git progress, the
   worker's `afk:verdict` marker and the state of every issue it names as a blocker, derives
   `idle_seconds` itself, and returns the 5-way verdict. It replaces `worker-status` + `verdict` +
   `classify-no-pr`. `rebuild`'s `mine` rows carry the `board_phase` each renders as, and `afk status`
   takes `--phase` rather than a hand-built JSON state.
4. **Every decision lives in `afk_decide.py`; `afk.py` has no test seams.** The provisional-frontier
   join (`frontier_candidates`), the qoderclicn short-circuit (a `runtime` argument to
   `resolve_worker_command`) and the furthest-ahead branch pick (`furthest_ahead`) moved into the pure
   core. The injection flags (`--state-json`, `--claims-json`, `--heartbeats-json`, `--comments-json`,
   `--orca-json`, `--base-url`) and the caller-less `frontier` / `subclassify` subcommands are gone.

Smaller consequences of the same rules: `takeover` names the dead instance with `--from`, so
`--instance` is "my id" on every subcommand; the pure core reads one input shape (issue `labels` are
names, normalised once in `_gather`; comments are `{id, body, url}`); and the status board names the
gate it is waiting on, so `gate.ci: local` no longer shows a green gate before the local gate has run.

## Why

Each of these was a way for an unattended fleet to be wrong with nothing on screen.

- A blocked namespace, an expired token, or a dropped connection all returned `{"won": false}`, and
  the skill told the tick that means "a peer won, skip it". The fleet idled, forever, looking healthy.
- The fallback namespace was a fourth launcher-held fact that the tick's spawn payload did not carry —
  the skill named three. A tick on a blocked repo scanned and claimed under `refs/afk` and, by the
  point above, read every failure as a lost race.
- ADR-0013 made *recency* the deciding liveness signal, and recency was the one input no code
  computed: the tick subtracted epoch timestamps across three tool outputs and pasted two JSON blobs
  between calls. ADR-0004 already said that kind of step belongs in a tested tool.
- The injection flags were used by no test, and each guarded an `if injected … else gather` branch
  whose *other* arm held the logic — so `rebuild`, `fingerprint` and `verdict` were untested end to
  end, and the provisional-frontier join existed twice with two different input assumptions.

## Considered and rejected

- **Keep `--ns` and add it to the tick payload.** That fixes today's omission and leaves the shape
  that caused it: a second thing to thread through every call. Folding it into the config removes the
  thing.
- **Drop the override flags entirely** (`--ttl`, `--retry`, …), leaving `--config` as the only
  input. Simpler still, but they are how the ref-race tests pin a lease and how a human debugs one
  call. Kept, behind one overlay table, with a test that walks every flag through the three-way order.
- **Fold `no-pr` into `rebuild`.** `rebuild` is the machine-independent observation the launcher's
  fingerprint gate shares (ADR-0008); `no-pr` asks *this machine* about a worktree and needs the
  terminal probe as input. Separate call, same as `recovery`.
- **Generate `tools.md` from argparse.** The table carries return shapes and rationale a parser does
  not have. Instead a test fails when the table omits a subcommand, or when any `afk …` written as
  code in SKILL.md, the references or CONTEXT.md names a subcommand or flag that does not exist — the
  same "pin the hand-sync with a test" move ADR-0009 uses for the config template.
- **Keep the injection flags and add tests that use them.** That tests the arm without the logic.
  The outside world is faked where it lives instead: `gh`, `orca` and `$SHELL` stand-ins on PATH,
  with real git against the bare-repo sandbox.

## Consequences

- **Behaviour changes a running fleet will notice.** A `blocked` verdict that names no blocker now
  escalates (it could previously be re-dispatched without ever clearing, outside the retry ladder).
  A blocker whose state cannot be read counts as open. `afk no-pr` refuses a `--worktree` path that
  does not exist rather than reading it as "no progress".
- **Interface changes** (a launcher must be restarted on the new skill; nothing durable in GitHub
  changes shape): `worker-status` / `verdict` / `classify-no-pr` → `no-pr`; `takeover --instance X
  --as ME` → `takeover --from X --instance ME`; `status --state <json>` → `status --phase …`;
  `probe` returns `config`; `--ns` survives only as an override of `claim_namespace`.
- **Config** gains `claim_namespace`. A repo may set `refs/heads` in its file to skip the
  probe-and-warn; existing files need no change.
- **Tests**: `test_afk_cli.py` (new) drives every gh/orca/shell-backed subcommand through the real
  CLI; `test_afk_refs.py` gains the failed-push, rejected-namespace and malformed-ref cases;
  `test_afk_decide.py` covers the functions that moved. A 38-case mutation run over the new behaviour
  was used to confirm each is actually asserted.
- Earlier ADRs keep their original text; where they name a removed flag or subcommand, this ADR is
  the current word.
