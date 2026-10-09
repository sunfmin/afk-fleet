# ADR-0023 — Invoking the skill is the launch

**Status:** accepted — amends the bootstrap of [ADR-0002](0002-launcher-and-disposable-ticks.md)
(drops the preview and the per-run authorization) and the asking rule of
[ADR-0010](0010-worker-launch-command.md). Amended by
[ADR-0042](0042-the-base-branch-is-confirmed-at-every-launch-and-kept-on-the-remote.md): a launch
confirms the base branch, the one thing whose answer can differ from the request.

## Context

Bootstrap ended in two steps that existed for each other: a **plan tick** spawned to show the
dispatch plan, and a gate — *"I will push worker branches and auto-merge green PRs … Confirm?"* —
waiting on an explicit yes. The yes became a third launcher-held fact, `authorized: true`, injected
into every tick; a cold `--tick` without it dispatched but held its merges.

The gate asked the human to repeat what they had just said. Nobody types `/afk-fleet` for any reason
other than to have the backlog implemented and merged unattended — that is the skill's whole
description — so the confirmation carried no information, and it cost the one thing the skill is for:
the human had to stay until a subagent finished computing a plan, read it, and answer, before they
could walk away. A launch started from another skill, or by someone already leaving, simply sat there.

## Decision

1. **The invocation is the authorization.** Running `/afk-fleet` is the go-ahead to push worker
   branches and auto-merge green PRs to `merge.target`, for this repo, for this run. Bootstrap asks
   for no confirmation and goes from the worker launch command straight into the loop.
2. **No bootstrap preview.** With nothing to authorize against, the plan tick at launch is a subagent
   whose output gates nothing — and the first cycle's tick rebuilds the same working set anyway.
   `/afk-fleet --plan` remains the way to look without acting, chosen by the human rather than
   imposed on every launch.
3. **The run authorization stops being a fact.** There are two launcher-held facts — the instance id
   and the worker launch command. A tick is handed no `authorized` flag, and a cold `--tick` acts as
   a launcher's tick does, merges included: it too was invoked.
4. **The worker launch command can ride in on the invocation.** `/afk-fleet --worker-command "<cmd>"`
   answers ADR-0010's question up front, and is checked by the same `afk worker-command --check`.
   Without it, a launcher on a custom provider is still asked — the one question a launch can ask.

Bootstrap still **stops** on what makes the run impossible — a config error, a missing config file, a
merge target that would reject every merge. Those are failures, not confirmations.

## Why

A gate is worth its interruption when the answer can differ from the request. Here it could not. The
protections that actually bound an unattended run are elsewhere and untouched: the completion gate
before every merge (ADR-0012), the retry ladder and escalation, the scope guardrail (worker branches
and `merge.target` only), and `ready_label` — the human's real, per-issue authorization, given when
the issue was labelled.

The worker launch command is kept as a question because it is the opposite case: a fact the launcher
cannot derive (ADR-0010) and whose wrong value is silent for days. It is not removed, only made
answerable in advance.

## Considered and rejected

- **Keep the preview, drop only the confirm.** The plan would scroll past a human who has already
  left, at the price of one subagent per launch. `--plan` serves the human who wants to look.
- **Keep `authorized` as a flag the launcher always sets.** A fact with one possible value is not a
  fact; it would leave the cold-`--tick` "dispatch but hold merges" branch alive with nothing able to
  reach it but a hand-built prompt.
- **Auto-pick the worker launch command when one alias wraps an environment.** Right most of the
  time and silently wrong the rest — on the wrong provider, for the whole backlog. ADR-0010's reasons
  stand.

## Consequences

- Stock launcher: `/afk-fleet` runs to its first cycle with no prompt at all. Custom provider:
  `/afk-fleet --worker-command ckimi` does the same.
- A mistaken invocation now acts, where before it waited. The first effects are claims and worker
  worktrees, undone by the drain; a merge needs a worker to finish and pass the gate first.
- `--takeover` is unchanged in what it asks: which dead instance to take, and the fresh-heartbeat
  confirm (ADR-0011). Those questions are the mode's purpose, not a launch gate.
- `authorize:` in a config file is still refused — as any unknown key is.
