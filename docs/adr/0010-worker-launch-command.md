# The worker launch command: one opaque string, confirmed by the human, never composed

**Status:** accepted — amends the dispatch step of [ADR-0005](0005-orca-owns-the-worktree.md).
Amended by [ADR-0014](0014-runtime-detection.md) (runtime-generalized stock detection).

## Decision

A run carries one **worker launch command**: an opaque shell string that starts every worker. It is a
third launcher-held fact alongside the instance id and the run authorization — settled at bootstrap
with the human present, injected into every tick's spawn prompt, never a config key, gone when the
launcher stops.

Dispatch drops orca's `--agent` and runs the command itself:

```bash
orca worktree create --repo id:<repo-id> --name issue-<n>-<slug> --no-parent \
     --base-branch <base_branch> --issue <n> --json          # no --agent
orca terminal create --worktree issue:<n> --command "<the worker launch command>" --json
orca terminal wait --terminal <handle> --for tui-idle
orca terminal send --terminal <handle> <worker prompt>
```

`afk worker-command` settles everything around the answer that code can settle:

- **Whether to ask at all.** No `ANTHROPIC_BASE_URL` → `stock`, the command is
  `claude --dangerously-skip-permissions`, and the human is not asked.
- **What to offer.** On a custom provider it returns the login shell's Claude-starting aliases, each
  marked `wraps_env`, so the human picks a name instead of typing a command.
- **Whether the answer runs.** `--check "<cmd>"` resolves the first word in the same interactive
  login shell orca gives a worker, and reports `unresolved` if it resolves to nothing.
- **Whether it looks unattended.** `yolo` is `true`/`false` when the resolution shows the whole story
  (an alias expansion, or `claude` itself), `null` when it cannot (a script path). Advisory only.

The command itself is never parsed, never composed, never appended to.

## Why

A launcher started through a provider wrapper carries that provider **only in its environment**. The
wrapper's name is destroyed by the shell: `ckimi` expands to
`(eval "$(mytokens env kimi)" && claude --dangerously-skip-permissions)`, so the process argv is
byte-identical to a plain `cc`, and only `ANTHROPIC_BASE_URL` distinguishes the two. A worker, meanwhile,
starts in a fresh login shell that inherits none of that environment and silently falls back to stock
Anthropic — unattended, for days, across every issue in the backlog. Nothing in the fleet notices,
because a worker on the wrong provider still opens perfectly good PRs.

So the name cannot be recovered, only supplied. The bootstrap gate is where it belongs: the human is
already standing there granting push+auto-merge for the whole run, and this is one more line at the
same gate — a one-time cost against a multi-day unattended run. Asking is also *conditional*: the
common stock case is never interrupted.

Keeping the string **opaque** is what keeps every credential out of the fleet. The fleet copies no
environment, writes no file, puts no key on any command line, and is coupled to no secret manager —
the wrapper the human already trusts does the resolving, in the worker's own shell. It also makes the
mechanism generic for free: an alias, `direnv exec . claude`, a wrapper script, and a plain `claude`
are all just strings.

Dropping `--agent` costs orca's YOLO default (`YOLO_TUI_AGENT_ARGS.claude`), which the command must now
carry itself — hence the advisory `yolo` check, because a worker parked on a permission prompt is
indistinguishable to the fleet from one that finished and went idle. Verified before committing to it:
a `--command`-launched Claude Code is still recognised by orca as an agent (terminal title `✳ Claude
Code`) and still satisfies `orca terminal wait --for tui-idle` — so the liveness probe that the `no_pr`
five-way classification depends on keeps working.

The single path — `stock` runs through `terminal create --command` exactly like a custom provider — is
deliberate, for the same reason `--plan` and a live tick share one rebuild (ADR-0002): a second path
means the unattended, days-long, custom-provider case is the *less* exercised one.

## Considered and rejected

- **Reverse-map the environment to a wrapper name** (scan `~/.zshrc`, match each secret-manager profile's
  base URL against the launcher's). Works, and was built — a base URL is unique per profile, so the match
  is equality, not a guess. Rejected because it hardcodes one person's secret manager into a skill meant
  to ship, and because the matching must decrypt *every* profile to answer: done in the launcher it
  prints a pile of API keys into its own context, and containing that needs a tool that exists only to
  hide the blast radius of a lookup the human answers in one word.
- **Copy the launcher's environment to the worker.** Portable and fully automatic, but "all of it" is
  wrong on the facts: the launcher's env carries 15 `ORCA_*` variables (`ORCA_TERMINAL_HANDLE`,
  `ORCA_PANE_KEY`, `ORCA_WORKTREE_ID`, …) that bind a process to the *launcher's own terminal*, plus
  `CLAUDE_CODE_SESSION_ID` and friends. Copied, every worker reports its agent status as the launcher's
  terminal and the liveness probe collapses. Narrowing to an allowlist fixes that but not the real
  objection: the API key must then materialise somewhere the fleet owns — a command line (visible in
  `ps`) or a file on disk (durable plaintext, and lethal in a worktree an autonomous `git add -A` can
  reach).
- **Stash the key in the macOS Keychain and have the worker fetch it.** Strictly better than a plaintext
  file, and argv holds only the lookup key. Rejected: it is macOS-only, it needs a run-scoped item to be
  reliably deleted at drain, `security add-generic-password` cannot take a value off stdin (so the write
  itself puts the key in argv), and an unattended worker that meets a Keychain authorisation prompt hangs
  forever — a worse failure than the one being fixed.
- **A `worker_command` config key.** The config file is checked into the *target* repo and shared with
  the team; a machine-local shell alias has no business there, and on a teammate's machine it is
  `command not found` — or, worse, a same-named alias pointing somewhere else.

## Consequences

- Dispatch is two orca calls instead of one, and SKILL.md carries no `--agent`.
- The launch command must be complete and unattended-ready on its own; the fleet appends nothing to it
  (appending to an alias that expands to a subshell is not even syntactically valid).
- Bootstrap gains one conditional question, asked only when `ANTHROPIC_BASE_URL` is set.
- The fleet still cannot *prove* worker/launcher provider parity — it verifies that the command resolves,
  not what it resolves to. A human who names the wrong wrapper gets a fleet on the wrong provider. That
  residue is accepted: proving it means running the wrapper, which means decrypting credentials.
- `references/config-template.md` states why there is no key for this, the same way it does for
  `authorize`.
