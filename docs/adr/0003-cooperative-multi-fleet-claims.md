# Cooperative multi-fleet claims via atomic lock refs + a per-instance lease

**Status:** accepted — amends [ADR-0001](0001-disposable-coordinator-context.md) rule 3
(assignee-as-claim) and the single-launcher framing of
[ADR-0002](0002-launcher-and-disposable-ticks.md). Keeps the fleet-state-in-GitHub foundation of both.

## Context

ADR-0002 says a repo is worked by one launcher. But several people (or one person with several
machines) may point a fleet at the same repo, all authenticated as the **same GitHub account** — the
realistic case is two laptops both `gh auth`'d as one user. In that world the existing claim
mechanism silently breaks:

- The claim was the **assignee** (`--add-assignee @me`). Under a shared account `@me` is *identical*
  on every machine, and `--add-assignee` is idempotent — a fleet **cannot tell "I just claimed it"
  from "a peer already had it."** The assignee is no longer a lock.
- Worse than a lost race: in-flight was reconstructed as `open & assignee=@me`. A peer's live claim is
  therefore indistinguishable from *my own*, so each fleet sees the other's in-flight issues (no PR
  yet, no locally-visible worker) as **orphaned claims** and re-dispatches them — the fleets
  cannibalise each other's live work.

## Decision

Support cooperative multi-fleet on one repo. Each launcher run is a **fleet instance** with an id
minted at bootstrap and injected into every tick (like the run authorization). The claim moves off
the assignee onto an **atomic lock ref**, and liveness is carried by a **per-instance lease**.

1. **Claim = a lock ref.** To claim issue *n*, create `afk-claim/<n>` pointing at a marker commit
   whose message carries `instance=<id> host=<host>`. The ref name is the issue number *only*, so the
   create collides: the server accepts exactly one and rejects every other with `[rejected] (already
   exists)`. **That rejection is the compare-and-swap** — a real mutex the assignee never was. The
   loser reads the winner's marker and backs off. The claim ref is immutable once created; its marker
   answers "is this mine?" on rebuild. It is the **single source of truth** for "taken" — the assignee
   is dropped as a claim signal entirely.

2. **Hidden ref namespace.** Claim and heartbeat refs live under `refs/afk/*`, outside `refs/heads`
   and `refs/tags`, so they are invisible to the branch UI and to `on: push` / `on: create` CI — no
   branch-list clutter, no junk CI runs from claim churn. Bootstrap probes one push to `refs/afk/*`;
   if an org ruleset forbids non-branch refs, it falls back to `refs/heads/afk-claim/*` and warns
   loudly (CI-on-push will then fire).

3. **Per-instance lease (self-healing).** A fleet publishes one `afk-heartbeat/<id>` ref carrying a
   timestamp, refreshed **per instance, not per claim** (claim refs stay immutable), and only while it
   holds claims — roughly once per `claim_lease_ttl`/3, decoupled from the faster tick cadence. A
   claim is leased-live while its owner's heartbeat is within `claim_lease_ttl` (≈ 3× `idle_interval`,
   ~75 min). A fleet reconciles only *its own* claims (**orphaned claims**); it may reclaim a
   **peer's** claim only when that peer's heartbeat has expired (**stale claim**), via an atomic
   `git push --force-with-lease=afk-claim/<n>:<sha-it-read>` takeover so two reclaimers can't both win.

4. **Open-PR guard.** An issue with an open linked PR is never in the frontier — a PR is itself
   durable in-flight evidence, independent of the claim ref. This hardens every recovery path (a
   released-but-still-finishing worker's PR is not re-dispatched) and makes the fleet correctly leave
   a *human's* PR alone.

## Why per-instance liveness (not per-claim)

A peer does not care whether the *worker* on #101 is alive — only whether the *fleet that owns* #101
is. If the owner is alive it detects its own dead workers locally (orphaned-claim reconciliation via
orca-cli) and re-dispatches; a peer must not interfere. So per-instance heartbeat is both **cheaper**
(O(1) writes per heartbeat instead of O(claims)) and **more correct** (it never invites a peer to
second-guess a live owner's workers).

## Considered and rejected

- **Distinct GitHub principals per fleet** (bot accounts / per-machine users). Makes `@me` a real
  owner discriminator and needs no new machinery. Rejected as the *baseline*: the actual setup is one
  account on N machines, and provisioning N repo-writable identities is friction we chose not to
  require. (Still the cleanest option if you happen to have distinct identities.)
- **Shared account + a label/comment discriminator** (`afk-owner/<id>`). Keeps claims in issue
  metadata but is not atomic — label/comment writes are last-write-wins, so it still needs a racy
  detect-and-back-off protocol bolted on. The lock ref gives atomicity for free. Rejected.
- **Per-claim heartbeat** (re-stamp every held claim each tick). O(claims) writes, churns every claim
  ref, and lets peers meddle at the wrong granularity. Rejected for per-instance.
- **Keep the assignee as a write-only cosmetic mirror** (human-visible "a bot's on this"). Tempting
  for the human-double-take guardrail, but it reintroduces a second "claimed" marker that can drift.
  Rejected in favour of ref-only single-source-of-truth; the human hands-off signal becomes the
  `ready_label` instead (remove it to reserve an issue).
- **Manual / no reclaim of a dead fleet's claims.** Simplest, but a crashed laptop then strands its
  issues (invisibly, behind phantom locks) until a human intervenes — breaking the multi-day
  unattended promise. Rejected for the lease.

## Consequences

- **Config:** `claim: ref` replaces `claim: assignee`; add `claim_lease_ttl_seconds` (default ~4500,
  3× idle). `refs/afk/*` joins `afk-attempt/*` as fleet-reserved.
- **Dispatch contract:** the frontier selector keys on `claimed` (from `git ls-remote 'refs/afk/claim/*'`)
  and `has_open_pr` (from open PRs' `closingIssuesReferences`) instead of the assignee. (The selector
  itself was later folded into the `afk` tool's decision core — see ADR-0004.)
- **Pacing coupling:** while holding any claim a fleet may not sleep past ~`claim_lease_ttl`/2, so a
  fleet with in-flight work cannot drop to the deep-idle cadence — the accepted price of a
  time-based lease.
- **Cleanup is load-bearing:** the claim ref must be deleted on merge, escalate, and release. A missed
  delete is a silent phantom lock (the one failure mode to guard hardest). ADR-0001's aversion to a
  second source of truth stands — the ref *is* the source of truth, and nothing reads the assignee.
- **Graceful stop** releases no-PR claims immediately and retains has-PR claims, so a peer inherits
  and merges the finished PR after the lease expires (bounded merge latency, only at stop).
- **Single-fleet is unchanged in behaviour** — one instance simply never sees a foreign claim, and its
  heartbeat is pure overhead of one tiny push per ~25 min while busy.
