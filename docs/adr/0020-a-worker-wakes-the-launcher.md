# ADR-0020 — A worker wakes the launcher; the wake carries nothing

**Status:** accepted — adds a second, contentless channel beside "a worker communicates only through
GitHub" ([ADR-0001](0001-disposable-coordinator-context.md)), and leaves the pacing of
[ADR-0007](0007-fingerprint-gated-ticks.md) as the backstop it already was.
**Note ([ADR-0028](0028-the-launcher-runs-each-cycle-itself.md)):** decision 4's reason is simpler
now. The handle is read from the environment of `afk dispatch` / `afk fail` / `afk turn`, and those
run inside `afk cycle`, which the launcher itself calls — it is the launcher's own environment, not
one inherited through "a tick is a subagent of the launcher". Where this ADR says a tick is
*spawned*, read: a cycle is opened.

## Context

A worker's outcome is durable the moment it exists: a PR whose body says `Closes #n`, a verdict
marker, a hand-back's resolution pushed. Nothing tells the fleet. The launcher finds it by polling —
it sleeps `busy_interval_seconds` (90 s by default, and the wake-up primitive cannot go below 60 s),
then `afk cycle` notices the digest moved and a tick is spawned. So every finished worker waits out
what is left of a sleep before its merge even starts, and with several workers landing in a row those
waits stack: each merge frees a slot whose next dispatch is itself a cycle away.

Shortening the interval buys little and costs a launcher turn per poll. The information is already
where it is needed — the worker knows the instant its outcome exists — it only has no way to say so.

## Decision

1. **A worker ends by waking the launcher.** Once its one outcome is on GitHub, the worker runs one
   line, `orca terminal send --terminal <launcher's handle> --text "afk-wake #<n>" --enter`, which
   types `afk-wake #<n>` into the terminal the launcher runs in. The worker prompt carries it in the
   outcome section, in step 6, and in the hand-back brief.
2. **The wake is a hint and carries no state.** The launcher's whole response is to open the next
   cycle now (`afk cycle`) instead of when its sleep ends. That cycle gathers from GitHub like any
   other: it may tick, it may skip. The launcher never acts on the line's content — the issue number
   is there for a human scrolling back — and never treats it as proof that anything happened.
3. **Correctness never depends on it.** A wake that is lost, sent to a dead launcher's terminal, sent
   before GitHub lists the PR, or never sent by a worker that forgot, costs exactly the wait it would
   have saved: the sleep still ends and the cycle still runs. Pacing is unchanged.
4. **The handle is detected, never passed.** `afk dispatch`, `afk fail` and `afk hand-back` read
   `ORCA_TERMINAL_HANDLE` from their own environment when they fill the prompt: a tick is a subagent
   of the launcher, so that is the launcher's terminal. It is not a config key (machine-local and
   run-local, like the worker launch command, ADR-0010) and not a fourth launcher-held fact. With no
   handle — a headless tick outside orca — the line renders as a no-op with a note, as an unset
   `gate.local_command` does.
5. **Only a bare handle is rendered into a command.** The line is run in the worker's shell, so a
   value that is not `[A-Za-z0-9_.:-]+` is treated as no handle at all.

## Consequences

- With a local gate, or checks already green, a finished PR's merge starts when the worker stops,
  not up to a busy interval later. Under `gate.ci: required` with real CI the wake lands while checks
  are pending and gains nothing; the tick it triggers would have been spawned at the next poll anyway.
- A claim taken over or reclaimed by another launcher has a worker holding the *old* launcher's
  handle. Its wake goes nowhere and the new launcher polls. Any brief written after the takeover — a
  continuation, a hand-back — carries the new handle.
- The launcher's terminal now receives lines it did not ask for. `afk-wake #<n>` is the only one the
  fleet ever sends there; the launcher treats anything else arriving unasked as it always did.
- A worker could already type at any orca terminal on the machine; the prompt now names one. The
  bound on what that can do is decision 2, not secrecy of the handle.
- Several wakes arriving during one tick each open a cycle afterwards; all but the first skip, at the
  cost of one tool call each.

## Considered and rejected

- **Replace the sleep with a code-level wait that polls the digest.** No worker cooperation, and it
  sees CI going green too — but it is still polling, now every few seconds against the GitHub API,
  and it needs a blocking primitive the launcher's runtime may not have. Worth revisiting if the
  wake proves unreliable in practice.
- **Let the wake say what happened** ("PR #40 is open, merge it"). That makes a line typed by a
  worker an input to a session holding the merge authorization. The PR is on GitHub; the tick reads
  it there.
- **Have the worker merge its own PR.** The serialized merge, the merge-time gate and the claim
  release are the tick's (ADR-0012, ADR-0017).
- **Pass the handle through the tick's spawn prompt.** Another fact for prose to carry and drop, for
  a value every `afk` call can read for itself.
