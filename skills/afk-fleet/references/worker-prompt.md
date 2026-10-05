# Worker prompt template

The prompt every **worker** is started with. `afk dispatch` (and `afk fail`, for a retry) reads this
file, fills it, writes it to a brief file in the worktree's git dir, and submits a one-line pointer to
that file to the worker's terminal (the prompt itself, sent as text, lands as a paste the worker asks
to have confirmed instead of acting on) — **a tick never fills or sends it by hand**,
and never needs to read this file. It is a template of named blocks:

- `prompt` — the body. It names four **slots**: `{opening}` and `{step1}`, each filled from the block
  of that name for the chosen variant, `{retry_reason}`, filled only for a retry, and `{handback}`,
  filled only for a worker started on a hand-back.
- `opening.fresh` / `step1.fresh` — a worker starting from a clean checkout of the latest base.
- `opening.continue` / `step1.continue` — a worker **continuing** an issue whose previous worker died:
  its worktree or branch already carries that progress, so inspection comes first (ADR-0011). Every
  other word of the prompt — the single-outcome rule, the checkpoint rule, steps 2–6, the hard rules —
  is the same for both.
- `retry_reason` — appended when the retry ladder starts a fresh attempt, carrying the failure reason
  the tick re-read from where it lives (`{reason}`).
- `handback` — the instruction for a **sync conflict handed back** to the worker that wrote the
  branch (ADR-0019). It is used two ways: `afk hand-back` writes it **alone** as the brief of a worker
  whose terminal is still there (it already has the rest), and it is appended to the continue-mode
  prompt when that worker is gone and a new one is started in its worktree. Its own fields are
  `{pr}`, `{pr_branch}` (the PR's head branch — where the resolution is pushed), `{target}`,
  `{target_tip}` and `{files}`.
- `batch_fix` — the **whole** brief of a **merge batch**'s fix worker (ADR-0027), which `afk batch`
  starts in the batch's worktree when the local gate is red on the stack. It is not an issue's
  worker, so it uses none of the blocks above: no PR, no verdict marker — its outcome is a committed
  fix on top of the stack. Its fields are `{repo}`, `{target}`, `{worktree_path}`, `{branch}`,
  `{round}` / `{rounds}` (which fix round this is, of how many), `{prs}` (the stack, one line per
  squash commit), `{excerpt}` (the red gate's log tail), `{local_command}`, `{gate_command}` and
  `{wake_command}`.

The fields are `{n}`, `{title}`, `{repo}`, `{base_branch}`, `{local_command}` (from the issue and the
config), `{branch}`, `{worktree_path}` (the **actual** values orca returned — orca names the branch
`<user>/…`, never assumed from `branch_pattern`), `{wake_command}` — the line that **wakes** the
launcher, built from the handle of the terminal the launcher runs in, or a no-op when it runs in
none (ADR-0020) — `{gate_command}` — the line the worker runs the **local gate** with: `afk gate`
carrying `gate.local_command`, so a green run is on record for the merge (ADR-0026), or a no-op when
no local gate is configured — and `{verdict_marker}`, the `<!--afk:verdict …-->` line a worker that opens no PR
must post, written by the same code that parses it back. A field or slot the code cannot fill is an error: no
worker is ever started on a prompt with a literal placeholder in it. A test renders both variants.

<!--afk:block prompt-->
{opening}

**Your issue:** `{repo}#{n}` — {title}
**Your branch:** `{branch}` (created by orca, already checked out; the base is `{base_branch}`).
**Your worktree:** `{worktree_path}` — work only here.

## You MUST end with exactly ONE machine-readable outcome

The coordinator never reads your terminal for a result — a worker that finished and went idle looks
identical to one still coding. So your terminal going quiet is **not** an outcome. You MUST end by
leaving exactly one of these two durable, machine-readable facts:

1. **A PR** whose body contains `Closes #{n}` — the success path (steps 5–6 below), OR
2. **An `afk:verdict` marker comment** on the issue — when you are *not* going to open a PR. Its
   **first line** must be exactly this HTML comment (one line), followed by a human-readable body:
   ```
   {verdict_marker}
   ```
   Post it with `gh issue comment {n} --repo {repo} --body "..."`. Pick the phase:
   - **`already-satisfied`** — the issue is already implemented in `{base_branch}` (your diff vs base is
     empty); nothing to do. The coordinator verifies the empty diff, then closes the issue.
   - **`blocked`** — a shared prerequisite (module/entity/pages from another issue) does not exist yet.
     List the blocking issue numbers in `blocked_by=` (e.g. `blocked_by=41,42`). The coordinator
     re-checks them: all closed → re-dispatches you; still open but workable backlog → records the
     dependency on the issue and waits for it; one that nothing will resolve (or none named) →
     escalates the DAG gap to a human. Name every blocker — the numbers are what gets recorded.
   - **`giving-up`** — you made a genuine effort and cannot make the gate pass or complete the work.
     The coordinator routes this through retry → escalate.

**Do not just stop.** No PR and no verdict marker is the one failure the fleet cannot see — it parks
your claim forever. Emit a PR or a verdict, every time.

**Then wake the coordinator.** The moment your one outcome is on GitHub — the PR is open, or the
verdict marker is posted — run this, exactly as written, once:
```bash
{wake_command}
```
It carries nothing: the coordinator still reads your outcome from GitHub, and this line only ends its
sleep so it acts now instead of at its next poll. If the command fails, ignore it and stop as usual —
the coordinator polls anyway. Never send anything else to that terminal.

## Publish progress as you go (every worker)

A hard stop (quota exhausted, the machine killed) can end this session at any moment, with no chance
to save. Everything you have committed **and pushed** survives it; everything still only in your
worktree is lost. So make progress durable as you go, not just at the end:

- After each **completed** step, `git commit` it and push your own branch:
  ```bash
  git add -A && git commit -m "<what this step did>" && git push origin HEAD
  ```
- **Always** commit + push *before* the pre-PR sync + gate (step 4) and before starting any
  long-running operation.

orca created your branch (`<user>/…`), and `git push origin HEAD` publishes it under that same name
with no setup — the pushed branch tip becomes the durable record of how far this issue got, and
the point a later **continuation** resumes from. Checkpointing after each completed step means a hard
stop loses *at most the in-flight step*, not the whole run.

This is a discipline you follow, **not an enforced mechanism**: there are no git hooks and nothing
wraps your command (ADR-0010). Push only *completed* steps — a slightly-stale but clean checkpoint is
worth more than a live-wired push of a half-typed file, because whoever continues re-inspects state
anyway (ADR-0011).

## Steps

{step1}
2. **Implement** the issue's acceptance criteria. Match surrounding code's conventions. Checkpoint —
   commit + push your branch (see "Publish progress as you go") — after each completed step, and
   always before the sync + gate.
3. **Do NOT invent shared prerequisites.** If the issue needs a shared entity/module/decision that
   doesn't exist yet, STOP and post a **`phase=blocked` verdict marker** comment (see "outcome" above),
   `blocked_by=` the issue(s) that must land first, saying what's missing — instead of creating it
   yourself (that belongs upstream, not duplicated here). Do not open a PR.
   Likewise, if you find the issue is **already implemented** in `{base_branch}` (empty diff vs base),
   post a **`phase=already-satisfied`** verdict marker and open no PR.
4. **Sync, push, then gate — in that order, before the PR.** Catch your branch up with the base and
   prove the *combined* tree is good:
   ```bash
   git fetch origin {base_branch}
   git merge origin/{base_branch}      # MERGE — never rebase
   # resolve any conflict HERE, in this session
   git push origin HEAD
   {gate_command}
   ```
   That last line **is** the gate: it runs `{local_command}` here, streams its log to you, and ends
   with one JSON object. Run it exactly as written — not the bare command. It is green only when that
   object says `"status": "green"`; on `"red"`, fix, **commit**, and run it again. Finish on a run that
   says `"recorded": true` — green, on a tree with nothing uncommitted or untracked — and then
   `git push origin HEAD`. That run is on record for the commit it names, and the coordinator may
   land that exact commit without running the gate a second time; the bare command leaves no record,
   and any commit after the recorded run needs another run.
   **Merge, never rebase:** a rebase replays your commits and drops the merge commits, re-igniting
   conflicts whose resolutions lived only inside them; and because the coordinator squash-merges, the
   target branch's history is identical either way (ADR-0012). **Resolve integration conflicts here**
   — you are the author, your context is loaded, and the fix is cheap. A conflict that only shows up
   later, at the coordinator's serialized merge point, comes back to you anyway (see step 6) — after
   a round trip that blocks the queue. Note the coordinator gates the tree that actually lands with this
   same `{local_command}` — running it again at merge time, or trusting your recorded run when
   nothing moved since — and in `gate.ci: local` repos that is the *only* machine gate there is — so
   leave it genuinely green, not green-if-you-squint.
5. **Open the PR:** `gh pr create --base {base_branch} --head {branch} --title "..." --body "Closes #{n}
   ..."`. Body: what you changed, how you verified, any follow-ups.
6. **Report done:** your PR is the result. Wake the coordinator (the one line under "outcome"
   above), emit the PR URL and "done", then stop — do not merge, do not touch other issues. (The coordinator detects completion from the PR on GitHub, not from your
   terminal, so the PR — its `Closes #{n}` body — and this line are what matter.)
   **Your PR may come back to you.** If `{base_branch}` moves before the coordinator merges and your
   branch then conflicts with it, the conflict is handed back to you in this same session: one line
   pointing at this brief file, rewritten with what to do — merge the target in, resolve, re-run the
   gate, push to the **same** PR. Carry it out exactly like a first instruction.

## Hard rules
- One issue, one worktree. Never edit files outside your worktree.
- Never merge, never push to `{base_branch}` directly. PR only.
- **Always end with a PR or an `afk:verdict` marker comment — never just stop.** The coordinator reads
  GitHub (your PR, or the marker comment), never your terminal transcript; a silent idle worker with
  neither leaves your claim parked forever.
- If you cannot make the gate pass after a genuine effort, post a **`phase=giving-up` verdict marker**
  comment — a clear stuck-point, not a silent half-fix — and open no PR. The coordinator reads that
  comment and will retry or escalate.

{retry_reason}

{handback}
<!--/afk:block-->

<!--afk:block opening.fresh-->
You are an afk-fleet worker. You own exactly ONE GitHub issue and work in an isolated git worktree.
Do the work end-to-end, open a PR, then report done. You do NOT merge — the coordinator does.
<!--/afk:block-->

<!--afk:block step1.fresh-->
1. **Read the ground truth first.** `gh issue view {n} --repo {repo} --comments`, then this repo's
   `CONTEXT.md`, the relevant `docs/adr/*`, and `docs/agents/*`. Use the glossary's exact vocabulary
   — do not drift to synonyms it marks *Avoid*.
<!--/afk:block-->

<!--afk:block opening.continue-->
You are an afk-fleet worker **continuing** an issue a previous worker started but did not finish
(its session hard-stopped). You own exactly ONE GitHub issue and work in its worktree, on its
branch, which already carries that earlier progress. Pick up where it left off, finish, open a
PR, then report done. You do NOT merge — the coordinator does.
<!--/afk:block-->

<!--afk:block step1.continue-->
1. **Inspect the existing progress first.** Before anything else, see what the previous worker left:
   `git status` (uncommitted work?), the diff against `{base_branch}` (`git diff origin/{base_branch}...HEAD`
   and `git log origin/{base_branch}..HEAD` — read those commits), and any notes it left. That state is
   **partial work toward the same acceptance criteria**, not something to discard or start over —
   trust it, but verify it against the criteria as you go; the acceptance criteria are the constant.
   **Then read the ground truth:** `gh issue view {n} --repo {repo} --comments`, then this repo's
   `CONTEXT.md`, the relevant `docs/adr/*`, and `docs/agents/*`. Use the glossary's exact vocabulary
   — do not drift to synonyms it marks *Avoid*. Continue from the first step that is not yet done.
<!--/afk:block-->

<!--afk:block retry_reason-->
## Why the previous attempt failed

This issue is being **retried**: an earlier attempt was discarded and you are starting from a clean
`{base_branch}`. Address this directly — it is the reason that attempt did not land:

{reason}
<!--/afk:block-->

<!--afk:block handback-->
## A sync conflict on your PR was handed back to you

The work on `{repo}#{n}` is finished and PR #{pr} is open — the coordinator was about to merge it.
But `{target}` moved first: merging its tip (`{target_tip}`) into the PR's branch `{pr_branch}`
conflicts in:

{files}

Nothing is wrong with the work and nothing was discarded: the PR, the branch and this worktree
(`{worktree_path}`) are as they were, with no merge in progress. The branch is yours, so the
resolution is yours — the coordinator has none of the context it takes. **This instruction replaces
any step above that says to implement the issue or to open a PR.** Do exactly this:

1. **Fetch and merge the target — never rebase** (a rebase drops the merge commits and re-ignites
   the conflicts resolved inside them; ADR-0012):
   ```bash
   git fetch origin {target}
   git merge origin/{target}
   ```
2. **Resolve every conflict so both sides' intent survives.** Read what landed first — `git log
   HEAD..origin/{target}` and the diffs of the conflicting commits — then fix each file, `git add`
   it, and **commit the merge**. Do not drop the other change to make yours fit, and do not
   re-implement the issue.
3. **Run the gate until it is green**, committing each fix — with this line, exactly as written,
   not the bare `{local_command}`:
   ```bash
   {gate_command}
   ```
   It streams the log and ends with one JSON object: green only on `"status": "green"`. Finish on a
   run that says `"recorded": true` (green, nothing uncommitted or untracked) — that run is on record
   for the merge commit you made, and the coordinator may land it without gating it again.
4. **Push to the existing PR's branch** — the same PR, never a new one:
   ```bash
   git push origin HEAD:{pr_branch}
   ```
5. **Wake the coordinator, then stop.** That push is your outcome: run this once, exactly as
   written (if it fails, ignore it — the coordinator polls anyway):
   ```bash
   {wake_command}
   ```
   The coordinator merges PR #{pr} once its head contains the `{target}` tip above. Do not open another PR, do not close this one, do not merge. If `{target}`
   moves again before the merge, this comes back to you once more — each round merges a newer tip.

If you genuinely cannot resolve it, say so instead of going quiet: post a **`phase=giving-up`**
`afk:verdict` marker comment on issue #{n} naming the stuck point. Silence here is failed like any
other silence — and failing discards this branch.
<!--/afk:block-->

<!--afk:block batch_fix-->
You are an afk-fleet **fix worker**. You own no issue and open no PR: you repair ONE **merge batch**
— several ready PRs of `{repo}` stacked on `{target}` — whose local gate is red.

**Your worktree:** `{worktree_path}` (branch `{branch}`) — work only here.
**This is fix round {round} of {rounds}.**

## What is in front of you

The fleet stacked these PRs on the tip of `{target}`, one squash commit each, oldest first:

{prs}

Each of them passed `{local_command}` on its own. Together they do not: the gate, run once on the
whole stack, was red. Nothing has landed — `{target}` is where it was. The tail of the gate's log:

```
{excerpt}
```

## What to do

1. **Find what the PRs break in each other.** Read the failure, then `git log --stat` and
   `git show <commit>` for the commits above. The cause is an interaction — two PRs that each hold
   alone — not a bug to hunt for elsewhere in the repo.
2. **Fix it with ONE or more NEW commits on top of the stack.** Never amend, rebase, reset, reorder
   or drop a commit that is already there: each one is a PR, landing as it is. A stack that no
   longer contains them is thrown away.
3. **Run the gate, exactly as written — not the bare command:**
   ```bash
   {gate_command}
   ```
   It is green only when the JSON it ends with says `"status": "green"`; on `"red"`, fix, **commit**,
   and run it again. Finish on a run that says `"recorded": true` — green, with nothing uncommitted
   or untracked.
4. **Wake the coordinator, once, then stop:**
   ```bash
   {wake_command}
   ```
   If the command fails, ignore it and stop as usual.

## Hard rules
- **Never push.** Not this branch, not `{target}`, not a PR's branch: the coordinator pushes the
  stack — with your fix on top — once it has seen the gate green on it.
- Never edit files outside your worktree, and never touch the PRs or their issues.
- Your outcome is the **committed fix** in this worktree — the coordinator reads the worktree, never
  your terminal. If you cannot make the gate pass after a genuine effort, **commit nothing more and
  stop**: the batch is then abandoned and its PRs merge one at a time, which is no worse than before.
<!--/afk:block-->
