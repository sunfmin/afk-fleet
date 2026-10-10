# ADR-0042 — The base branch is confirmed at every launch, and kept on the remote

**Status:** accepted — amends [ADR-0023](0023-invoking-the-skill-is-the-launch.md) (a launch asks
one confirmation after all, and may ask two questions) and
[ADR-0038](0038-a-config-key-is-one-repos-really-differ-in.md) (`base_branch` is no longer a config
key: eight remain). The settled field of [ADR-0003](0003-cooperative-multi-fleet-claims.md)'s
probe gains a sibling.

## Context

`base_branch` was a config key with a default: a repo whose `docs/agents/afk-fleet.md` did not name
it had every PR merged into `main`, unattended, without anyone having said so. Some projects never
merge agent work into `main` — it goes to an integration or release branch a human promotes from —
and for them the default is the one outcome that must not happen. Even where the key was set, the
person launching the fleet today was not the person who wrote the file, and nothing told them where
the changes would end up.

ADR-0023 removed the launch confirmation because its answer could not differ from the request. This
one can: "merge everything, where?" has more than one answer, and the wrong one is not undone by a
drain.

## Decision

1. **Every launch confirms the base branch.** Bootstrap reads back the branch every PR of the run
   will land on and the human confirms it, or names another. `/afk-fleet --base-branch <name>` is
   that confirmation given up front — as `--worker-command` is for ADR-0010's question — so a
   launch from another skill, or by someone already leaving, still asks nothing. `--tick` and
   `--takeover` bootstrap the same way; `--plan` shows the branch and settles nothing.
2. **It has no default.** A repo no launch has settled a base branch for has none; the repo's
   default branch is offered, never assumed.
3. **It is kept on the remote, not in a file.** One record — `afk-base branch=<name>` on
   `refs/afk/base` (`refs/heads/afk-base` where the remote refuses `refs/afk/*`), in the encoding of
   ADR-0031 — written by `afk probe --base-branch`, and read by the next launch as the branch to
   read back. `base_branch` is the second field of the canonical config that no file sets: the
   probe returns it in the config the launcher holds. A file that still sets it is refused with a
   note, as every retired key is.
4. **It changes only while nothing stands on it.** An answer other than the recorded branch is
   refused while the remote holds any claim — each is work cut from the recorded base, whether its
   fleet is alive or dead — or any fleet instance's heartbeat is fresh. So two launchers never run
   one backlog onto two branches, and a takeover inherits the dead fleet's base. The write is a
   compare-and-swap on the record, so two launches answering at once cannot both win.
5. **The branch must exist.** The fleet does not create a base branch.
6. **A landing aims its PR first.** A PR still open against an earlier base — one escalated to a
   human before the base changed — is pointed at the current one before it is synced and gated:
   GitHub merges a PR into the branch it is open against, whatever the fleet meant.

## Considered and rejected

- **Keep the key, drop the default, print the branch at launch.** It makes someone choose once and
  keeps ADR-0023 whole. But the file is checked in: changing where a run lands takes a commit on the
  very branch in question, and the person launching still only reads what someone else decided.
- **A local file on the launcher's machine.** Per-machine: two people's fleets would land one
  backlog on two branches, and an issue closed on one would unblock work cut from the other, where
  its code is not. It is also the one kind of state this fleet has none of (ADR-0001).
- **Ask only when the recorded branch is missing.** The cheapest for the human, and it leaves the
  case this exists for — a launch by someone who does not know what is recorded — exactly as it was.
- **Let each launch choose freely.** Decision 4's hazard, with nothing to detect it: launchers
  cooperate through claims alone.

## Consequences

- A stock launch is no longer silent: it stops once, for a yes. `/afk-fleet --base-branch <name>`
  restores the unattended start, and names the destination in the command that starts the work.
- Bootstrap's probe may run twice — once to learn what to ask, once with the answer.
- A repo that set `base_branch:` must delete the line; the first launch after that asks, with
  nothing on record.
- A fleet that stopped holds the base where it is for a while: its heartbeat stays fresh for the
  claim lease (75 minutes), and the claims it kept — those with an open PR behind them — until they
  land or are taken over. That is the point of decision 4, and occasionally a wait.
- Off the repo's default branch GitHub links no issue to a PR and closes none when it merges; the
  fleet reads `Closes #n` from the PR's body and closes the issue itself (`afk_decide.issues_closed_by`),
  without which a base other than the default could not be run on at all.
