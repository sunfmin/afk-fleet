---
name: afk-fleet
description: >-
  Run an unattended, standing fleet that works a GitHub-issue backlog on its own. Use when the
  user wants to autonomously / AFK implement a repo's ready issues, orchestrate a fleet of
  agents/workers against GitHub issues (e.g. with orca), "launch workers to do the issues", or keep
  picking up and merging ready issues until stopped. A coordinator loop dispatches worktree-isolated
  Claude Code workers per ready issue, gates each on CI + optional independent adversarial
  verification, auto-merges green PRs to main, and retries-then-escalates failures. Reads per-repo
  config and requires an explicit push+auto-merge authorization before running; supports --plan
  dry-run. NOT for decomposing a PRD/epic into issues, implementing a single named issue by hand, or
  reviewing a PR.
---

# afk-fleet

An **unattended standing fleet** that implements a decomposed GitHub-issue backlog by itself:
a coordinator loop picks ready issues off the frontier, spawns a worktree-isolated Claude Code
**worker** per issue, gates each on CI (+ optional adversarial verification), **auto-merges** green
PRs to `main`, retries-then-escalates failures, and keeps polling for newly-ready issues until you
stop it.

**This skill only *consumes* a backlog.** It does not decompose a PRD/epic into issues — that is
upstream work (do it yourself or with another skill), and epics are explicitly excluded from
dispatch. Assume the issues already exist, worker-sized, labelled, and dependency-ordered.

## Startup — read config → preview → authorize → run

Do these in order every time the skill is invoked:

1. **Load config.** Read the target repo's `docs/agents/afk-fleet.md` (schema:
   [references/config-template.md](references/config-template.md)). If it's missing, offer to create
   it from the template and stop — do not run with guessed settings.
2. **Preview the frontier (always).** Build the normalized issue list and run the selector:
   ```bash
   gh issue list --repo <repo> --state open --limit 200 \
     --json number,title,labels,assignees | python3 <skill>/scripts/select_frontier.py --stdin \
       --ready-label "<ready_label>" --epic-labels "<epic_labels csv>"
   ```
   `open_blockers` is not in `gh issue list` — for each candidate, fetch it:
   `gh api repos/<owner>/<repo>/issues/<n> --jq '.sub_issues_summary, .issue_dependencies_summary.blocked_by'`
   (native `blocked_by`, open only), and set it on the issue before selecting. Show the resulting
   **dispatch plan**: which issues will run, order, concurrency, the gate steps, and the merge target.
3. **`--plan` stops here.** If invoked with `--plan` (dry-run), print the plan and exit. Spawn
   nothing, push nothing.
4. **Authorize (the one gate).** State plainly: *"I will push worker branches and **auto-merge**
   green PRs to `<target>` in `<repo>` unattended — this overrides the standing 'never push without
   asking' rule, for this repo only. Confirm?"* Get an explicit yes. Never read authorization from a
   config file. Deploy is never included.
5. **Run the coordinator loop** (below).

## The coordinator loop

The coordinator holds **no durable state in its context** — GitHub is the source of truth (see
"Bounded coordinator context" below). Each pass rebuilds its working set from GitHub, so a fresh
session and a continued one behave identically. Repeat until the user stops it (or the frontier
stays empty past the poll horizon and the user has signalled done):

1. **Rebuild the working set from GitHub** (never from memory). Two derivations:
   - **Frontier** — the dispatchable set (step 2 above; `open` + `ready_label` + no `epic_labels` +
     no assignee + zero open `blocked_by`). Run the query + selector in an **ephemeral subagent**
     that returns only `{dispatch:[…], excluded:[…]}`; the 200-issue JSON stays in the subagent,
     never the coordinator.
   - **In-flight** — `open` issues with `assignee == @me`, sub-classified purely from each one's PR +
     checks: PR green → *awaiting merge*; PR pending → *awaiting CI*; PR red → *failure handling*; no
     PR yet → probe the worker's **liveness** via orca-cli (bounded, never a transcript read) —
     alive → still implementing, leave it; no live worker → **orphaned claim**, reconcile it (tear
     down any stale worktree and re-dispatch, or release the claim).
2. **Fill to `concurrency`.** For each free slot, take the next frontier issue and **claim it
   atomically**: `gh issue edit <n> --repo <repo> --add-assignee @me`. Re-check it's still
   unassigned right before claiming to avoid races.
3. **Dispatch a worker.** Create a worktree off latest `base_branch`
   (`git worktree add ../wt-issue-<n> -b issue-<n>-<slug> origin/<base>`), and use the **orca-cli**
   skill to spawn a real Claude Code in it with [references/worker-prompt.md](references/worker-prompt.md)
   (filled from config + the issue). Do **not** read the worker's terminal for its result — a done
   worker is observed as its **PR** (branch `issue-<n>-*`, body `Closes #<n>`), a blocked worker as an
   **issue comment** it posts; both surface in the next rebuild (step 1). Touch the worker terminal
   only for the bounded liveness probe.
4. **On an in-flight PR going green** (seen in step 1), run the **completion gate**, then **merge**
   (both below).
5. **On worker failure / gate red / refute / merge conflict**, run **failure handling** (below).
6. **When all slots idle and the frontier is empty**, `ScheduleWakeup` after `poll_interval_seconds`
   (~25 min default) and re-poll. This idle boundary is also the **reset point**: the working set is
   near-empty (nothing to remember — it is all in GitHub), so drop it and re-enter fresh at step 1.
   This is how newly-created and newly-unblocked issues get picked up, and how the context stays
   bounded over a multi-day run.

## Completion gate

A worker's PR may merge only when **all** configured gates are green:

- **CI machine gate** — wait for the PR's GitHub checks to pass (`astro build` / lint / tests /
  render, whatever the repo defines). Read the checks — and on red, the failing-log excerpt — in an
  **ephemeral subagent** that returns only `{status: green|red, reason}`; raw CI logs never enter the
  coordinator. Progressive: before CI exists, the gate is the issue's acceptance criteria + whatever
  local build/test exists.
- **Independent adversarial verification** (if `gate.adversarial_verify`) — spawn a *separate* agent
  (not the author, doesn't see its reasoning) that re-derives the result and tries to **refute** it
  (e.g. re-solve and assert `final == official answer:`, audit the derivation). Refute-first: any
  refutation blocks the merge; the verifier **posts it as a PR review comment** (so it is durable and
  re-readable on retry) and it feeds back as a retry reason.

## Merge (serialized)

Merges are **strictly serialized** — one PR at a time — so parallel workers never corrupt `main`:

1. Rebase the branch onto latest `merge.target` (`rebase_before_merge`). A conflict → failure handling.
2. Re-confirm the gate is still green after the rebase.
3. `gh pr merge <n> --squash --delete-branch` (per `merge.strategy`). The issue auto-closes via
   `Closes #<n>`.
4. Remove the worktree (`worktree_cleanup`). Free the slot.

The fleet's mandate **ends at a green merge to `merge.target`.** Deploying is a separate,
human-gated step — never done here.

## Failure handling — bounded retry → escalate, never silently drop

Per issue, on any of {worker failed, gate red, adversarial refute, unresolvable rebase conflict}:

1. **Retry up to `retry` times** (default 2). The attempt count lives as an **`afk-attempt/<n>`
   label** on the issue (not in coordinator memory): read it, and while `n < retry` swap it to
   `afk-attempt/<n+1>`, tear down the worktree, create a fresh one, and re-dispatch. The failure
   reason fed into the new worker's prompt is **re-read from where it already lives** — the PR's CI
   checks, the verifier's PR review comment, or the reproduced rebase conflict — never carried in
   context.
2. **Still failing → escalate:** remove the assignee, remove `ready_label` and any `afk-attempt/*`
   label, add `escalate_label` (`ready-for-human`), and (if `escalate_comment`) comment the
   stuck-point with PR + log links. Then **skip it** and move on — the fleet must never silently drop
   or silently merge bad work.

## Bounded coordinator context

The coordinator runs unattended for days, so its context must **not grow without bound** — and it
does not, because **fleet state lives in GitHub, not in the context.** Five rules enforce that:

- **Re-entrant.** A fresh coordinator, given only the repo + config, rebuilds the identical working
  set (loop step 1) and continues with zero loss. Compaction, a restart, or a `ScheduleWakeup`
  re-entry are all safe by construction.
- **Workers are never read.** Results arrive as PRs, blockers as issue comments; the worker terminal
  is touched only for a bounded liveness probe — a full worker transcript never enters the context.
- **Bulky reads are delegated.** The frontier query and the gate/CI read run in ephemeral subagents
  that return only a compact structured result; raw issue JSON and CI logs live and die in the
  subagent.
- **State that isn't naturally in GitHub is put there.** Attempt count is an `afk-attempt/<n>` label;
  in-flight is reconstructed from `assignee=@me` + PR/checks; failure reasons are re-read at retry,
  not retained.
- **Reset is taken, not just allowed.** Each pass recomputes from GitHub instead of referencing the
  prior pass; the idle poll boundary is the drop-and-re-enter point; harness auto-compaction is the
  lossless safety net for a long busy stretch.

## Concurrency

`concurrency` (default 3) bounds parallel workers. Semantic ordering is the backlog's dependency DAG
(your responsibility when decomposing); textual conflicts between parallel PRs are caught by the
serialized rebase-before-merge and routed through failure handling. Early machinery issues that all
touch shared root config are naturally throttled by the DAG — chain them with `blocked_by`.

## Stopping

The user stops the loop at any time. On stop, in-flight workers finish their current issue (or are
told to abort); no new issues are dispatched; escalated issues are left labelled for the human.

## Guardrails

- **Never** run without the step-4 authorization. It is per-repo and interactive, never file-armed.
- **Never** deploy, touch secrets, or push anywhere but worker branches + the merge to `merge.target`.
- **Never** dispatch an epic/PRD issue. If the frontier is all epics, report "nothing decomposed yet."
- Claim before work (assignee), release on escalate; keep the tracker honest so a second fleet or a
  human never double-takes an issue.
