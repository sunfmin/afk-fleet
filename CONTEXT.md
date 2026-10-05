# afk-fleet

An unattended fleet that works a GitHub-issue backlog on its own, for days, without any session's
context growing without bound: a thin **launcher** spawns a fresh disposable **tick** each cycle;
each tick dispatches worktree-isolated **workers** per ready issue, gates them, and auto-merges green
PRs to the target branch.

## Language

**Launcher**:
The interactive session `/afk-fleet` is invoked in. Invoking it is the launch: it asks for no
confirmation, mints a **fleet-instance** id, then loops: spawn a **tick**, ingest its one-line summary, pace, repeat. It does no coordination
itself: it never computes the **frontier**, never reads tick-only files (the `afk.py`/`afk_decide.py`
source, `worker-prompt.md`). It is
thin *by construction* from its first action — it only ever spawns subagents and ingests their compact
summaries — so its context stays flat over a multi-day run. Several launchers — on several machines,
even under one GitHub account — may run against the same repo at once; they cooperate only through
**claims**, never a central coordinator.
_Avoid_: coordinator (there is no single long-lived coordinator; a tick coordinates one pass),
orchestrator, manager, main agent

**Fleet instance**:
One launcher run and everything it owns — the ticks it spawns, the workers they dispatch, and the
**claims** it holds — identified by an id minted at bootstrap and injected into every tick. That id
and the **worker launch command** are the run's two launcher-held facts:
settled once at bootstrap, carried in every tick's spawn prompt, never written to a file,
and gone when the launcher stops. Its liveness is published as a **heartbeat**; when it stops or dies
its claims are released or reclaimed. Distinct instances (even on one GitHub account) are the unit of
cooperative concurrency across machines.
_Avoid_: node, worker (that is the per-issue coding agent), coordinator

**Tick**:
One fresh-context, disposable reconciliation pass, run as an Agent subagent. It rebuilds the working
set from fleet state, acts once (merge green PRs, escalate exhausted, dispatch to free slots — each a
single **transition**), returns
a compact summary, and dies — without waiting for the workers it dispatched. Runtime is bounded
because ticks are disposable, not because one session stays disciplined.
_Avoid_: batch (implies draining a whole wave), poll (a tick acts, not just observes), coordinator

**Plan tick**:
A **tick** run in dry-run mode (`--plan`): it does the full **rebuild** (recompute the **frontier**,
classify claims into mine/peer-live/stale) and then **stops before the Act phase**, returning the
dispatch plan instead of merging/dispatching/reclaiming. It is the *same* procedure as an acting tick,
short-circuited — so the plan a human reads cannot drift from what a live tick will actually do. It
is how to look before launching: `/afk-fleet --plan`. A launch itself spawns none (ADR-0023).
_Avoid_: dry run (that is its mode, not its name), preview pass

**Worker**:
A fire-and-forget, ephemeral coding-agent session (Claude Code or qoderclicn — the run's **runtime**),
isolated in one git worktree, that owns exactly one
issue, opens a PR, reports done via GitHub, and then **wakes** the launcher. It never merges, and its terminal is never read for
its result (only, once it has gone silent with no outcome, for *where it stopped* — see **Nudge**). Its worktree is created and later torn down by the **worker backend** — orca (`orca
worktree create` / `orca worktree rm`), the only supported backend — never by the tick with raw `git
worktree`; orca also names the branch (a `<user>/…` prefix), and the tick **reads that back** rather
than dictating it (ADR-0005). It is started by running the **worker launch command** in the
worktree's first terminal, so it runs on the same runtime as the **launcher** that dispatched it.
It publishes its progress as it goes — incrementally pushing its own branch after each completed
step, and always before the local gate or any long-running operation — so a hard stop loses at most
the in-flight step; that pushed branch tip is the durable progress a later **continuation** resumes
from when this worker's machine is gone (ADR-0011).
_Avoid_: agent (too generic), subagent, child

**Worker state**:
Whether a **worker** is busy or has stopped, as its **runtime** reports it to orca — *working*,
*waiting* (on a question or a permission), or *done* (its turn ended) — never inferred from what its
screen shows. It is a reading, not a conclusion: *done* means only that the worker stopped, not that it
finished — a stopped worker may have left a verdict, be waiting on a question nobody will answer, or
have given up, and telling those apart is what the rest of the **no-PR** check is for. A *working*
state counts only while the terminal is still producing output, so a lost stop report cannot hold a
claim forever; a runtime that reports no state is read through orca's own view of whether its
terminal is idle (ADR-0021).
_Avoid_: liveness (that is the **heartbeat**, per fleet instance), status (the **status board**),
terminal busy/idle (the screen is not the source)

**Runtime**:
The agent binary that executes a **worker** session. One **fleet instance** runs one runtime, detected
at bootstrap from the launcher's own environment (`QODERCN_CLI=1` → `qoderclicn`; otherwise →
`claude`). It determines the stock **worker launch command** default: `qoderclicn
--dangerously-skip-permissions` or `claude --dangerously-skip-permissions`. A qoderclicn runtime is
always stock (no custom provider, no wrapping — the ask flow is skipped entirely); the Claude runtime
retains the full provider-parity machinery of ADR-0010. The runtime is a property of the fleet
instance, not of individual workers — no mixing within a run (ADR-0014).
_Avoid_: provider (that is the API backend, e.g. Anthropic), CLI (too generic), agent binary
(implementation-level)

**Worker launch command**:
The one shell string that starts every **worker** of a run — held by the **fleet instance**, injected
into each **tick**, and handed to orca verbatim. It is **opaque**: the fleet never parses it, composes
it, or appends to it, so it can be a provider alias (`ckimi`), a wrapper (`direnv exec . claude`), or
a script, and no credential ever enters the fleet. It exists because a launcher's provider lives only
in its environment — the wrapper's name is gone by the time the process exists, and its argv is
identical to a stock `claude` — while a worker starts in a fresh login shell that inherits none of it
and would otherwise fall back to stock Anthropic, silently, for days. Undetectable by construction, it
is therefore **supplied by the human** — passed with the invocation (`--worker-command`) or asked for at
bootstrap, the one question a launch can ask — but only when it can matter: a launcher with no custom
provider is never asked. Code still settles everything
around the answer: which wrappers exist to offer, whether the answer resolves to something runnable,
and whether an unattended flag is visible in it (ADR-0010).
_Avoid_: worker command (ambiguous with what the worker itself runs), agent command, provider profile
(the fleet deliberately does not model the provider — only the command), launch wrapper

**Local gate**:
The repo-local build/test command (`gate.local_command`) that, in `gate.ci: local` mode, *is* the
completion gate — promoted from the worker's optional pre-PR filter to the only machine verification
a PR must pass (ADR-0012). It runs twice in a PR's life: the **worker** runs it after its pre-PR
**sync**, so it tests "my code + current base"; and the **tick** re-runs it at merge time, after the
merge-time **sync**, in the branch's worktree (recreated from the branch tip when none survives
locally — the **continuation** tier-2 move). The invariant both runs serve: *what lands on the target
branch was tested in the form it lands.* GitHub checks are never read in this mode — the repo is
expected to scope remote CI away from worker branches, and a target branch whose protection requires
checks is rejected at bootstrap. A red run's log excerpt is posted as a PR comment, so the retry
ladder re-reads the failure from where it lives, never from a dead tick's context.
_Avoid_: local CI (it substitutes for CI; it is not CI), pre-push check, local build

**Sync**:
The one way a worker branch catches up with its base: merging `origin/<base>` into the branch —
never rebasing (ADR-0012). It happens twice in a PR's life: the **worker** syncs and pushes right
before its pre-PR **local gate**, so integration conflicts surface inside the worker's own session,
where they are cheapest to fix; and the **tick** syncs again at merge time (serialized), picking up
whatever the base gained since the worker's sync. Merge rather than rebase because a rebase drops
merge commits and re-ignites the conflicts already resolved inside them, and because squash-merging
makes the target-branch history identical either way. Retires `rebase_before_merge` (the config key
becomes `sync_before_merge`).
_Avoid_: rebase (retired from the merge path), rebase onto latest, update branch

**Mechanics vs judgment**:
The line that divides the fleet's work into what code owns and what the LLM owns. **Mechanics** are
the deterministic steps whose inputs uniquely fix the correct action, so a wrong result is a defect,
not a difference of opinion — selecting the **frontier**, claiming/reclaiming/releasing, the
mine/live-peer/stale partition, lease arithmetic, retry accounting, pacing, reading a worker's
**worker state**. They are extracted into tested **tools**. **Judgment** is everything that must read
context and can be reasonably contested — whether an implementation is correct (the gate), whether a
refutation holds, whether an empty diff really is empty, how to word an escalation. It stays with the **tick** (an LLM). "Extract mechanics to code, keep judgment in the
LLM" is the fleet's core build rule.
_Avoid_: automation vs decision, deterministic vs heuristic (near, but this is specifically the
code/LLM ownership split), script vs agent (the tick is not a script)

**Transition**:
One change of a **claim**'s state, performed as a single `afk` call that runs its whole ordered
sequence in code: **dispatch** (claim → worktree at the right commit → worker started → prompt
delivered → status board), **merge** (sync → gate → merge pinned to the gated head → status board →
release → cleanup), **hand-back** (abort the conflicted sync → instruct the worker → record on the PR
→ status board), **fail** (count the attempt, then a fresh retry or an escalation), **escalate**
(status board → relabel → comment → release, the release last), **park** (dependency edge → status
board → release → cleanup), **close** (status board → close → release → cleanup). A transition stops with an **outcome** exactly where the next move is judgment —
a sync conflict, a PR with no checks, a verification still owed — and takes the tick's judgment as an
argument (a reason, a verified head, "start fresh"). The tick therefore types no raw `git`, `gh` or
`orca` to act; orderings such as "relabel before release" are code under test, not prose (ADR-0017).
_Avoid_: recipe, procedure, step list (those were the prose a tick used to re-derive), action
(`action` is a field of an outcome)

**Cycle state**:
The one value the **launcher** carries between cycles: an opaque object `afk cycle` returns and takes
back verbatim, holding the last fingerprint, the skip streak, the empty streak and what the last
**tick** left in flight. The launcher never reads into it or does arithmetic on it; pacing, the
skipped cycle's heartbeat and the forced tick are all decided from it in code (ADR-0017).
_Avoid_: launcher memory, last summary (the summary is folded in and discarded)

**Fleet state**:
The authoritative record of the fleet's progress — what is claimed, in-flight, gated, merged,
escalated, and retried. It lives *outside* any context, in GitHub (claim/heartbeat refs, labels, PR
state, branches). It is the single source of truth; no tick holds it in memory.
_Avoid_: progress, run state, memory

**Working set**:
A tick's disposable, in-context view of the fleet at one moment. It is derived from fleet state and
is dropped when the tick returns.
_Avoid_: coordinator memory, session state

**Frontier**:
The set of currently-dispatchable issues — `open` + `ready_label` + not an epic + **unclaimed** (no
`afk-claim` ref) + **no open linked PR** + zero open `blocked_by`. Recomputed from GitHub every tick.
It is also what does the waiting for a **parked** issue: nothing else remembers that one is parked.
_Avoid_: queue, backlog (the backlog is the whole issue set; the frontier is only the ready edge)

**Claim**:
The atomic lock that marks one issue as owned by one **fleet instance**: a git ref `afk-claim/<n>` in
the hidden `refs/afk/*` namespace, whose creation the server accepts for exactly one fleet and rejects
for every other (that rejection is the compare-and-swap; a push that fails for any *other* reason is
an error, never a lost race — ADR-0015). Where the refs live is the config key `claim_namespace`,
one of exactly two layouts: `refs/afk` (`refs/afk/claim/<n>`), or `refs/heads` (ordinary
`afk-claim/<n>` branches), which bootstrap's probe switches to when an org ruleset forbids non-branch
refs. Every later call inherits it through the config, which every call must carry (ADR-0016). Its marker commit names the owning instance.
It is the single source of truth for "taken" — replacing the assignee, which under a shared account
cannot say *who* owns an issue. Deleted at every terminal transition; a leaked claim is a phantom lock
that silently starves an issue.
_Avoid_: assignee (dropped as a claim signal), assignment, lock (too generic)

**Heartbeat** (and its lease):
The liveness signal a **fleet instance** publishes for itself — one ref `afk-heartbeat/<id>` carrying
a timestamp, refreshed while it holds any claim (per instance, not per claim; roughly once per
`claim_lease_ttl_seconds`/3, not once per tick). A claim is leased-live while its owner's heartbeat is within
`claim_lease_ttl_seconds`; its freshness is the only thing that lets a peer tell a live owner from a dead one.
_Avoid_: ping, keepalive, liveness probe, **worker state** (that is per worker, read from orca — a
different thing, at a different granularity)

**Rebuild**:
The bounded pass, run at the top of every tick, that re-derives the whole working set from fleet
state: recompute the frontier, and reconstruct the in-flight set (the `afk-claim/<n>` refs owned by
this instance, sub-classified from each issue's PR + checks). Any tick — a fresh one or a later one — produces the
same working set from the same GitHub; this equivalence is the re-entrancy invariant that makes
disposable ticks safe. Its deterministic half is one read-only tool call — `afk rebuild`, which
gathers and assembles the working set (plus its fingerprint digest) in code. Why a `no_pr` claim has
no PR is a second, machine-dependent call — `afk no-pr`, which reads the worktree, the worker's
verdict marker and its blockers and returns the **outcome** (what the tick does about it — distinct
from the worker's *verdict*, which is only what the worker declared); it reads the **worker state**
first and gathers nothing more for a worker that is busy (ADR-0008, ADR-0015, ADR-0021).
_Avoid_: refresh, resync, reload

**Orphaned claim**:
One of **my own** claims (an `afk-claim/<n>` owned by this instance) with no PR and no live worker — a
claim whose worker crashed or never started. Reconciled *locally* on every rebuild by **continuation**
— recovered from its durable progress (the local worktree if still present, else the pushed branch)
and only re-dispatched fresh when nothing survives, or released — never assumed still-running.
Contrast **Stale claim**, which is a peer's.
_Avoid_: stuck issue, dead worker, zombie

**Stale claim**:
A **peer's** claim whose owner's **heartbeat** has expired past `claim_lease_ttl_seconds` — evidence the owning
instance died mid-flight. It is the only claim a fleet may take from another *unattended*: reclaimed
by an atomic `git push --force-with-lease` takeover of the ref, and only then, then recovered by
**continuation**. A live peer's claim is never touched — that is what keeps cooperating fleets from
cannibalising each other's in-flight work. The lease-bypassing, human-authorized sibling of this
reclaim is the **Takeover**. A stale claim whose issue is already **closed** is not work to continue
but a **phantom lock** — the owner finished the issue and died before releasing — so the rebuild
lists it apart (`stale_closed`) and it is deleted under the same lease, never reclaimed.
_Avoid_: dead claim, abandoned claim, orphaned claim (that is one's *own* worker-less claim)

**Takeover**:
The **human-authorized**, **immediate** reclaim of a dead **fleet instance**'s claims — the
lease-bypassing sibling of **stale-claim** reclaim. Where a stale reclaim is unattended and waits for
the owner's **heartbeat** to expire past `claim_lease_ttl_seconds` (the only machine-visible proof of death),
a takeover is initiated by a present human who *is* the proof of death — the oracle that knows, before
the lease lapses, that the fleet hard-stopped (quota exhausted, process killed). It is a **launcher**
bootstrap variant (`afk-fleet --takeover`): the new instance runs the full bootstrap (config, instance
id, **worker launch command**), then lists the instances
discoverable in the claim markers and heartbeat refs and, on the human's selection, force-takes the
chosen instance's claims with the *same* atomic `--force-with-lease` push as a stale reclaim — only
skipping the staleness gate. A target whose heartbeat is still fresh prompts an explicit confirm,
since the human may be wrong. Thereafter it is an ordinary standing fleet whose opening working set is
the dead peer's claims plus the **frontier**. Claims taken are recovered by **continuation**, and a
takeover never counts as a **retry** (ADR-0011).
_Avoid_: failover (implies automatic), rescue (it seeds a standing fleet, not a bounded mission),
stale reclaim (that is the unattended, lease-gated path)

**Nudge**:
The one line the fleet types at a live **worker** that went idle past the grace period with no PR and
no verdict — the `idle_stalled` **outcome** of `afk no-pr`. Such a worker has not failed; it stopped
without an outcome, usually to ask a question nobody will answer. A nudge tells it to carry on, is
sent **once per worker** (recorded in the worktree's git dir), spends no **retry** and discards
nothing; a worker still silent a grace period later is a failure, and the last screen of its terminal
travels in the failure reason. That screen is the only thing the fleet ever reads from a worker's
terminal, and it is read for *where the worker stopped*, never for its result (ADR-0018).
_Avoid_: ping, poke, retry (a retry discards the attempt; a nudge keeps it), reminder

**Park**:
What the fleet does with a dependency a **worker** discovered — a `blocked` verdict naming issues
that are still open — when the backlog will resolve them on its own: each open blocker is one a fleet
holds a **claim** on, has an open PR, or carries `ready_label`. The missing dependency is written onto
the issue as a native `blocked_by` edge, the claim is released, and the **frontier** contract does the
rest: the issue is excluded while a blocker is open and dispatchable again the tick after the last one
closes. No label changes, no **retry** is spent, no human is involved — and it cannot loop, because
the edge keeps the issue off the frontier for exactly as long as the worker would report `blocked`
again. One **transition** (`afk park`). A blocker nothing will resolve — missing, closed as not
planned, an epic, open with no fleet to work it, or one that would close a dependency cycle — is still
escalated (ADR-0022).
_Avoid_: defer, snooze, hold (nothing is held: the claim is released), blocked (that is the worker's
verdict; park is what the fleet does about it), escalate (that hands the issue to a human)

**Wake**:
The one line a **worker** types into the **launcher**'s terminal once its outcome is on GitHub — a PR,
a verdict marker, a **hand-back**'s resolution pushed: `afk-wake #<n>`. It ends the launcher's sleep
so the next cycle opens now rather than a busy interval later, and it carries nothing: the cycle it
triggers reads GitHub like any other, and the launcher never acts on the line itself. A lost wake
costs only the wait it would have saved — polling is unchanged underneath. The worker is handed the
line ready-made in its prompt; the launcher's terminal handle is read from the environment by the
**transition** that fills the prompt, never configured (ADR-0020).
_Avoid_: notification (nothing is conveyed, and no human is told), callback, done signal (the outcome
is the PR or the verdict, not this), **nudge** (that is fleet → worker; a wake is worker → launcher)

**Hand-back**:
Returning a **sync** conflict to the **worker** that wrote the branch, instead of failing the claim.
When the merge-time sync conflicts, the work is finished and gate-green — only the target moved — so
the **tick** hands the conflict back in one **transition** (`afk hand-back`): the worker is told to
merge the target in (never rebase), resolve, re-run the **local gate** and push to the same PR. The
claim, the PR, the branch and the worktree are kept and no **retry** is spent. It is recorded as a
marker comment on the PR naming the target tip; while the PR head does not contain that tip the claim
is `handed_back` — never `awaiting_merge` — and its worker is watched like a PR-less one, so an
unanswered hand-back is **nudged** and then failed. A worker whose terminal is gone is replaced by
**continuation** in the same worktree, started on the hand-back (ADR-0019). The push that answers a
hand-back is not yet the worker's outcome: it usually pushes the merge and gates it afterwards, so a
claim can be `awaiting_merge` with its worker still busy — and the **merge** transition stays out of
a worktree whose worker is busy, stopping with `worker_busy` (ADR-0024).
_Avoid_: retry (nothing is discarded), bounce, re-dispatch (the worker and its worktree are kept),
conflict resolution (that is what the worker then does)

**Continuation**:
The fleet's default way of recovering a claim whose **worker** died mid-flight — recovering it *from
its durable progress* rather than re-dispatching fresh. It is tiered by what survived the death:
first the worker's local worktree, if it is still on this machine (resumed in place — lossless,
capturing even uncommitted/unpushed work); else the branch the worker incrementally pushed to GitHub
(a new worktree recreated at its tip); and only when nothing survives, a fresh re-dispatch from base
(the old behaviour). It is what makes a **takeover** and a **stale-claim** reclaim actually *continue*
work instead of restarting it, and bounds a hard-stop's loss to "since the last push." Because
progress accumulates across continuations, a claim taken over repeatedly converges rather than loops —
which is why a takeover is kept orthogonal to the retry ladder. It is a *recovery behaviour* of the
existing rebuild path, not a mode (ADR-0011).
_Avoid_: resume (too narrow — covers only the in-place worktree tier), re-dispatch (that is the fresh
last tier), checkpoint restore (the fleet does not model checkpoints as objects; the branch tip is the
progress)

**Status board** (a.k.a. progress comment):
The human-facing projection of an issue's lifecycle onto the issue surface: a **single** comment the
owning **fleet instance**'s **tick** upserts each **rebuild**, rendering a milestone checklist (claimed
→ PR open → gate green → merged, with the *ci-failed*, *handed-back*, *escalated* and *parked* off-ramps) **derived** from
**fleet state**. It exists because the **claim** lives in a hidden ref namespace and the assignee is
unused, so the "claimed but no PR yet" phase is otherwise invisible to a reader. It is a *rendering* of
existing state, **never a source of truth** and **never read back by a tick**; it is edited in place
(idempotent — identical state renders identical text, so re-entrant ticks don't churn it), never
appended. Contrast the **escalation comment**, which is a durable, appended, re-readable handoff record
the board merely points to (ADR-0006).
_Avoid_: progress log (it is upserted, not appended), worklog, status label (a label is machine state;
this is human narration), progress ref
