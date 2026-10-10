# afk-fleet

An unattended fleet that works a GitHub-issue backlog on its own, for days, in one session that can
be compacted at any point and lose nothing: a **launcher** runs one cycle after another, and each
cycle's **tick** — one reconciliation pass, run in code — dispatches worktree-isolated **workers** per ready issue and gives each finished PR its
**landing turn**, on which its worker gates it and lands it on the target branch — one PR at a time, or, where
several may land together, as one **merge batch** behind a single gate run.

## Language

**Launcher**:
The interactive session `/afk-fleet` is invoked in. Invoking it is the launch: it has the human
confirm the **base branch** and nothing else, mints a **fleet-instance** id, then loops: run one cycle (`afk cycle`, whose **tick** runs in code),
answer the **judgments** it returns, keep the **cycle state**, sleep, repeat. It is the one session that
runs each tick, and the one LLM a judgment reaches. Its context is bounded by ordinary
auto-compaction, which is safe by construction: after any compaction it needs only the repo, the
config and the last cycle state, and no rule of the pass is its to remember (ADR-0028). It still
never computes the **frontier** in its own context and never reads a worker. Several launchers — on several machines,
even under one GitHub account — may run against the same repo at once; they cooperate only through
**claims**, never a central coordinator.
_Avoid_: coordinator (**fleet state** is all in GitHub: no session holds it, and a launcher that
forgot everything but its cycle state carries on),
orchestrator, manager, main agent

**Fleet instance**:
One launcher run and everything it owns — the ticks it runs, the workers they dispatch, and the
**claims** it holds — identified by an id minted at bootstrap, in the one grammar `afk` admits as
`--instance` (lowercase letters, digits and `-`: ADR-0044). That id
and the **worker launch command** are the run's two launcher-held facts:
settled once at bootstrap, passed to the first cycle and riding in the **cycle state** from then on —
not in the launcher's own memory, which a compaction may cut —
never written to a file, and gone when the launcher stops. Its liveness is published as a **heartbeat**; when it stops or dies
its claims are released or reclaimed. Distinct instances (even on one GitHub account) are the unit of
cooperative concurrency across machines.
_Avoid_: node, worker (that is the per-issue coding agent), coordinator

**Tick**:
One reconciliation pass. It is code, inside one call (`afk cycle`): it rebuilds the working
set from fleet state and acts once (give a finished PR its **landing turn**, escalate exhausted, dispatch to free slots — each a
single **transition**), without waiting for the workers it dispatched, and returns what it could not
decide as **judgments**. It is a pass, not a context: the **launcher** makes the call, and nothing
of a tick outlives it but the **cycle state**. A run lasts because every tick starts from GitHub and
the procedure is code, not because one session stays disciplined (ADR-0028).
_Avoid_: batch (implies draining a whole wave), poll (a tick acts, not just observes), coordinator,
subagent (a tick was one, per cycle, until ADR-0028)

**Plan tick**:
A **tick** run in dry-run mode (`--plan`): it does the full **rebuild** (recompute the **frontier**,
classify claims into mine/peer-live/stale) and then **stops before the Act phase**, returning the
dispatch plan instead of granting/dispatching/reclaiming. It is the *same* procedure as an acting tick,
short-circuited — so the plan a human reads cannot drift from what a live tick will actually do. It
is how to look before launching: `/afk-fleet --plan`. A launch itself runs none (ADR-0023).
_Avoid_: dry run (that is its mode, not its name), preview pass

**Worker**:
A fire-and-forget, ephemeral coding-agent session (Claude Code or qoderclicn — the run's **runtime**),
isolated in one git worktree, that owns exactly one
issue, opens a PR, reports done via GitHub, and then **wakes** the launcher. It merges only on its **landing turn** — its own PR, with `afk land`, never otherwise — and its terminal is never read for
its result (only, once it has gone silent with no outcome, for *where it stopped* — see **Nudge**). Its worktree is created and later torn down by the **worker backend** — orca (`orca
worktree create` / `orca worktree rm`), the only supported backend — never by the tick with raw `git
worktree`; orca also names the branch (a `<user>/…` prefix), and the tick **reads that back** rather
than dictating it (ADR-0005). It is started by running the **worker launch command** in the
worktree's only terminal — the bare shell orca opens a new worktree on is closed first — so it runs
on the same runtime as the **launcher** that dispatched it.
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
The one shell string that starts every **worker** of a run — held by the **fleet instance**, carried
in the **cycle state**, and handed to orca verbatim. It is **opaque**: the fleet never parses it, composes
it, or appends to it, so it can be a provider alias (`ckimi`), a wrapper (`direnv exec . claude`), or
a script, and no credential ever enters the fleet. It exists because a launcher's provider lives only
in its environment — the wrapper's name is gone by the time the process exists, and its argv is
identical to a stock `claude` — while a worker starts in a fresh login shell that inherits none of it
and would otherwise fall back to stock Anthropic, silently, for days. Undetectable by construction, it
is therefore **supplied by the human** — passed with the invocation (`--worker-command`) or asked for at
bootstrap, one of the two questions a launch can ask (the other is the **base branch**) — but only when it can matter: a launcher with no custom
provider is never asked. Code still settles everything
around the answer: which wrappers exist to offer, whether the answer resolves to something runnable,
and whether an unattended flag is visible in it (ADR-0010).
_Avoid_: worker command (ambiguous with what the worker itself runs), agent command, provider profile
(the fleet deliberately does not model the provider — only the command), launch wrapper

**Base branch**:
The one branch a repo's fleet work goes to: every **worker** cuts its branch from it, opens its PR
against it, and its **landing** merges into it — where the fleet's mandate ends. It has no default
and belongs to no file: the human **confirms it at every launch** — read back and answered, or
passed with the invocation (`--base-branch`) — and it is kept on the remote, so every **launcher**
on the repo reads the same one. It may become another branch only while nothing stands on it: no
**claim**, and no live **fleet instance**. A **takeover** therefore inherits the dead fleet's. It
need not be the repo's default branch, and the fleet never creates it (ADR-0042).
_Avoid_: trunk, main (it may be neither), default branch (that is GitHub's, and only offered as
an answer)

**Local gate**:
The repo-local build/test command (`gate.local_command`) that, in `gate.ci: local` mode, *is* the
completion gate — promoted from the worker's optional pre-PR filter to the only machine verification
a PR must pass (ADR-0012). It runs twice in a PR's life: the **worker** runs it after its pre-PR
**sync**, so it tests "my code + current base"; and the worker's **landing** runs it again, after the
landing's **sync**, in the same worktree. The invariant both runs serve: *what lands on the target
branch was tested in the form it lands.* A green run on a committed tree becomes a **recorded gate
run**, and a landing skips its own run when one stands for the tree that would land. A run is on a
committed tree only when the worktree is exactly its commit before the run and after it — nothing
uncommitted, nothing untracked; a landing refuses to merge on any other run. GitHub checks
are never read in this mode — the repo is
expected to scope remote CI away from worker branches, and a target branch whose protection requires
checks is rejected at bootstrap. A red run at landing is the worker's to fix in place — off the turn, the first time (ADR-0045); its log
excerpt is also posted as a PR comment, so a failure that does reach the retry ladder is re-read
from where it lives, never from anyone's context. In a **merge batch** the landing's run is
made once, on the batch's stack, and proves the stack rather than each PR alone — the invariant
holds for the commit the target is moved to (ADR-0029).
_Avoid_: local CI (it substitutes for CI; it is not CI), pre-push check, local build

**Recorded gate run**:
The evidence that the **local gate** passed on a piece of content: one green run, of one command, on
one committed tree. It is made by the tool that saw the exit code — never by a worker saying so —
and kept on GitHub under the tree it tested, so it stands wherever that same content is about to
land: the worker's worktree, one recreated from the pushed branch, another machine, another commit
holding the same files, a **merge batch**'s stack. It is always trusted while it stands, and it
stops standing when the same tree is later run red, or after a day. One stamped from the future
never stood (see **heartbeat**). A **sync** that brought the
target in or a later commit makes a different tree, which has its own record or none — and with
none, the landing runs the gate (ADR-0030).
_Avoid_: gate cache (it is evidence, not an optimisation that may be wrong), cached result, CI
status (GitHub's checks are a different gate)

**Sync**:
The one way a worker branch catches up with its base: merging `origin/<base>` into the branch —
never rebasing (ADR-0012). It happens twice in a PR's life: the **worker** syncs and pushes right
before its pre-PR **local gate**, so integration conflicts surface inside the worker's own session,
where they are cheapest to fix; and the worker's **landing** syncs again, on its **landing turn**, picking up
whatever the base gained since — a conflict there is left in progress for the same worker to resolve, off the turn the first time (ADR-0045). Merge rather than rebase because a rebase drops
merge commits and re-ignites the conflicts already resolved inside them — and those merge commits
land on the target as they are: a PR lands as a merge commit, never squashed (ADR-0034). It is not an option: there is no config key for it (ADR-0038).
_Avoid_: rebase (retired from the merge path), rebase onto latest, update branch

**Mechanics vs judgment**:
The line that divides the fleet's work into what code owns and what the LLM owns. **Mechanics** are
the deterministic steps whose inputs uniquely fix the correct action, so a wrong result is a defect,
not a difference of opinion — selecting the **frontier**, claiming/reclaiming/releasing, the
mine/live-peer/stale partition, lease arithmetic, retry accounting, pacing, reading a worker's
**worker state**. They are extracted into tested **tools**. **Judgment** is everything that must read
context and can be reasonably contested — whether an implementation is correct (the gate), whether a
refutation holds, whether an empty diff really is empty, how to word an escalation. It stays with the **launcher** (an LLM): the tick returns it as a **judgment** instead of deciding it. "Extract mechanics to code, keep judgment in the
LLM" is the fleet's core build rule.
_Avoid_: automation vs decision, deterministic vs heuristic (near, but this is specifically the
code/LLM ownership split), script vs agent

**Judgment** (as returned by a cycle):
One question a **tick**'s pass could not decide, handed back by `afk cycle` instead of decided: is
an empty diff really empty, may a PR with no checks land, does a head survive the adversarial
verify, how is a failure or an escalation worded. It carries the one **transition** to run for
either answer, ready to run, so answering it *is* a transition and the next cycle does not ask
again; there is nothing to resume. One whose answer takes reading something long (a diff under
review, a CI log) is marked for an ephemeral subagent — the launcher's only use of one. A step whose reason is already on record is
not a judgment — the pass performs it (ADR-0017).
_Avoid_: question, prompt, decision point, outcome (an **outcome** is where one transition stopped;
a judgment is what a whole pass returns)

**Transition**:
One change of a **claim**'s state, performed as a single `afk` call that runs its whole ordered
sequence in code: **dispatch** (claim → worktree at the right commit → worker started → prompt
delivered → status board), **turn** (grant the **landing turn**: the tick's judgments checked → the
brief written → the turn recorded on the PR → the worker told, or continued onto it → status board), **fail** (count the attempt, then a fresh retry or an escalation), **escalate**
(status board → comment → relabel → release, the release last), **park** (dependency edge → status
board → release → cleanup), **close** (status board → close → release → cleanup). A transition stops with an **outcome** exactly where the next move is judgment —
a PR with no checks, a verification still owed — and takes the tick's judgment as an
argument (a reason, a verified head, "start fresh"). The tick therefore types no raw `git`, `gh` or
`orca` to act; orderings such as "relabel before release" are code under test, not prose (ADR-0017).
_Avoid_: recipe, procedure, step list (those were the prose a tick used to re-derive), action
(`action` is a field of an outcome)

**Cycle state**:
The one value the **launcher** carries between cycles: an opaque object `afk cycle` returns and takes
back verbatim, holding the fingerprint of the fleet as the last **tick** left it, the skip streak, the empty streak, what that
tick left in flight, whether it left anything unsettled (a judgment, an error, a PR that opened while it ran) and which **status board** each claim already shows — and the **fleet instance**'s two facts, its id and the
**worker launch command**, from the first cycle on. The launcher never reads into it or does arithmetic on it; pacing, the
skipped cycle's heartbeat and the forced tick are all decided from it in code (ADR-0017).
_Avoid_: launcher memory, last summary (a tick's account of what it did is folded in by the same call, and never travels)

**Fleet state**:
The authoritative record of the fleet's progress — what is claimed, in-flight, gated, merged,
escalated, and retried. It lives *outside* any context, in GitHub (claim/heartbeat refs, labels, PR
state, branches). It is the single source of truth; no tick holds it in memory.
_Avoid_: progress, run state, memory

**Working set**:
A tick's view of the fleet at one moment. It is derived from fleet state inside the `afk cycle`
call and dropped when that call returns: it never enters a context.
_Avoid_: coordinator memory, session state

**Frontier**:
The set of currently-dispatchable issues — `open` + `ready_label` + not an epic + **unclaimed** (no
claim ref) + **no open linked PR** + zero open `blocked_by`. Recomputed from GitHub every tick, over
every open issue: the issue list carries each one's open-blocker count, and the pull requests GitHub lists among them are left out.
Dispatched in issue-number order, lowest first — the issues' own order, never the one a read happened to list them in.
It is also what does the waiting for a **parked** issue: nothing else remembers that one is parked.
_Avoid_: queue, backlog (the backlog is the whole issue set; the frontier is only the ready edge)

**Claim**:
The atomic lock that marks one issue as owned by one **fleet instance**: a git ref `refs/afk/claim/<n>` in
the hidden `refs/afk/*` namespace, whose creation the server accepts for exactly one fleet and rejects
for every other (that rejection is the compare-and-swap; a push that fails for any *other* reason is
an error, never a lost race — ADR-0015). Where the refs live is the run's `claim_namespace`,
which no file sets (ADR-0038) — one of exactly two layouts: `refs/afk` (`refs/afk/claim/<n>`), or `refs/heads` (ordinary
`afk-claim/<n>` branches), which bootstrap's probe switches to when an org ruleset forbids non-branch
refs. Every later call inherits it through the config, which every call must carry (ADR-0016). Its marker commit names the owning instance.
It is the single source of truth for "taken" — replacing the assignee, which under a shared account
cannot say *who* owns an issue. Deleted at every terminal transition; a leaked claim is a phantom lock
that silently starves an issue.
_Avoid_: assignee (dropped as a claim signal), assignment, lock (too generic)

**Heartbeat** (and its lease):
The liveness signal a **fleet instance** publishes for itself — one ref `refs/afk/heartbeat/<id>` carrying
a timestamp, refreshed before it takes a claim and while it holds any (per instance, not per claim; roughly once per
a third of the claim lease, not once per tick) — so a claim is never on the remote ahead of its owner's heartbeat. A claim is leased-live while its owner's heartbeat is within
the claim lease (`CLAIM_LEASE_TTL_SECONDS`, the same in every repo); its freshness is the only thing that lets a peer tell a live owner from a dead one.
The timestamp is the writing host's clock, so freshness has a floor as well as a ceiling: a stamp
ahead of the reader by more than the skew tolerance (`CLOCK_SKEW_TOLERANCE_SECONDS`, the one place
skew is allowed for) is not evidence — a heartbeat from the future is stale, a **recorded gate run**
from the future does not stand, and a worker's sign of life from the future is no sign. Within the
tolerance a stamp ahead reads as "just now".
_Avoid_: ping, keepalive, liveness probe, **worker state** (that is per worker, read from orca — a
different thing, at a different granularity)

**Rebuild**:
The bounded pass, run at the top of every tick, that re-derives the whole working set from fleet
state: recompute the frontier, and reconstruct the in-flight set (the claim refs owned by
this instance, sub-classified from each issue's PR + checks). Any tick — a fresh one or a later one — produces the
same working set from the same GitHub; this equivalence is the re-entrancy invariant that makes
a killed tick, and a launcher that forgot the last one, safe. Its deterministic half is one read-only tool call — `afk rebuild`, which
gathers and assembles the working set (plus its fingerprint digest) in code: three reads — open issues, open PRs, the claim refs — made at once,
and each read once for the whole tick, kept in step with what the tick then writes. Why a `no_pr` claim has
no PR is a second, machine-dependent call — `afk no-pr`, which reads the worktree, the worker's
verdict marker and its blockers and returns the **outcome** (what the tick does about it — distinct
from the worker's *verdict*, which is only what the worker declared); it reads the **worker state**
first and gathers nothing more for a worker that is busy (ADR-0008, ADR-0015, ADR-0021).
_Avoid_: refresh, resync, reload

**Orphaned claim**:
One of **my own** claims (a claim ref owned by this instance) with no PR and no live worker — a
claim whose worker crashed or never started. Reconciled *locally* on every rebuild by **continuation**
— recovered from its durable progress (the local worktree if still present, else the pushed branch)
and only re-dispatched fresh when nothing survives — never assumed still-running, and never released
back to the frontier by an unattended run.
Contrast **Stale claim**, which is a peer's.
_Avoid_: stuck issue, dead worker, zombie

**Stale claim**:
A **peer's** claim whose owner's **heartbeat** has expired past the claim lease — evidence the owning
instance died mid-flight. It is the only claim a fleet may take from another *unattended*: reclaimed
by an atomic `git push --force-with-lease` takeover of the ref, and only then, then recovered by
**continuation**. A tick reclaims one only into a free slot under `concurrency` — lowest issue number
first, ahead of the frontier — and leaves the rest stale for a later tick (ADR-0044). A live peer's claim is never touched — that is what keeps cooperating fleets from
cannibalising each other's in-flight work. The lease-bypassing, human-authorized sibling of this
reclaim is the **Takeover**. A stale claim whose issue is already **closed** is not work to continue
but a **phantom lock** — the owner finished the issue and died before releasing — so the rebuild
lists it apart (`stale_closed`) and it is deleted under the same lease, never reclaimed.
_Avoid_: dead claim, abandoned claim, orphaned claim (that is one's *own* worker-less claim)

**Takeover**:
The **human-authorized**, **immediate** reclaim of a dead **fleet instance**'s claims — the
lease-bypassing sibling of **stale-claim** reclaim. Where a stale reclaim is unattended and waits for
the owner's **heartbeat** to expire past the claim lease (the only machine-visible proof of death),
a takeover is initiated by a present human who *is* the proof of death — the oracle that knows, before
the lease lapses, that the fleet hard-stopped (quota exhausted, process killed). It is a **launcher**
bootstrap variant (`afk-fleet --takeover`): the new instance runs the full bootstrap (config, instance
id, **base branch**, **worker launch command**), then lists the instances
discoverable in the claim markers and heartbeat refs and, on the human's selection, force-takes the
chosen instance's claims with the *same* atomic `--force-with-lease` push as a stale reclaim — only
skipping the staleness gate. A target whose heartbeat is still fresh prompts an explicit confirm,
since the human may be wrong. Thereafter it is an ordinary standing fleet whose opening working set is
the dead peer's claims plus the **frontier**. Claims taken are recovered by **continuation**, and a
takeover never counts as a **retry** (ADR-0011).
_Avoid_: failover (implies automatic), rescue (it seeds a standing fleet, not a bounded mission),
stale reclaim (that is the unattended, lease-gated path)

**Verdict** (a worker's):
What a **worker** that opens no PR declares instead, as an `afk:verdict` marker leading a comment on
its issue — one of four phases, told apart by **who can supply what is missing**: nothing is
(`already-satisfied`: the issue is closed once the empty diff is confirmed), the backlog can
(`blocked`: **park**), the next worker can (`giving-up`: **retry**), or only the issue's owner can
(`needs-decision`: the issue as written has a premise that does not hold, or criteria that
contradict each other — escalated at once, no **retry** spent, because a fresh worker reads the
same issue and stops at the same question; ADR-0041). A verdict is an input: what the fleet
concludes from it and from the worktree is the **outcome**.
_Avoid_: outcome (that is the fleet's conclusion), result (that is the PR), a gate's verdict (what
one gate run came to), `giving-up` for an issue nobody could do as written

**Retry**:
What a failed attempt costs and gets: the failure is counted on the issue — its `afk-attempt/<n>`
label goes up by one — the failed attempt is discarded (its PR closed, its branch deleted, its
worktree removed — the branches the fleet recorded on the issue as it cut them, never one a person
gave a like name: ADR-0043) and a fresh **worker** starts from the base under the same **claim**, told why the
last attempt failed — the branch is never handed on as-is (ADR-0017; the sentence of ADR-0013 that
said otherwise is superseded). Config `retry` is how many an issue gets; the failure after the last one is
escalated to a human instead. One **transition** (`afk fail`), and its one writer. A failure is
counted **once**: the edit that raises the number also adds `afk-attempt/starting` — this failure is
counted, its fresh worker has not started — and starting a worker removes it, so an `afk fail` that
was cut short after counting and runs again (by hand, or from the next **tick**, which reads the
label as the row's `starting`) finishes the same retry instead of spending another. A new failure
of the fresh attempt finds no such label and is counted. An escalation cut short is finished the
same way: its comment records whose it is — the **claim**'s — and after how many retries, before
the relabel strips the count, so the same failure failed again escalates, with no second comment,
and never starts the retries over (ADR-0033).
_Avoid_: re-dispatch (that is a **continuation**: nothing discarded, nothing counted), nudge,
restart (a silent worker on a **landing turn** is restarted onto it by continuation — nothing
discarded, nothing counted; ADR-0035), attempt (the attempt is the thing that failed; the retry is
what replaces it)

**Nudge**:
The one line the fleet types at a live **worker** that went idle past the grace period with no PR and
no verdict — the `idle_stalled` **outcome** of `afk no-pr`. Such a worker has not failed; it stopped
without an outcome, usually to ask a question nobody will answer. A nudge tells it to carry on, is
sent **once per worker** (recorded in the worktree's git dir), spends no **retry** and discards
nothing; a worker still silent a grace period later is a failure, and the last screen of its terminal
travels in the failure reason — except on a **landing turn**, where that second silence restarts the
worker onto the turn once, and the restarted worker's own unanswered nudge escalates the claim with
its PR kept, the screen travelling in the escalation's reason instead (ADR-0035). That screen is the
only thing the fleet ever reads from a worker's terminal, and it is read for *where the worker
stopped*, never for its result (ADR-0018).
_Avoid_: ping, poke, retry (a retry discards the attempt; a nudge keeps it), reminder, restart (a
restart replaces the worker; a nudge keeps it)

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
a verdict marker, a **landing** that merged, stopped for the tick, gave its turn up or made its PR ready again: `afk-wake #<n>`. It ends the launcher's sleep
so the next cycle opens now rather than a busy interval later, and it carries nothing: the cycle it
triggers reads GitHub like any other, and the launcher never acts on the line itself. One that arrives
while a cycle is running is passed to the next (`afk cycle --wake`), which then ticks whatever its
fingerprint says — the running tick may already have digested, unseen, what the wake was about. A lost wake
costs only the wait it would have saved — polling is unchanged underneath. The worker is handed the
line ready-made in its prompt; the launcher's terminal handle is read from the environment by the
**transition** that fills the prompt, never configured (ADR-0020).
_Avoid_: notification (nothing is conveyed, and no human is told), callback, done signal (the outcome
is the PR or the verdict, not this), **nudge** (that is fleet → worker; a wake is worker → launcher)

**Landing turn**:
The fleet's permission for one finished PR to land, granted by the **tick** in one **transition**
(`afk turn`) and held by one PR of a **fleet instance** at a time — or by
the several PRs of one **merge batch** at once (ADR-0029): one turn, two kinds of holder, which land
in different ways and are deliberately not one abstraction (ADR-0036). It is recorded as a single marker
comment on the PR naming the instance that granted it — so it dies with the PR, and does not survive
a **takeover** — and it carries the tick's judgments made *before* the grant (the head an adversarial
verify passed, a PR with no checks waived) and where the worker's last `afk land` stopped. A turn
covers the bounded part of a **landing** — sync, gate, merge: the first time a PR's landing stops on
a conflict or a red gate the PR **gives its turn up** — the marker says so, for good, and holds no
turn; the claim is `fixing`, the same worker fixes in the same worktree, and the next cycle grants
the turn elsewhere. A PR gives its turn up **once**: on its next turn the same stop keeps the turn
(ADR-0045). While its
PR holds the turn the claim is `landing`, and its worker is watched like a PR-less one — as is one
fixing off a turn its PR gave up, on the same rungs: silent past
grace it is **nudged**; silent again it is **restarted onto the turn** — the idle session closed, a
worker started by **continuation** in the same worktree (or one recreated at the PR's head, never
from base), briefed only to land the PR, with the PR, branch, worktree and attempt untouched and the
restart recorded on the turn marker — once per turn; silent again after the restarted worker's own
nudge, the claim is **escalated**: the PR stays open, the branch and worktree stay, no attempt is
spent, and the released claim holds no turn — so the next PR gets it (ADR-0035). That is the whole
bound of a turn, and of a fix off one — told → grace → nudge → grace → restart → grace → nudge → grace → escalate — and
nothing is discarded at any step: `afk fail` reaches a landing claim only by the tick's own
judgments (red checks in `required`, a refuted verify), never by silence. A worker whose terminal
is gone gets that same continuation at once, unbounded. The check
guards against a worker that strays, not a malicious one — worker and launcher share one `gh`
credential (ADR-0027).
_Avoid_: lock, merge lock (nothing is held on the target; the turn is a record on the PR), token,
approval (no human or review is involved), turn lost / revoked / taken back (the landing gives it up
itself; nobody takes it)

**Landing**:
What a **worker** does on its **landing turn**, with one command in its own worktree (`afk land`):
**sync** with the merge target → push → the machine gate on that exact head → merge pinned to the
gated head. It stops with an outcome the worker acts on itself: a **sync** conflict is left in
progress and resolved in place, a red gate is fixed in place, and the worker lands again — no round
trip through the tick, no **retry** spent, the PR kept. The turn is kept only from a PR's second turn
on: the first such stop **gives the turn up**, and the worker fixes off it. Run off the turn, the
same command still syncs and gates and merges nothing; green there, the PR is **ready again**
(`awaiting_turn`), the worker **wakes** the launcher and stops, and the PR waits for its next turn at
the head of the **merge queue**. Nothing lands off a turn (ADR-0045). Checks that must run on the head
it pushed are waited for by the landing itself, up to a bound. A gate run or that wait is long, so
right before the merge the landing reads the turn and the target's tip again: a turn no longer its
own lands nothing, and a target that moved is synced with and gated by the next run. Where the next move is the
tick's (checks still running when that bound runs out, a verify owed on a moved head, absent checks) the
worker **wakes** the launcher and stops, and the tick tells it to land again. The claim and the
worktree are settled by the next cycle, from the claim whose issue is now closed (ADR-0027).
_Avoid_: hand-back (retired: the tick no longer syncs a PR and returns its conflict — the worker
meets the conflict itself, on its turn), tick-side merge, auto-merge (the tick merges nothing),
merge-time gate run (the gate run is the landing's)

**Merge batch**:
One **landing turn** held by several finished PRs of a **fleet instance** at once, so that they land
behind ONE run of the **local gate** instead of one each (ADR-0029; `gate.ci: local` only, and
never an option: where it can form it does, ADR-0034). Its whole record is the turn marker on every member PR, naming the batch,
its members and its phase — `stacking`, `gating` or `fixing` — and it is the only place the members
are kept: the batch's worktree holds no list of them. A **batch worker** stacks the members
on the target's tip with one merge commit per PR, in **merge queue** order, gates the stack once, and
pushes it to the target as a fast-forward: that push is the only lock, and a target that moved
refuses it. Each PR's own head is then on the target, so GitHub shows it *merged* by itself. What is
on the target has landed, whatever cut the batch worker short after the push: a member whose issue is
still open is settled by the next **tick** from its commit there, and an abandon leaves it alone. A red stack is repaired with a fix commit on top, never
bisected. Whether the turn goes to a batch or to one PR is decided in code
(`afk_decide.batch_candidates`): never while a turn is out, never a PR that owes an adversarial
verify, whose own worker is still working, that gave a turn up, or that is a peer's. A PR being fixed
off a turn it gave up holds no turn, so a batch forms beside it; ready again, it takes a single turn
first (ADR-0045). A PR that leaves a batch without
landing — *left out* because it conflicts with the stack, or because the batch was *abandoned* or
*dissolved* — takes a single turn next and is never batched again.
_Avoid_: merge train (nothing is speculatively gated, and there is one batch at a time, not a
pipeline of them), rollup PR (no PR is opened for a batch), bisect (a red batch is fixed, not
searched)

**Batch worker**:
The **worker** that lands a **merge batch**, with one command (`afk land --batch`) in a worktree of
the batch's own, cut at the target's tip and linked to no issue. It wrote none of the PRs it lands,
holds no **claim**, takes no dispatch slot and spends nobody's **retry**. It is watched like any
worker holding a turn: gone, it is replaced by **continuation** in the batch's worktree, else from
the batch's pushed branch; silent past grace it is **nudged** once, and silent again the batch is
abandoned with nothing landed — which fails no PR (ADR-0029).
_Avoid_: merger, integrator, release manager (it decides nothing: which PRs, in what order, and
whether to batch at all are the tick's, in code)

**Merge queue**:
The order **landing turns** are granted in (ADR-0027): among a **fleet instance**'s ready PRs, the
one that already holds a turn first, then one that gave a turn up and is ready again (ADR-0045), then
one that left a **merge batch** without landing, then the
lower PR number, then — one PR closing several issues — the lower issue number: a total order,
whatever order the claims were read in. `afk rebuild` returns it as
`merge_order`, and the **tick** grants the turn to its first PR only when none of its claims is
landing. Every other ready PR waits as `awaiting_turn` — not synced, not told anything, holding its
slot, its **status board** saying so. A PR that gave its turn up and is still being fixed is not in
the queue, and holds nobody back. PRs that conflict with each other are each resolved against a
target that holds everything landed before their turn — once, unless a PR gave that turn up, which
may cost it one more resolution, on its second turn (ADR-0045). Waiting is bounded by a turn's own
length — a sync, a gate run and a merge, or on a PR's second turn the
silent-worker ladder on the PR that holds it — and spends no **retry**. Turns are per fleet
instance: two fleets on one repo each grant their own. When two or more of
those PRs may land together the turn goes to all of them as one **merge batch**, in this same order.
_Avoid_: merge train (nothing is speculatively gated; a **merge batch** is one turn, not a train),
lock (nothing is held: the order
is recomputed from GitHub every time), priority (it is not configurable)

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
→ PR open → landing turn → merged, with the *ci-failed*, *awaiting-turn*, *fixing* (turn given up), *ready-again*, *escalated* and *parked* off-ramps) **derived** from
**fleet state**. It exists because the **claim** lives in a hidden ref namespace and the assignee is
unused, so the "claimed but no PR yet" phase is otherwise invisible to a reader. It is a *rendering* of
existing state, **never a source of truth** and **never read back by a tick**; it is edited in place
(idempotent — identical state renders identical text, so re-entrant ticks don't churn it), never
appended. Contrast the **escalation comment**, which is a durable, appended, re-readable handoff record
the board merely points to (ADR-0006).
_Avoid_: progress log (it is upserted, not appended), worklog, status label (a label is machine state;
this is human narration), progress ref
