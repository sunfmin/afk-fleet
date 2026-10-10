# ADR-0032 — Records kept in comments share the encoding of records on refs

**Status:** accepted. Extends [ADR-0031](0031-records-kept-on-refs-share-one-encoding.md) to the
other carrier. No marker changes: what [ADR-0027](0027-a-worker-lands-its-own-pr-on-a-landing-turn.md)
and [ADR-0029](0029-a-merge-batch-lands-n-prs-behind-one-gate-run.md) write on a PR, what a worker
is told to write for its verdict, and the status board of
[ADR-0006](0006-progress-status-board-comment.md) are the same bytes as before.

## Context

The other half of what the fleet remembers rides in comments: the **landing turn** on a PR, a
worker's **verdict** on an issue, and the **status board**. Each had a marker syntax, a regular
expression and a parser of its own, and its own answer to "which comment is the record" — the turn
and the verdict took the latest marker, the board the first; only the turn said what a marker that
names nobody is. ADR-0031 had just given records on refs one encoding, and a marker is the same
thing in a different place: a word and `field=value` words.

## Decision

1. **A record in a comment is the record of ADR-0031 between `<!--` and `-->`**, leading the
   comment, with the same facts worded for a human under it: `<!--<word> <field>=<value> …-->`.
   `afk_decide.record_comment` writes the comment, `read_marker` reads one body, and both are the
   ref pair (`record_message` / `read_record`) with a different wrapper.
2. **A kind still only declares itself.** `TURN_RECORD`, `VERDICT_RECORD` and `STATUS_RECORD` are
   `RecordKind`s like `CLAIM_RECORD`. The status board declares no field: its marker says which
   comment is the board, and everything it tells is the text under it.
3. **A field's type is how its value is spelled** (`FieldType`: a writer and a reader). `str` and
   `int` stay; a comment's markers needed three more — `FLAG` (`=1`, or left out), `INTS` (a
   comma-separated list) and `one_of(vocabulary)` — and the turn declares the one that is only its
   own, a batch's members. A value outside a closed vocabulary is a field that is missing when read
   and a defect in the caller when written.
4. **A kind may declare a `tail`**: its last field, written raw, running to the end of the record.
   A verdict's `reason` is one — a phrase a worker types, spaces and all. It is the only value not
   percent-encoded, which is what keeps the markers workers already post readable.
5. **The latest marker wins, and it is said once**: `afk_decide.latest_record(kind, comments)`
   returns the record and the comment that carries it. A comment whose marker is not a record is
   passed over, never read as the latest.
6. **What makes a marker not a record is the kind's `required`**, as on a ref. A turn requires
   `instance` — a turn nobody could hold is no turn. A verdict requires nothing: it is written by
   hand, and one with no phase, or a phase the fleet does not know, is still a verdict
   (`classify_stopped` fails the attempt rather than nudging a worker that did answer).
7. **A record is kept in one comment and rewritten in place.** The comment `latest_record` found
   is where the rewrite goes (`afk._comment`, by id); with none, the comment is created. A turn is
   rewritten from the record it was read as plus what changed (#55), a board when its text changed.

## What differs from before, on purpose

- With two status boards on one issue — a state only a race or a hand-edit makes — the later one
  is now the board, where the earlier one was.
- A comma in a list may still be followed by whitespace (`blocked_by=3, 4`); a list separated by
  whitespace alone (`blocked_by=3 4`) now reads as its first number. The worker is told to write
  commas.
- A turn's `phase` is read only on a marker that names a `batch`, as it was only ever written.

## Considered and rejected

- **Percent-encode a verdict's `reason` like any value.** Every verdict a worker posts would need
  the worker to encode it by hand, and every one already posted would stop reading.
- **Require `phase` of a verdict.** A marker without one would read as silence and earn a nudge;
  today it fails the attempt, and nothing a tick decides may change here.
- **A second reader for comments.** That is the state this replaces.
