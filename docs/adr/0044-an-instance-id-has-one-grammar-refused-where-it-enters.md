# ADR-0044 — An instance id has one grammar, refused where it enters

**Status:** accepted — refines the batch id of
[ADR-0029](0029-a-merge-batch-lands-n-prs-behind-one-gate-run.md) and the heartbeat ref of
[ADR-0003](0003-cooperative-multi-fleet-claims.md); an instance of
[ADR-0016](0016-the-seam-enforces-its-own-rules.md).

## Context

An instance id was free text, and three places read it back differently:

- A batch id was `<instance>-<second>`, and orca puts `-<k>` behind a branch cut under a name
  already taken. So `afk-batch-fl-1-1700000000`, a batch of instance `fl-1`, also read as batch `1`
  of instance `fl` behind a continuation suffix — and the batch sweep of `fl` deletes the branches
  it reads as its own.
- The batch id replaced every character outside `[A-Za-z0-9_.-]` with `-`: `fl/1`, `fl 1` and `fl-1`
  formed one batch id.
- A heartbeat's owner is its ref's name, read as the last path segment: a beat written for `fl/1`
  was read back as `1`, so every claim of `fl/1` looked stale to every peer.

The claims "a batch id is unique … never another fleet's" and "whose it is is the ref's name" were
the stated rules. They stand; the code moves.

## Decision

1. **An instance id matches `[a-z0-9][a-z0-9-]{0,39}`** (`afk_decide.INSTANCE_ID_GRAMMAR`, its one
   home). Lowercase only: refs mirrored into a checkout on a case-insensitive filesystem fold `Fl`
   and `fl` into one.
2. **It is checked once, where `--instance` enters** — the flag's argparse type, on every subcommand
   that has the flag. A value outside the grammar is the CLI's one error, exit 3. Nothing downstream
   sanitizes or re-checks an id.
3. **A batch id is `<instance>-t<second>`.** The `t` cannot be a continuation suffix's first
   character, so a branch `afk-batch-<id>[-<k>]` parses one way only: for any two distinct ids,
   neither instance's match accepts a batch branch of the other. Held by an exhaustive test over
   every id of up to four characters from an alphabet with each kind of character the grammar has.
4. **`takeover --from` is not checked.** It names an id already stamped on the remote, possibly by
   a fleet older than this ADR; refusing it would leave that fleet's claims untakeable.

## Consequences

- A launcher that minted an id outside the grammar is refused at its first cycle and mints another.
- A batch formed before this ADR keeps its old id and still lands — a batch is found by its id, not
  by its shape — but the sweep no longer reads its leftover branch or worktree as the instance's
  own, so those are left for a human to delete.

## Considered and rejected

- **Forbid ids that end in `-<digits>`.** Removes the prefix pair by refusing `fl-1`, the commonest
  shape of id in use.
- **Sanitize instead of refuse.** That is what merged `fl/1` and `fl-1`.
