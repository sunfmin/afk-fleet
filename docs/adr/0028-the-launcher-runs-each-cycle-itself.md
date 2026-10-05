# ADR-0028 — The launcher runs each cycle itself; compaction is safe by construction

**Status:** accepted — supersedes three decisions of
[ADR-0002](0002-launcher-and-disposable-ticks.md): *a tick is a fresh-context Agent subagent*, *the
launcher is thin by construction*, and *compaction is not the mechanism*. Keeps its foundation (no
session holds fleet state; a tick is one reconciliation pass that does not wait for its workers) and
[ADR-0001](0001-disposable-coordinator-context.md)'s rule 5 (bulky reads are delegated). Notes
[ADR-0007](0007-fingerprint-gated-ticks.md) and
[ADR-0020](0020-a-worker-wakes-the-launcher.md), whose decisions stand with one reason each changed.
Builds on the tick-in-code amendment of [ADR-0017](0017-the-act-half-is-transitions.md).

## Context

ADR-0002 gave every tick a fresh context for one reason: a pass used to put bulky things in front
of an LLM — `gh` issue and PR JSON, CI logs, a merge-time gate run — and a session that did that
every 90 seconds for days could only be kept bounded by compaction, which could silently drop a
rule of the ~300-line procedure the session was executing from memory.

Each of those has since moved out of any context:

- the raw observation lives and dies inside the `afk` process (ADR-0008);
- every transition is one call that performs its whole ordered sequence (ADR-0017);
- the landing, and its minutes-long gate run, happens in the worker (ADR-0027);
- the pass itself — the routing between those calls — is code inside `afk cycle` (ADR-0017's
  amendment). What reaches an LLM is one compact JSON object and, sometimes, a judgment.

So the per-tick subagent now buys nothing and still costs a cold start — a fresh context
re-reading the skill and the config — every time anything on GitHub changes. Its whole job had
shrunk to "run `afk cycle`, answer the judgments, hand three values back".

## Decision

1. **The launcher runs each cycle itself.** The loop is: `afk cycle` in the foreground → for each
   judgment, decide and run the command it handed over → judgments answered, the next cycle at
   once; otherwise show `progress` and sleep `sleep_seconds`. An `afk-wake` line ends the sleep
   early, as before. No Agent subagent is spawned for a tick.
2. **A tick is a pass, not a context.** The word stays — one reconciliation pass, inside one call —
   but it names code. Two roles hold a context: the launcher and the worker.
3. **The launcher's context is bounded by auto-compaction, and that is safe by construction.**
   After any compaction the launcher needs exactly three values: the repo, the config, and the last
   cycle `state`. The `state` carries the instance id and the worker launch command, so the next
   cycle claims as the same fleet instance and starts workers the same way; a judgment's commands
   carry everything they need. The procedure is code, so there is no rule a compaction can lose. A
   test holds this: a caller given only those three values dispatches, is handed runnable
   judgments, and drains, as the same instance.
4. **The stop is one more cycle.** `afk cycle --drain` releases this fleet's claims that no open PR
   stands behind and keeps the rest — what the drain tick did by hand with `afk rebuild` and
   `afk release`. Then no more cycles.
5. **One use of the Agent tool is left:** a judgment flagged `bulky` (an adversarial verify, a
   failure reason that lives in a CI log) goes to an ephemeral subagent that returns one line.
   ADR-0001's rule 5 is unchanged; it is now the only place a diff or a log could enter the
   launcher's context, and it does not.

## Why ADR-0002's objection no longer holds

ADR-0002 rejected compaction as the mechanism twice — "flat by discipline / hoped-for compaction".
The objection had two halves, and both are gone:

- **What accumulated.** Then: every busy poll added `gh` output and reasoning about it. Now: a
  cycle adds one small JSON object — on most cycles a skip. The growth compaction has to absorb is
  a few hundred tokens a cycle, not a working set.
- **What compaction could cost.** Then: the session's knowledge of the procedure, and facts held
  nowhere else (the instance id, the worker launch command, the last summary). Now: nothing. The
  procedure is code; the two facts ride in `state`; fleet state was always GitHub's. A launcher
  that forgot everything but `{repo, config, state}` is indistinguishable, to the fleet, from one
  that forgot nothing.

Boundedness is still structural — it moved from "each context is thrown away" to "nothing the run
needs is in the context".

## Consequences

- No cold start per tick: a change on GitHub costs one tool call in a warm session.
- `/afk-fleet --tick` is the bootstrap plus one cold cycle (no `state`), judgments answered, no
  sleep and no drain. `--plan` is unchanged: `afk rebuild`, printed.
- ADR-0007's gate stays, for a different saving: a skipped cycle is two list reads instead of a
  full pass. Its rejection of "fingerprint inside the tick" — spawning the context being the
  expensive part — no longer describes anything; the gate *is* inside the call that runs the tick.
- ADR-0020's wake handle is the launcher's own `ORCA_TERMINAL_HANDLE`, read by the `afk` process
  the launcher started. No inheritance through a subagent is involved.
- The launcher is no longer "thin": it answers judgments. It still never computes a frontier,
  reads a worker, or types a transition that a cycle did not hand it.
- *Coordinator* stays out of the vocabulary. Fleet state is all in GitHub and no session holds it;
  the launcher is the session that keeps calling.
- A `state` lost outright — not compacted, gone — cannot be rebuilt, because the instance id is in
  it. That run is over; its claims are recovered like any dead fleet's (`--takeover`, or the lease).

## Considered and rejected

- **Keep the per-tick subagent.** Structural reset for a context that no longer fills: every
  changed cycle pays a cold start to relay three values.
- **Write `state` to a file so a compaction cannot touch it.** The two launcher-held facts are
  deliberately never on disk (ADR-0010), and a file outliving the launcher would let a second
  session resume as the same instance beside a live one.
- **A code loop with no LLM** (`afk run`, sleeping in-process). Judgments still need an LLM, the
  wake arrives as a line in an interactive terminal, and the human stops the run by saying so —
  the context-free launcher ADR-0002 rejected, for the reasons it gave.
