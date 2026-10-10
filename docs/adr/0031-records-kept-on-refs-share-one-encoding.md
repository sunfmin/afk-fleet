# ADR-0031 — Records kept on refs share one encoding

**Status:** accepted. Changes the message format of the recorded gate run of
[ADR-0030](0030-a-gate-run-is-recorded-on-the-remote-under-the-tree-it-tested.md) (its decision 2:
the fields are the same, how they are written is not). The claim and heartbeat of
[ADR-0003](0003-cooperative-multi-fleet-claims.md) keep their format, byte for byte.
[ADR-0032](0032-records-kept-in-comments-share-the-encoding-of-records-on-refs.md) carries the same
encoding to records kept in comments, and widens decision 2's types.

## Context

Three kinds of fleet state ride on git refs, each as the message of a small commit: a **claim**, a
**heartbeat**, and a **recorded gate run**. The first two were a line of `key=value` words, written
by one function and parsed by another that returned whatever words it found — so "a claim with no
owner" and "a commit that is no claim at all" were told apart, or not, at each call site. The third
arrived with ADR-0030 as a JSON body under a fixed subject, with its own writer, its own parser and
its own rule for what is not a record, because there was nothing to reuse.

## Decision

1. **A record is a set of named fields, carried as the subject of the commit a ref points at:**
   `<word> <field>=<value> …`. The word says what kind of record it is.
2. **A kind only declares itself** (`afk_decide.RecordKind`): its word, its fields with their types
   (`str` or `int`) in the order they are written, and which are required. `CLAIM_RECORD`,
   `HEARTBEAT_RECORD` and `GATE_RUN_RECORD` are three such declarations and nothing more.
3. **One pair of functions writes and reads every kind** — `afk_decide.record_message` /
   `read_record`, pure; `afk._record_commit` / `_read_record` are their git half.
4. **Reading has one rule for each thing that can be wrong:**
   - a field the kind does not declare is ignored, so a newer fleet may add one;
   - a field with no value, or a value not of its type, is a field that is missing; a missing
     optional field is absent from the record;
   - a commit whose subject does not open with the kind's word, or that lacks a required field, is
     **not a record** — it reads as `None`, never as a record with holes in it.
5. **Writing a record and reading it back is the identity, for every value** (#114; a generated
   test holds it for every declared kind and both carriers). A value is percent-encoded only where
   it would break that — whitespace, `%`, anything outside ASCII, and the `>` of a `-->`, which
   would close a comment's marker (ADR-0032). An instance id or a hostname is written as itself,
   which is what keeps decision 6; a gate command, which has spaces, is what needed it. A word is
   one field: reading never joins two, so a value that ends in a comma is only that value.
6. **Claims and heartbeats on a remote keep working, both ways.** What the previous code wrote is
   read by this one, and what this one writes is the same bytes, so a fleet that updates mid-run
   loses no claim and a peer that has not updated still reads its neighbour. Tests pin a literal of
   each.
7. **A recorded gate run written before this is not read.** Its subject is `afk-gate green` with
   the fields in a JSON body: no `at` field in the subject, so by decision 4 it is no record. The
   landing runs the gate once and writes a readable record; the bootstrap probe sweeps the old one.

What a caller does with "not a record" stays the caller's, because it differs by what the ref
*is*: a claim ref is a lock whether or not it says whose, so it is listed as a claim that names
nobody (stale to everyone, never mine); a heartbeat ref that says no time is no heartbeat; a gate
ref that is not a record is no record, and the gate runs.

## Considered and rejected

- **JSON for all three.** It would orphan every claim and heartbeat on every remote at the moment
  of update — the one thing the change must not do — or need a second parser kept forever.
- **Keep the gate run's JSON body beside the `key=value` subject.** Two encodings behind one
  function name is the state this replaces.
- **Fields in the commit body, one per line.** No better for the two kinds that already exist, and
  `git log --format=%s` over a ref scan stops being enough.
- **Require every declared field.** A claim made by hand as the reference documents it
  (`afk-claim instance=<id> host=<host>`, no time) would read as nobody's and be reclaimed.
