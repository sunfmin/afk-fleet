# Takeover + progress preservation: a dead fleet's work is continued, not restarted

**Status:** accepted — amends the orphaned-claim recovery of [ADR-0003](0003-cooperative-multi-fleet-claims.md)
(tear-down-and-re-dispatch) and the single-machine framing of its reclaim path. Keeps the
fleet-state-in-GitHub foundation, the atomic lock ref, and the per-instance lease of ADR-0003 intact.

## Context

A fleet hard-stops when its provider quota runs out: the launcher is killed mid-run with no chance to
drain. ADR-0003 already makes a dead fleet's *ownership* recoverable — its `afk-heartbeat/<id>` stops
refreshing, and once it is stale past `claim_lease_ttl` a peer reclaims each claim by an atomic
`git push --force-with-lease`. But two things about that recovery are lossy and slow:

- **It loses the work.** Ownership transferred; the *progress* did not. A worker's half-finished code
  lives in its local git worktree, which orca owns on the *dead machine* ([ADR-0005](0005-orca-owns-the-worktree.md))
  and which no peer can reach. The worker opens its PR only at the very end of its run
  ([worker-prompt](../../skills/afk-fleet/references/worker-prompt.md) step 5), so before then nothing
  is on GitHub. The reclaiming fleet therefore finds a `no_pr` claim, classifies it `dead` (orphaned),
  and does what ADR-0003 prescribes: `orca worktree rm` + re-dispatch **from a fresh base**. The dead
  worker's effort is discarded; "another fleet continues" is, today, "another fleet starts over."
- **It is slow.** The reclaim cannot fire until the lease expires (~75 min by default), because the
  lease is the only machine-visible line between "dead" and "alive but slow" — and a fleet must never
  touch a peer whose heartbeat is fresh, or cooperating fleets cannibalise each other's live work.

The human who runs fleets is present at the death (they see the quota error). They want to start
another fleet and *continue* — promptly, and without losing the partial work.

## Decision

Two changes that compose: **preserve progress on every recovery**, and **let a present human take over
immediately**.

1. **Workers publish progress as they go (soft checkpointing).** The worker prompt is amended to
   instruct each worker to `git commit` + `git push` its own branch after each completed step, and
   always before running the local gate or any long-running operation. The branch already exists on
   GitHub (orca creates it, `<user>/…`), so no new ref namespace is needed; the branch tip becomes the
   durable record of "how far this issue got." This is a **prompt instruction, not an enforced
   mechanism** — see *Why soft, not enforced* below. A hard stop now loses at most the in-flight step.

2. **Continuation is the default recovery behaviour (not a mode).** Whenever a claim's worker has died
   — an **orphaned claim** reconciled locally, or a **stale claim** reclaimed from a peer — the fleet
   recovers it *from its durable progress* instead of unconditionally tearing down and re-dispatching
   fresh. Tiered by what survived the death:
   - **Tier 1 — resume the local worktree.** If a worktree for `issue:<n>` still exists on *this*
     machine (the taking-over fleet runs on the same box), do **not** `orca worktree rm` it: spawn the
     new worker *inside it*, on the same branch. Lossless — it captures even uncommitted/unpushed work.
   - **Tier 2 — continue from the pushed branch.** If there is no local worktree but the branch has
     commits ahead of base on GitHub, recreate a worktree at the branch tip and continue there. Bounds
     the loss to "since the last push."
   - **Tier 3 — fresh re-dispatch.** Only when neither survives (no local worktree, nothing pushed)
     fall back to today's behaviour: re-dispatch from base.

   The continue-vs-fresh *selection* is mechanics (deterministic from `orca worktree list` + a
   git compare of branch vs base) and belongs in the `afk` tool; whether a given recovered state is
   sane to build on stays tick judgment, like the existing orphan-vs-alive read
   ([ADR-0008](0008-rebuild-as-one-observation-tool.md)). A continuing worker gets a *continue* variant
   of the worker prompt: inspect the current state first (`git status`, diff vs base, read the existing
   commits), treat it as partial work toward the *same* acceptance criteria, gate locally, finish, and
   open the PR — trust-but-verify, with the acceptance criteria as the constant. The "end with exactly
   one machine-readable outcome" rule is unchanged.

3. **Takeover: a human-authorized, immediate reclaim.** A new launcher bootstrap variant,
   `afk-fleet --takeover`, for the case the lease cannot cover — a present human who *knows* the fleet
   is dead and will not wait ~75 min. The dying fleet cannot help (a hard stop runs no code: no drain,
   no release), so the human is the only oracle for "it is really dead." It runs the **full** bootstrap
   (config, minted instance id, settled **worker launch command**, the one push+auto-merge
   authorization — [ADR-0010](0010-worker-launch-command.md)) and is therefore a real fleet instance;
   its only difference is how it *seeds its opening working set*. Instead of starting from the frontier
   alone, it:
   - **lists the instances** discoverable in the claim marker commits (`instance=<id> host=<host>`) and
     the heartbeat refs (`afk-heartbeat/<id>`) — the launcher forgets its id on death, but GitHub does
     not — showing each one's heartbeat age, host, and claim count, for the human to select;
   - **force-takes the selected instance's claims** with the *same* atomic `git push --force-with-lease`
     used by stale reclaim — only **skipping the staleness gate**. It is still atomic against a
     not-actually-dead fleet (the second pusher is rejected);
   - **warns and requires an explicit confirm if the target's heartbeat is still fresh** — "this fleet
     looks alive; forcing takeover steals its live work if you are wrong" — because the human may be
     mistaken (wrong terminal, or the fleet is merely slow). The lease remains the *unattended*
     safety net; takeover is the *human-gated* fast path; both coexist.

   Thereafter the takeover instance is an ordinary standing fleet: it recovers the inherited claims by
   continuation **and** works the frontier up to `concurrency`, running until stopped. `--takeover`
   changes only the opening working set, not the loop.

## Why soft checkpointing, not enforced

An enforced auto-push (a git hook in the worktree, or a wrapper around the worker command) was
rejected:

- It violates [ADR-0010](0010-worker-launch-command.md): the **worker launch command** is opaque — the
  fleet never parses, composes, or appends to it — precisely so every credential stays inside the
  wrapper the human trusts. Injecting hooks is the fleet reaching into the worker's world.
- A slightly-stale but *clean* checkpoint is better than a live-wired push of a half-typed file. A
  continuing worker re-inspects state anyway; resuming from "the last completed step" is safer than
  from a torn edit.
- It keeps the mechanics/judgment line: *when a step is complete* is judgment (the LLM's), not
  mechanics. The change stays entirely inside the worker prompt and adds no tool.

## Why continuation is the default, not a takeover mode

Continuation is strictly dominant over today's behaviour — recovering progress is always ≥ restarting;
there is no scenario where throwing progress away is preferred, so there is no switch and no mode.
Making it the default means *every* recovery trigger benefits — orphan reconciliation, stale reclaim,
graceful-stop inheritance, and takeover alike — through one mechanism. A separate "preserve progress"
mode would leave the ordinary path silently lossy. This deepens an existing module (claim recovery),
rather than adding a parallel one.

## Why takeover keeps the lease (and does not shorten the TTL)

Shortening `claim_lease_ttl` to make unattended reclaim faster was rejected as treating a rare event
with a global, permanent cost: while holding any claim the launcher never sleeps past `ttl/2` and the
heartbeat fires ~`ttl/3`, so a shorter TTL means more ticks/heartbeats *and* a higher chance that a
live-but-slow fleet's lease lapses mid-operation and a peer steals genuinely-live work. The TTL is a
safety margin, not a knob to zero. Takeover instead adds a *human-gated* fast path that leaves the
unattended margin untouched: no human present → you wait, safely; human present → you may skip the wait
and bear the responsibility.

## Why takeover is keyed by instance, and does not consume a retry

- **By instance, not by issue.** The fleet instance is the domain's unit of concurrency and liveness —
  the heartbeat is per-instance ([ADR-0003](0003-cooperative-multi-fleet-claims.md) chose this over
  per-claim), and a fleet dies *wholesale* (quota is account/session-scoped), not per-issue. Taking one
  dead fleet's claims is one action over all its claims; per-issue takeover would be N commands and
  would invite cherry-picking. The list-and-select UX recovers the id from the markers/refs, so the
  human need not have it.
- **Orthogonal to the retry ladder.** The `afk-attempt/<n>` counter answers "is this *work* failing?"
  (gate red, giving-up, adversarial refute). Takeover answers "did the *fleet* die?" — a different
  axis. Counting a takeover as a retry would march a perfectly healthy issue to escalation merely
  because the account keeps exhausting quota. Continuation makes this safe without a separate takeover
  bound: because progress accumulates, repeated takeovers of one claim *converge* (each starts further
  along) rather than loop; a genuinely-cursed issue still escalates through the ordinary failure path.

## Considered and rejected

- **Dying-fleet self-release** (the fleet releases its claims just before death so a peer grabs them
  instantly). Dead on arrival for the stated scenario: a hard stop runs no code — no drain tick, no
  release. It only covers graceful stop, which already exists. A speculative "low-quota warning →
  pre-emptive drain" variant is unreliable (quota exhaustion is usually sudden) and was not built.
- **A distinct `--takeover` rescue that stops after finishing the inherited claims.** Rejected: the
  bootstrap is already paid, so stopping right after the rescue wastes it; the standing use of the
  fleet is multi-day backlog work; and "rescue then stop" needs special end-condition machinery the
  thin launcher otherwise lacks. A human who wants a bounded rescue simply stops the launcher — the
  graceful-stop path exists. `--takeover` therefore seeds a normal standing fleet.
- **Hard-refusing takeover of a fresh-heartbeat instance.** Rejected as over-paternalistic: the human
  is the oracle and may legitimately know the fleet is dead (wedged process, a heartbeat written by a
  now-dead tick). The fresh heartbeat is surfaced as a warning requiring explicit confirm instead — an
  *informed* override, still atomic underneath via `--force-with-lease`.
- **Per-issue takeover as the primary unit.** Rejected for per-instance (above); an optional issue
  filter may be added later if a need appears.

## Consequences

- **Worker prompt** gains the soft checkpoint rule (commit+push per completed step; always before the
  local gate / long-running ops) and a *continue* variant for continuing workers.
- **`afk` tool** grows a takeover subcommand (list instances from markers + heartbeat refs; force-take
  a selected instance's claims, skipping staleness, with a fresh-heartbeat confirm) and an extension to
  the recovery mechanics so `rebuild`/`worker-status` report, per dead claim, whether recoverable
  progress exists and where (a local worktree via `orca worktree list`; a branch ahead of base via
  git). The continue-vs-fresh selection is mechanics; sanity-of-the-recovered-state stays tick judgment.
- **Recovery paths change behaviour**: orphaned-claim reconciliation and stale-claim reclaim now
  continue from progress (tier 1 → tier 2 → tier 3) instead of unconditionally tearing down. The
  tear-down survives only as the tier-3 fallback.
- **Config:** no new key is required for the behaviour change (continuation is the default). Takeover
  reuses `claim_lease_ttl_seconds` semantics for its *warning* only; it does not gate on it.
- **Retry ladder is untouched**; takeover neither increments nor reads `afk-attempt/<n>`.
- **Single-fleet is unaffected** in the common case — continuation simply makes its own orphan
  recovery lossless on the same machine; takeover is invoked only when a human starts one.
- **Reserved surfaces:** unchanged — takeover writes the same `refs/afk/claim/*` refs (re-stamped with
  the new instance id) and adds no new namespace.
