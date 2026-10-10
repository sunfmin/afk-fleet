# The CLI seam enforces its own rules: a required config, one override, no error dressed as an answer

**Status:** accepted — completes [ADR-0015](0015-one-cli-seam.md), and supersedes two of its
clauses: the override flags it kept ("Considered and rejected: drop the override flags entirely")
and the fields `afk probe` returned beside `config`. Tightens
[ADR-0009](0009-one-home-for-config.md) (the resolution order's first step is now `--set`).

## Decision

ADR-0015 stated four rules for the `afk` CLI. Two of them — *errors are not outcomes* and *the config
is the one carrier* — held only where a call site remembered them. They are now properties of the
structure:

1. **`--config` is required.** Every subcommand that reads config refuses to run without it (exit 3).
   Only `afk config` and `afk worker-command`, which run before a config exists, take none. Keys the
   JSON omits still default; the flag itself cannot be omitted. A key the JSON *does* carry is held
   to the schema the config file is held to — unknown, renamed, removed or wrong-typed, it is an
   error naming the key (exit 3), never dropped (#113).
2. **One override, by the config's own key names.** `--set <key>=<value>` (repeatable, dotted for
   `gate.` / `merge.`) replaces the eleven per-key flags (`--ns`, `--ttl`, `--retry`, `--base`,
   `--grace`, `--ci`, `--command`, `--target`, `--force-after`, `--ready-label`, `--epic-labels`) and
   the table that mapped them. Values are typed by the key's default, as the file's are. Resolution is
   `--set` → `--config` → defaults, in one function (`_cfg`), which then **validates** the result —
   so no subcommand runs on a config `afk config` would refuse.
3. **No failure is an exit-0 answer.** A claim/heartbeat scan that cannot fetch raises (it used to
   return no claims); `afk release` raises when the delete failed and the claim is still on the remote
   (it used to return `released: false`); `afk recovery` raises when the remote cannot be read (it
   used to fall through to tier 3). A bad command line is the same `{"error": …}`, exit 3, as every
   other failure.
4. **The claim namespace is a closed set** — `refs/afk` or `refs/heads`, one table
   (`CLAIM_NAMESPACES`) holding both layouts. `afk probe` returns `{blocked, config}`; `namespace`,
   `hidden` and `ci_on_push` are gone.
5. **One shape per fact across the seam.** The attempt count is a number: `rebuild` puts `attempt` on
   each `mine` row, and `afk next-attempt --attempt` and `afk status --attempt` take it
   (`current_attempt` is the one reader of the `afk-attempt/<n>` label). `afk no-pr` calls the worker's
   marker `worker_verdict`; *outcome* is the tool's conclusion. `subclassify_pr` returns
   `(status, board_phase)` together, so `gate.ci: local` bends both in one place.
6. **The pure core re-applies no default.** `pace`, `render_status_board` and `subclassify_pr` take the
   values `_cfg` resolved; none falls back to `required`, the defaults table, or a second
   `resolve_config`.

## Why

A review of ADR-0015's change found each rule with a hole the tests did not cover, and each hole was
the failure the ADR had been written to remove — an unattended fleet wrong with nothing on screen.

- With `--config` optional, forgetting it ran on the defaults. Reproduced: a claim made under
  `refs/heads`, then `afk release <n>` without the config → `{"released": true}`, exit 0, deleting
  nothing — a phantom lock. The skill's own inline examples omit the flag for readability, so this was
  the likeliest mistake a tick could make.
- With git unable to fetch while `gh` still answered (two credentials, one expired), `rebuild`
  assembled a working set with no claims: nothing of mine in flight, every claimed issue back on the
  frontier, an unchanging fingerprint. The fleet paces to idle and stops beating.
- The override table was keyed by argparse `dest`, globally: a later subcommand adding a `--base` or
  `--command` of its own would have silently overridden config. Which subcommand accepted which
  override was hand-picked and uneven (`status` reads `retry` and `gate.ci` and accepted neither),
  and gh-only subcommands carried a `--remote` and `--ns` that did nothing.
- `claim_namespace` was validated only as "starts with `refs/`", so `refs/heads/afk` passed and was
  reported `hidden: true, ci_on_push: false` — for refs that are ordinary branches.
- The attempt count crossed the seam as a label list (`rebuild`), a number the tick parsed out of a
  label (`status`) and a csv (`next-attempt`), with the parse written once in code and once in prose.

## Considered and rejected

- **Keep the per-key flags and add the missing ones.** That is the table plus eleven-and-growing
  `add_argument` calls, each needing `default=None` to avoid silently beating config. ADR-0015 kept
  them because the ref-race tests pin a lease and a human debugs with them; `--set` serves both, and
  cannot drift from the schema because it *is* the schema's key names.
- **An environment variable (`AFK_CONFIG`) instead of a required flag.** It would spare the tick
  repeating itself, but a tick's shell state does not persist between tool calls, so it would have to
  be re-exported per call anyway — the same repetition, less visibly.
- **Validate only in `afk config`.** That was the rule ("load time is the one place"), and it is why
  `--ns refs/heads/afk` went unchecked. Validating in `_cfg` costs microseconds and removes the
  question of whether a given path was validated.
- **A new `status` name for `gate.ci: local`** (so `awaiting_merge` would always mean "gate green").
  The status is the tick's cue, and the cue is the same in both modes — run the merge sequence, which
  re-confirms the gate either way. Only the board differs, so the two are now decided in one function
  and documented as "what the tick does" vs "what a human sees".
- **Keep `released: false` and tell the tick to check it.** That is prose guarding a correctness rule,
  the shape ADR-0004 exists to remove.

## Consequences

- **Interface changes** (a launcher must be restarted on the new skill; nothing durable in GitHub
  changes shape): `--config` required; `--<override>` → `--set <key>=<value>`;
  `next-attempt --labels <csv>` → `next-attempt --attempt <k>`; `mine[].attempt_labels` →
  `mine[].attempt`; `no-pr`'s `verdict` → `worker_verdict`; `probe` returns `{blocked, config}`;
  `release` returns `{released: true, issue, ref}` or errors; `recovery`'s `branch` loses `detail`
  and an absent worktree is `{present: false, path}`.
- **Behaviour a running fleet will notice.** A tick on a machine whose git cannot reach the remote now
  stops with an error instead of idling. A config file naming a `claim_namespace` other than the two
  is refused at bootstrap.
- **Tests.** `test_afk_cli.py` walks every subcommand through the refusal and the three-step
  resolution, and drives `rebuild` / `fingerprint` with gh up and git down; `test_afk_refs.py` gains
  the failed-release, unreadable-scan and unreadable-recovery cases; `test_afk_decide.py` walks
  `override_config` over every key in the schema. A mutation run over the new behaviour confirmed each
  is asserted.
- ADR-0015 keeps its original text; where it names an override flag or a `probe` field, this ADR is
  the current word.
