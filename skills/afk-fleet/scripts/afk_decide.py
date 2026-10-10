#!/usr/bin/env python3
"""
afk_decide.py — the afk-fleet decision core, as pure functions.

The single source of truth for every deterministic "should we?" verdict the fleet
makes. No I/O: each function takes normalized inputs (the effectful `afk.py` layer
gathers them from gh + git refs, and injects `now`) and returns a verdict. Purity
is what makes the correctness-critical logic — dispatch eligibility, claim
ownership/liveness, retry/escalate, pacing — testable with fixtures, without gh,
git, or the network. See ADR-0004 (deterministic mechanics → tested code tools;
orchestration + judgment → the LLM tick).

The dangerous ones (ADR-0003): `classify_claims` decides mine-vs-live-peer-vs-stale,
where a wrong verdict silently corrupts state (a phantom lock starves an issue; a
stolen live claim double-works it). Those get the hardest fixtures.

None of these functions read the clock — `now` is always an argument — so a fixture
pins behaviour deterministically.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import urllib.parse
from typing import (Any, Callable, Collection, Generator, Iterable, Literal, Mapping, NamedTuple,
                    NoReturn, Optional, TypedDict, TypeVar, cast, get_args)

# --------------------------------------------------------------------------- #
# Types — what the gate's type checker holds the code to (ADR-0039)            #
# --------------------------------------------------------------------------- #
#
# A closed vocabulary is a `Literal`, declared beside the table or the tuple that
# lists its words: the tuple is read off the Literal, and a table is keyed by it,
# so a word misspelled where it is used fails the gate rather than a run. Where a
# chain of branches handles every word, it ends in `assert_never` — and a word
# added without its branch fails the gate too.

# A JSON object as the fleet passes it around: a config, a row gh or orca
# returned, a record, a result. What its keys are is said where it is made.
Obj = dict[str, Any]

# The records the fleet's own code makes and reads are not `Obj`: each is a
# TypedDict here, so a key misspelled where it is subscripted, a key left out
# where one is built, or a word outside its vocabulary fails the gate. A row gh
# or orca returned stays `Obj` — which keys it has is the caller's `--jq`, not
# ours to declare — and so does a subcommand's result, which only JSON reads.


# A row gh returned is declared too, where the fleet's own `--jq` or `--json`
# says which keys it has (afk.py's `_ISSUES_JQ`, `_ISSUE_JQ`, `_PR_FIELDS`): the
# projection is the promise that each is there. What orca answers, and what
# GitHub nests inside a check rollup or a branch protection, has no such
# promise — those stay `Obj`, read with `.get` where they come in.


class Claim(TypedDict):
    """One claim ref, as the scan lists it. A ref that carries no claim record
    is still a claim: one that names nobody."""
    number: int
    instance: str | None
    host: str | None
    ts: int | None
    sha: str


class Issue(TypedDict):
    """One open issue, as the gather lists it."""
    number: int
    id: int
    title: str
    labels: list[str]
    updatedAt: str
    blocked_by: int                 # how many open issues block it


class EligibleIssue(Issue):
    """An open issue with the three facts the frontier reads (`_eligibility_rows`)."""
    claimed: bool
    has_open_pr: bool
    open_blockers: int


class IssueRead(TypedDict):
    """One issue read on its own, open or closed."""
    number: int
    id: int
    title: str
    state: str
    state_reason: str | None
    labels: list[str]
    pull_request: bool


class IssueRef(TypedDict):
    number: int


class PullRequest(TypedDict):
    """One open PR, as `gh pr list` returns the fields asked for — but for
    `closingIssuesReferences`, which is `issues_closed_by` of the PR."""
    number: int
    title: str
    headRefName: str
    headRefOid: str
    baseRefName: str            # the branch it is open against
    updatedAt: str
    statusCheckRollup: list[Obj] | None
    closingIssuesReferences: list[IssueRef]


class Comment(TypedDict):
    """One comment on an issue or a PR."""
    id: int
    body: str
    url: str


class _WorktreeHere(TypedDict):
    present: bool
    path: str | None


class WorktreeSignal(_WorktreeHere, total=False):
    """Whether an issue's worktree is still on this machine — and, when it is
    `present`, its git progress."""
    commits_ahead: int | None
    dirty: bool
    last_commit_ts: int | None
    worktree_mtime_ts: float | None


class _BranchPushed(TypedDict):
    name: str | None
    commits_ahead: int | None


class BranchSignal(_BranchPushed, total=False):
    """The branch a dead worker pushed, and how far ahead of base it is."""
    candidates: list[str]


class GateConfig(TypedDict):
    ci: GateCiMode
    local_command: str
    adversarial_verify_prompt: str


class Config(TypedDict):
    """The canonical config (`resolve_config`): every key of CONFIG_DEFAULTS,
    and the settled ones beside them."""
    ready_label: str
    epic_labels: list[str]
    base_branch: str
    concurrency: int
    gate: GateConfig
    retry: int
    escalate_label: str
    claim_namespace: str


class BatchMember(TypedDict):
    """One PR a merge batch holds, with the issue it closes."""
    issue: int
    pr: int


class Turn(TypedDict):
    """A landing turn as `latest_turn` reads it off a PR: every field of
    TURN_RECORD, one the marker does not state at its blank, plus the comment
    it was read from."""
    instance: str
    at: int | None
    verified: str | None
    allow_no_checks: bool
    stopped: LandOutcome | None
    head: str | None
    restarted: int | None
    batch: str | None
    members: list[BatchMember]
    phase: BatchPhase | None
    unbatched: Unbatched | None
    of: str | None
    released: bool
    comment_id: int | None


class Verdict(TypedDict):
    """A worker's verdict as `latest_verdict` reads it off an issue. `phase` is
    whatever the marker says: a word outside VERDICT_PHASES is a failure, not
    an error."""
    found: bool
    phase: str | None
    blocked_by: list[int]
    reason: str | None
    comment_url: str | None


class Standing(TypedDict):
    """Where one issue a `blocked` verdict names stands (`blocker_standings`)."""
    number: int
    standing: BlockerStanding
    reason: str | None


class Progress(TypedDict):
    """One worktree's git progress, as `afk no-pr` and `afk recovery` read it."""
    commits_ahead: int | None       # None: unreadable — never zero
    dirty: bool
    last_commit_ts: int | None
    worktree_mtime_ts: float | None


class WorkerReading(TypedDict):
    """What orca says of a worker's terminal (`read_worker_state`)."""
    terminal: Literal["busy", "idle", "none"]
    terminal_idle_seconds: int | None
    state: str | None


class Seen(TypedDict):
    """One worker's classification (`classify_stopped`): its cause, and the row
    of WORKER_CAUSES it comes to."""
    cause: WorkerCause
    outcome: str
    action: str
    idle_seconds: int | None
    pending_blockers: list[int]


class WorkerRow(Seen):
    """`afk no-pr`'s row for the worker of one claim: its classification, and
    the signals it was read from."""
    worktree: str | None
    progress: Progress | None       # None: not read — the reading alone settled it
    worker_verdict: Verdict | None
    blockers: list[Standing]
    nudged_at: int | None
    turn_at: int | None


class BatchWorkerRow(Seen):
    """`afk no-pr --batch`'s row: a batch's worker declares no verdict and
    names no blocker."""
    batch: str
    worktree: str | None
    progress: Progress | None
    nudged_at: int | None
    turn_at: int | None
    worker_state: str | None


class Call(TypedDict):
    """What every `afk` line a tick hands back repeats (`afk_command`)."""
    afk_path: str
    repo: str
    config: str                     # the run's config, as JSON
    instance: str
    worker_command: str


class _Judgment(TypedDict):
    issue: int
    kind: JudgmentKind
    question: str
    context: Obj
    if_yes: str
    if_no: str


class Judgment(_Judgment, total=False):
    """One question a tick returns instead of deciding, with the `afk` line for
    either answer. `bulky`: answering takes reading something long."""
    bulky: bool


class CycleState(TypedDict):
    """What a launcher hands back between cycles (`cycle_state`), verbatim."""
    fingerprint: str
    skips: int
    empty_streak: int
    in_flight: int
    frontier_remaining: int
    unsettled: bool
    boards: dict[str, str]
    instance: str
    worker_command: str


class RecoveryPlan(TypedDict):
    """How one claim's worker is (re)started (`select_recovery`)."""
    tier: Literal[1, 2, 3]
    action: Literal["reuse_worktree", "recreate_at_tip", "dispatch_fresh"]
    prompt: Literal["fresh", "continue", "landing"]
    reason: str


class RowBatch(TypedDict):
    """The merge batch a `landing` row's turn is in, as the row carries it."""
    id: str
    members: list[int]
    phase: BatchPhase | None


class MineRow(TypedDict):
    """One claim of mine in the working set (`assemble_working_set`)."""
    number: int
    title: str | None
    status: ClaimStatus
    board_phase: StatusPhase | None
    pr: int | None
    checks: ChecksState | None
    attempt: int
    starting: bool
    stopped: LandOutcome | None
    batch: RowBatch | None
    unbatched: Unbatched | None


class BoardBatch(TypedDict):
    """The merge batch a status board names: its PRs and what is being done."""
    prs: list[int]
    phase: BatchPhase


class Stacked(BatchMember):
    """A member whose PR is on the batch's stack, with the commit that put it there."""
    commit: str


class Batch(TypedDict):
    """A merge batch on record on my claims' PRs, mine or a dead fleet's."""
    id: str
    instance: str
    members: list[BatchMember]
    phase: BatchPhase | None
    at: int | None


class Dispatchable(TypedDict):
    number: int
    title: str | None


class Excluded(TypedDict):
    number: int
    reason: str


class Frontier(TypedDict):
    dispatch: list[Dispatchable]
    excluded: list[Excluded]


class PeerClaim(TypedDict):
    number: int
    instance: str | None


class StaleClaim(TypedDict):
    number: int
    instance: str | None
    sha: str | None         # what `--expect-sha` takes


class WorkingSet(TypedDict):
    """The tick's whole working set (`assemble_working_set`)."""
    frontier: Frontier
    mine: list[MineRow]
    merge_order: list[int]
    batches: list[Batch]
    peer_live: list[PeerClaim]
    stale: list[StaleClaim]
    stale_closed: list[StaleClaim]
    free_slots: int
    fingerprint: str
    now: float


def assert_never(value: NoReturn) -> NoReturn:
    """The end of a chain that handles every word of a closed vocabulary."""
    raise AssertionError(f"unhandled: {value!r}")


# --------------------------------------------------------------------------- #
# Config — one home for every key and default (ADR-0009)                       #
# --------------------------------------------------------------------------- #
#
# THE single source of truth for the config schema: every key the fleet knows,
# with its default and (via the default's type) its shape. The template in
# references/config-template.md is the human-facing rendering of this table —
# a fixture test keeps the two equal, so a hand-edit that drifts turns the
# suite red. The instance id and the worker launch command are deliberately NOT
# keys here: they are per-run, launcher-held facts, and the unknown-key error below is
# what keeps them out of files.
#
# A key is here because repos really differ in it (ADR-0038): which labels the
# repo triages with, how many workers the machine carries, what its
# gate is, how often a failure is retried. What every fleet does alike is not a
# key — it is the constants under this table, or simply what the code does.

CONFIG_DEFAULTS: Obj = {
    # dispatch contract
    "ready_label": "ready-for-agent",
    "epic_labels": ["epic", "prd", "wayfinder:map"],
    # workers
    "concurrency": 3,
    # completion gate
    "gate": {
        "ci": "required",
        "local_command": "",
        "adversarial_verify_prompt": "",
    },
    # failure handling
    "retry": 2,
    "escalate_label": "ready-for-human",
}

# The fields of the canonical config that no file sets: `afk probe` settles each
# at bootstrap and returns it in the config the launcher holds from then on, so
# every later call agrees with it.
#
#   claim_namespace  where this run's claim and heartbeat refs live.
#   base_branch      the branch every worker cuts from, opens its PR against, and
#                    lands on. It has no default: the human confirms it at every
#                    launch, and it is kept on the remote (`BASE_RECORD`), where
#                    every launcher on the repo reads the same one (ADR-0042).
#                    "" is a config no launch has settled yet.
CONFIG_SETTLED = {"claim_namespace": "refs/afk", "base_branch": ""}

# What a fleet does the same in every repo. Each was a config key once, and no
# repo ever set one to anything else (ADR-0038).
BRANCH_PATTERN = "issue-{number}-{slug}"    # the worktree-NAME hint handed to orca (ADR-0005)
WORKER_IDLE_GRACE_SECONDS = 300             # quiet before an idle no-PR worker is judged finished
BUSY_INTERVAL_SECONDS = 90                  # the launcher's sleep while there is work or a claim
IDLE_INTERVAL_SECONDS = 1500                # ... and once the fleet has gone quiet
IDLE_TICKS_BEFORE_SLEEP = 3                 # empty cycles in a row before it counts as quiet
CLAIM_LEASE_TTL_SECONDS = 4500              # a claim is live while its heartbeat is this fresh
FORCE_TICK_AFTER_SKIPS = 6                  # a full tick at least every N skipped cycles (ADR-0007)


# The two places claim + heartbeat refs can live, as namespace → (claim ref prefix,
# heartbeat ref prefix, the ref of the base branch's record). `refs/afk` is hidden
# from branch listings and `on: push` CI; `refs/heads` is the fallback for a remote
# whose rules forbid non-branch refs, where the same markers are ordinary
# `afk-claim/*` / `afk-heartbeat/*` branches and an `afk-base` one (ADR-0003). A
# closed set: any other prefix would be a third layout no probe, warning or doc
# describes.
CLAIM_NAMESPACES = {
    "refs/afk": ("refs/afk/claim", "refs/afk/heartbeat", "refs/afk/base"),
    "refs/heads": ("refs/heads/afk-claim", "refs/heads/afk-heartbeat", "refs/heads/afk-base"),
}
BRANCH_NAMESPACE = "refs/heads"


# --------------------------------------------------------------------------- #
# Records — one encoding, on a ref or in a comment (ADR-0031, ADR-0032)        #
# --------------------------------------------------------------------------- #
#
# What the fleet remembers is a record: a set of named fields, written as
#
#     <word> <field>=<value> <field>=<value> …
#
# and carried one of two ways — as the subject of the small commit a ref points
# at (`record_message` / `read_record`), or as a marker in a comment on an issue
# or a PR, `<!--<word> <field>=<value> …-->`, with the same facts worded for a
# human under it (`record_comment` / `read_marker` / `latest_record`).
#
# A kind of record declares its word and its fields and nothing else; writing
# one and reading one back are the same code for every kind and both carriers,
# and so are the rules of reading:
#
#   - a field the kind does not declare is ignored — a newer fleet may write one
#     an older fleet reads past;
#   - a field with no value, or a value that is not of its type, is a field that
#     is missing; a missing optional field is simply absent from the record;
#   - a commit whose subject does not open with the kind's word, a comment with
#     no marker of the kind, or either one lacking a required field, is not a
#     record: it reads as None, never as a record with holes in it;
#   - of the comments on one issue or PR, the latest that carries a record is
#     the record (`latest_record`).
#
# A value is percent-encoded only where it would break a word (whitespace, `%`,
# anything outside ASCII), so an instance id or a hostname is written as itself.
# The one value written raw is a kind's `tail`: the field a person fills in with
# a phrase, which runs from its name to the end of the record.

class RecordKind(NamedTuple):
    word: str           # the record's first word: what kind of record this is
    fields: dict[str, Any]      # field → its type, in the order they are written
    required: tuple[str, ...]   # the fields without which a commit or a marker is not this record
    tail: str | None = None     # the field, declared last, whose value runs to the record's end


class FieldType(NamedTuple):
    """How one field's value is spelled. `str` and `int` are declared as
    themselves; the rest are below, or beside the one kind that needs them."""
    write: Callable[[Any], str]     # value → its text; "" when there is nothing to write
    read: Callable[[str], Any]      # text (never empty) → value; None when it is no such value
    empty: object = None            # what a record that does not state the field reads as


def _digits(raw: str) -> int | None:
    return int(raw) if raw.isascii() and raw.isdigit() else None


_FIELD_TYPES = {str: FieldType(str, lambda raw: raw),
                int: FieldType(lambda value: str(int(value)), _digits)}

# A fact that is either so or not: written `=1` when so, left out when not.
FLAG = FieldType(lambda value: "1" if value else "", lambda raw: True if raw == "1" else None,
                 empty=False)

# Whole numbers, comma-separated; anything else in the list is dropped.
INTS = FieldType(lambda values: ",".join(str(int(v)) for v in values),
                 lambda raw: [int(x) for x in re.split(r"[,\s]+", raw) if _digits(x) is not None]
                 or None, empty=())


def one_of(vocabulary: tuple[str, ...]) -> FieldType:
    """A word of a closed vocabulary: any other is not written (a defect in the
    caller: it raises) and not read (the field is missing)."""
    def write(value: str) -> str:
        if value not in vocabulary:
            raise ValueError(f"not one of {', '.join(vocabulary)}: {value!r}")
        return value
    return FieldType(write, lambda raw: raw if raw in vocabulary else None)


# A claim names the fleet instance that holds an issue (ADR-0003). The ref is the
# lock; the record says whose it is.
CLAIM_RECORD = RecordKind("afk-claim", {"instance": str, "host": str, "ts": int}, ("instance",))
# A heartbeat is the time a fleet instance last said it was alive; whose it is
# is the ref's name.
HEARTBEAT_RECORD = RecordKind("afk-heartbeat", {"instance": str, "ts": int}, ("ts",))
# A recorded gate run: a green run of `command` on `tree`, finished at `at`
# (`gate_record`, ADR-0030). The ref's name is the key; the time is the record.
GATE_RUN_RECORD = RecordKind("afk-gate", {"tree": str, "command": str, "at": int}, ("at",))
# What the bootstrap probe pushes to learn whether a namespace takes a push.
PROBE_RECORD = RecordKind("afk-probe", {"ts": int}, ("ts",))
# The repo's base branch, as the human last confirmed it at a launch (ADR-0042).
BASE_RECORD = RecordKind("afk-base", {"branch": str, "ts": int}, ("branch",))

def _field_type(kind: RecordKind, name: str) -> FieldType:
    declared = kind.fields[name]
    return _FIELD_TYPES.get(declared, declared)


def blank_record(kind: RecordKind) -> Obj:
    """Every field of `kind` at the value that says nothing (its type's
    `empty`) — what a reader lays a record over, so a field the marker does not
    state is still there to be asked for."""
    blank = {name: _field_type(kind, name).empty for name in kind.fields}
    return {name: [] if value == () else value for name, value in blank.items()}


_RECORD_SAFE = "!\"#$&'()*+,/:;<=>?@[\\]^`{|}~"


def _field_words(kind: RecordKind, record: Obj, spell: Callable[[str, Any], str]) -> list[str]:
    """The `<field>=<value>` words of `record`, in the kind's order — each value
    as `spell(name, value)` gives it, a field it gives no text for left out."""
    unknown = sorted(set(record) - set(kind.fields))
    if unknown:
        raise ValueError(f"{kind.word} record has no field {', '.join(unknown)}")
    words = []
    for name in kind.fields:
        value = record.get(name)
        text = "" if value is None else spell(name, value)
        if text:
            words.append(f"{name}={text}")
        elif name in kind.required:
            raise ValueError(f"{kind.word} record needs {name}")
    return words


def record_message(kind: RecordKind, record: Obj) -> str:
    """A record → the one line that carries it: a commit's message as it is, a
    comment's marker inside `<!--` and `-->` (`record_marker`). Fields are written
    in the kind's order; one that is None or empty is left out. A field the kind
    does not declare, or a required one left out, is a defect in the caller and
    raises."""
    def spell(name: str, value: Any) -> str:
        text = _field_type(kind, name).write(value)
        return text if name == kind.tail else urllib.parse.quote(text, safe=_RECORD_SAFE)
    return " ".join([kind.word, *_field_words(kind, record, spell)])


def _read_fields(kind: RecordKind, text: str) -> Obj | None:
    """What follows a kind's word → the record, or None when a required field is
    missing. A comma may be followed by whitespace: a list is still one value."""
    record: Obj = {}
    tail = re.search(rf"\b{kind.tail}=(.*)$", text, re.DOTALL) if kind.tail else None
    if kind.tail and tail:
        text = text[:tail.start()]
        if tail.group(1).strip():
            record[kind.tail] = tail.group(1).strip()
    for word in re.sub(r",\s+", ",", text).split():
        name, _, raw = word.partition("=")
        if name not in kind.fields or not raw:
            continue
        value = _field_type(kind, name).read(urllib.parse.unquote(raw))
        if value is not None:
            record[name] = value
    return record if all(name in record for name in kind.required) else None


def read_record(kind: RecordKind, message: str | None) -> Obj | None:
    """A commit message → the record of `kind` it carries, as {field: value} with
    every required field present and each optional one present only when the
    commit states it — or None when the commit is not such a record."""
    word, _, rest = (message or "").split("\n", 1)[0].strip().partition(" ")
    return _read_fields(kind, rest) if word == kind.word else None


def record_marker(kind: RecordKind, record: Obj) -> str:
    """A record → the marker that carries it in a comment."""
    return f"<!--{record_message(kind, record)}-->"


def marker_format(kind: RecordKind, shown: Obj, optional: tuple[str, ...] = ()) -> str:
    """A kind's marker as it is shown to whoever must write one by hand: each
    field of `shown` ({field: placeholder, or the value already known}) under
    its own name and in the kind's order, the `optional` ones in brackets."""
    words = _field_words(kind, shown, lambda name, value: str(value))
    words = [f"[{word}]" if word.partition("=")[0] in optional else word for word in words]
    return f"<!--{' '.join([kind.word, *words])}-->"


def record_comment(kind: RecordKind, record: Obj, text: str) -> str:
    """The comment that keeps a record: its marker, then the same facts worded
    for a human reading the issue or the PR."""
    return f"{record_marker(kind, record)}\n{text}"


def read_marker(kind: RecordKind, body: str | None) -> Obj | None:
    """A comment body → the record of `kind` its first marker carries, read as
    `read_record` reads a commit; None when the body has no such marker, or the
    marker lacks a required field."""
    m = re.search(rf"<!--\s*{re.escape(kind.word)}\b(.*?)-->", body or "", re.DOTALL)
    return _read_fields(kind, m.group(1)) if m else None


def latest_record(kind: RecordKind,
                  comments: Iterable[Comment] | None) -> tuple[Obj | None, Comment | None]:
    """The record of `kind` kept on one issue or PR, from its comments
    ([{"id", "body", "url"}...], oldest first) → (record, the comment that
    carries it), or (None, None). The latest marker wins — and a comment whose
    marker is not a record is passed over, not read as the latest. The comment
    is where a rewrite goes: a record is kept in ONE comment, rewritten in place."""
    found: tuple[Obj | None, Comment | None] = (None, None)
    for comment in comments or []:
        record = read_marker(kind, comment["body"])
        if record is not None:
            found = (record, comment)
    return found

# The completion gate's two modes (ADR-0012), each with what the status board calls
# that gate. `required` waits for the PR's GitHub checks; `local` never reads them
# and makes `gate.local_command` the gate, run by `afk land` against the exact
# tree that lands.
GateCiMode = Literal["required", "local"]
GATE_CI_MODES: dict[GateCiMode, str] = {"required": "CI", "local": "本地门"}

# Keys that were renamed, and why. A file still carrying the old name must fail
# LOUDLY with the migration note rather than be silently defaulted — a config that
# lies to its author is the failure mode ADR-0009 exists to prevent.
CONFIG_RENAMED: dict[str, tuple[str, str]] = {}

# Why no file names the branch the fleet lands on — said of each key that did.
_BASE_NOTE = ("the base branch is not set in a file (ADR-0042): it is confirmed at every launch "
              "— asked for, or passed as `/afk-fleet --base-branch <name>` — and kept on the "
              "remote, where every launcher on the repo reads the same one.")

# What ADR-0038 says of every key that turned out to be the same in every repo.
_ALWAYS = "(ADR-0038). Delete the key."

# Keys that were removed, and why — refused as loudly as a renamed one.
CONFIG_REMOVED = {
    "merge.target": f"{_BASE_NOTE} Delete the merge: section.",
    "gate.trust_recorded_run": (
        "a recorded gate run is always trusted now (ADR-0030): the landing skips its own run "
        "whenever a green run of the configured command is on record for the tree that lands. "
        "Delete the key."),
    "merge.strategy": (
        "every PR lands as a merge commit now (ADR-0034): a PR's own head reaches the target, so "
        "GitHub shows it merged whether it landed alone or in a merge batch. There is no squash "
        "and no rebase. Delete the key."),
    "claim": (
        "an issue is always claimed by its claim ref (ADR-0003): the key named the one way "
        "there is, and nothing read it. Delete the key."),
    "dependencies": (
        "an issue's open blockers are always GitHub's native blocked_by edges: the key named the "
        "one way there is, and nothing read it. Delete the key."),
    "worker": (
        "a worker is always started through orca (ADR-0005): the key named the one backend "
        "there is, and nothing read it. Delete the key."),
    "merge.batch": (
        "merge batches are no longer an option (ADR-0034): with gate.ci 'local' and no "
        "adversarial verify, two or more PRs that are ready together always land as "
        "one batch. Delete the key."),
    "gate.adversarial_verify": (
        "a non-empty gate.adversarial_verify_prompt is what turns the adversarial verify on "
        "(ADR-0038): say what the verifier checks, or leave it empty for none. Delete the key."),
    "merge.sync_before_merge": (
        f"a landing always merges the base branch into the PR's branch before it gates: the "
        f"tree that is gated is the tree that lands {_ALWAYS}"),
    "merge.rebase_before_merge": (
        f"a landing always merges the base branch into the PR's branch before it gates — a "
        f"merge, never a rebase (ADR-0012) {_ALWAYS}"),
    "merge.delete_branch": (
        f"a landed or superseded PR's branch is always deleted {_ALWAYS}"),
    "branch_pattern": (
        f"a worker's worktree is always named issue-<number>-<slug>, which is how its branch "
        f"is found again {_ALWAYS}"),
    "worktree_cleanup": (
        f"a landed or closed issue's worktree is always removed, and an escalated one's is "
        f"always left for the human {_ALWAYS}"),
    "escalate_comment": (
        f"an escalation always says where it got stuck {_ALWAYS}"),
    "progress_comment": (
        f"every claimed issue carries its status board (ADR-0006) {_ALWAYS}"),
    "fingerprint_gate": (
        f"a cycle always skips its tick while nothing observable moved (ADR-0007) {_ALWAYS}"),
    **{key: f"the fleet's timing is the same in every repo {_ALWAYS}"
       for key in ("worker_idle_grace_seconds", "busy_interval_seconds", "idle_interval_seconds",
                   "idle_ticks_before_sleep", "claim_lease_ttl_seconds",
                   "force_tick_after_skips")},
}

# Sections that no longer hold a key: a file that still opens one is read on, so
# that each key under it gets its own note.
_RETIRED_SECTIONS = {key.partition(".")[0] for key in (*CONFIG_RENAMED, *CONFIG_REMOVED)
                     if "." in key} - set(CONFIG_DEFAULTS)


# Why a file may not set a settled field — said where a file tries to.
_SETTLED_NOTES = {
    "claim_namespace": ("config: 'claim_namespace' is not set in a file — `afk probe` settles "
                        "where claim refs live at every bootstrap, and falls back to refs/heads "
                        "by itself when the remote refuses refs/afk (ADR-0038). Delete the key."),
    "base_branch": f"config: 'base_branch' is not set in a file — {_BASE_NOTE} Delete the key.",
}


def _renamed(dotted: str) -> str | None:
    """The migration error text for a renamed or removed key, or None if it is neither."""
    if dotted in CONFIG_REMOVED:
        return f"config: {dotted!r} was removed — {CONFIG_REMOVED[dotted]}"
    hit = CONFIG_RENAMED.get(dotted)
    if not hit:
        return None
    new, why = hit
    return f"config: {dotted!r} was renamed to {new!r} — {why}"


def validate_config(cfg: Config) -> Config:
    """
    The semantic checks a per-key type cannot express, run on the CANONICAL config
    every time one is resolved — first at load time (`afk config`), at bootstrap,
    with the human present, where a bad combination can still be fixed; and again
    on every subcommand, so nothing downstream ever sees an invalid config.
    Raises ValueError; returns `cfg` unchanged so it can be used inline.
    """
    ns = cfg["claim_namespace"]
    if ns not in CLAIM_NAMESPACES:
        raise ValueError(f"config claim_namespace: expected one of "
                         f"{' | '.join(CLAIM_NAMESPACES)}, got {ns!r}")
    gate = cfg["gate"]
    ci = gate["ci"]
    if ci not in GATE_CI_MODES:
        raise ValueError(f"config gate.ci: expected one of "
                         f"{' | '.join(GATE_CI_MODES)}, got {ci!r}")
    if ci == "local" and not (gate["local_command"] or "").strip():
        raise ValueError("config gate.ci: 'local' requires a non-empty gate.local_command — in "
                         "local mode that command IS the completion gate (ADR-0012), so an empty "
                         "one would merge every PR unverified")
    return cfg


def verifies(config: Config) -> bool:
    """Whether a PR owes an adversarial verify of its head before it lands: the
    repo said what a verifier checks (`gate.adversarial_verify_prompt`)."""
    return bool(config["gate"]["adversarial_verify_prompt"].strip())


def _yaml_block(text: str) -> str:
    """The first ```yaml fence's body if `text` is a markdown file, else the
    text itself (already a bare block)."""
    if "```yaml" in text:
        return text.split("```yaml", 1)[1].split("```", 1)[0]
    return text


def _strip_comment(line: str) -> str:
    """Cut an unquoted trailing `# …` comment; quotes are respected."""
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).strip()


def _coerce(key: str, raw: str, default: object) -> bool | int | list[str] | str:
    """One scalar, typed by its default: bool, int, [a, b] list, or string."""
    if isinstance(default, bool):
        if raw in ("true", "True"):
            return True
        if raw in ("false", "False"):
            return False
        raise ValueError(f"config key {key!r}: expected true/false, got {raw!r}")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"config key {key!r}: expected an integer, got {raw!r}")
    if isinstance(default, list):
        if not (raw.startswith("[") and raw.endswith("]")):
            raise ValueError(f"config key {key!r}: expected [a, b, ...], got {raw!r}")
        return [i.strip().strip("'\"") for i in raw[1:-1].split(",") if i.strip()]
    return raw.strip("'\"")


def parse_config_yaml(text: str) -> Obj:
    """
    Read the per-repo config — the ```yaml block in docs/agents/afk-fleet.md
    (a whole markdown file or a bare block both work). Schema-aware, zero-dep:
    it parses only the dialect this schema uses (`key: value` scalars, one
    inline `[a, b]` list, the one-level `gate:` section), and every key
    and type is checked against CONFIG_DEFAULTS — so parsing IS validation. An
    unknown key raises (a typo silently ignored would be a config that lies to
    its author, and a launcher-held fact in a file is refused by construction); so does
    a wrong shape. Returns the PARTIAL config — only the keys present.
    """
    partial: Obj = {}
    section: str | None = None
    for ln in _yaml_block(text).splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        indented = ln[0] in " \t"
        s = _strip_comment(ln)
        if not s:
            continue
        if ":" not in s:
            raise ValueError(f"config: unparseable line {ln.strip()!r}")
        key, _, raw = s.partition(":")
        key, raw = key.strip(), raw.strip()
        if indented:
            if section is None:
                raise ValueError(f"config: indented key {key!r} outside a gate: section")
            sub = CONFIG_DEFAULTS.get(section, {})
            if key not in sub:
                raise ValueError(_renamed(f"{section}.{key}")
                                 or f"config: unknown key {section}.{key}")
            partial.setdefault(section, {})[key] = _coerce(f"{section}.{key}", raw, sub[key])
        else:
            if key in _RETIRED_SECTIONS and not raw:
                section = key           # read on: each key under it has its own note
                continue
            if key in CONFIG_SETTLED:
                raise ValueError(_SETTLED_NOTES[key])
            if key not in CONFIG_DEFAULTS:
                raise ValueError(_renamed(key)
                                 or f"config: unknown key {key!r} (note: the instance id and "
                                    f"the worker launch command are per-run facts, never config keys)")
            default = CONFIG_DEFAULTS[key]
            if isinstance(default, dict):
                if raw:
                    raise ValueError(f"config key {key!r} is a section — write `{key}:` "
                                     f"with indented keys")
                section = key
                partial.setdefault(key, {})
            else:
                section = None
                partial[key] = _coerce(key, raw, default)
    return partial


def resolve_config(partial: Obj) -> Config:
    """Partial config → the complete canonical config: every key present,
    defaults filled from CONFIG_DEFAULTS (one level deep for gate), and the
    settled fields beside them. Idempotent — resolving an already-canonical
    config is a no-op."""
    out: Obj = {}
    for k, dv in {**CONFIG_DEFAULTS, **CONFIG_SETTLED}.items():
        if isinstance(dv, dict):
            merged = dict(dv)
            merged.update(partial.get(k) or {})
            out[k] = merged
        elif k in partial:
            out[k] = partial[k]
        else:
            out[k] = list(dv) if isinstance(dv, list) else dv
    return cast("Config", out)      # every key of the two tables: that is what a Config is


def override_config(cfg: Config, assignments: Iterable[str] | None) -> Config:
    """Lay `key=value` overrides (the CLI's `--set`) onto a canonical config, in
    place, and return it. Keys are the config file's own — dotted for a section
    (`gate.ci=local`), plus the settled fields — and values are typed
    by the key's default exactly as the file's are, except that a string is taken
    verbatim (the shell already unquoted it). An unknown key, or an item with no `=`, raises ValueError; a
    renamed or removed one raises with its migration note, as the file does."""
    keyed = cast(Obj, cfg)          # written by a key read off the command line
    for item in assignments or []:
        dotted, eq, raw = item.partition("=")
        if _renamed(dotted.strip()):
            raise ValueError(_renamed(dotted.strip()))
        section, _, key = dotted.strip().rpartition(".")
        table = CONFIG_DEFAULTS.get(section) if section else {**CONFIG_DEFAULTS, **CONFIG_SETTLED}
        if not eq or not isinstance(table, dict) or isinstance(table.get(key), (dict, type(None))):
            raise ValueError(f"--set: expected <config key>=<value>, got {item!r}")
        default = table[key]
        value = raw if isinstance(default, str) else _coerce(dotted.strip(), raw.strip(), default)
        (keyed[section] if section else keyed)[key] = value
    return cfg

# --------------------------------------------------------------------------- #
# Dispatch eligibility — "can a worker take this issue right now?"             #
# --------------------------------------------------------------------------- #
#
# Issues arrive from afk.py's gatherer as OPEN issues with `labels` already a
# list of names and `blocked_by` their open-blocker count — the issue list read
# carries it on every row; the two eligibility facts that are not on the issue
# itself (claimed / has_open_pr) are grafted by `_eligibility_rows`.

def _eligibility_rows(issues: list[Issue], prs: list[PullRequest],
                      claims: list[Claim]) -> list[EligibleIssue]:
    """Each issue + the three eligibility facts `select_frontier` reads:
    `claimed` (a claim ref exists, any owner — not the assignee, ADR-0003),
    `has_open_pr` (an open PR closes it — the open-PR guard) and
    `open_blockers` (the issue's own `blocked_by` count; missing → 0)."""
    claimed = {c["number"] for c in claims}
    pr_for = _closing_pr_map(prs)
    return [{**i,
             "claimed": i["number"] in claimed,
             "has_open_pr": i["number"] in pr_for,
             "open_blockers": int(i["blocked_by"] or 0)}
            for i in issues]


def label_bars(labels: Iterable[str] | None, ready_label: str,
               epic_labels: Iterable[str]) -> dict[str, str]:
    """
    Every reason an issue's LABELS keep a fleet from dispatching it, as
    {kind: reason} in the order the frontier reports them; {} when its labels
    allow dispatch. The one home of the label rules: the frontier reads it, and
    so does the standing of a blocker (`blocker_standings`), so the two cannot
    disagree about what a fleet will work.

      not_ready  it lacks `ready_label` — the one bar a claim or an open PR
                 overrides (the issue is being worked anyway);
      epic       it carries an epic label — never dispatched, whatever else holds.
    """
    labels = set(labels or [])
    bars: dict[str, str] = {}
    if ready_label not in labels:
        bars["not_ready"] = f"no {ready_label} label"
    hit_epic = labels & {e.strip() for e in epic_labels if e.strip()}
    if hit_epic:
        bars["epic"] = f"epic label ({', '.join(sorted(hit_epic))})"
    return bars


def select_frontier(issues: list[EligibleIssue], ready_label: str, epic_labels: Iterable[str]) -> Obj:
    """
    Decide which issues are dispatchable RIGHT NOW. Shared by `--plan` and the live
    tick, so both compute the identical frontier (a stable published contract).

    `issues` are `_eligibility_rows`. One is dispatchable iff ALL hold: has
    ready_label · no epic label · not claimed · no open linked PR · zero open
    blocking dependencies.

    Returns {"dispatch": [num...], "excluded": [{"number","reason"}...]}.
    """
    dispatch, excluded = [], []
    for issue in issues:
        num = issue["number"]
        bars = label_bars(issue["labels"], ready_label, epic_labels)
        if bars:
            reason = next(iter(bars.values()))
        elif issue["claimed"]:
            reason = "already claimed (afk-claim ref exists)"
        elif issue["has_open_pr"]:
            reason = "has an open linked PR"
        elif issue["open_blockers"]:
            reason = f"{issue['open_blockers']} open blocker(s)"
        else:
            dispatch.append(num)
            continue
        excluded.append({"number": num, "reason": reason})
    return {"dispatch": dispatch, "excluded": excluded}


# --------------------------------------------------------------------------- #
# Claim ownership + owner-liveness — the correctness-critical partition        #
# --------------------------------------------------------------------------- #

def is_stale(last_ts: float | None, now: float, ttl: float) -> bool:
    """A claim's owner is presumed dead when its heartbeat is missing or older
    than `ttl`. Missing (None) counts as stale — an owner that never beat."""
    if last_ts is None:
        return True
    return (now - int(last_ts)) > ttl


def heartbeat_due(last_ts: float | None, now: float, ttl: float) -> bool:
    """Refresh my own heartbeat once it is older than ttl/3 (or never beat). Beating
    at ttl/3 keeps a comfortable 3x margin under the lease while staying cheap."""
    if last_ts is None:
        return True
    return (now - int(last_ts)) > ttl / 3.0


def classify_claims(claims: list[Claim], heartbeats: Mapping[str, float], me: str | None,
                    now: float, ttl: float) -> dict[str, list[int]]:
    """
    Partition every afk-claim ref by ownership and owner-liveness — the verdict a
    wrong answer would silently corrupt (ADR-0003).

      claims:     [{"number": int, "instance": str}, ...]  (from refs/afk/claim/*)
      heartbeats: {instance_id: last_ts_epoch}             (from refs/afk/heartbeat/*)
      me:         my instance id
      now, ttl:   epoch seconds / CLAIM_LEASE_TTL_SECONDS

    Returns {"mine":[n...], "peer_live":[n...], "stale":[n...]}:
      mine       — stamped with my instance; I reconcile these locally (a no-PR/
                   no-live-worker one is an *orphaned claim*, decided by the tick
                   with the worker liveness probe — not here).
      peer_live  — a peer owns it AND its heartbeat is within ttl → never touch.
      stale      — a peer owns it AND its heartbeat is missing/expired → the only
                   foreign claim I may reclaim (--force-with-lease takeover), or —
                   when its issue is already closed — delete (`assemble_working_set`
                   splits those out as `stale_closed`).
    A claim whose marker names no instance is treated as a peer's and, lacking a
    heartbeat, is reclaimable.
    """
    mine, peer_live, stale = [], [], []
    for c in claims:
        n = c["number"]
        inst = c["instance"]
        if inst is not None and inst == me:
            mine.append(n)
        elif is_stale(heartbeats.get(inst) if inst else None, now, ttl):
            stale.append(n)
        else:
            peer_live.append(n)
    return {"mine": sorted(mine), "peer_live": sorted(peer_live), "stale": sorted(stale)}


# Every `status` a `mine` row can carry (`claim_status`) → the status-board
# phase a human is shown meanwhile; None for a row whose board is not
# re-rendered. The keys are the vocabulary the tick's instructions route on (a
# test holds the docs to it). `merged` / `escalated` / `parked`, the board's
# terminal phases, are written by the transitions that reach them.
ClaimStatus = Literal["awaiting_turn", "landing", "awaiting_ci", "failure", "no_pr", "closed"]
BOARD_PHASE_OF: dict[ClaimStatus, Optional[StatusPhase]] = {
    "awaiting_turn": "awaiting_turn", "landing": "landing",
    "awaiting_ci": "pr_open", "failure": "ci_failed", "no_pr": "claimed",
    "closed": None}
CLAIM_STATUSES = tuple(BOARD_PHASE_OF)

# What a PR's checks come to (`pr_checks_state`); no checks at all is None.
ChecksState = Literal["green", "red", "pending"]


def claim_status(has_pr: bool, checks_state: ChecksState | None, ci_mode: GateCiMode,
                 closed: bool = False, landing: bool = False) -> ClaimStatus:
    """
    Classify one of MY in-flight claims from its PR + checks → its `status`, one
    of CLAIM_STATUSES: what the tick does next.

      has_pr:       an open PR closes the issue
      checks_state: "green" | "red" | "pending" | None  (`pr_checks_state`)
      ci_mode:      gate.ci — "required" reads the checks; "local" never does
      closed:       the claimed issue is itself CLOSED — its worker landed the PR
                    (`afk land` cannot release the claim), or a human finished it
                    by hand
      landing:      the PR holds this fleet instance's landing turn (`held_turn`)

      closed         its leftover claim (and worktree) is released
      no_pr          `afk no-pr` is asked why
      landing        `afk no-pr` is asked whether its worker is still at it — or,
                     when the landing stopped for the tick, `afk turn` again
      awaiting_ci    left: checks exist and are still running
      failure        `afk fail`
      awaiting_turn  granted the landing turn (`afk turn`) when it is first in
                     `merge_order` and no claim of mine is `landing`

    Three cases a first guess gets wrong:

    - A PR that holds the turn is `landing` whatever its checks say: a pending
      or red run on the way is the landing's own business (`afk land` answers
      `awaiting_ci` / `gate_red`), never a second route into `afk fail`.
    - In `local` mode (ADR-0012) every open PR without the turn is
      `awaiting_turn`: gating is an action `afk land` takes, not an observation
      the tick waits for, so a red remote run the fleet does not gate on must
      not park the claim in `failure`.
    - A PR with NO checks at all is `awaiting_turn` in `required` mode too:
      nothing is running, so `awaiting_ci` would park it forever. `afk turn` is
      where the tick's judgment is asked for (`no_checks`).
    """
    if closed:
        return "closed"
    if not has_pr:
        return "no_pr"
    if landing:
        return "landing"
    if ci_mode == "local" or checks_state in ("green", None):
        return "awaiting_turn"
    return "failure" if checks_state == "red" else "awaiting_ci"


# --------------------------------------------------------------------------- #
# The local completion gate + its bootstrap compatibility probe (ADR-0012)     #
# --------------------------------------------------------------------------- #
#
# In `gate.ci: local` the repo-local build/test command IS the completion gate:
# the worker runs it after its pre-PR sync, and `afk land` runs it again on the
# landing turn, after the landing's sync, in the same worktree. The invariant
# both runs serve: *what lands on the target branch was tested in the form it
# lands.* A green run is put on record on the remote, under the TREE it tested and
# the command that ran, so any run — the landing's, a batch's — is skipped when,
# and only when, that tree was already tested green by that command, wherever it
# ran (`gate_record_void`, ADR-0030). A red run comes back as a bounded excerpt,
# never a raw log.

GATE_EXCERPT_LINES = 40


def gate_verdict(exit_code: int, output: str | None, max_lines: int = GATE_EXCERPT_LINES,
                 timed_out: bool = False) -> Obj:
    """
    One local-gate run → `{status, exit_code, excerpt, omitted_lines, timed_out}`.

    Pure so the one thing that could quietly poison a merge — "which exit code
    counts as green" — is fixture-pinned: ONLY 0 is green, and a run that timed out
    is red, never green-by-default. The excerpt is the LAST `max_lines` lines
    (where build/test runners put the failure summary), bounded so a 50k-line log
    reaches the PR comment as a readable tail instead of flooding it.
    """
    lines = [ln.rstrip() for ln in (output or "").splitlines()]
    tail = lines[-max_lines:] if max_lines and max_lines > 0 else lines
    return {"status": "green" if (int(exit_code) == 0 and not timed_out) else "red",
            "exit_code": int(exit_code),
            "timed_out": bool(timed_out),
            "excerpt": "\n".join(tail).strip(),
            "omitted_lines": max(0, len(lines) - len(tail))}


# Where recorded gate runs live on the remote, one ref per record, and how long
# one is believed: a green that depends on something outside the tree (a
# toolchain, a service, the date) is trusted for a day, on any machine.
GATE_RECORD_NAMESPACE = "refs/afk/gate"
GATE_RECORD_TTL = 24 * 3600


def gate_record_ref(tree: str, command: str) -> str:
    """The remote ref a recorded gate run of `command` on `tree` lives at. The
    name IS the key — the tree tested and the command that tested it — so asking
    "was this tested green?" is asking for one ref, and two runs never contend."""
    return (f"{GATE_RECORD_NAMESPACE}/{tree}-"
            f"{hashlib.sha256(command.encode('utf-8')).hexdigest()[:16]}")


def gate_record(tree: str, command: str, at: float) -> Obj:
    """
    What a GREEN run of the local gate on a committed tree puts on record — by
    `afk gate`, `afk land` and a batch's landing alike; never after a red or
    timed-out run, and never for a run over uncommitted or untracked files, which
    tested a tree no commit holds:

      tree:    the tree that was tested — the content, whatever commit holds it
      command: the `gate.local_command` that ran, verbatim
      at:      when the run finished (epoch seconds)
    """
    return {"tree": tree, "command": command, "at": int(at)}


def gate_record_void(record: Obj | None, now: float) -> str | None:
    """
    Why a recorded gate run does NOT stand in for a run of the gate — or None
    when it does, which is the only case a landing skips its own (ADR-0030).

      record:  the `gate_record` the remote holds at `gate_record_ref` for the
               tree that would land, after the landing's sync, and the
               `gate.local_command` configured now — None when it holds none
               (never gated through `afk`, a red run since, a record that could
               not be written or read)
      now:     epoch seconds

    The record proves one command passed on one tree, and it was asked for by
    exactly that tree and that command: its name is its key, so which tree and
    which command it is of are not questions left to ask here. A sync that
    brought the target in, a later commit or another command is another name,
    with its own record or none; a record older than `GATE_RECORD_TTL` is no
    longer believed. Void is the safe side: the landing runs the gate.
    """
    if record is None:
        return "no green run of the gate is on record for the tree that would land"
    age = int(now) - record["at"]
    if age > GATE_RECORD_TTL:
        return (f"the recorded run is {age}s old — a record is trusted for "
                f"{GATE_RECORD_TTL}s")
    return None


def base_refusal(answer: str, recorded: str | None, heads: Iterable[str], claims: int,
                 live: list[str]) -> str | None:
    """Why `answer` cannot be the repo's base branch, or None when it can
    (ADR-0042). It must be a branch of the remote (`heads`) — the fleet never
    makes one. And it may differ from the `recorded` one only while nothing
    stands on that: no claim (`claims`, how many the remote holds — each is work
    cut from the recorded base, whoever holds it) and no `live` fleet instance."""
    if answer not in heads:
        return (f"no branch {answer!r} on the remote — the base branch must exist before a "
                f"launch; the fleet does not create it")
    if recorded is None or answer == recorded:
        return None
    standing = [f"{claims} claim(s)"] if claims else []
    standing += [f"live fleet instance(s) {', '.join(sorted(live))}"] if live else []
    if not standing:
        return None
    return (f"the base branch is {recorded!r} and {' and '.join(standing)} stand on it, so it "
            f"cannot become {answer!r} now. Launch on {recorded!r}, or let that work finish "
            f"first (stop the other launchers; `/afk-fleet --takeover` inherits a dead one's "
            f"claims), then launch again")


def protection_verdict(ci_mode: GateCiMode, protection: Obj | None,
                       unavailable: str | None = None, batch: bool = False) -> Obj:
    """
    Is the merge target's branch protection compatible with the configured gate?
    Read at bootstrap, with the human present (ADR-0012).

      ci_mode:     gate.ci ("required" | "local")
      protection:  the target branch's protection object, or None if it has none
      unavailable: why protection could not be read (no admin rights, an API
                   error); None when the read succeeded
      batch:       merge batches form under this config (`batches_form`) — one
                   lands by PUSHING its stack to the target, so the target must
                   also accept a direct push

    Returns {"verdict": "ok"|"error"|"warn", "required_checks": [...], "detail"}.

      error  `ci: local` + the target REQUIRES status checks. `gh pr merge` is
             rejected no matter how green the local gate is, and the only bypass —
             `--admin` — also overrides human review, far too much power for an
             unattended fleet. So this is a hard error at bootstrap, not a
             surprise on the first merge.
             Likewise `batch` + a target that refuses a direct push (it
             requires a pull request, restricts who may push, or is locked): every
             batch would gate its stack and then be refused (ADR-0029).
      warn   the probe itself was inconclusive: continue, but say so.
      ok     nothing incompatible. In `required` mode required checks are exactly
             what the fleet waits for, so they are never a problem.
    """
    if ci_mode != "local":
        return {"verdict": "ok", "required_checks": [],
                "detail": f"gate.ci is {ci_mode!r} — required checks are the gate, not an obstacle"}
    if unavailable:
        return {"verdict": "warn", "required_checks": [],
                "detail": f"could not read branch protection ({unavailable}) — if the target "
                          f"requires status checks, merges will be rejected"}
    rsc = (protection or {}).get("required_status_checks") or {}
    checks = list(rsc.get("contexts") or [])
    for c in rsc.get("checks") or []:
        name = c.get("context") if isinstance(c, dict) else c
        if name and name not in checks:
            checks.append(name)
    if checks:
        return {"verdict": "error", "required_checks": sorted(checks),
                "detail": "gate.ci is 'local' but the target branch requires status checks "
                          f"({', '.join(sorted(checks))}) — every merge would be rejected. Either "
                          f"drop the required checks on that branch or use gate.ci: required."}
    refusals = [why for key, why in (("required_pull_request_reviews", "requires a pull request"),
                                     ("restrictions", "restricts who may push"))
                if (protection or {}).get(key)]
    if ((protection or {}).get("lock_branch") or {}).get("enabled"):
        refusals.append("is locked")
    if batch and refusals:
        return {"verdict": "error", "required_checks": [],
                "detail": f"the target branch {' and '.join(refusals)} — with gate.ci 'local', "
                          f"PRs that are ready together land as a merge batch, by pushing its "
                          f"stack to the target as a fast-forward, and that push would be "
                          f"refused. Either lift that protection or use gate.ci: required."}
    return {"verdict": "ok", "required_checks": [],
            "detail": "target branch requires no status checks — a local gate can merge"}


def checks_gate(checks_state: ChecksState | None,
                allow_no_checks: bool = False) -> Literal["green", "awaiting_ci", "gate_red",
                                                          "no_checks"]:
    """
    The `gate.ci: required` machine gate, for `afk turn` and `afk land` alike:
    may this PR land on what its GitHub checks say, right now?

      checks_state:    `pr_checks_state` of the PR AT THE HEAD THAT WOULD LAND —
                       a landing whose sync pushed a new head reads them again
                       on that head (`checks_owed`), never the ones it had before
      allow_no_checks: the tick's judgment that a repo with no CI at all may land
                       on its acceptance criteria (the progressive gate)

    Returns "green" | "awaiting_ci" | "gate_red" | "no_checks".
    """
    if checks_state is None:
        return "green" if allow_no_checks else "no_checks"
    if checks_state == "green":
        return "green"
    return "gate_red" if checks_state == "red" else "awaiting_ci"


def checks_owed(checks_state: ChecksState | None, at_head: bool, had_checks: bool) -> bool:
    """
    Is a landing still waiting for its PR's checks to speak? `afk land` asks after
    every read of the PR while it waits (ADR-0027); the first False ends the wait
    and `checks_gate` decides.

      checks_state: `pr_checks_state` of the PR as just read
      at_head:      that read was of the head that would land — GitHub has caught
                    up with the landing's push
      had_checks:   the PR had checks before a push of this landing moved its head

    Owed while GitHub still shows the head before the push, while a check is
    running, and while a head just pushed shows no checks although the PR had
    them: they have not been registered yet, which is not a repo with no CI.
    """
    if not at_head or checks_state == "pending":
        return True
    return checks_state is None and had_checks


def gate_comment(verdict: Obj, command: str) -> str:
    """The PR comment a red landing gate leaves behind, so whoever words the
    failure — the worker fixing it, or the tick failing a worker that gave up —
    re-reads it from where it lives (ADR-0012)."""
    how = (f"timed out (exit {verdict['exit_code']})" if verdict["timed_out"]
           else f"exit {verdict['exit_code']}")
    omitted = (f"\n\n_({verdict['omitted_lines']} earlier line(s) omitted)_"
               if verdict["omitted_lines"] else "")
    return (f"**afk-fleet landing gate: red** — `{command}` → {how}, run after syncing "
            f"with the merge target.\n\n```\n{verdict['excerpt']}\n```{omitted}")


# --------------------------------------------------------------------------- #
# no_pr reconciliation — disambiguating a FINISHED worker from a CODING one    #
# --------------------------------------------------------------------------- #
#
# `claim_status` only says a claim has no PR yet — or that its PR holds the
# landing turn and has not landed. Either way a worker that finished and went
# idle looks identical, to a terminal probe, to one still working, so three
# signals disambiguate (all gathered by `afk no-pr`):
#   1. git PROGRESS in the worktree (commits ahead / dirty tree / last activity);
#   2. the worker's VERDICT marker on the issue — its declared reason for opening
#      no PR: `already-satisfied` (done in base, empty diff), `blocked` (a
#      dependency gap, see blocked_by), `giving-up` (a failure it could not fix) or
#      `needs-decision` (the issue as written asks its owner for a decision);
#      for `blocked`, also the STANDING of each issue it names (`blocker_standings`);
#   3. the terminal's busy / idle / none state from the orca probe.
# The join is two pure decisions, asked in this order: `settled_by_worker_state`
# — does signal 3 alone settle it (busy, gone)? — and, only when it does not,
# `classify_stopped` over all three. Each answers with a CAUSE, one of
# `WORKER_CAUSES`: the one thing decided here, and the one thing the tick routes
# on (`worker_step`, `batch_step`). Of a WORKER, two words are kept apart: its
# VERDICT is what it declared in its marker (an input); its OUTCOME is what
# this code concludes from all three signals. (A gate's verdict, or branch
# protection's, is another thing: what one run or one read came to.) Whether to TRUST the marker
# stays the tick's call.

# The phases a worker may declare in its marker (worker-prompt.md asks for exactly
# these; anything else `classify_stopped` treats as a failure).
VerdictPhase = Literal["already-satisfied", "blocked", "giving-up", "needs-decision"]
VERDICT_PHASES: tuple[VerdictPhase, ...] = get_args(VerdictPhase)
_SATISFIED, _BLOCKED, _GIVING_UP, _NEEDS_DECISION = VERDICT_PHASES

# What a tick does about a worker, by the cause it was classified with: the two
# columns of WORKER_CAUSES `tick_plan` and `_turn_plan` route on.
WorkerStep = Literal["leave", "dispatch", "park", "nudge", "restart", "escalate", "fail", "judge"]
BatchStep = Literal["leave", "continue", "nudge", "abandon"]


class Cause(NamedTuple):
    """One reason a claim waiting on its worker is where it is — the row of
    WORKER_CAUSES it is declared in is everything that follows from it."""
    outcome: str            # } how `afk no-pr` prints it for a human; the tick
    action: str             # } routes on neither
    step: WorkerStep        # what a tick does about an ISSUE's worker (`worker_step`)
    batch_step: BatchStep | None = None     # …about a MERGE BATCH's worker (`batch_step`);
    #                         None: a batch's worker is never classified so


# Every cause a classification can name. The cause is the one thing
# `settled_by_worker_state` and `classify_stopped` decide; what the tick then
# does is this table's, read by `worker_step` and `batch_step` and re-derived
# by neither.
#
#   step        leave | dispatch | park | nudge | restart   run as it stands
#               escalate | fail    with the reason `worker_step` words — or, when
#                                  the reason is not on record, the `reason`
#                                  judgment that asks for it
#               judge              the tick's own judgment (`empty_diff`)
#   batch_step  leave | continue (a new batch worker, in its worktree or from
#               its pushed branch) | nudge | abandon (nothing landed; its PRs
#               take single turns)
#
# The silence ladder is the `silent*` rows: a worker that stopped with no
# verdict is nudged once (`silent`, no attempt spent — ADR-0018); silent again
# a grace period later, a PR-less claim fails (`silent_after_nudge`), while ONE
# PR's landing turn is restarted onto once (`silent_on_turn`) and after that
# escalated with its PR, branch and worktree kept (`silent_past_restart`) — a
# landing turn's silence never spends an attempt (ADR-0035). A batch has
# nothing to restart onto: its second silence abandons it.
#
# What a worker DECLARED is routed by who can supply what it lacks: the backlog
# (`blocked` → park), the next worker (`giving-up` → fail, a fresh retry), or
# the issue's owner (`needs-decision` → escalate at once: a retry would read the
# same issue and stop at the same question — ADR-0041).
WorkerCause = Literal[
    "working", "just_stopped", "gone", "within_grace", "awaiting_tick", "satisfied",
    "satisfied_refuted", "blockers_closed", "blockers_waiting", "blocker_unmet",
    "no_blocker_named", "silent", "silent_on_turn", "silent_past_restart", "gave_up",
    "needs_decision", "unknown_phase", "silent_after_nudge", "silent_unnudgeable"]
WORKER_CAUSES: dict[WorkerCause, Cause] = {
    # settled by the worker state alone (`settled_by_worker_state`)
    "working":             Cause("coding", "leave", "leave", "leave"),        # its runtime says so
    "just_stopped":        Cause("coding", "leave", "leave", "leave"),        # stopped, or nudged, within grace
    "gone":                Cause("dead", "orphan", "dispatch", "continue"),   # no live terminal: continued, never released
    # a stopped worker (`classify_stopped`)
    "within_grace":        Cause("coding", "leave", "leave", "leave"),        # a sign of life within grace
    "awaiting_tick":       Cause("coding", "leave", "leave", "leave"),        # its landing stopped for the tick
    "satisfied":           Cause("idle_done", "close_release", "judge"),      # `already-satisfied`, nothing on the branch
    "satisfied_refuted":   Cause("idle_failed", "next_attempt", "fail"),      # …refuted by work on the branch
    "blockers_closed":     Cause("idle_blocked", "redispatch", "dispatch"),   # `blocked`, every blocker now closed
    "blockers_waiting":    Cause("idle_blocked", "park", "park"),             # …the open ones will resolve (ADR-0022)
    "blocker_unmet":       Cause("idle_blocked", "escalate", "escalate"),     # …one never will
    "no_blocker_named":    Cause("idle_blocked", "escalate", "escalate"),     # `blocked`, naming none
    "silent":              Cause("idle_stalled", "nudge", "nudge", "nudge"),  # no verdict, never nudged
    "silent_on_turn":      Cause("idle_stalled", "restart", "restart"),       # silent after its nudge, on a landing turn
    "silent_past_restart": Cause("idle_stalled", "escalate", "escalate"),     # …and again, after the turn's one restart
    "gave_up":             Cause("idle_failed", "next_attempt", "fail"),      # `giving-up`
    "needs_decision":      Cause("idle_undecided", "escalate", "escalate"),   # `needs-decision`: no attempt spent
    "unknown_phase":       Cause("idle_failed", "next_attempt", "fail"),      # a verdict naming no phase the fleet knows
    "silent_after_nudge":  Cause("idle_failed", "next_attempt", "fail", "abandon"),
    "silent_unnudgeable":  Cause("idle_failed", "next_attempt", "fail", "abandon"),  # no worktree here to nudge it in
}

# Every (outcome, action) a `afk no-pr` row can carry — the vocabulary the
# reference lists for a human reading one (a test holds the docs to it).
NO_PR_ROUTES = tuple(dict.fromkeys((c.outcome, c.action) for c in WORKER_CAUSES.values()))


# A worker's verdict, kept as a marker leading a comment on its issue:
#
#   <!--afk:verdict n=<issue> phase=<one of VERDICT_PHASES> \
#       [blocked_by=<csv of issue numbers>] [reason=<short>]-->
#
# The worker writes it by hand, so nothing is required of it: a marker with no
# phase, or one the fleet does not know, is still a verdict — what it means is
# `classify_stopped`'s call. `reason` is the kind's tail: a short human phrase,
# spaces and all, to the end of the marker.
VERDICT_RECORD = RecordKind("afk:verdict",
                            {"n": int, "phase": str, "blocked_by": INTS, "reason": str},
                            (), tail="reason")

_NO_VERDICT = blank_record(VERDICT_RECORD)


def verdict_marker(n: int, phase: VerdictPhase, blocked_by: Iterable[int] = (),
                   reason: str | None = None) -> str:
    """The marker a worker posts for one verdict — what `latest_verdict` reads
    back (a test round-trips every phase)."""
    return record_marker(VERDICT_RECORD, {"n": n, "phase": phase, "blocked_by": list(blocked_by),
                                          "reason": reason})


def verdict_marker_format(n: int) -> str:
    """The marker as the worker prompt shows it to issue <n>'s worker: its own
    number filled in, the rest as placeholders, optional fields in brackets."""
    return marker_format(VERDICT_RECORD,
                         {"n": n, "phase": f"<{'|'.join(VERDICT_PHASES)}>",
                          "blocked_by": "<csv of issue numbers>", "reason": "<short>"},
                         optional=("blocked_by", "reason"))


def latest_verdict(comments: Iterable[Comment] | None) -> Verdict:
    """
    The worker's verdict on an issue, from its comments ([{"body", "url"}...],
    oldest first, the gh default) — `latest_record`'s. Returns:
      {"found": bool, "phase": str|None, "blocked_by": [int], "reason": str|None,
       "comment_url": str|None}
    """
    record, comment = latest_record(VERDICT_RECORD, comments)
    said = {**_NO_VERDICT, **(record or {})}
    return {"found": record is not None, "phase": said["phase"], "blocked_by": said["blocked_by"],
            "reason": said["reason"], "comment_url": comment["url"] if comment else None}


# Where one issue a `blocked` verdict names stands (ADR-0022):
#   closed   done — it no longer blocks anything;
#   waiting  open, and the backlog will resolve it with no human: a fleet holds
#            it, a PR is open for it, or it carries `ready_label`;
#   unmet    nothing will resolve it — `reason` says why.
BlockerStanding = Literal["closed", "waiting", "unmet"]
BLOCKER_STANDINGS: tuple[BlockerStanding, ...] = get_args(BlockerStanding)
_CLOSED, _WAITING, _UNMET = BLOCKER_STANDINGS

# The `state_reason`s of a closed issue whose work was NOT done.
_CLOSED_UNDONE = {"not_planned": "was closed as not planned",
                  "duplicate": "was closed as a duplicate"}


def _blocker_standing(blocker: IssueRead | None, claimed: bool, has_open_pr: bool,
                      config: Config) -> tuple[BlockerStanding, str | None]:
    """One named blocker's `(standing, reason)`, before the cycle check."""
    if blocker is None:
        return _UNMET, "could not be read (it may not exist)"
    if blocker["pull_request"]:
        return _UNMET, "is a pull request, not an issue"
    if blocker["state"] == "closed":
        undone = _CLOSED_UNDONE.get(blocker["state_reason"] or "")
        return (_UNMET, undone) if undone else (_CLOSED, None)
    bars = label_bars(blocker["labels"], config["ready_label"], config["epic_labels"])
    if set(bars) - {"not_ready"}:          # a bar no claim overrides: nothing will dispatch it
        return _UNMET, f"will never be dispatched: {'; '.join(bars.values())}"
    if bars and not (claimed or has_open_pr):
        return _UNMET, f"is open but no fleet will work it (unclaimed, {bars['not_ready']})"
    return _WAITING, None


def depends_on(start: int, target: int, edges: Mapping[int, list[int]]) -> bool:
    """Does issue `start` depend — directly or through any chain of open
    blockers — on issue `target`? `edges` is {issue number: [its open blockers]}."""
    seen, todo = set(), [start]
    while todo:
        n = todo.pop()
        if n == target:
            return True
        if n not in seen:
            seen.add(n)
            todo.extend(edges.get(n) or [])
    return False


def blocker_standings(number: int, named: Iterable[int], config: Config, *,
                      blockers: Mapping[int, IssueRead | None] | None, claimed: Collection[int],
                      open_pr: Collection[int],
                      edges: Mapping[int, list[int]] | None) -> list[Standing]:
    """
    Where each issue a `blocked` verdict names stands — will the dependency
    issue <number>'s worker discovered resolve on its own, or must a human look?

      named:    the verdict's blocked_by, in order
      config:   the canonical config — read for ready_label and epic_labels
      blockers: {n: {"state", "state_reason", "labels": [name...], "pull_request": bool}};
                a missing or None entry is an issue that could not be read
      claimed:  the issue numbers a claim ref exists for — ANY owner: mine and a
                live peer's are being worked, a stale one is reclaimed and continued
      open_pr:  the issue numbers an open PR closes
      edges:    {n: [its open blockers]}, covering everything reachable from a
                `waiting` blocker (`afk.py` walks it) — the cycle check

    Returns [{"number", "standing": one of BLOCKER_STANDINGS, "reason": str|None}...].
    A `waiting` blocker that itself depends on <number> is `unmet`: recording
    <number> as blocked by it would close a cycle neither side ever leaves.
    """
    rows: list[Standing] = []
    for n in named:
        standing, reason = _blocker_standing((blockers or {}).get(n), n in claimed, n in open_pr,
                                             config)
        if n == number:
            standing, reason = _UNMET, "is the issue itself"
        elif standing == _WAITING and depends_on(n, number, edges or {}):
            standing, reason = _UNMET, (f"already depends on #{number}: waiting on it would "
                                        f"close a cycle")
        rows.append({"number": n, "standing": standing, "reason": reason})
    return rows


def blocked_route(named: list[int], standings: Mapping[int, str] | None) -> Obj:
    """
    What a `blocked` verdict comes to, from the standing of each blocker it names.

      named:     the verdict's blocked_by
      standings: {issue number: one of BLOCKER_STANDINGS}; anything else — a
                 missing entry, an unknown word — is `unmet`

    Returns {"action", "pending_blockers"}:
      redispatch  every named blocker is closed: the dependency cleared.
      park        every named blocker is closed or `waiting`: record the
                  dependency and wait for it (`afk park`).
      escalate    one is `unmet`, or none was named — nothing will ever clear it.
    `pending_blockers` is the named blockers not `closed` — still open, or
    closed without the work being done.
    """
    standings = standings or {}
    pending = [n for n in named if standings.get(n) != _CLOSED]
    if not named or any(standings.get(n) != _WAITING for n in pending):
        return {"action": "escalate", "pending_blockers": pending}
    return {"action": "park" if pending else "redispatch", "pending_blockers": pending}


def park_refusal(verdict: Verdict, standings: list[Standing]) -> str | None:
    """
    Why a claim cannot be parked, or None when it can — `afk park`'s own
    check of what `afk no-pr` told the tick, worded so the tick knows what to do
    instead.

      verdict:   the `latest_verdict` dict
      standings: `blocker_standings`' rows for its blocked_by
    """
    if not verdict["found"] or verdict["phase"] != _BLOCKED:
        return "its worker's latest verdict is not `blocked`"
    named = verdict["blocked_by"]
    action = blocked_route(named, {b["number"]: b["standing"] for b in standings})["action"]
    if action == "park":
        return None
    if action == "redispatch":
        return "every blocker it named is closed — dispatch it again instead"
    if not named:
        return "its `blocked` verdict names no blocker — escalate it instead"
    unmet = "; ".join(f"#{b['number']} {b['reason']}" for b in standings if b["standing"] == _UNMET)
    return f"nothing will resolve {unmet} — escalate it instead"


def read_worker_state(row: Obj | None, now: float, grace_seconds: float,
                      tui_idle: bool | None = None) -> WorkerReading:
    """
    A worker's state, read from its worktree's row of `orca worktree ps --json` —
    the reading `settled_by_worker_state` takes,
    made in code instead of by a tick looking at a screen (ADR-0021).

      row: {liveTerminalCount, lastOutputAt, agents: [{state, stateStartedAt,
           parentPaneKey}]} (orca's clocks are epoch MILLISECONDS), or None when
           orca lists no such worktree. `state` is what the agent's own hooks
           reported: working | waiting | blocked | done.
      now, grace_seconds: epoch seconds / WORKER_IDLE_GRACE_SECONDS.
      tui_idle: for a runtime that reports NO state (qoderclicn), whether orca
           sees its terminal idle — `orca terminal wait --for tui-idle` answered
           (True) or timed out (False). None when not asked, read as idle.

    Returns {"terminal", "terminal_idle_seconds", "state"}:
      none  no live terminal in the worktree — the worker is gone.
      busy  the agent reports `working` AND the terminal produced output within
            grace. Both, because a stop report the runtime lost would otherwise
            read `working` forever — a claim parked on a state that cannot change
            (ADR-0013).
            A runtime that reports no state is busy when orca does not see its
            terminal idle (`tui_idle` False).
      idle  anything else, timed from when the agent reported it stopped. A lost
            stop report is timed from the terminal's last output; a runtime that
            reports no state is not timed at all (None) — its idle screen redraws
            on a timer, so its output says nothing — and the worktree's own
            clocks decide.
    `state` is the report it was read from: the top-level agent that changed
    state last (an older pane's report, a subagent's, do not speak for it).
    """
    if not row or not row.get("liveTerminalCount"):
        return {"terminal": "none", "terminal_idle_seconds": None, "state": None}

    def ago(ms: float | None) -> int | None:
        return max(0, int(now) - int(ms) // 1000) if ms else None

    output_idle = ago(row.get("lastOutputAt"))
    agents = [x for x in row.get("agents") or [] if x.get("state") and not x.get("parentPaneKey")]
    lead = max(agents, key=lambda x: x.get("stateStartedAt") or 0, default=None)
    state = lead["state"] if lead else None

    def out(terminal: Literal["busy", "idle"], idle: int | None) -> WorkerReading:
        return {"terminal": terminal, "terminal_idle_seconds": idle, "state": state}

    if state == "working":
        live = output_idle is not None and output_idle < grace_seconds
        return out("busy" if live else "idle", output_idle)
    if lead:
        stopped = ago(lead.get("stateStartedAt"))
        return out("idle", output_idle if stopped is None else stopped)
    return out("busy" if tui_idle is False else "idle", None)


def _seen(cause: WorkerCause, idle_seconds: int | None,
          pending_blockers: Iterable[int] = ()) -> Seen:
    """One worker's classification: its cause, and the row words it comes to."""
    row = WORKER_CAUSES[cause]
    return {"cause": cause, "outcome": row.outcome, "action": row.action,
            "idle_seconds": idle_seconds,
            "pending_blockers": list(pending_blockers)}


def _idle_seconds(now: float, terminal_idle_seconds: float | None,
                  *signs: float | None) -> int | None:
    """Seconds since the MOST RECENT sign of life: the terminal's own clock, and
    each epoch-second `signs` that is known. None when none is known — which is
    never "within grace"."""
    seen = [int(t) for t in signs if t is not None]
    if terminal_idle_seconds is not None:
        seen.append(int(now) - int(terminal_idle_seconds))
    return max(0, int(now) - max(seen)) if seen else None


def settled_by_worker_state(reading: WorkerReading, now: float, grace_seconds: float,
                            nudged_at: float | None = None) -> Seen | None:
    """
    Does a worker's state alone settle what the tick does about it? Asked before
    anything else is gathered — a worker it settles costs no git and no GitHub read
    (ADR-0021).

      reading:   `read_worker_state`'s.
      nudged_at: epoch seconds this worker was nudged (`afk nudge`), None if never.

    Returns the classification (`classify_stopped`'s shape) with the cause
    `gone` (no live terminal), `working` (its runtime says so) or `just_stopped`
    (it stopped, or was nudged, within grace: more signs of life could only say
    the same) — or None for a worker that stopped and whose reasons must be
    gathered.
    """
    idle_seconds = _idle_seconds(now, reading["terminal_idle_seconds"], nudged_at)
    if reading["terminal"] == "none":
        return _seen("gone", idle_seconds)
    if reading["terminal"] == "busy":
        return _seen("working", idle_seconds)
    if idle_seconds is not None and idle_seconds < grace_seconds:
        return _seen("just_stopped", idle_seconds)
    return None


def single_turn_held(turn: Turn | None) -> bool:
    """Is `turn` (`latest_turn`, or {} / None) ONE PR's landing turn, held — `at`
    set, not `released`, no merge batch's? A no_pr claim has no turn, so never."""
    return bool(turn and turn["at"] is not None and not turn["released"] and not turn["batch"])


def restartable_turn(turn: Turn | None) -> bool:
    """Is `turn` one PR's held landing turn whose silent worker may still be
    restarted onto it — one that has not had its one restart (`restarted`)?
    ADR-0035. Past the restart the same silence escalates the claim with
    everything kept (`silent_past_restart`)."""
    return bool(turn and single_turn_held(turn) and not turn["restarted"])


def classify_stopped(progress: Progress | None, terminal_idle_seconds: float | None,
                     worker_verdict: Verdict | None, blocker_states: Mapping[int, str] | None,
                     now: float, grace_seconds: float, nudged_at: float | None = None,
                     can_nudge: bool = True, turn: Turn | None = None) -> Seen:
    """
    The classification of one of MY claims that is waiting on a worker which has
    STOPPED — a `no_pr` claim, or a `landing` one, that `settled_by_worker_state`
    did not settle — from everything gathered about it.

      progress:        the worktree's git progress {"commits_ahead", "dirty",
                       "last_commit_ts", "worktree_mtime_ts"}; {} / None if unreadable.
      terminal_idle_seconds: seconds since the terminal last showed activity
                       (the reading's); None if the probe could not say.
      worker_verdict:  the `latest_verdict` dict (or None) — what the worker declared.
      blocker_states:  {issue number: one of BLOCKER_STANDINGS} for the verdict's
                       blocked_by (`blocker_standings`). Anything else is `unmet`.
      now, grace_seconds: epoch seconds / WORKER_IDLE_GRACE_SECONDS.
      nudged_at:       epoch seconds this worker was nudged (`afk nudge`), None if
                       it never was. A nudge is spent once.
      can_nudge:       False when there is nowhere to record a nudge (no worktree
                       on this machine): a silence then takes its next rung at once.
      turn:            the landing turn its PR carries (`latest_turn`), None when
                       it has none. Three of its fields are read: `at`, a sign of
                       life like the nudge; `stopped` — one of LAND_WAITS means
                       the worker is idle because the next move is the tick's,
                       which is not a silence; and whether it is ONE PR's held
                       turn, restarted yet or not (`single_turn_held`,
                       `restartable_turn`), which picks the rung of the silence
                       ladder. A merge batch's turn (`batch`) has no rungs of its
                       own: its worker is classified like a PR-less claim's.

    Returns {"cause", "outcome", "action", "idle_seconds", "pending_blockers"}.
    `cause` is the decision — one of the `classify_stopped` rows of WORKER_CAUSES,
    which says what each means and what the tick does about it; `outcome` /
    `action` are that row's words for a human.

    `idle_seconds` is the time since the MOST RECENT sign of life (last commit,
    newest file mtime, terminal activity, the nudge, the landing turn); None
    when none is known, which is never "within grace". Commits ahead / a dirty
    tree are standing facts — true until the branch merges — never signs of life
    (ADR-0013). `pending_blockers` is `blocked_route`'s: the blocked_by not yet
    done, [] for any other verdict.
    """
    told, stopped = (turn["at"], turn["stopped"]) if turn else (None, None)
    committed, touched = ((progress["last_commit_ts"], progress["worktree_mtime_ts"])
                          if progress else (None, None))
    idle_seconds = _idle_seconds(now, terminal_idle_seconds, committed, touched, nudged_at, told)
    if idle_seconds is not None and idle_seconds < grace_seconds:
        return _seen("within_grace", idle_seconds)

    # idle past grace: the cause is what it declared…
    verdict = worker_verdict
    if not verdict or not verdict["found"]:
        # …or, having declared nothing, why it is quiet
        if stopped in LAND_WAITS:
            return _seen("awaiting_tick", idle_seconds)
        if single_turn_held(turn) and (nudged_at is not None or not can_nudge):
            # one PR's landing turn climbs its own ladder: restart, then escalate
            return _seen("silent_on_turn" if restartable_turn(turn) else "silent_past_restart",
                         idle_seconds)
        if nudged_at is not None:
            return _seen("silent_after_nudge", idle_seconds)
        return _seen("silent" if can_nudge else "silent_unnudgeable", idle_seconds)
    phase = verdict["phase"]
    if phase == _SATISFIED:
        # "nothing needed doing" is refuted by work sitting on the branch.
        has_changes = bool(progress and ((progress["commits_ahead"] or 0) > 0
                                         or progress["dirty"]))
        return _seen("satisfied_refuted" if has_changes else "satisfied", idle_seconds)
    if phase == _BLOCKED:
        named = verdict["blocked_by"]
        route = blocked_route(named, blocker_states)
        causes: dict[str, WorkerCause] = {
            "redispatch": "blockers_closed", "park": "blockers_waiting",
            "escalate": "blocker_unmet" if named else "no_blocker_named"}
        return _seen(causes[route["action"]], idle_seconds, route["pending_blockers"])
    if phase == _NEEDS_DECISION:
        return _seen("needs_decision", idle_seconds)
    return _seen("gave_up" if phase == _GIVING_UP else "unknown_phase", idle_seconds)


# How much of a stalled worker's screen is carried into a failure reason.
STALL_TAIL_LINES = 30
_STALL_LINE_CHARS = 200


def nudge_text(brief: str | None = None) -> str:
    """The one line `afk nudge` types at a worker that stopped without an outcome.
    Short on purpose: a long text arrives as a paste the worker asks to have
    confirmed, which is the stall this is sent to break."""
    task = f"your task brief ({brief})" if brief else "your task"
    return (f"You stopped without an outcome. Nobody is watching this terminal, so do not wait "
            f"for a confirmation or an answer: continue {task} to the end, and finish with the "
            f"outcome it asks for — a PR, a landing, or an afk:verdict marker comment.")


def stall_tail(lines: Iterable[str] | None, limit: int = STALL_TAIL_LINES) -> list[str]:
    """The last `limit` non-blank lines of a terminal screen, each cut to a
    bounded width — what a stalled worker was last saying, small enough to carry."""
    kept = [ln.rstrip()[:_STALL_LINE_CHARS] for ln in (lines or []) if str(ln).strip()]
    return kept[-limit:]


def stall_reason(reason: str, tail: Iterable[str] | None) -> str:
    """A failure or escalation reason with the stalled worker's last screen
    appended, so the retry (or the human it escalates to) reads WHERE it stopped,
    not just that it did."""
    kept = stall_tail(tail)
    if not kept:
        return reason
    return (f"{reason.rstrip()}\n\nThe worker stopped without an outcome and stayed silent "
            f"after one nudge. Its terminal ended with:\n\n```\n" + "\n".join(kept) + "\n```")


# --------------------------------------------------------------------------- #
# The landing turn — a worker lands its own PR, one PR at a time (ADR-0027)    #
# --------------------------------------------------------------------------- #
#
# A finished PR is landed by the worker that wrote it (`afk land`), on a landing
# turn its fleet instance grants (`afk turn`) — one turn at a time. The turn is
# recorded where it concerns, as ONE marker comment on the PR:
#
#   <!--afk:turn instance=<id> at=<epoch> [verified=<sha>] [allow_no_checks=1]
#       [stopped=<outcome> head=<sha>]-->
#
# `instance` is the fleet instance that granted it: `afk land` refuses a marker
# that does not name the instance holding the claim, so a turn does not survive
# a takeover. `verified` and `allow_no_checks` are the tick's two judgments, made
# BEFORE the turn is granted and carried to the landing. `stopped` is where
# `afk land` last stopped short of merging, on which `head`. The record lives
# and dies with the PR: merged or closed, the turn is free.
#
# A turn is held by one PR or by one MERGE BATCH (ADR-0029). A batch's turn is
# the same marker on every member PR, with three more fields:
#
#   batch=<id> members=<issue>:<pr>,… phase=<stacking|gating|fixing>
#
# and a PR that left a batch without landing — left out of the stack, or in a
# batch that was abandoned or dissolved — carries `unbatched=<why> of=<batch>`
# from then on, on every marker written for it: it lands on a single turn and
# is never batched again. The marker that only says so holds no turn:
# `released=1`.
#
# A marker is never written from loose fields: it is REWRITTEN from the record
# it was read as (`latest_turn`) plus what changed (`next_turn`), and rendered
# by `turn_comment` — so a field no rewrite names survives it. The fields, their
# order and their types are `TURN_RECORD`'s, declared under the vocabularies it
# is made of.

# Every `outcome` `afk land` can stop with — the vocabulary the worker's prompt
# routes on (a test holds the prompt and the docs to it). What each means is
# `afk.cmd_land`'s docstring, and nowhere else in the code.
LandOutcome = Literal["merged", "conflict", "gate_red", "target_moved", "awaiting_ci",
                      "needs_verify", "no_checks"]
LAND_OUTCOMES: tuple[LandOutcome, ...] = get_args(LandOutcome)

# The landing outcomes where the next move is the TICK's, not the worker's: the
# worker wakes the launcher and stops, and is told to land again (`afk turn`).
LandWait = Literal["awaiting_ci", "needs_verify", "no_checks"]
LAND_WAITS: tuple[LandWait, ...] = get_args(LandWait)

# Every `outcome` `afk turn` can stop with — the vocabulary the tick's
# instructions route on (a test holds the docs to it). What each means is
# `afk.cmd_turn`'s docstring; what the tick does next with it is `turn_step`.
TurnOutcome = Literal["granted", "waiting", "landing", "awaiting_ci", "gate_red", "no_checks",
                      "needs_verify"]
TURN_OUTCOMES: tuple[TurnOutcome, ...] = get_args(TurnOutcome)

# Every `outcome` `afk turn --batch` and `afk turn --abandon` can stop with: the
# three a single turn shares, `too_few` (no batch to form: the turn goes to one
# PR) and `abandoned`. The pass routes them in code (`_turn_plan`).
BatchTurnOutcome = Literal["granted", "waiting", "landing", "too_few", "abandoned"]
BATCH_TURN_OUTCOMES: tuple[BatchTurnOutcome, ...] = get_args(BatchTurnOutcome)

# What a merge batch's worker is doing with the stack, as its last
# `afk land --batch` wrote it on the members' turn markers.
BatchPhase = Literal["stacking", "gating", "fixing"]
BATCH_PHASES: tuple[BatchPhase, ...] = get_args(BatchPhase)

# Every `outcome` `afk land --batch` can stop with — the vocabulary the batch
# brief routes on (a test holds the brief and the docs to it).
BatchOutcome = Literal["landed", "gate_red", "target_moved", "too_small"]
BATCH_OUTCOMES: tuple[BatchOutcome, ...] = get_args(BatchOutcome)

# Why a PR left a merge batch without landing: it conflicted with the stack, the
# batch's worker went silent (or its fleet died), or too few members remained.
Unbatched = Literal["left_out", "abandoned", "dissolved"]
UNBATCHED: tuple[Unbatched, ...] = get_args(Unbatched)


def _batch_members(raw: str | None) -> list[dict[str, int]]:
    """`1:10,2:20` → [{"issue": 1, "pr": 10}, {"issue": 2, "pr": 20}]; anything
    else in the list is dropped."""
    pairs = [tok.split(":") for tok in (raw or "").split(",")]
    return [{"issue": int(p[0]), "pr": int(p[1])} for p in pairs
            if len(p) == 2 and p[0].isdigit() and p[1].isdigit()]


# The PRs a merge batch holds, in stack order, each with the issue it closes.
_MEMBERS = FieldType(lambda members: ",".join(f"{m['issue']}:{m['pr']}" for m in members),
                     lambda raw: _batch_members(raw) or None, empty=())

# The landing turn, as the marker of one comment on a PR. A marker that names no
# instance is not a record — nobody could hold it.
TURN_RECORD = RecordKind("afk:turn", {
    "instance": str, "at": int, "verified": str, "allow_no_checks": FLAG,
    "stopped": one_of(LAND_OUTCOMES), "head": str, "restarted": int,
    "batch": str, "members": _MEMBERS, "phase": one_of(BATCH_PHASES),
    "unbatched": one_of(UNBATCHED), "of": str, "released": FLAG}, ("instance",))


_Word = TypeVar("_Word", bound=str)


def _refusing(vocabulary: tuple[_Word, ...], what: str) -> Callable[[str], _Word]:
    """`outcome → outcome`, refused (ValueError) unless it is a word of
    `vocabulary`: a subcommand cannot stop with a word its caller was never told
    how to act on."""
    def check(outcome: str) -> _Word:
        if outcome not in vocabulary:
            raise ValueError(f"not {what}: {outcome!r}")
        return vocabulary[vocabulary.index(outcome)]
    return check


# One guard per vocabulary: one PR's turn cannot stop with a word only a merge
# batch's is routed on, nor the other way round (ADR-0036).
land_outcome = _refusing(LAND_OUTCOMES, "a landing outcome")
turn_outcome = _refusing(TURN_OUTCOMES, "a turn outcome")
batch_turn_outcome = _refusing(BATCH_TURN_OUTCOMES, "a batch turn outcome")
batch_outcome = _refusing(BATCH_OUTCOMES, "a batch outcome")


# The record of a PR that was never granted a turn: every field of TURN_RECORD
# saying nothing, plus the comment a record was read from — which is where its
# rewrite goes.
_NO_TURN = {**blank_record(TURN_RECORD), "comment_id": None}


def _whole_turn(fields: Obj) -> Obj:
    """A turn's fields with the ones that say nothing alone taken out: `head` is
    where a landing `stopped`, `members` and `phase` are a `batch`'s."""
    return {**fields,
            **({} if fields.get("stopped") else {"head": None}),
            **({} if fields.get("batch") else {"members": [], "phase": None})}


def next_turn(prev: Turn | None, **changed: Any) -> Turn:
    """The record a turn marker is REWRITTEN from: `prev` — the record as it was
    read (`latest_turn`), None when the PR has none — plus what changed. A field
    nobody names survives, `comment_id` among them: the rewrite replaces the
    comment it was read from. Refuses a field `latest_turn` does not give back."""
    unknown = sorted(set(changed) - set(_NO_TURN))
    if unknown:
        raise ValueError(f"not a field of a turn record: {', '.join(unknown)}")
    turn: Obj = {**_NO_TURN, **(prev or {}), **changed}
    if turn["at"] is not None:
        turn["at"] = int(turn["at"])
    return cast("Turn", turn)       # _NO_TURN's keys, and `changed` names no other


def single_turn(prev: Turn | None, instance: str, at: float, verified: str | None = None,
                allow_no_checks: bool = False, restarted: int | None = None) -> Turn:
    """The record of ONE PR's landing turn, granted now (or granted again) over
    `prev`: the worker is told to land, so it has not stopped, and the turn is
    no batch's.

      instance:        the fleet instance granting the turn
      at:              when the worker was told (epoch seconds) — the sign of
                       life its silence is timed from
      verified:        the head an adversarial verify passed, if one did
      allow_no_checks: the tick judged a PR with no checks at all may land
      restarted:       when a silent worker was restarted onto this turn
                       (`afk turn --restart`, ADR-0035) — the one the turn had,
                       carried over by a re-delivery of the same turn; None on a
                       turn that never had one. Named every time, so a restart
                       recorded by another instance's turn does not outlive it.
    """
    return next_turn(prev, instance=instance, at=at, verified=verified,
                     allow_no_checks=bool(allow_no_checks), stopped=None, head=None,
                     restarted=restarted, batch=None, members=[], phase=None, released=False)


def batch_turn(prev: Turn | None, instance: str, at: float, batch: str,
               members: Iterable[BatchMember], phase: BatchPhase) -> Turn:
    """The record of a MERGE BATCH's landing turn on one member PR, over that
    PR's `prev` — the same on every member (ADR-0029).

      batch:   the batch's id (`batch_id`)
      members: [{"issue", "pr"}...], in stack order — every PR the batch holds
      phase:   one of BATCH_PHASES
    """
    return next_turn(prev, instance=instance, at=at, verified=None, allow_no_checks=False,
                     stopped=None, head=None, batch=batch, members=list(members), phase=phase,
                     released=False)


def unbatched_turn(prev: Turn | None, instance: str, at: float, batch: str,
                   why: Unbatched) -> Turn:
    """The record that replaces a batch's turn on a PR that left it without
    landing. It holds no turn (`released`): the PR is back to waiting, takes a
    single landing turn, and is never batched again.

      why: one of UNBATCHED
    """
    return next_turn(prev, instance=instance, at=at, batch=None, members=[], phase=None,
                     unbatched=why, of=batch, released=True)


def turn_comment(turn: Turn) -> str:
    """The PR comment that records a landing turn, from its record (`latest_turn`,
    or one of `next_turn` / `single_turn` / `batch_turn` / `unbatched_turn`): the
    marker `latest_turn` reads back — every field the record holds — then the
    same facts worded for a human reading the PR. The record says which of the
    three it is: a PR that left a batch and holds no turn (`released`), a merge
    batch's turn (`batch`), or one PR's turn.
    """
    instance, batch, stopped = turn["instance"], turn["batch"], turn["stopped"]
    why = turn["unbatched"]
    if batch and turn["phase"] not in BATCH_PHASES:
        raise ValueError(f"not a batch phase: {turn['phase']!r}")
    if turn["released"] and (why is None or why not in UNBATCHED):
        raise ValueError(f"not a reason a PR leaves a batch: {why!r}")
    if turn["released"] and why:
        said = {"left_out": "it conflicted with the PRs stacked before it, and was left out",
                "abandoned": "the batch was abandoned, and nothing landed",
                "dissolved": "fewer than two PRs were left in the batch, so it was dissolved"
                }[why]
        text = (f"**afk-fleet: this PR was in merge batch `{turn['of']}`** — {said}. It now "
                f"waits for a landing turn of its own, on which its worker lands it, and it is "
                f"not batched again.")
    elif batch:
        prs = ", ".join(f"#{m['pr']}" for m in turn["members"])
        text = (f"**afk-fleet: this PR is in a merge batch** (`{batch}`) of fleet instance "
                f"`{instance}`: {prs} are stacked on the target as one merge commit each, gated "
                f"once as a stack, and landed together by the batch's own worker "
                f"(`afk land --batch`). This PR's branch is not touched; GitHub shows the PR "
                f"merged once the stack is on the target. Phase: "
                f"`{turn['phase']}`.")
    else:
        state = (f"Its last `afk land` stopped with `{stopped}` on `{(turn['head'] or '')[:12]}`."
                 if stopped else "The worker has been told to land it.")
        again = (" Its worker went silent on the turn and a new one was started onto it, in the "
                 "same worktree; a second silence hands the PR to a human as it is, with the "
                 "branch and worktree kept." if turn["restarted"] else "")
        text = (f"**afk-fleet: this PR holds the landing turn** of fleet instance `{instance}`. "
                f"The worker that wrote the branch lands it with `afk land` — sync with the target, "
                f"gate, merge — and the next PR's turn comes when this one has landed, failed or been escalated. "
                f"{state}{again}")
    stated = cast(Obj, turn)
    fields = _whole_turn({name: stated[name] for name in TURN_RECORD.fields})
    return record_comment(TURN_RECORD, fields, text)


def latest_turn(comments: Iterable[Comment] | None) -> Turn | None:
    """
    The landing turn recorded on a PR, from its comments ([{"id", "body"}...],
    oldest first) → every field of TURN_RECORD — one the marker does not state
    at its type's `empty` — plus `comment_id`; or None when the PR was never
    granted one (`latest_record`'s). `batch` / `members` / `phase` are set on a
    merge batch's turn only, `stopped` / `head` once an `afk land` stopped;
    `released` says the marker holds no turn at all. `turn_comment` renders the
    record back: the two are a round trip.
    """
    record, comment = latest_record(TURN_RECORD, comments)
    if record is None or comment is None:
        return None
    return cast("Turn", {**_whole_turn({**_NO_TURN, **record}), "comment_id": comment["id"]})


def held_turn(turn: Turn | None, owner: str | None) -> Turn | None:
    """`turn` (`latest_turn`) when it is held by the claim's owner, else None.

      owner: the instance id the claim ref is stamped with; None or "" when
             there is no claim, or one that names nobody

    A turn granted by another instance — the fleet the claim was taken over
    from — is nobody's: the new owner grants its own. So is a marker that only
    says the PR left a batch (`released`).
    """
    if turn and owner and turn["instance"] == owner and not turn["released"]:
        return turn
    return None


def turn_gate(ci_mode: GateCiMode, checks_state: ChecksState | None, allow_no_checks: bool,
              adversarial_verify: bool, verified: str | None,
              head: str) -> Literal["ready", "awaiting_ci", "gate_red", "no_checks",
                                    "needs_verify"]:
    """
    May a landing turn be granted (or its worker told to land again) on what is
    known of the PR right now? The tick's judgments are settled HERE, before the
    worker is told — a worker never verifies itself.

      ci_mode:            gate.ci
      checks_state:       `pr_checks_state` of the PR's current head
      allow_no_checks:    the tick's judgment that a PR with no checks may land
      adversarial_verify: whether the repo asks for one (`verifies`)
      verified:           the head the tick says an adversarial verify passed
      head:               the PR's current head

    Returns "ready", or the TURN_OUTCOMES word it is refused with: `awaiting_ci`
    / `gate_red` / `no_checks` (`required` only — in `local` the machine gate is
    run by `afk land`, so there is nothing to wait for), then `needs_verify`.
    """
    if ci_mode != "local":
        checks = checks_gate(checks_state, allow_no_checks)
        if checks != "green":
            return checks
    if adversarial_verify and verified != head:
        return "needs_verify"
    return "ready"


def turn_order(rows: list[MineRow]) -> list[int]:
    """
    The order landing turns are granted in — the merge queue: the issue numbers
    of the `mine` rows whose PR is ready, a PR that already holds a turn first,
    then a PR that left a merge batch without landing, then the lower PR number.

      rows: `mine` rows {"number", "status", "pr", "unbatched"}; only `landing`
            and `awaiting_turn` ones are in the queue
    """
    ready = [r for r in rows if r["status"] in ("landing", "awaiting_turn")]
    return [r["number"] for r in sorted(ready, key=lambda r: (r["status"] != "landing",
                                                              not r["unbatched"], r["pr"]))]


# --------------------------------------------------------------------------- #
# The merge batch — N ready PRs behind one gate run (ADR-0029)                 #
# --------------------------------------------------------------------------- #
#
# When two or more finished PRs wait for the landing turn, the turn goes to a
# MERGE BATCH instead of to one PR: a batch worker, in a worktree of the
# batch's own, stacks them on the target tip — one merge commit per PR — runs
# the local gate once on the stack, and pushes the stack to the target as a
# fast-forward (`afk land --batch`). Still one turn out at a time; what the
# gate proves is the stack, in the form it lands.

_BATCH_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def batch_id(instance: str, now: float) -> str:
    """A new merge batch's id: the granting fleet instance and the second it was
    formed — unique, since an instance has one turn out at a time. It is a bare
    token: it names a branch, a worktree and a marker field."""
    return f"{_BATCH_ID_UNSAFE.sub('-', instance)}-{int(now)}"


def batch_formed_by(batch: str | None, instance: str | None) -> bool:
    """Is `batch` an id `batch_id` gives a batch of `instance`?"""
    return bool(instance) and bool(
        re.fullmatch(rf"{re.escape(_BATCH_ID_UNSAFE.sub('-', instance))}-\d+", batch or ""))


def batch_name(batch: str) -> str:
    """The name a batch's worktree is created under — and so, behind orca's
    `<user>/` prefix, its branch (`batch_branch_regex`)."""
    return f"afk-batch-{batch}"


def batch_branch_regex(batch: str | None = None,
                       instance: str | None = None) -> re.Pattern[str]:
    """The regex a batch's branch matches, as orca names it: `<user>/` in front,
    and `-<k>` behind when a continuation was cut under a name already taken.
    For one `batch`, or — group 1 the id — for every batch of one `instance`."""
    if batch:
        which = re.escape(batch)
    elif instance:
        which = rf"({re.escape(_BATCH_ID_UNSAFE.sub('-', instance))}-\d+)"
    else:
        raise ValueError("batch_branch_regex takes a batch or an instance")
    return re.compile(rf"^(?:[^/]+/)?afk-batch-{which}(?:-\d+)?$")


def batch_branches(heads: Iterable[str] | None, batch: str) -> list[str]:
    """The remote branches that are one batch's, sorted: the stack its worker
    pushed (ADR-0011) — what a continuation on another machine starts from."""
    rx = batch_branch_regex(batch)
    return sorted(h for h in (heads or []) if h and rx.match(h))


def batch_worktrees(worktrees: Iterable[Obj] | None, repo: str | None,
                    batch: str | None = None, instance: str | None = None) -> list[Obj]:
    """The orca worktrees on this machine that are a batch's — one `batch`'s, or
    every batch of `instance` — as [{"batch", "path", "branch"}...], the most
    recently active first. Same row shape and repo check as `find_orca_worktree`;
    a batch's worktree is linked to no issue, so it is known by its branch."""
    rx = batch_branch_regex(batch, instance)
    hits = []
    for w in worktrees or []:
        m = rx.match(short_branch(w.get("branch")))
        if not m or w.get("isMainWorktree") or w.get("isArchived"):
            continue
        if repo and not in_orca_project(w, repo):
            continue
        hits.append((int(w.get("lastActivityAt") or 0),
                     {"batch": batch or m.group(1), "path": w.get("path"),
                      "branch": short_branch(w.get("branch"))}))
    return [row for _, row in sorted(hits, key=lambda h: -h[0])]


def batches_form(config: Config) -> bool:
    """Whether this config's PRs may land as merge batches at all: the stack is
    gated by ONE run of `gate.local_command`, which only `gate.ci: local` has,
    and with an adversarial verify every PR owes one of its own head before
    its turn (ADR-0029)."""
    return config["gate"]["ci"] == "local" and not verifies(config)


def batch_candidates(mine: list[MineRow], merge_order: list[int], config: Config,
                     busy: Iterable[int] = ()) -> list[int]:
    """
    The claims a merge batch is formed from — their issue numbers, in merge
    order — or [] when the turn goes to ONE PR as before. The cycle's
    batch-or-single decision, whole (ADR-0029):

      mine, merge_order: the working set's
      config:  read for gate.ci and gate.adversarial_verify_prompt (`batches_form`)
      busy:    the issue numbers whose own worker is still working — its PR may
               yet move, so it is not stacked

    No batch unless batches form under this config; while any PR or batch
    holds the turn; while a PR that left a batch still waits (those go first, on
    single turns); or when fewer than two claims are eligible. A peer fleet's PR
    is never in `mine`.
    """
    if not batches_form(config):
        return []
    rows = {r["number"]: r for r in mine}
    waiting = [rows[n] for n in merge_order]
    if any(r["status"] != "awaiting_turn" or r["unbatched"] for r in waiting):
        return []
    picked = [r["number"] for r in waiting if r["number"] not in set(busy)]
    return picked if len(picked) >= 2 else []


def turn_holder(ws: WorkingSet,
                instance: str) -> tuple[Literal["dead", "mine", "single"] | None, Any]:
    """Who holds this fleet's landing turn, as far as a merge batch goes → (who,
    what), the first of these that is so — the one precedence `tick_plan` and
    `afk turn --batch` both read:

      "dead"    the merge batches of other (dead) fleet instances still on record
                on claims I took — nothing is granted until they are abandoned
      "mine"    my own batch
      "single"  the issue number of the one PR that holds the turn
      None      no turn is out (what is None too)"""
    dead = [b for b in ws["batches"] if b["instance"] != instance]
    if dead:
        return "dead", dead
    mine = next((b for b in ws["batches"] if b["instance"] == instance), None)
    if mine:
        return "mine", mine
    single = next((r["number"] for r in ws["mine"] if r["status"] == "landing"), None)
    return ("single", single) if single is not None else (None, None)


def stack_message(title: str | None, pr: int, issue: int) -> str:
    """The message of the merge commit a batched PR is stacked with: the PR's
    title, `(#<pr>)`, and the closing keyword. The `(#<pr>)` is also how the
    stack is read back (`read_stack`)."""
    return f"{(title or '').strip() or f'PR {pr}'} (#{pr})\n\nCloses #{issue}\n"


def stacked_pr(subject: str | None) -> int | None:
    """The PR a commit subject names the way `stack_message` writes it — its
    trailing ` (#<pr>)` — or None."""
    m = re.search(r" \(#(\d+)\)$", subject or "")
    return int(m.group(1)) if m else None


def read_stack(commits: Iterable[tuple[str, str]],
               prs: Collection[int]) -> tuple[dict[int, str], list[str]]:
    """
    A batch worktree's commits above the target, read back → (stacked, fixes):

      commits: [(sha, subject)...] oldest first — `git log --first-parent
               <target tip>..HEAD`: the stack's own line, without the commits
               each PR brought
      prs:     the member PR numbers

      stacked: {pr: sha} — the merge commit each member was stacked with
               (`stack_message`'s subject ends ` (#<pr>)`)
      fixes:   [sha...] — every other commit, oldest first: what the batch
               worker committed to turn a red stack green
    """
    stacked: dict[int, str] = {}
    fixes: list[str] = []
    for sha, subject in commits:
        pr = stacked_pr(subject)
        if pr is not None and pr in prs and pr not in stacked:
            stacked[pr] = sha
        else:
            fixes.append(sha)
    return stacked, fixes


def batch_landed_comment(commit: str, target: str, batch: str, prs: Iterable[int]) -> str:
    """The comment a batched PR is closed with when GitHub did not show it merged
    — its head moved after it was stacked, or GitHub never caught up: the PR
    itself says which commit landed it."""
    others = ", ".join(f"#{p}" for p in prs)
    return (f"**afk-fleet: landed on `{target}` as {commit}** — in merge batch `{batch}` "
            f"({others}), stacked as one merge commit per PR and gated once as a stack. "
            f"GitHub did not mark this PR merged, so it is closed here; what was stacked "
            f"is on `{target}`.")


def batch_step(worker: BatchWorkerRow) -> BatchStep:
    """What a tick does about the batch that holds the turn, from the cause its
    worker was classified with → that cause's `batch_step` in WORKER_CAUSES. A
    batch's worker holds no claim and declares no verdict, so a cause with none
    is an error."""
    cause = WORKER_CAUSES.get(worker["cause"])
    step = cause.batch_step if cause else None
    if step is None:
        raise ValueError(f"no step for a batch worker classified {worker['cause']!r}")
    return step


# --------------------------------------------------------------------------- #
# Takeover — the human-authorized, lease-skipping reclaim (ADR-0011)           #
# --------------------------------------------------------------------------- #
#
# The lease is the *unattended* line between "dead" and "alive but slow". A
# human watching a fleet die is a faster oracle: takeover lists the instances
# GitHub still remembers and force-takes one's claims with the same atomic
# --force-with-lease push a stale reclaim uses, only skipping the staleness
# gate. It never counts as a retry: it answers "did the FLEET die?", not "is
# this WORK failing?".

def group_instances(claims: Iterable[Claim] | None, heartbeats: Mapping[str, float] | None,
                    me: str | None, now: float, ttl: float) -> list[Obj]:
    """
    Every fleet instance discoverable in fleet state, with what it holds and how
    stale its lease is — the takeover picker's input (`afk takeover --list`).

      claims:     [{"number","instance","host","sha"}...] (the ref scan)
      heartbeats: {instance: last_ts}
      me:         my own instance id (marked, never a takeover target)

    Returns rows [{"instance","host","claims","claim_count","heartbeat_ts",
    "heartbeat_age","fresh","is_me"}...], the ones holding most claims first
    (then by id, so the listing is stable). An instance with a heartbeat but no
    claims is still listed — that is a fleet that drained cleanly or is idle, and
    seeing it is how a human tells it apart from the one that died mid-flight. A
    claim whose marker names no instance appears under `instance: null`; it needs
    no takeover, being already reclaimable as stale.
    """
    by: dict[Any, Obj] = {}
    for c in claims or []:
        inst = c["instance"]
        row = by.setdefault(inst, {"instance": inst, "host": None, "claims": []})
        row["claims"].append(c["number"])
        row["host"] = row["host"] or c["host"]
    for inst in (heartbeats or {}):
        by.setdefault(inst, {"instance": inst, "host": None, "claims": []})

    out: list[Obj] = []
    for inst, row in by.items():
        ts = (heartbeats or {}).get(inst)
        nums = sorted(n for n in row["claims"] if n is not None)
        out.append({"instance": inst, "host": row["host"], "claims": nums,
                    "claim_count": len(nums),
                    "heartbeat_ts": ts,
                    "heartbeat_age": None if ts is None else int(now) - int(ts),
                    "fresh": ts is not None and not is_stale(ts, now, ttl),
                    "is_me": inst is not None and inst == me})
    out.sort(key=lambda r: (-r["claim_count"], r["instance"] is None, str(r["instance"])))
    return out


def plan_takeover(claims: Iterable[Claim] | None, heartbeats: Mapping[str, float] | None,
                  target: str | None, me: str | None, now: float, ttl: float,
                  confirmed: bool = False) -> Obj:
    """
    Whether `afk takeover --instance <target>` may proceed, and over which claims.
    Pure: the effectful layer scans the refs, this decides, then it pushes.

    Returns {"action", "instance", "claims", "fresh", "detail"} where `claims` is
    [{"number","sha","host"}...] (the sha each force-with-lease push needs):

      take     go — force-take these claims, skipping the staleness gate.
      confirm  the target's heartbeat is still FRESH: taking it steals live work if
               the human is wrong, so an explicit confirmation is required first.
               Surfaced as a warning rather than a refusal — the human may
               legitimately know better (a wedged process, a heartbeat written by a
               now-dead tick), and the push stays atomic underneath (ADR-0011).
      none     nothing to take: no such instance, or it holds no claims.
      error    the target is THIS fleet — its claims are already mine.
    """
    rows = sorted(({"number": c["number"], "sha": c["sha"], "host": c["host"]}
                   for c in claims or [] if c["instance"] == target),
                  key=lambda r: (r["number"] is None, r["number"]))
    ts = (heartbeats or {}).get(target) if target else None
    fresh = ts is not None and not is_stale(ts, now, ttl)
    age = None if ts is None else int(now) - int(ts)
    known = ts is not None or bool(rows)

    def out(action: str, detail: str) -> Obj:
        return {"action": action, "instance": target, "claims": rows,
                "fresh": fresh, "heartbeat_age": age, "detail": detail}

    if target is not None and target == me:
        return out("error", "that is this fleet's own instance id — its claims are already mine")
    if not rows:
        return out("none", "no instance by that id holds any claim" if not known
                           else "that instance holds no claims (nothing to take)")
    if fresh and not confirmed:
        return out("confirm",
                   f"that fleet's heartbeat is only {age}s old (lease {int(ttl)}s) — "
                   f"it looks ALIVE; forcing a takeover steals its live work if you are wrong")
    return out("take", f"taking {len(rows)} claim(s) from {target}"
                       + (" (fresh heartbeat, human-confirmed)" if fresh else ""))


# --------------------------------------------------------------------------- #
# Continuation — recovering a dead claim FROM ITS PROGRESS (ADR-0011)          #
# --------------------------------------------------------------------------- #
#
# A claim whose worker died — an *orphaned claim*, or a *stale claim* reclaimed
# from a peer — is recovered from its durable progress, tiered by what survived:
# a worktree still on THIS machine, else the branch the worker pushed, else
# nothing. The selection is mechanics; "is this recovered state sane to build
# on" stays tick judgment. Only tier 3 tears anything down.

_PLACEHOLDER_RE = re.compile(r"\{(number|slug)\}")


def branch_regex(number: int) -> re.Pattern[str]:
    """
    An issue number → the regex that matches the branch orca
    ACTUALLY created for it. Two things are wildcards, by construction: orca
    prefixes the branch with `<user>/` (ADR-0005 — the fleet reads the name back
    rather than dictating it), and the slug is whatever the dispatching tick
    passed. The number is not: it is the one field that identifies the issue.
    """
    out, pos = [], 0
    pattern = BRANCH_PATTERN
    for m in _PLACEHOLDER_RE.finditer(pattern):
        out.append(re.escape(pattern[pos:m.start()]))
        out.append(str(number) if m.group(1) == "number" else "[^/]*")
        pos = m.end()
    out.append(re.escape(pattern[pos:]))
    return re.compile(r"^(?:[^/]+/)?" + "".join(out) + r"$")


def branch_candidates(heads: Iterable[str] | None, number: int) -> list[str]:
    """The remote branch names that could be issue <number>'s work branch, sorted.
    Used when NO local worktree survived: the claim ref records the issue, not the
    branch, so tier 2 has to recognise the branch by its name."""
    rx = branch_regex(number)
    return sorted(h for h in (heads or []) if h and rx.match(h))


def short_branch(ref: str | None) -> str:
    """`refs/heads/x/y` → `x/y`; a name that is already short is returned as is.
    orca reports a worktree's branch either way."""
    ref = ref or ""
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref


def in_orca_project(worktree: Obj, repo: str) -> bool:
    """Whether an orca worktree row belongs to `repo` ("owner/name"): its
    `projectId` is `github:owner/name`, which orca writes lower-cased whatever
    the repo's own casing — so, like `find_orca_repo`, compared case-insensitively
    (as GitHub does). Compared exactly, a repo with a capital in its name owns no
    worktree at all: every live worker reads as dead and is dispatched again."""
    return (worktree.get("projectId") or "").lower() == f"github:{repo}".lower()


def find_orca_worktree(worktrees: Iterable[Obj] | None, number: int,
                       repo: str | None = None) -> Obj:
    """
    The orca worktree belonging to issue <number> on THIS machine, from
    `orca worktree list --json`'s `result.worktrees` rows — the tier-1 signal.
    Pure so the shape of orca's JSON is fixture-pinned rather than re-derived.

      worktrees: rows carrying {linkedIssue, path, branch, projectId,
                 isMainWorktree, isArchived, lastActivityAt}
      repo:      "owner/name" — when given, a row must belong to it (orca's
                 `projectId` is `github:owner/name`, any casing), so a same-numbered issue in
                 another repo's worktree is never mistaken for this one.

    Returns {"found": bool, "path": str|None, "branch": str|None}. Several
    matches (a stale leftover plus a live one) → the most recently active.
    """
    hits = []
    for w in worktrees or []:
        # compared numerically: a str/int drift in orca's JSON would silently
        # downgrade every tier-1 recovery and lose the uncommitted work it saves
        try:
            linked = w.get("linkedIssue")
            if linked is None or int(linked) != int(number):
                continue
        except (TypeError, ValueError):
            continue
        if w.get("isMainWorktree") or w.get("isArchived"):
            continue
        if repo and not in_orca_project(w, repo):
            continue
        hits.append(w)
    if not hits:
        return {"found": False, "path": None, "branch": None}
    best = max(hits, key=lambda w: int(w.get("lastActivityAt") or 0))
    return {"found": True, "path": best.get("path") or None,
            "branch": short_branch(best.get("branch")) or None}


def remotes_of(urls: Mapping[str, str] | None, repo: str) -> list[str]:
    """The names of the git remotes that ARE the target repo, from a checkout's
    `remote.<name>.url` settings ({name: url}) — the ones whose remote-tracking
    ref a fetch by URL leaves behind. A URL matches when it ends in
    `github.com/<owner>/<name>` or `github.com:<owner>/<name>`, `.git` or not,
    case-insensitively, as GitHub does."""
    want = repo.lower()

    def names(url: str) -> bool:
        url = url.lower().rstrip("/").removesuffix(".git")
        return url.endswith(f"github.com/{want}") or url.endswith(f"github.com:{want}")
    return sorted(name for name, url in (urls or {}).items() if names(url))


def find_orca_repo(repos: Iterable[Obj] | None, repo: str) -> Obj | None:
    """
    The repo orca knows the target by, from `orca repo list --json`'s
    `result.repos` rows → {"id", "path"}: the id `orca worktree create --repo
    id:<id>` needs, and the checkout whose object store a new worktree is cut from.
    A row matches when its `gitRemoteIdentity.canonicalKey` is
    `github.com/<owner>/<name>` (compared case-insensitively, as GitHub does). None
    when orca has no such repo.
    """
    want = f"github.com/{repo}".lower()
    for r in repos or []:
        key = ((r.get("gitRemoteIdentity") or {}).get("canonicalKey") or "").lower()
        if key == want and r.get("id") and r.get("path"):
            return {"id": r["id"], "path": r["path"]}
    return None


def worktree_name(number: int, title: str | None) -> str:
    """BRANCH_PATTERN filled for one issue — the NAME hint handed to `orca
    worktree create --name` (orca derives the real branch from it, ADR-0005). The
    slug is the title lowercased to `[a-z0-9-]`, at most 40 characters; a title
    with nothing usable (all CJK, say) slugs to `work`."""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:40].rstrip("-")
    return (BRANCH_PATTERN.replace("{number}", str(number))
            .replace("{slug}", slug or "work"))


def furthest_ahead(ahead_by_branch: Mapping[str, int | None]) -> str | None:
    """Of several remote branches matching one issue (an earlier attempt left one
    behind), the one furthest ahead of base — ties, and unmeasurable counts
    (None), go to the first by name. None when there is no candidate."""
    names = sorted(ahead_by_branch)
    return max(names, key=lambda b: ahead_by_branch[b] or 0) if names else None


def select_recovery(worktree: WorktreeSignal | None, branch: BranchSignal | None, fresh: bool = False,
                    landing_pr: int | None = None) -> RecoveryPlan:
    """
    How to recover ONE claim whose worker has died — the continue-vs-fresh
    selection of ADR-0011, tiered by what survived. Pure: `afk recovery` gathers
    the two signals, this decides, and a fixture pins every tier.

      worktree: this machine's worktree for the issue (None / {} if there is none)
                {"present": bool, "commits_ahead": int|None, "dirty": bool}
      branch:   the issue's branch on the remote (None / {} if unknown)
                {"name": str|None, "commits_ahead": int|None}   (ahead of base)
      fresh:    the previous attempt was discarded — a retry. Nothing is read:
                tier 3, whatever the signals say.
      landing_pr: the number of the issue's PR when it holds this fleet's landing
                turn on its own (not in a merge batch), else None. The worker is
                then started ON the turn (ADR-0027): `prompt` is "landing", and
                with no worktree here one is recreated at the PR's head — never
                from base.

    Returns {"tier", "action", "prompt", "reason"}:
      1  reuse_worktree   a worktree is still HERE → spawn the new worker inside
                          it, on the same branch, and do NOT `orca worktree rm`
                          it. Lossless: it carries even uncommitted work.
      2  recreate_at_tip  no local worktree, but the dead worker pushed → recreate
                          one at the branch tip and continue there. Loss is
                          bounded to "since the last push".
      3  dispatch_fresh   nothing survived → re-dispatch from base. The ONLY
                          tier that tears down.

    `prompt` picks the brief: a worker-prompt variant — `continue` (inspect the
    existing progress first) or `fresh` — or `landing`, the landing brief
    (`render_landing`). A surviving worktree that is *provably* pristine
    — zero commits ahead, a clean tree, and nothing pushed on its branch either —
    gets the fresh prompt: there is nothing to continue, and telling a worker
    otherwise sends it looking for work that isn't there. Unreadable progress
    (`commits_ahead: None`) is NOT pristine: we never hand out a fresh prompt over
    a worktree we could not read.
    """
    if fresh:
        return {"tier": 3, "action": "dispatch_fresh", "prompt": "fresh",
                "reason": "fresh start: the previous attempt was discarded"}
    wt, br = worktree, branch
    pushed = int((br["commits_ahead"] if br else None) or 0)
    held = "landing" if landing_pr else None
    if wt and wt["present"]:
        ahead = wt["commits_ahead"]
        known = ahead is not None
        pristine = known and int(ahead) == 0 and not wt["dirty"] and pushed == 0
        if pristine:
            reason = "local worktree present but pristine — reuse it, nothing to continue"
        elif not known:
            reason = "local worktree present, progress unreadable — reuse it and inspect"
        elif int(ahead) == 0 and not wt["dirty"]:
            reason = (f"local worktree present and clean, but its branch carries {pushed} "
                      f"pushed commit(s) ahead of base — reuse it and continue from them")
        else:
            reason = (f"local worktree present with {int(ahead)} commit(s) ahead"
                      + (" and uncommitted changes" if wt["dirty"] else ""))
        return {"tier": 1, "action": "reuse_worktree",
                "prompt": held or ("fresh" if pristine else "continue"), "reason": reason}
    if held:
        return {"tier": 2, "action": "recreate_at_tip", "prompt": held,
                "reason": f"no local worktree; PR #{landing_pr} holds the landing turn — "
                          f"recreate at its head"}

    name, ahead = (br["name"], br["commits_ahead"]) if br else (None, None)
    if name and ahead is not None and int(ahead) > 0:
        return {"tier": 2, "action": "recreate_at_tip", "prompt": "continue",
                "reason": f"no local worktree; branch {name} is {int(ahead)} commit(s) "
                          f"ahead of base — recreate at its tip"}
    return {"tier": 3, "action": "dispatch_fresh", "prompt": "fresh",
            "reason": "no local worktree and nothing pushed ahead of base — "
                      "re-dispatch from base"}


# --------------------------------------------------------------------------- #
# The worker prompt — one template, filled by code (`afk dispatch`)            #
# --------------------------------------------------------------------------- #
#
# references/worker-prompt.md is the template: named blocks between
# `<!--afk:block NAME-->` and `<!--/afk:block-->`. The `prompt` block is the body;
# it names three slots — {opening} and {step1}, each filled from the block of
# that name for the chosen variant (`opening.fresh`, `step1.continue`, …), and
# {retry_reason}, filled from the `retry_reason` block only when a failure reason
# is handed over. The `landing` block is a brief of its own — `render_landing` —
# pointed at when the worker is given its landing turn: the one command a worker
# lands its PR with and what each of its outcomes asks for (ADR-0027). It is the
# only place either is spelled; the body says no more than that the worker does
# not merge its PR and is told when to land it. The `batch` block is a third
# brief, for a merge batch's worker — `render_batch_brief`, with fields of its
# own (ADR-0029). Everything else in braces is a
# field — four of them derived: {wake_command}, the line a worker runs to wake
# the launcher once its outcome is on GitHub, built from the `launcher_terminal`
# field (ADR-0020), {gate_command}, the line a worker runs the local gate with —
# `afk gate`, built from the `afk_path` and `local_command` fields, so a green
# run is on record for the landing (ADR-0026) — {land_command}, `afk land` with
# the run's config, built from `afk_path`, `n`, `repo` and `config`, and
# {verdict_marker}, the marker a worker that opens no PR must post
# (`verdict_marker_format`).

_BLOCK_RE = re.compile(r"<!--afk:block ([a-z0-9_.]+)-->\n(.*?)\n?<!--/afk:block-->", re.DOTALL)
PromptVariant = Literal["fresh", "continue"]
PROMPT_VARIANTS: tuple[PromptVariant, ...] = get_args(PromptVariant)
PROMPT_FIELDS = ("n", "title", "repo", "base_branch", "local_command", "afk_path", "config",
                 "branch", "worktree_path", "launcher_terminal")
LANDING_FIELDS = ("pr", "pr_branch", "target")
_PROMPT_SLOTS = ("opening", "step1", "retry_reason")
_PROMPT_DERIVED = ("wake_command", "gate_command", "land_command", "verdict_marker")
_NO_LOCAL_COMMAND = "true   # (no gate.local_command configured: run the repo's own build/test, if any)"
_NO_WAKE = "true   # (no coordinator terminal to wake: it finds your outcome at its next poll)"
_TERMINAL_HANDLE_RE = re.compile(r"[A-Za-z0-9_.:-]+")


def wake_line(number: int | str) -> str:
    """The one line a wake types at the launcher's terminal. It names the issue for
    the human scrolling back, and nothing the launcher may act on (ADR-0020)."""
    return f"afk-wake #{number}"


def wake_command(launcher_terminal: str | None, number: int | str) -> str:
    """
    The command a worker runs once its outcome is on GitHub — a PR, a verdict
    marker, a landing that merged or stopped for the tick — to wake the launcher out of its sleep
    (ADR-0020), so the next cycle opens now instead of a busy interval later.

      launcher_terminal: the orca handle of the terminal the launcher runs in; ""
                         or None when it runs in none (a headless tick)

    The wake is a hint and carries no state: the cycle it triggers reads GitHub
    like any other, and a wake that is lost costs only the wait it would have
    saved. With no handle — or one that is not a bare handle, since this string is
    run in the worker's shell — the command is a no-op with a note.
    """
    handle = (launcher_terminal or "").strip()
    if not _TERMINAL_HANDLE_RE.fullmatch(handle):
        return _NO_WAKE
    return f'orca terminal send --terminal {handle} --text "{wake_line(number)}" --enter'


def gate_command(afk_path: str, local_command: str | None) -> str:
    """
    The command a worker runs the local gate with: `afk gate`, carrying the
    configured `gate.local_command` as its config, so the run is made — and, when
    green, recorded — by the tool rather than reported by the worker (ADR-0026).

      afk_path:      the afk executable on this machine (the worker runs here)
      local_command: `gate.local_command`; empty → a no-op with a note

    The command travels inside the line, so a config that changes after the worker
    was briefed records a command the landing no longer recognises: void, not wrong.
    """
    command = (local_command or "").strip()
    if not command:
        return _NO_LOCAL_COMMAND
    config = json.dumps({"gate": {"local_command": command}}, ensure_ascii=False)
    return f"{shlex.quote(afk_path)} gate --config {shlex.quote(config)}"


def _prompt_blocks(template: str | None) -> Callable[[str], str]:
    blocks = dict(_BLOCK_RE.findall(template or ""))

    def block(name: str) -> str:
        if name not in blocks:
            raise ValueError(f"worker prompt template has no {name!r} block")
        return blocks[name]
    return block


def land_command(afk_path: str, number: int, repo: str, config: str) -> str:
    """
    The one command a worker lands its PR with, on its landing turn: `afk land`,
    carrying the run's config — the merge target, the gate, the
    claim namespace the turn is checked against (ADR-0027).

      afk_path: the afk executable on this machine (the worker runs here)
      config:   the run's canonical config, as its JSON text
    """
    return (f"{shlex.quote(afk_path)} land --issue {number} --repo {shlex.quote(repo)} "
            f"--config {shlex.quote(config)}")


def _fill_prompt(text: str, fields: Obj, landing: Obj | None = None,
                 reason: str | None = None) -> str:
    """Fill every field of an assembled prompt text. Raises ValueError on a missing
    field or a placeholder left unfilled; the free-text values (title, reason, the
    land command's config) go in last and in one pass, so one that happens to
    contain "{branch}" is never itself substituted into."""
    missing = [k for k in PROMPT_FIELDS if k not in fields]
    missing += [k for k in LANDING_FIELDS if landing is not None and k not in landing]
    if missing:
        raise ValueError(f"worker prompt: missing field(s) {', '.join(missing)}")
    values = {k: str(fields[k]) for k in PROMPT_FIELDS}
    values["land_command"] = land_command(values["afk_path"], fields["n"], values["repo"],
                                          values.pop("config"))
    values["gate_command"] = gate_command(values.pop("afk_path"), values["local_command"])
    values["local_command"] = values["local_command"].strip() or _NO_LOCAL_COMMAND
    values["wake_command"] = wake_command(values.pop("launcher_terminal"), fields["n"])
    values["verdict_marker"] = verdict_marker_format(fields["n"])
    # the land command carries the whole config, and a config may hold braces
    free_text = {"title": values.pop("title"), "reason": (reason or "").strip(),
                 "land_command": values.pop("land_command")}
    if landing is not None:
        values.update({k: str(landing[k]) for k in LANDING_FIELDS})
    for name, value in values.items():
        text = text.replace("{" + name + "}", value)
    known = (*PROMPT_FIELDS, *LANDING_FIELDS, *_PROMPT_SLOTS, *_PROMPT_DERIVED)
    left = sorted(set(re.findall(r"\{(?:%s)\}" % "|".join(known), text))
                  - {"{%s}" % k for k in free_text})
    if left:
        raise ValueError(f"worker prompt: unfilled placeholder(s) {', '.join(left)}")
    text = re.sub(r"\{(%s)\}" % "|".join(free_text), lambda m: free_text[m.group(1)], text)
    return text.strip() + "\n"


def render_worker_prompt(template: str, variant: PromptVariant, fields: Obj,
                         reason: str | None = None) -> str:
    """
    The prompt one worker is started with, from the template file's text.

      template: the text of references/worker-prompt.md
      variant:  "fresh" (a clean checkout of the base) or "continue" (the worktree
                or branch already carries a dead worker's progress — ADR-0011)
      fields:   {name: value} for every one of PROMPT_FIELDS. An empty
                `local_command` renders as a no-op with a note (`gate_command`),
                and so does the wake when `launcher_terminal` is empty
                (`wake_command`).
      reason:   why the previous attempt failed, when this is a retry; None otherwise

    Raises ValueError on a template missing a block, a missing field, or a
    placeholder left unfilled — a worker must never be started on a prompt with a
    literal `{branch}` in it.
    """
    if variant not in PROMPT_VARIANTS:
        raise ValueError(f"unknown worker prompt variant: {variant!r}")
    block = _prompt_blocks(template)
    text = block("prompt")
    slots = {"opening": block(f"opening.{variant}"), "step1": block(f"step1.{variant}"),
             "retry_reason": block("retry_reason") if reason else ""}
    for name, body in slots.items():
        text = text.replace("{" + name + "}", body)
    text = re.sub(r"\n{3,}", "\n\n", text)      # an unfilled slot leaves no gap behind
    return _fill_prompt(text, fields, reason=reason)


BATCH_FIELDS = ("batch", "members", "repo", "target", "afk_path", "config", "branch",
                "worktree_path", "launcher_terminal")


def batch_land_command(afk_path: str, batch: str, repo: str, config: str) -> str:
    """The one command a batch worker stacks, gates and lands its batch with:
    `afk land --batch`, carrying the run's config (ADR-0029)."""
    return (f"{shlex.quote(afk_path)} land --batch {shlex.quote(batch)} --repo {shlex.quote(repo)} "
            f"--config {shlex.quote(config)}")


def render_batch_brief(template: str, fields: Obj) -> str:
    """
    The brief a merge batch's worker is started on: the template's `batch` block
    — the `afk land --batch` command and its outcome table.

      fields: {name: value} for every one of BATCH_FIELDS. `members` is
              [{"issue", "pr", "title"}...] in stack order, rendered as a list.

    Raises ValueError on a missing field or a placeholder left unfilled.
    """
    missing = [k for k in BATCH_FIELDS if k not in fields]
    if missing:
        raise ValueError(f"batch brief: missing field(s) {', '.join(missing)}")
    text = _prompt_blocks(template)("batch")
    batch = str(fields["batch"])
    values = {k: str(fields[k]) for k in ("batch", "repo", "target", "branch", "worktree_path")}
    values["wake_command"] = wake_command(fields["launcher_terminal"], f"batch-{batch}")
    # free text — titles, and a config that may hold braces — goes in last
    free_text = {"members": "\n".join(f"- PR #{m['pr']} — closes #{m['issue']} — {m['title']}"
                                      for m in fields["members"]),
                 "batch_land_command": batch_land_command(str(fields["afk_path"]), batch,
                                                          values["repo"], str(fields["config"]))}
    for name, value in values.items():
        text = text.replace("{" + name + "}", value)
    left = sorted(set(re.findall(r"\{[a-z_]+\}", text)) - {"{%s}" % k for k in free_text})
    if left:
        raise ValueError(f"batch brief: unfilled placeholder(s) {', '.join(left)}")
    text = re.sub(r"\{(%s)\}" % "|".join(free_text), lambda m: free_text[m.group(1)], text)
    return text.strip() + "\n"


def render_landing(template: str, fields: Obj, landing: Obj) -> str:
    """
    The brief a worker is pointed at when its PR is given the landing turn: the
    template's `landing` block — the `afk land` command and its outcome table —
    filled from the same `fields` as `render_worker_prompt` plus `landing`,
    {name: value} for every one of LANDING_FIELDS. It is the whole brief either way: for the worker that wrote
    the branch and is still there, and for one started in its worktree because
    it is gone — that one is briefed only to land the PR (ADR-0027).
    """
    block = _prompt_blocks(template)
    return _fill_prompt(block("landing"), fields, landing)


# --------------------------------------------------------------------------- #
# Human-facing progress status board — render only (ADR-0006)                  #
# --------------------------------------------------------------------------- #
#
# The fleet's machine state (claim ref + PR + checks + afk-attempt label) is the
# single source of truth for *decisions*. But a human reading the issue can't see
# the claim — it lives in the hidden refs/afk/* namespace — and the assignee is
# unused, so the whole "claimed, worker coding, no PR yet" phase is invisible on
# the issue surface. The status board projects that lifecycle onto the issue as
# ONE comment the owning tick upserts each rebuild. It is a *rendering* of state
# the tick already derived — never a second source of truth, never read back by a
# tick. Rendered as a GitHub task list so the issue shows a progress meter.
#
# Idempotent by construction: the body carries NO wall-clock time, so identical
# lifecycle state renders identical text; the effectful layer then writes only
# when the text actually changed, and re-entrant/disposable ticks never spam.

# The board is a record with no fields: its marker only says which comment is
# the board, and everything it tells is the text under it.
STATUS_RECORD = RecordKind("afk:status", {}, ())
STATUS_MARKER = record_marker(STATUS_RECORD, {})

# Happy-path milestones, in order — these are the task-list checkboxes.
_STATUS_STEPS = (
    ("claimed",  "已认领 · worker 实现中"),
    ("pr_open",  "PR 已开{pr} · 等 {gate}"),
    ("landing",  "轮到落地 · worker 同步、过门、合并"),
    ("merged",   "已合并"),
)

# The closed set of lifecycle phases the board renders, each with everything the
# board says about it: how far along the happy path it has reached (the key of
# the last DONE step in _STATUS_STEPS) and its single ▸/✅/⚠️ 'where are we now' line. Happy path
# plus five off-ramps that reuse the same checkboxes + an annotation: ci_failed,
# awaiting_turn (the PR is ready and waits for the landing turn — one PR lands
# at a time), escalated (a terminal give-up, ticked specially in
# `render_status_board`), closed (the worker found the issue already satisfied —
# `afk close`), and parked (the worker found an open dependency; the claim is
# released until it closes — `afk park`).
_NOTHING_REACHED = None    # no step ticked: the phase is before, or outside, the happy path
StatusPhase = Literal["claimed", "pr_open", "ci_failed", "awaiting_turn", "landing", "merged",
                      "escalated", "closed", "parked"]
_PHASES: dict[StatusPhase, tuple[Optional[str], str]] = {
    "claimed":        ("claimed", "▸ 当前:worker 实现中,尚无 PR"),
    "pr_open":        ("pr_open", "▸ 当前:等 {gate}"),
    "ci_failed":      ("pr_open", "▸ 当前:{gate} 失败,修复重试中({attempt}/{retry_max}) —— 见下方 {gate} 与评论"),
    "awaiting_turn":  ("pr_open", "▸ 当前:PR 已就绪,排队等落地轮次 —— 一次只落地一个 PR,轮到后由 worker 自己合并"),
    "landing":        ("landing", "▸ 当前:已轮到落地,worker 正在与目标分支同步、过门并合并 —— 见 PR 评论"),
    "merged":         ("merged", "✅ 已合并,完成"),
    "escalated":      ("pr_open", "⚠️ 已升级给人处理 —— 见下方评论"),
    "closed":         ("claimed", "✅ 主干已满足此需求,无需改动 —— 已关闭"),
    "parked":         (_NOTHING_REACHED, "⏸ 等待依赖 {blockers} 关闭 —— 关闭后自动重新派发,无需人工处理"),
}
STATUS_PHASES = tuple(_PHASES)

# The 'where are we now' line of a claim whose PR holds the turn as a member of
# a merge batch (ADR-0029), and what its batch worker is doing, by BATCH_PHASES.
_BATCH_LINE = ("▸ 当前:已轮到落地,与 {prs} 合为一个 merge batch —— batch worker {doing},"
               "整批过一次门后一起落地")
_BATCH_DOING: dict[BatchPhase, str] = {"stacking": "正在把各 PR 叠放到目标分支上(stacking)",
                                       "gating": "正在对整批过门(gating)",
                                       "fixing": "正在修复整批的红门(being fixed)"}


def render_status_board(phase: StatusPhase, gate_ci: GateCiMode, retry_max: int,
                        instance: str | None = None, pr: int | None = None, attempt: int = 0,
                        blocked_by: Iterable[int] = (), batch: BoardBatch | None = None) -> str:
    """
    Render the human-facing progress *status board* comment body. Pure: a function
    of the discrete lifecycle state the tick already derived from fleet state; no
    I/O and no clock, so identical state → identical body (this is what lets the
    upsert write only when it changed, and keeps re-entrant ticks from spamming).
    Human-read only — never parsed back as a source of truth.

      phase:     one of STATUS_PHASES
      gate_ci:   config `gate.ci` — names the gate being waited on
      retry_max: config `retry` — shown with `attempt`, for ci_failed only
      instance:  owning fleet-instance id, shown in the header when given
      pr:        the PR number, once one is open
      attempt:   the claim's current attempt (`current_attempt`)
      blocked_by: the open blockers the issue waits on, for parked only
      batch:     {"prs": [pr...], "phase": one of BATCH_PHASES} when the PR holds
                 the turn in a merge batch — for landing only: the line names
                 the batch's PRs and what its worker is doing

    Returns the full markdown body, led by STATUS_MARKER (the find-or-create anchor).
    """
    if phase not in STATUS_PHASES:
        raise ValueError(f"unknown status phase: {phase!r}")
    gate = GATE_CI_MODES[gate_ci]
    reached, current = _PHASES[phase]
    steps = [key for key, _ in _STATUS_STEPS]
    ticked = steps[:steps.index(reached) + 1] if reached is not _NOTHING_REACHED else []
    escalated = phase == "escalated"

    def done(key: str) -> bool:
        if escalated:              # terminal give-up: only what truly happened stays ticked
            return key == "claimed" or (key == "pr_open" and bool(pr))
        return key in ticked

    header = "**afk-fleet 进度**" + (f" · 认领方 `{instance}`" if instance else "")
    lines = [header, ""]
    for key, label in _STATUS_STEPS:
        label = label.format(pr=f" (#{pr})" if pr else "", gate=gate)
        lines.append(f"- [{'x' if done(key) else ' '}] {label}")
    lines.append("")
    if batch and phase == "landing":
        current = _BATCH_LINE.format(prs="、".join(f"#{n}" for n in batch["prs"]),
                                     doing=_BATCH_DOING[batch["phase"]])
    lines.append(current.format(gate=gate, attempt=attempt, retry_max=retry_max,
                                blockers="、".join(f"#{n}" for n in blocked_by)))
    return record_comment(STATUS_RECORD, {}, "\n".join(lines))


def board_key(body: str) -> str:
    """A short digest of a status board body — what the cycle state keeps per
    claim instead of the body. A board that renders to the key the state holds
    is the one already on the issue, and is not read to find that out."""
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()[:8]


# --------------------------------------------------------------------------- #
# Retry accounting + launcher pacing                                          #
# --------------------------------------------------------------------------- #

_ATTEMPT_PREFIX = "afk-attempt/"


def current_attempt(labels: Iterable[str] | None) -> int:
    """The attempt an issue is on, from its label names: the highest n across its
    `afk-attempt/<n>` labels, 0 when it has none (never retried). The ONE reader
    of that label format — `afk rebuild` puts the number on every `mine` row, and
    everything downstream (`next_attempt`, the status board) takes the number."""
    attempts = [0]
    for lb in labels or []:
        if isinstance(lb, str) and lb.startswith(_ATTEMPT_PREFIX):
            n = lb[len(_ATTEMPT_PREFIX):]
            if n.isdigit():
                attempts.append(int(n))
    return max(attempts)


def attempt_labels(labels: Iterable[str] | None) -> list[str]:
    """Every `afk-attempt/*` label among an issue's label names — what a retry
    swaps out and an escalation strips, however many a hand-edit left behind."""
    return sorted(lb for lb in labels or []
                  if isinstance(lb, str) and lb.startswith(_ATTEMPT_PREFIX))


# Beside the attempt label while a retry is under way: the failure in hand is
# already counted in it, and the fresh worker of that attempt has not started.
# Written in the same edit as the count and removed once a worker has started,
# so a failure handled twice — `afk fail` cut short, then run again — is told
# from a new failure of the fresh attempt.
ATTEMPT_STARTING = f"{_ATTEMPT_PREFIX}starting"


def attempt_starting(labels: Collection[str] | None) -> bool:
    """Whether the failure an issue's claim is in has already been counted
    (`ATTEMPT_STARTING`) — a retry that was begun and has not put a worker on
    the issue yet. Never so for an issue that carries no counted attempt."""
    return ATTEMPT_STARTING in (labels or []) and current_attempt(labels) > 0


def next_attempt(attempt: int, retry_max: int, counted: bool = False) -> Obj:
    """
    Retry-or-escalate for a failed issue on attempt `attempt` (`current_attempt`).

      {"action":"retry","attempt":<n+1>,"to_label":"afk-attempt/<n+1>"}
      {"action":"escalate","attempt":<n>}                when n >= retry_max
      {"action":"retry","attempt":<n>,"to_label":"afk-attempt/<n>"}
                                 when `counted` (`attempt_starting`): this failure
                                 is the one that made the attempt n — the retry
                                 is finished, and nothing is added

    `afk fail` is the one caller, and the one writer of the label: it applies
    `retry_labels` of `to_label`.
    """
    if counted:
        return {"action": "retry", "attempt": attempt, "to_label": f"{_ATTEMPT_PREFIX}{attempt}"}
    if attempt >= retry_max:
        return {"action": "escalate", "attempt": attempt}
    return {"action": "retry", "attempt": attempt + 1,
            "to_label": f"{_ATTEMPT_PREFIX}{attempt + 1}"}


def retry_labels(labels: Iterable[str] | None, to_label: str) -> tuple[list[str], list[str]]:
    """The label edit that counts a failure: `(add, remove)`, made in ONE edit of
    the issue. Adds `to_label` and `ATTEMPT_STARTING` where the issue lacks them
    and removes every other attempt label it carries — so for a failure already
    counted there is nothing to add or remove, and no edit to make."""
    present, wanted = set(labels or []), [to_label, ATTEMPT_STARTING]
    return ([lb for lb in wanted if lb not in present],
            [lb for lb in attempt_labels(labels) if lb not in wanted])


def escalation_comment(reason: str | None, attempt: int, pr: int | None = None) -> str:
    """The durable hand-off comment an escalation appends to the issue (ADR-0006
    keeps it apart from the status board, which only points here). The stuck-point
    wording is the tick's; this frames it."""
    tried = f"after {attempt} retr{'y' if attempt == 1 else 'ies'}" if attempt else "without a retry"
    link = f" Last PR: #{pr}." if pr else ""
    return (f"**afk-fleet: escalated to a human** ({tried}).{link}\n\n"
            f"{(reason or '').strip()}")


def escalation_labels(labels: Iterable[str] | None,
                      config: Config) -> tuple[list[str], list[str]]:
    """The label edit that hands an issue to a human: `(add, remove)`. Removes
    `ready_label` and every attempt label the issue actually carries (never one it
    does not — gh refuses to remove an absent label), adds `escalate_label`."""
    present = set(labels or [])
    remove = sorted(present & {config["ready_label"], *attempt_labels(labels)})
    return [config["escalate_label"]], remove


def pace(did_work: bool, in_flight: int, empty_streak: int) -> int:
    """
    The launcher's next sleep, in seconds.

      did_work:     the tick that just ran granted a turn / dispatched / reclaimed / escalated
      in_flight:    claims this fleet holds
      empty_streak: consecutive empty cycles so far (`cycle_ticked` / `cycle_wake`)

    - did work, or holding claims → BUSY_INTERVAL_SECONDS;
    - else stay busy until IDLE_TICKS_BEFORE_SLEEP empty cycles, then
      IDLE_INTERVAL_SECONDS.

    While it holds a claim a fleet sleeps the busy interval, which is far inside
    half the lease — so the per-instance heartbeat cannot lapse and get a live
    claim reclaimed (ADR-0003).
    """
    if did_work or in_flight > 0 or empty_streak < IDLE_TICKS_BEFORE_SLEEP:
        return BUSY_INTERVAL_SECONDS    # working, or recently active — stay responsive
    return IDLE_INTERVAL_SECONDS


# --------------------------------------------------------------------------- #
# The cycle — gate, heartbeat, streaks and sleep as one state machine          #
# --------------------------------------------------------------------------- #
#
# Everything carried between cycles is ONE opaque value, the cycle state, which
# `afk cycle` hands back and takes again:
#
#   fingerprint         the digest the last gate computed (ADR-0007)
#   skips               consecutive skipped cycles
#   empty_streak        consecutive EMPTY cycles — a tick that did nothing with
#                       nothing in flight and nothing on the frontier, or a skip
#                       while that was still so
#   in_flight           claims held as the last tick ended
#   frontier_remaining  dispatchable issues the last tick left undispatched
#   unsettled           the last tick left a judgment open, met an error or
#                       ended with a PR it had not seen: the next cycle ticks
#                       whatever the digest says
#   boards              {issue number: `board_key`} for the claims held as the
#                       last tick ended — the status board each one was left
#                       with, so a tick that would render the same one again
#                       spends no read on it
#   instance            the fleet instance id      } the run's two launcher-held
#   worker_command      the worker launch command  } facts, passed once
#
# The caller never reads or edits a field; it is state for this code alone.

CYCLE_START: Obj = {"fingerprint": "", "skips": 0, "empty_streak": 0,
               "in_flight": 0, "frontier_remaining": 0, "unsettled": False, "boards": {}}
CYCLE_FACTS = ("instance", "worker_command")

# What a tick reports having done, per issue number — each with the word the
# progress line uses for it. The first seven are WORK: any of them keeps the fleet
# on the busy interval (`cycle_ticked`).
TickDid = Literal["granted", "dispatched", "reclaimed", "cleared", "escalated", "parked",
                  "abandoned", "retried", "nudged", "restarted"]
TICK_WORK: dict[TickDid, str] = {
    "granted": "landing turn to",
    "dispatched": "dispatched",
    "reclaimed": "reclaimed",
    "cleared": "cleared",
    "escalated": "escalated",
    "parked": "parked",
    "abandoned": "abandoned the batch of",
}
TICK_DID: dict[TickDid, str] = {**TICK_WORK, "retried": "retried", "nudged": "nudged", "restarted": "restarted"}
TICK_COUNTS = ("in_flight", "frontier_remaining")


def cycle_state(raw: Any, instance: str | None = None,
                worker_command: str | None = None) -> CycleState:
    """The cycle state from what the caller handed back (None / "" on the first
    cycle → CYCLE_START plus the two facts, which the first cycle must be given).
    Raises ValueError on anything that is not a state this code produced — a
    caller that mangled it must hear so, not run on zeros — and on a fact passed
    again that disagrees with the one the state carries."""
    given = {"instance": instance, "worker_command": worker_command}
    if raw is None or raw == "":
        missing = [f"--{k.replace('_', '-')}" for k, v in given.items() if not v]
        if missing:
            raise ValueError(f"the first cycle (no --state) needs {' and '.join(missing)}: "
                             f"they are carried in the state from then on")
        return cast("CycleState", {**CYCLE_START, "boards": {}, **given})
    if not isinstance(raw, dict) or set(raw) != {*CYCLE_START, *CYCLE_FACTS} \
            or not all(isinstance(raw[k], str) and raw[k] for k in CYCLE_FACTS) \
            or not isinstance(raw["boards"], dict):
        raise ValueError(f"--state is not a cycle state carrying the instance id and the worker "
                         f"launch command (pass back the `state` the previous `afk cycle` "
                         f"returned, verbatim): {raw!r}")
    for key, value in given.items():
        if value and value != raw[key]:
            raise ValueError(f"--{key.replace('_', '-')} {value!r} is not the one --state carries "
                             f"({raw[key]!r}): omit it after the first cycle")
    # the keys were just checked to be exactly a state's
    return cast("CycleState", {
        "fingerprint": str(raw["fingerprint"]), "unsettled": bool(raw["unsettled"]),
        "boards": {str(n): str(key) for n, key in raw["boards"].items()},
        **{k: int(raw[k]) for k in ("skips", "empty_streak", *TICK_COUNTS)},
        **{k: raw[k] for k in CYCLE_FACTS}})


def cycle_wake(state: CycleState, current_fp: str, woke: bool = False) -> Obj:
    """
    The top of one cycle: tick, or skip?

      state:      the cycle state (`cycle_state`)
      current_fp: `fingerprint` of what a rebuild would observe now
      woke:       a wake arrived while the previous cycle was running. The digest
                  that cycle kept was taken as its tick ENDED, so whatever the
                  wake announced may already be inside it, unseen by that tick

    Returns {"action": "tick"|"skip", "reason", "state"}. A skip is the whole
    cycle, so it carries what a cycle owes: `sleep_seconds`, `progress`, and
    `heartbeat` — True when the fleet holds claims, so the effect layer refreshes
    the lease no tick will. On a tick the pass runs and `cycle_ticked` closes it.

    A cycle after a tick that left something `unsettled` — an open judgment, an
    error, a PR that opened while it ran — ticks whatever the digest says: nothing
    may have moved, and the judgment, or the PR's landing turn, is still owed. So does one that `woke`. A skipped cycle extends the empty streak only while
    nothing is in flight and nothing is left on the frontier: unchanged state
    then proves the cycle empty.
    """
    gate = fingerprint_gate(state["fingerprint"], current_fp, state["skips"],
                            FORCE_TICK_AFTER_SKIPS)
    new: CycleState = {**state, "fingerprint": current_fp, "skips": gate["skips"]}
    if gate["action"] == "tick":
        return {"action": "tick", "reason": gate["reason"], "state": new}
    if state["unsettled"] or woke:
        return {"action": "tick", "reason": "unsettled" if state["unsettled"] else "wake",
                "state": {**new, "skips": 0}}
    if new["in_flight"] == 0 and new["frontier_remaining"] == 0:
        new["empty_streak"] += 1
    return {"action": "skip", "reason": gate["reason"], "state": new,
            "heartbeat": new["in_flight"] > 0,
            "sleep_seconds": pace(False, new["in_flight"], new["empty_streak"]),
            "progress": f"nothing moved; {_standing(new)}"}


def cycle_ticked(state: CycleState, did: Obj, judgments: int = 0, errors: int = 0,
                 left: str | None = None, boards: Mapping[Any, str] | None = None,
                 unseen: int = 0) -> Obj:
    """
    The bottom of a cycle that ran a tick: fold what the tick did into the cycle
    state and say how long to sleep.

      did:       the tick's own account — a list of issue numbers per TICK_DID
                 key, and the two integers of TICK_COUNTS
      judgments: how many judgments the tick returned instead of deciding
      errors:    how many of its transitions failed
      left:      `fingerprint` of the fleet as the tick LEFT it, taken after its
                 last write — what the state keeps, so the boards, refs, markers
                 and labels the tick itself wrote do not read as a change next
                 cycle (ADR-0007). None keeps the digest the cycle opened with
      boards:    {issue number: `board_key`} for the claims the tick ended
                 holding; None keeps the state's
      unseen:    how many of my claims' PRs opened while the tick ran
                 (`unseen_prs`) — inside `left`, and acted on by nobody

    Returns {"state", "sleep_seconds", "progress"}. A tick with open judgments
    sleeps 0: the caller answers them and opens the next cycle at once. So does
    one that left a PR unseen: the next tick is the one that gives it its turn.
    One that left a judgment, an error or an unseen PR is `unsettled`, and never
    counts as empty.
    """
    did_work = any(did.get(k) for k in TICK_WORK)
    in_flight, remaining = (int(did[k]) for k in TICK_COUNTS)
    unsettled = bool(judgments or errors or unseen)
    empty = not did_work and in_flight == 0 and remaining == 0 and not unsettled
    new: CycleState = {
           **state, "in_flight": in_flight, "frontier_remaining": remaining,
           "unsettled": unsettled, "empty_streak": state["empty_streak"] + 1 if empty else 0,
           "fingerprint": state["fingerprint"] if left is None else left,
           "boards": state["boards"] if boards is None
           else {str(n): key for n, key in boards.items()}}
    parts = [f"{word} {', '.join(f'#{n}' for n in did[k])}"
             for k, word in TICK_DID.items() if did.get(k)]
    parts += [f"{count} {noun}{'' if count == 1 else 's'}{tail}"
              for count, noun, tail in ((judgments, "judgment", " open"), (errors, "error", ""),
                                        (unseen, "PR", " opened meanwhile"))
              if count]
    return {"state": new,
            "sleep_seconds": 0 if judgments or unseen else pace(did_work, in_flight,
                                                      new["empty_streak"]),
            "progress": "; ".join([*parts, _standing(new)])}


def cycle_drained(state: CycleState, released: Collection[int], kept: Collection[int],
                  errors: int = 0) -> Obj:
    """
    The bottom of the LAST cycle of a run — `afk cycle --drain`, the launcher's
    stop: fold what the drain released and kept into the cycle state.

      released: issue numbers whose claim was released — it had no PR yet, or
                had outlived its issue
      kept:     issue numbers whose claim is still held: an open PR closes the
                issue, and a peer (or a later run) lands it once the lease lapses
      errors:   how many releases failed — those claims are still held too

    Returns {"state", "sleep_seconds", "progress"}. `sleep_seconds` is None:
    nothing follows a drain. One that met an error is `unsettled`, so a caller
    that does run it again is not told it has nothing to do.
    """
    new: CycleState = {**state, "in_flight": len(kept), "unsettled": bool(errors)}
    parts = [f"{word} {', '.join(f'#{n}' for n in numbers)}"
             for word, numbers in (("released", released), ("kept", kept)) if numbers]
    parts += [f"{errors} error{'' if errors == 1 else 's'}"] if errors else []
    return {"state": new, "sleep_seconds": None,
            "progress": "; ".join(["drained", *parts])}


def _standing(state: CycleState) -> str:
    return (f"{state['in_flight']} in flight, "
            f"{state['frontier_remaining']} left on the frontier")


# --------------------------------------------------------------------------- #
# The tick's routing — which transition each row gets, and what is a judgment  #
# --------------------------------------------------------------------------- #
#
# After `rebuild` gives each claim a `status` and `no-pr` an `action`, the next
# `afk` call is a table lookup: `afk cycle` runs it. What code cannot decide is
# RETURNED as a judgment — a question, and for each answer the one transition to
# run. Every answer is a transition, so the next cycle does not ask again.

JudgmentKind = Literal["empty_diff", "no_checks", "adversarial_verify", "reason"]
JUDGMENT_KINDS: tuple[JudgmentKind, ...] = get_args(JudgmentKind)

# The subcommands that may start a worker, and so take `--worker-command`: the
# parser adds the flag to exactly these, and `afk_command` writes it for them.
STARTS_WORKER = ("dispatch", "turn", "fail")


def afk_command(call: Call, sub: str, number: int, *flags: str) -> str:
    """One runnable `afk` transition on issue <number>, as a shell line.

      call:  {"afk_path", "repo", "config" (the run's config, as JSON),
              "instance", "worker_command"} — what every such line repeats
      flags: the transition's own, last — a `--reason` is the final argument
    """
    argv = [call["afk_path"], sub, "--issue", str(number), "--instance", call["instance"]]
    if sub in STARTS_WORKER:
        argv += ["--worker-command", call["worker_command"]]
    return shlex.join([*argv, "--repo", call["repo"], "--config", call["config"], *flags])


def judgment(kind: JudgmentKind, number: int, question: str, context: Obj, if_yes: str,
             if_no: str, bulky: bool = False) -> Judgment:
    """One judgment a tick returns instead of deciding. `bulky` marks one whose
    answer takes reading something long (a diff under review, a CI log): the
    caller delegates it to an ephemeral subagent that returns one line."""
    assert kind in JUDGMENT_KINDS, kind
    return {"issue": number, "kind": kind, "question": question, "context": context,
            "if_yes": if_yes, "if_no": if_no, **({"bulky": True} if bulky else {})}


def reason_judgment(call: Call, number: int, sub: Literal["fail", "escalate"], default: str,
                    where: str, context: Obj | None = None, bulky: bool = False) -> Judgment:
    """A `reason` judgment: the transition is already fixed — `afk fail` or `afk
    escalate` — and what is asked for is its wording. Both answers are therefore
    the SAME command, runnable as it stands with `default`; the answer is the
    text put in place of it, after `--reason`."""
    command = afk_command(call, sub, number, "--reason", default)
    doing = "failure" if sub == "fail" else "escalation"
    return judgment("reason", number,
                    f"Word the {doing} reason for issue #{number}, re-read from {where}, and put "
                    f"it in place of the text after --reason.",
                    {"where": where, **(context or {})}, command, command, bulky=bulky)


def asks_after(mine: list[MineRow]) -> list[int]:
    """The claims a tick asks `afk no-pr` about, in one call: every `no_pr` row,
    and every `landing` row whose worker has not stopped for the tick. A row in
    a merge batch is not one of them: its own worker has nothing to do, and the
    batch's worker is asked after once, for the batch."""
    return [r["number"] for r in mine
            if r["status"] == "no_pr"
            or (r["status"] == "landing" and not r["batch"]
                and r["stopped"] not in LAND_WAITS)]


def turn_due(mine: list[MineRow], merge_order: list[int]) -> int | None:
    """The ONE issue a tick runs `afk turn` on, or None: the head of the merge
    queue — unless its PR holds the turn and its worker is at it."""
    row = next((r for r in mine if merge_order and r["number"] == merge_order[0]), None)
    if row is None or (row["status"] == "landing" and row["stopped"] not in LAND_WAITS):
        return None
    return row["number"]


def failure_judgment(call: Call, row: MineRow) -> Judgment:
    """The judgment for a `failure` row — its PR's checks are red: the reason
    lives in a CI log, which is bulky to read."""
    return reason_judgment(call, row["number"], "fail", f"the checks of PR #{row['pr']} are red",
                           f"the failing checks of PR #{row['pr']}", {"pr": row["pr"]}, bulky=True)


def turn_step(call: Call, result: Obj, config: Config,
              restart: bool = False
              ) -> tuple[Literal["granted", "leave", "judge"], Judgment | None]:
    """
    What a tick does with `afk turn`'s result → (do, judgment):

      ("granted", None)   the worker was told: count it
      ("leave", None)     waiting / landing / awaiting_ci: nothing this tick
      ("judge", {...})    gate_red → `reason`; no_checks → `no_checks`;
                          needs_verify → `adversarial_verify`

    With an adversarial verify (`verifies`), a PR with no checks at all owes both
    judgments, and `afk turn` records neither until both are in: they are asked
    as ONE `adversarial_verify` whose yes carries both flags — a head that
    survives the verify is one whose acceptance criteria are met.

    `restart`: the result is `afk turn --restart`'s (a silent worker restarted
    onto its turn, ADR-0035), so a judgment's yes runs the restart, not a grant.
    """
    number, pr, head = result["issue"], result["pr"], result["head"]
    outcome: TurnOutcome = result["outcome"]
    if outcome == "granted":
        return "granted", None
    context = {"pr": pr, "head": head}
    again = ["--restart"] if restart else []
    if outcome == "gate_red":
        return "judge", reason_judgment(call, number, "fail", f"the checks of PR #{pr} are red",
                                        f"the failing checks of PR #{pr}", context, bulky=True)
    if outcome == "needs_verify" or (outcome == "no_checks" and verifies(config)):
        flags = [*(["--allow-no-checks"] if outcome == "no_checks" else []), "--verified", head]
        bare = " It has no checks at all, so the verify is its only gate." \
            if outcome == "no_checks" else ""
        return "judge", judgment(
            "adversarial_verify", number,
            f"Does head {head} of PR #{pr} survive the adversarial verify?{bare}",
            {**context, "prompt": config["gate"]["adversarial_verify_prompt"]},
            afk_command(call, "turn", number, *again, *flags),
            afk_command(call, "fail", number, "--reason",
                        f"the adversarial verify refuted head {head} of PR #{pr}"), bulky=True)
    if outcome == "no_checks":
        return "judge", judgment(
            "no_checks", number,
            f"PR #{pr} has no checks at all — are issue #{number}'s acceptance criteria met?",
            context, afk_command(call, "turn", number, *again, "--allow-no-checks"),
            afk_command(call, "fail", number, "--reason",
                        f"PR #{pr} has no checks, and it does not meet the issue's acceptance "
                        f"criteria"))
    if outcome in ("waiting", "landing", "awaiting_ci"):
        return "leave", None
    assert_never(outcome)


def _reason_on_record(cause: WorkerCause, row: MineRow, worker: WorkerRow) -> tuple[str, bool]:
    """The words a tick fails or escalates a claim with, for a cause that ends
    in one → (the words, whether they are the reason itself — False when the
    reason is not on record and must be asked for, and these stand in for it
    meanwhile). The cause picks the wording; what the verdict declared is read
    only for the words quoted."""
    verdict = worker["worker_verdict"]
    declared = verdict["reason"] if verdict else None
    if cause == "silent_past_restart":
        return (f"PR #{row['pr']} was judged ready and given the landing turn, its worker was "
                f"restarted onto the turn once, and the landing still did not happen: "
                f"{row['stopped'] or 'no `afk land` outcome'}. The PR, its branch and its "
                f"worktree are kept as they are"), True
    if cause == "blocker_unmet":
        unmet = [f"#{b['number']} {b['reason']}" for b in worker["blockers"]
                 if b["standing"] == "unmet"]
        return f"blocked by a dependency nothing will resolve: {'; '.join(unmet)}", True
    if cause == "no_blocker_named":
        said = declared
        return ((f"its worker reported blocked, naming no blocker: {said}", True) if said
                else ("its worker reported blocked without naming a blocker", False))
    if cause == "satisfied_refuted":
        return "its worker declared `already-satisfied`, but the branch holds changes", True
    if cause == "gave_up":
        said = declared
        return (f"its worker gave up: {said}", True) if said else ("its worker gave up", False)
    if cause == "needs_decision":
        # always on record: the decision itself is the verdict comment's body
        asks = f": {declared}" if declared else ""
        return (f"its worker found that the issue as written needs a decision from its owner"
                f"{asks}. A retry would stop at the same question, so none was spent — the "
                f"decision to make is in the worker's comment: "
                f"{verdict['comment_url'] if verdict else None}"), True
    if cause == "unknown_phase":
        return (f"its worker's verdict names no phase the fleet knows "
                f"({(verdict['phase'] if verdict else None)!r})"), True
    if cause == "silent_after_nudge":
        return "idle with no PR and no verdict a grace period after its nudge", True
    if cause == "silent_unnudgeable":
        return "idle with no PR and no verdict, and no worktree here to nudge it in", True
    raise ValueError(f"issue #{row['number']}: no reason is worded for a worker classified "
                     f"{cause!r}")


def worker_step(call: Call, row: MineRow, worker: WorkerRow, config: Config) -> tuple[WorkerStep, Any]:
    """
    What a tick does about one claim it asked after → (do, detail). `do` is the
    `step` of the cause its worker was classified with (WORKER_CAUSES), which
    is mapped and never re-derived:

      ("leave" | "dispatch" | "park" | "nudge" | "restart", None)
      ("escalate" | "fail", reason)   the reason is on record (`_reason_on_record`)
      ("judge", {...})                `satisfied` → `empty_diff`; a failure or an
                                      escalation whose reason is NOT on record →
                                      `reason`

      row:    the claim's `mine` row      worker: its classification plus what
                                          `afk no-pr` gathered for it

    Two rules sit on top of the table. A `starting` row — a retry already
    counted, cut short before its fresh worker started — is failed whatever its
    worker is, short of at work: the retry is finished, and nothing is counted
    again. And a `landing` row is never failed for silence: classified with one
    of the two causes that fail a PR-less claim, it holds no turn of one PR,
    which is an error (ADR-0035). A cause with no row is an error too.
    """
    number, cause = row["number"], worker["cause"]
    if cause not in WORKER_CAUSES:
        raise ValueError(f"issue #{number}: no route for a worker classified {cause!r}")
    step = WORKER_CAUSES[cause].step
    verdict = worker["worker_verdict"]
    said = {"verdict": verdict["comment_url"] if verdict else None}
    if step == "leave":
        return "leave", None
    if row["starting"] and step != "fail":
        # a retry cut short: whatever is here is the attempt it was discarding
        return "fail", "the retry of its failed attempt was cut short before a fresh worker started"
    if step in ("dispatch", "park", "nudge", "restart"):
        return step, None
    if step == "judge":
        base = config["base_branch"]
        return "judge", judgment(
            "empty_diff", number,
            f"Is the diff of issue #{number}'s branch against {base} really empty? Its worker "
            f"declared `already-satisfied`.",
            {"worktree": worker["worktree"], "base_branch": base, **said},
            afk_command(call, "close", number),
            afk_command(call, "fail", number, "--reason",
                        f"its worker declared `already-satisfied`, but the branch's diff "
                        f"against {base} is not empty"))
    if row["status"] == "landing" and cause in ("silent_after_nudge", "silent_unnudgeable"):
        raise ValueError(f"issue #{number}: a landing claim's silence is never a failure — "
                         f"its turn is restarted onto, then escalated (ADR-0035); a "
                         f"`landing` row classified {cause!r} holds no turn of one PR")
    words, on_record = _reason_on_record(cause, row, worker)
    if on_record:
        return step, words
    return "judge", reason_judgment(call, number, step, words, "its worker's verdict comment",
                                    said)


# --------------------------------------------------------------------------- #
# The tick's plan — which transitions a tick runs, and in what order           #
# --------------------------------------------------------------------------- #
#
# The routing above says what ONE row gets. How those answers are put together —
# the order they run in, the slot count, "at most one landing turn a tick", "a
# start that fails to begin ends the starting", which claims count as settled —
# is `tick_plan`. Later choices depend on earlier results (did the reclaim win,
# what did the turn answer), so the plan is asked in stages: it hands out ONE
# step, is told how that step ended, and only then says what comes next.
# `afk.py` runs what it is handed and reports back (`follow`); it holds no
# ordering rule and no count of its own (ADR-0017).

# Every step a plan hands out, as {"do": <key>, **arguments} → the name its
# failure is reported under in the cycle's `errors`.
TickStep = Literal["no-pr", "turn", "batch-turn", "abandon", "restart", "nudge", "park", "fail",
                   "escalate", "release", "reclaim", "begin", "finish", "heartbeat", "status",
                   "sweep"]
TICK_STEPS: dict[TickStep, str] = {
    "no-pr": "no-pr",           # {issues} | {batch}: `afk no-pr`'s rows for them
    "turn": "turn",             # {issue}: `afk turn`
    "batch-turn": "turn",       # `afk turn --batch`: form a merge batch, or continue mine
    "abandon": "turn",          # {batch}: `afk turn --abandon`
    "restart": "turn",          # {issue}: `afk turn --restart`
    "nudge": "nudge",           # {issue} | {batch}
    "park": "park",             # {issue}
    "fail": "fail",             # {issue, reason}
    "escalate": "escalate",     # {issue, reason}
    "release": "release",       # {issue}, + {expect_sha} for a dead peer's phantom lock
    "reclaim": "reclaim",       # {issue, sha}
    "begin": "dispatch",        # {issue}: `afk dispatch` up to the agent's terminal → BEGUN | LOST
    "finish": "dispatch",       # {issues}: the rest of every start begun, at once
    "heartbeat": "heartbeat",
    "status": "status",         # {issue, phase, pr, attempt}
    "sweep": "sweep",           # {live}: what a finished merge batch left behind
}

# How beginning a dispatch ended. A `begin` step answers with the first two;
# the third is a start that raised, or was not tried.
StartOutcome = Literal["begun", "lost", "failed"]
START_OUTCOMES: tuple[StartOutcome, ...] = get_args(StartOutcome)
BEGUN, LOST, FAILED = START_OUTCOMES
#   BEGUN   the claim is held and the agent's terminal is open
#   LOST    a peer won the claim: nothing was started

# How a step a plan handed out ended: (its result, None), or (None, what it raised).
Answer = tuple[Any, Optional[str]]
# A plan, or a stage of one: it yields steps, is sent each one's answer, and
# returns what the stage came to.
Plan = Generator[Obj, Answer, Any]


class TickBooks:
    """One tick's books, shared by the stages of its plan: the working set it
    acts on, what it has done so far, the judgments it hands back and the steps
    that failed. A stage records what happened — `run`, `did`, `begin`, `take`
    — and every count is read back from that record: nothing a tick reports
    (`account`) is kept twice."""

    SETTLES: tuple[TickDid, ...] = ("parked", "escalated", "cleared")   # these release the claim
    WRITES_BOARD: tuple[TickDid, ...] = ("granted", "abandoned", "retried", "dispatched",
                                         "reclaimed", "restarted")

    def __init__(self, ws: WorkingSet) -> None:
        self.ws = ws
        self.mine: dict[int, MineRow] = {r["number"]: r for r in ws["mine"]}
        self.judgments: list[Judgment] = []
        self.errors: list[Obj] = []
        self.starting = True            # False once a start failed to begin
        # (the list it joins, issue) of each start begun
        self.begun: list[tuple[TickDid, int]] = []
        # (a TICK_DID key, issue), in the order it happened
        self._done: list[tuple[TickDid, int]] = []
        self._took: set[int] = set()            # stale claims taken from a dead peer
        self._off_frontier: set[int] = set()    # frontier issues begun, or lost to a peer
        self._fresh: set[int] = set()           # frontier issues whose start was begun

    def run(self, do: TickStep, **args: Any) -> Plan:
        """Hand out one step and wait for its answer → its result, None when it
        failed. One that failed is recorded in `errors` and the tick goes on."""
        result, error = yield {"do": do, **args}
        if error is not None:
            self.failed(do, args.get("issue"), error)
        return result

    def failed(self, do: TickStep, number: int | None, error: str) -> None:
        self.errors.append({"step": TICK_STEPS[do], **({"issue": number} if number else {}),
                            "error": error})

    def did(self, what: TickDid, *numbers: int) -> None:
        """Record that `what` — a key of TICK_DID — happened to these issues."""
        self._done += [(what, n) for n in numbers]

    def take(self, number: int) -> None:
        """Record a stale claim taken from a dead peer: held from here, started or not."""
        self._took.add(number)

    def begin(self, number: int, counted: TickDid, outcome: StartOutcome,
              frontier: bool = False) -> None:
        """Record how beginning one start ended — a START_OUTCOMES. `counted` is
        the list the issue joins once its worker runs; `frontier` says it came
        off the frontier."""
        if outcome == FAILED:
            self.starting = False       # orca or the remote is unwell: no further start
            return
        if frontier:
            self._off_frontier.add(number)
        if outcome == BEGUN:
            self.begun.append((counted, number))
            if frontier:
                self._fresh.add(number)

    def _numbers(self, *whats: TickDid) -> list[int]:
        return [n for what, n in self._done if what in whats]

    @property
    def settled(self) -> set[int]:
        """My claims this tick released."""
        return set(self._numbers(*self.SETTLES)) & set(self.mine)

    @property
    def touched(self) -> set[int]:
        """The claims whose status board a transition of this tick wrote."""
        return set(self._numbers(*self.WRITES_BOARD))

    @property
    def held(self) -> set[int]:
        """The claims whose status board is still mine to remember."""
        return (set(self.mine) - self.settled) | set(self._numbers("dispatched", "reclaimed"))

    @property
    def slots(self) -> int:
        """The dispatch slots still free for the frontier."""
        return (self.ws["free_slots"] + len(self.settled) - len(self._took)
                - len(self._fresh))

    @property
    def in_flight(self) -> int:
        """The claims this fleet holds: a frontier issue counts once its worker runs."""
        staffed = self._fresh & set(self._numbers("dispatched"))
        return len(self.mine) - len(self.settled) + len(self._took) + len(staffed)

    @property
    def frontier_remaining(self) -> int:
        return len(self.ws["frontier"]["dispatch"]) - len(self._off_frontier)

    def account(self) -> Obj:
        """What the tick did, as `cycle_ticked` reads it: the issues per TICK_DID
        key, and the TICK_COUNTS."""
        return {**{k: self._numbers(k) for k in TICK_DID},
                "in_flight": self.in_flight, "frontier_remaining": self.frontier_remaining}


def tick_plan(ws: WorkingSet, call: Call, config: Config) -> Plan:
    """
    One reconciliation pass over the working set `ws`, as the steps to run — a
    generator: each value it yields is ONE step ({"do": a TICK_STEPS key,
    **arguments}), and it is sent that step's answer before it says the next.

      ws:     the working set (`assemble_working_set`)
      call:   what a judgment's commands are built from (`afk_command`); its
              `instance` is the fleet instance the tick runs as
      config: the canonical config

      answer: (result, None) when the step was carried out — `result` is what
              its transition returned, a START_OUTCOMES word for `begin` — or
              (None, "<what it raised>") when it failed. `finish`'s result is
              itself a list of such pairs, one per issue, in the order given:
              one start may fail where the others ran

    Returns (as the generator's value) {"did": the tick's account for
    `cycle_ticked`, "judgments", "errors", "held": the claims whose status board
    is still mine to remember}.

    In order: `no-pr` for the claims waiting on a worker → the landing turn, to
    one PR or to one merge batch (`_turn_plan`) → nudge / restart / fail / park /
    escalate where the reason is on record, or the judgment that stands in for one →
    release `closed` rows and `stale_closed` phantom locks → begin the starts:
    continuations of claims already held, each `stale` claim reclaimed, then the
    frontier into the free slots → finish every start begun, at once → heartbeat
    → status boards → what a finished batch left behind.

    A step that fails is recorded in `errors` and settles nothing: its claim is
    still held and the rest of the tick goes on. A start that fails to BEGIN
    ends the starting for this tick — it is orca or the remote that is unwell,
    and every further dispatch would take a claim it cannot staff. A claim
    settled earlier in the tick frees its slot for the frontier; a stale claim
    taken uses one; a frontier issue a peer won comes off the frontier and uses
    none.
    """
    tick = TickBooks(ws)
    run = tick.run

    # --- observe: the claims waiting on their worker ---
    asked = asks_after(ws["mine"])
    seen = (yield from run("no-pr", issues=asked)) if asked else None
    routes = [(w["issue"], *worker_step(call, tick.mine[w["issue"]], w, config))
              for w in (seen or {}).get("workers", [])]

    # --- the landing turn: at most one grant a tick, to one PR or to one merge batch ---
    live = yield from _turn_plan(tick, call, config)

    # --- nudge / restart / fail / park / escalate, or the judgment that stands in for one ---
    tick.judgments += [failure_judgment(call, r) for r in ws["mine"] if r["status"] == "failure"]
    for number, do, detail in routes:
        if do == "judge":
            tick.judgments.append(detail)
        elif do == "nudge":
            if (yield from run("nudge", issue=number)):
                tick.did("nudged", number)
        elif do == "restart":
            again = yield from run("restart", issue=number)
            if again:
                told, asks = turn_step(call, again, config, restart=True)
                if told == "granted":
                    tick.did("restarted", number)
                elif asks:      # "judge"
                    tick.judgments.append(asks)
        elif do == "park":
            if (yield from run("park", issue=number)):
                tick.did("parked", number)
        elif do in ("fail", "escalate"):
            done = yield from run(do, issue=number, reason=detail)
            if done:        # a failure past its last retry is an escalation
                tick.did("escalated" if done["action"] == "escalate" else "retried", number)
        elif do not in ("leave", "dispatch"):       # a dispatch is begun below, with the starts
            assert_never(do)

    # --- release what outlived its issue ---
    for row in ws["mine"]:
        if row["status"] == "closed" and (yield from run("release", issue=row["number"])):
            tick.did("cleared", row["number"])
    for row in ws["stale_closed"]:
        if (yield from run("release", issue=row["number"], expect_sha=row["sha"])):
            tick.did("cleared", row["number"])

    # --- start workers: continuations of claims already held, then the frontier ---
    def begin(number: int, counted: TickDid, frontier: bool = False) -> Plan:
        """Begin one start; the claim is held from here unless it failed or a peer won it."""
        outcome = (yield from run("begin", issue=number)) if tick.starting else None
        tick.begin(number, counted, outcome or FAILED, frontier=frontier)

    for number, do, _ in routes:
        if do == "dispatch":
            yield from begin(number, "dispatched")
    for row in ws["stale"]:
        took = (yield from run("reclaim", issue=row["number"], sha=row["sha"])) \
            if tick.starting else None
        if took and took["won"]:
            tick.take(row["number"])
            yield from begin(row["number"], "reclaimed")
    for issue in ws["frontier"]["dispatch"]:
        if tick.slots <= 0 or not tick.starting:
            break
        yield from begin(issue["number"], "dispatched", frontier=True)
    if tick.begun:      # every agent is waited for and handed its prompt at once
        workers = yield from run("finish", issues=[n for _, n in tick.begun])
        for (counted, number), (worker, error) in zip(tick.begun, workers or []):
            if error is not None:
                tick.failed("finish", number, error)
            elif worker:
                tick.did(counted, number)

    # --- the lease, and what a human reads on each issue ---
    if tick.in_flight:
        yield from run("heartbeat")
    written = tick.settled | tick.touched
    for row in ws["mine"]:
        if row["board_phase"] and row["number"] not in written:
            yield from run("status", issue=row["number"], phase=row["board_phase"],
                           pr=row["pr"], attempt=row["attempt"])
    if batches_form(config) or ws["batches"]:
        yield from run("sweep", live=sorted(live))
    return {"did": tick.account(), "judgments": tick.judgments, "errors": tick.errors,
            "held": tick.held}


def _turn_plan(tick: TickBooks, call: Call, config: Config) -> Plan:
    """The landing-turn stage of `tick_plan` → the ids of the merge batches that
    hold a turn when it is done; what it did and what it could not decide go
    into `tick`. One turn is out at a time, held by one PR or by one batch
    (ADR-0029), so exactly one of these happens:

      a dead fleet's batch on claims I took   abandoned; nothing is granted until
                                              the next cycle reads the result
      my batch holds the turn                 its worker is asked after: left,
                                              continued, nudged once, or the
                                              batch abandoned
      two or more PRs are eligible            a batch is formed (`afk turn --batch`)
      otherwise                               the head of the merge queue gets
                                              the turn, as before"""
    ws, run, instance = tick.ws, tick.run, call["instance"]

    def abandon(batch: Batch) -> Plan:
        gone = yield from run("abandon", batch=batch["id"])
        if gone:
            tick.did("abandoned", *gone["issues"])
        return gone

    who, what = turn_holder(ws, instance)
    live = {b["id"] for b in ws["batches"] if b["instance"] == instance}
    if who == "dead":
        for batch in what:
            if not (yield from abandon(batch)):
                live.add(batch["id"])
        return live
    if who == "mine":
        mine = what
        seen = yield from run("no-pr", batch=mine["id"])
        do = batch_step(seen["workers"][0]) if seen else "leave"
        if do == "continue":
            again = yield from run("batch-turn")
            if again and again["outcome"] == "granted":
                tick.did("granted", *again["issues"])
        elif do == "nudge":
            if (yield from run("nudge", batch=mine["id"])):
                tick.did("nudged", *(m["issue"] for m in mine["members"]))
        elif do == "abandon":
            if (yield from abandon(mine)):
                live = set()
        elif do != "leave":
            assert_never(do)
        return live
    if batch_candidates(ws["mine"], ws["merge_order"], config):
        formed = yield from run("batch-turn")
        if formed is None:
            return live
        if formed["outcome"] == "granted":
            tick.did("granted", *formed["issues"])
            return {formed["batch"]}
    due = turn_due(ws["mine"], ws["merge_order"])
    single = (yield from run("turn", issue=due)) if due else None
    if due and single:
        do, asks = turn_step(call, single, config)
        if do == "granted":
            tick.did("granted", due)
        elif asks:      # "judge"
            tick.judgments.append(asks)
    return live


def follow(plan: Plan, carry_out: Callable[[Obj], Answer]) -> Any:
    """Carry a `tick_plan` out → what it returns. `carry_out(step)` performs one
    step and answers as the plan expects; it is all the effectful side supplies."""
    try:
        step = next(plan)
        while True:
            step = plan.send(carry_out(step))
    except StopIteration as end:
        return end.value


# --------------------------------------------------------------------------- #
# Worker launch command — launcher/worker provider parity (ADR-0010)           #
# --------------------------------------------------------------------------- #
#
# A launcher started through a provider wrapper (`ckimi`, `csk`, a direnv, a
# script) carries that provider only in its ENV — the wrapper's NAME is gone by
# the time the process exists, and its argv is byte-identical to a stock
# `claude`. A worker orca starts in a fresh login shell inherits none of that
# env, so it silently falls back to stock Anthropic and runs that way,
# unattended, for days.
#
# What travels is therefore an OPAQUE command string the fleet never parses and
# never composes — supplied by the human, with the invocation or at bootstrap.
# That choice is what keeps the credential out of the fleet
# entirely: no env is copied, nothing is written to disk, no argv carries a key,
# and the fleet is coupled to no particular secret manager. What code *can*
# settle, it does: whether to ask at all (a stock launcher is never asked), which
# wrappers exist to offer, and whether the answer resolves to something runnable.

WORKER_COMMAND_DEFAULT = "claude --dangerously-skip-permissions"
WORKER_COMMAND_DEFAULT_QODERCN = "qoderclicn --dangerously-skip-permissions"

# orca's per-agent unattended flags. Used ONLY to warn: a worker started without
# one parks on a permission prompt forever, and the fleet reads that as a silent
# no-PR claim. Checked against the RESOLVED text (an alias hides its own flags).
YOLO_FLAGS = ("--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox",
              "--yolo", "--yes-always", "--dangerously-allow-all", "--trust-all-tools",
              "--unrestricted", "--auto-approve")


Runtime = Literal["claude", "qoderclicn"]


def detect_runtime(env: Mapping[str, str]) -> Runtime:
    """The agent runtime a launcher with environment `env` runs under —
    'qoderclicn' or 'claude'. One fleet instance runs one runtime (ADR-0014):
    qoderclicn sets QODERCN_CLI=1 in every child process; its absence means
    Claude (the default)."""
    if (env.get("QODERCN_CLI") or "").strip() in ("1", "true"):
        return "qoderclicn"
    return "claude"


_ALIAS_RE = re.compile(r"^(?:alias\s+)?([^=\s]+)=(.*)$")


def first_word(command: str | None) -> str:
    """The token whose resolvability decides whether the command can run at all."""
    parts = (command or "").strip().split()
    return parts[0] if parts else ""


def parse_aliases(text: str | None) -> dict[str, str]:
    """Shell `alias` output → {name: expansion}. Accepts both zsh's `n='v'` and
    bash's `alias n='v'`. Quotes are stripped; the expansion is only ever shown
    to a human or substring-searched, never executed by the fleet."""
    out = {}
    for line in (text or "").splitlines():
        m = _ALIAS_RE.match(line.strip())
        if not m:
            continue
        name, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        out[name] = val
    return out


def launch_candidates(aliases: Mapping[str, str] | None) -> list[Obj]:
    """The shell aliases that start a Claude Code, as {name, expansion, wraps_env}
    — what the bootstrap offers the human. `wraps_env` marks the ones that do more
    than run `claude` bare, i.e. the ones that could carry a provider; a stock
    launcher's own alias is listed too, and marked False, rather than guessed at.
    Deliberately NOT a mapping from the launcher's provider to one alias: inferring
    that means executing each wrapper's env prefix, which would decrypt every
    provider's credentials to answer a question a human answers in one word."""
    out = []
    for name, exp in sorted((aliases or {}).items()):
        if "claude" not in exp:
            continue
        bare = exp.split()[0] == "claude" if exp.split() else False
        out.append({"name": name, "expansion": exp, "wraps_env": not bare})
    return out


def resolve_worker_command(base_url: str | None, supplied: str | None = None,
                           resolved: str | None = None, runtime: Runtime = "claude") -> Obj:
    """
    Settle the one string every worker is started with.

      runtime:  `detect_runtime`'s answer. qoderclicn has no custom providers, so
                it is always `stock` with its own default — never asked, and a
                supplied command is ignored (ADR-0014).
      base_url: the launcher's own ANTHROPIC_BASE_URL (None/"" = stock Anthropic).
      supplied: the human's answer, verbatim, or None if they haven't been asked.
      resolved: what the shell says `first_word(supplied)` is — the `type` output
                (an alias's full expansion, a function body, a path), or None if
                it resolves to nothing. Only meaningful when `supplied` is given.

    Returns {status, command, base_url, first_word, yolo, detail, runtime}. `command` is
    non-null only when the run may proceed — every other status is the launcher's
    cue to ask (again), never to quietly fall back to stock.

      stock       no custom provider and nothing supplied → the default command,
                  and the human is not asked at all.
      confirmed   a supplied command whose first word resolves → use it verbatim.
      unresolved  a supplied command whose first word resolves to nothing (the
                  typo case: `ckim`). Left unchecked this starts no worker, so the
                  claim goes PR-less into the retry ladder and escalates — three
                  issues burnt on a missing letter.
      ask         a custom provider and no answer yet.

    `yolo` is advisory, not a gate: True if an unattended flag is visible in the
    resolved text, False if it plainly is not, None when the resolution can't show
    it (a script path). A False is worth a warning — a worker that stops on a
    permission prompt is indistinguishable to the fleet from one that finished.
    """
    if runtime == "qoderclicn":
        base_url, supplied = None, None
    fw = first_word(supplied) if supplied else ""

    def out(status: str, command: str | None = None, yolo: bool | None = None,
            detail: str = "") -> Obj:
        return {"status": status, "command": command, "base_url": base_url or None,
                "first_word": fw or None, "yolo": yolo, "detail": detail, "runtime": runtime}

    if runtime == "qoderclicn":
        fw = first_word(WORKER_COMMAND_DEFAULT_QODERCN)
        return out("stock", command=WORKER_COMMAND_DEFAULT_QODERCN, yolo=True)
    if supplied:
        if not resolved:
            return out("unresolved", detail=f"{fw!r} resolves to nothing in an interactive shell")
        # An alias resolution shows its whole expansion and `claude` is its own
        # whole story, so a missing flag there is a fact. A path or a function in
        # another file could carry the flag inside — that is unknown, not absent.
        seen_whole = " is an alias for " in resolved or fw == "claude"
        visible = f"{supplied}\n{resolved}"
        yolo = True if any(f in visible for f in YOLO_FLAGS) else (False if seen_whole else None)
        return out("confirmed", command=supplied.strip(), yolo=yolo, detail=resolved.strip())
    if not (base_url or "").strip():
        return out("stock", command=WORKER_COMMAND_DEFAULT, yolo=True)
    return out("ask", detail=f"launcher is on a custom provider ({base_url})")


# --------------------------------------------------------------------------- #
# Fingerprint gate — skip ticks code can prove are no-ops (ADR-0007)           #
# --------------------------------------------------------------------------- #
#
# A tick is a rebuild and a pass of transitions: a run of gh calls, just to
# conclude "still waiting", most cycles of a run. The gate collapses everything a
# tick's Rebuild observes into a short digest; `afk cycle` runs a tick only
# when the digest moved (or a forced full pass is due). A false "changed" costs
# one tick; a missed change waits at most `force_after`
# cycles. Correctness never depends on the gate.

def fingerprint(issues: Iterable[Issue], prs: Iterable[PullRequest],
                claims: Iterable[Claim]) -> str:
    """
    Digest the observable fleet inputs — open issues (number + labels +
    updatedAt + open-blocker count, so label churn, closes, fresh blocker
    comments and a dependency edge all move it), open PRs (number + head sha +
    updatedAt + `pr_checks_state` + the issues it closes, so pushes, CI finishing
    and a PR becoming an issue's all move it), and claim refs (number + sha, so peer claims/releases/reclaims move it).

    A PR's checks enter as the ONE word a tick acts on — green / red / pending /
    none — not check by check: a check going queued → in progress, or the first
    of several finishing green, leaves the claim `awaiting_ci` and so leaves the
    digest alone. Only the verdict changing moves it.

    The issues a PR closes are in its row because a claim has a PR only through
    them (`closing_pr`), and GitHub may list a new PR before it lists what the PR
    closes: that link arriving must read as a change, or the PR waits for the
    forced tick.

    Heartbeats are deliberately NOT an input: the launcher refreshes its own
    lease on skipped cycles, which would move the digest every cycle and defeat
    the gate — and a lease *expiring* is a time-driven event no state hash can
    see anyway. The forced tick covers those.

    Canonicalizes (sorts, keeps only the fields above) so row order and extra
    fields never move the digest. Returns a 16-hex digest.
    """
    canon = {
        "issues": sorted([i["number"], sorted(i["labels"] or []), i["updatedAt"] or "",
                          int(i["blocked_by"] or 0)]
                         for i in issues),
        "prs": sorted([p["number"], p["headRefOid"] or "", p["updatedAt"] or "",
                       pr_checks_state(p["statusCheckRollup"]) or "none",
                       sorted(ref["number"] for ref in p["closingIssuesReferences"] or [])]
                      for p in prs),
        "claims": sorted([c["number"], c["sha"] or ""] for c in claims),
    }
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def fingerprint_gate(last: str | None, current: str, skips: int, force_after: int) -> Obj:
    """
    Skip-or-tick verdict for one launcher cycle.

      last:        the previous cycle's digest ("" / None on the first cycle)
      current:     the digest just computed
      skips:       consecutive skipped cycles so far
      force_after: run a full tick at least every N skips (>= 1)

    Returns {"action": "tick"|"skip", "reason": "first"|"changed"|"forced"|
    "unchanged", "skips": <new streak>} — `cycle_wake` folds both into the cycle
    state the launcher carries.
    """
    if not last:
        return {"action": "tick", "reason": "first", "skips": 0}
    if current != last:
        return {"action": "tick", "reason": "changed", "skips": 0}
    if skips + 1 >= int(force_after):
        return {"action": "tick", "reason": "forced", "skips": 0}
    return {"action": "skip", "reason": "unchanged", "skips": skips + 1}


# --------------------------------------------------------------------------- #
# Working-set assembly — the Rebuild's deterministic half (ADR-0008)           #
# --------------------------------------------------------------------------- #
#
# One pure function turns the raw observables (issues, PRs, claim/heartbeat
# refs) into the tick's whole working set: graft the
# eligibility facts, match each claim to its PR, partition mine/peer_live/stale.
# The gh/git gather lives in afk.py; why a `no_pr` claim has no PR is a separate,
# machine-dependent question (`afk no-pr`).

_CHECK_RED = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED",
              "STARTUP_FAILURE", "ERROR"}
_CHECK_OK = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def pr_checks_state(rollup: Iterable[Obj] | None) -> ChecksState | None:
    """Collapse a gh statusCheckRollup into "green" | "red" | "pending" | None.
    None = no checks at all — the progressive gate's "no CI yet" case, which the
    tick judges. Accepts CheckRun rows (status/conclusion) and StatusContext
    rows (state). Any red conclusion wins; anything not conclusively ok
    (running, PENDING, STALE, unknown) holds the verdict at pending."""
    if not rollup:
        return None
    state: ChecksState = "green"
    for c in rollup:
        concl = (c.get("conclusion") or c.get("state") or "").upper()
        if concl in _CHECK_RED:
            return "red"
        if concl not in _CHECK_OK:
            state = "pending"
    return state


_CLOSING_KEYWORD = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+#(\d+)\b", re.I)


def issues_closed_by(body: str | None, linked: Iterable[IssueRef] | None) -> list[IssueRef]:
    """The issues a PR closes: the ones GitHub links to it, and the ones its body
    names with a closing keyword (`Closes #7`). GitHub reads the keyword only on
    a PR against the repo's default branch — against any other base it links
    nothing and closes nothing — so the body is read here, and the answer is the
    same whatever branch the fleet lands on."""
    numbers = {ref["number"] for ref in linked or []}
    numbers.update(int(n) for n in _CLOSING_KEYWORD.findall(body or ""))
    return [{"number": n} for n in sorted(numbers)]


def _closing_pr_map(prs: Iterable[PullRequest]) -> dict[int, PullRequest]:
    """issue number → the open PR that closes it. When several do, the highest
    PR number wins — the latest attempt is the live one."""
    m: dict[int, PullRequest] = {}
    for p in prs:
        for ref in p["closingIssuesReferences"] or []:
            n = ref["number"]
            cur = m.get(n)
            if cur is None or p["number"] > cur["number"]:
                m[n] = p
    return m


def closing_pr(prs: Iterable[PullRequest], number: int) -> PullRequest | None:
    """The open PR that closes issue <number> (the latest, when several do), or None."""
    return _closing_pr_map(prs).get(number)


def unseen_prs(mine: list[MineRow], prs: Iterable[PullRequest]) -> list[int]:
    """The claims whose PR a tick did not act on: the issue numbers of the `mine`
    rows it worked from whose closing PR, among the open PRs `prs` read as it
    ended, is not the one the row names — opened, or replaced, while it ran."""
    now = _closing_pr_map(prs)
    return sorted(r["number"] for r in mine
                  if r["number"] in now and now[r["number"]]["number"] != r["pr"])


def superseded_prs(prs: Iterable[PullRequest] | None, number: int) -> list[PullRequest]:
    """The open PRs a FRESH start of issue <number> supersedes: the ones that
    close it from a branch shaped like the fleet's own (`branch_regex`). A PR a
    human opened from some other branch is never one of them — the fleet closes
    only what the fleet opened."""
    rx = branch_regex(number)
    return [p for p in prs or []
            if any(ref["number"] == number for ref in p["closingIssuesReferences"] or [])
            and rx.match(p["headRefName"] or "")]


def assemble_working_set(issues: list[Issue], prs: list[PullRequest], claims: list[Claim],
                         heartbeats: Mapping[str, float], me: str, now: float, config: Config,
                         closed: Iterable[int] = (),
                         turns: Mapping[int, Turn] | None = None) -> WorkingSet:
    """
    The tick's whole working set from the raw observables. Pure — afk.py's
    `rebuild` gathers, this assembles, and a fixture pins the join.

      issues:      open issues {number, title, labels: [name...], updatedAt,
                   blocked_by: <open blocker count>}
      prs:         gh pr list rows (number, headRefOid, updatedAt,
                   statusCheckRollup, closingIssuesReferences)
      claims:      [{"number","instance","sha",...}]  (ref-scan shape)
      heartbeats:  {instance: last_ts}
      me, now:     my instance id / epoch seconds
      config:      the canonical config — read for ready_label, epic_labels,
                   gate.ci and concurrency
      closed:      the numbers of the claims whose issue is closed (`issues` holds
                   only open ones, so `afk rebuild` asks about each claim that is
                   missing from it). One of mine becomes a `closed` row; a stale
                   peer's moves from `stale` to `stale_closed`
      turns:       {number: its PR's `latest_turn`} for MY claims whose PR
                   carries a turn marker, whoever wrote it (`afk rebuild` asks
                   about each of mine that has a PR)

    Returns:
      {"frontier": {"dispatch": [{"number","title"}...], "excluded": [...]},
       "mine": [{"number","title","status","board_phase","pr","checks",
                 "attempt","starting","stopped","batch","unbatched"}...],
       "merge_order": [number...],   # the `landing` and `awaiting_turn` rows (`turn_order`)
       "batches": [{"id","instance","members":[{"issue","pr"}...],"phase","at"}...],
       "peer_live": [{"number","instance"}...],
       "stale": [{"number","instance","sha"}...],   # sha feeds reclaim --expect-sha
       "stale_closed": [{"number","instance","sha"}...],  # sha feeds release --expect-sha
       "free_slots": <how many workers may be dispatched: concurrency - len(mine)>,
       "fingerprint": <digest of the same observables the gate hashes>,
       "now": now}

    `status` is `claim_status`'s and `board_phase` what BOARD_PHASE_OF shows for it; `attempt` is
    `current_attempt` — the number `afk status` takes; `starting` is
    `attempt_starting` — a retry cut short, for `afk fail` to finish; `stopped` is the
    LAND_OUTCOMES word a `landing` row's `afk land` last stopped with, None while
    it has not stopped and on every other row. `batch` is {"id", "members":
    [issue...], "phase"} on a `landing` row whose turn is a merge batch's, else
    None — such a row's board is written by the transitions that move the batch,
    so its `board_phase` is None; `unbatched` is the UNBATCHED word of a PR that
    left a batch without landing, else None.

    `batches` is every merge batch a turn marker on one of MY claims' PRs names.
    At most one is mine and holds my turn; one whose `instance` is not me is a
    dead fleet's, on claims I took — to be abandoned (`afk turn --abandon`).

    `stale` holds only work to continue: a stale claim on an OPEN issue. One whose
    issue is already closed — its worker landed it, or its fleet closed it, and the
    fleet died before releasing — is a phantom lock with nothing behind it, and is listed in
    `stale_closed` instead, to be deleted rather than taken and dispatched.
    """
    ttl, ci_mode = CLAIM_LEASE_TTL_SECONDS, config["gate"]["ci"]
    by_num = {i["number"]: i for i in issues}
    pr_for = _closing_pr_map(prs)

    ready = select_frontier(_eligibility_rows(issues, prs, claims),
                            config["ready_label"], config["epic_labels"])
    frontier: Frontier = {
        "dispatch": [{"number": n, "title": by_num[n]["title"]}
                     for n in ready["dispatch"]],
        "excluded": ready["excluded"]}

    part = classify_claims(claims, heartbeats, me, now, ttl)
    by_claim = {c["number"]: c for c in claims}
    closed = set(closed)

    def stale_rows(numbers: Iterable[int]) -> list[StaleClaim]:
        return [{"number": n, "instance": by_claim[n]["instance"], "sha": by_claim[n]["sha"]}
                for n in numbers]

    turns = turns or {}
    mine: list[MineRow] = []
    batches: dict[str, Batch] = {}
    for n in part["mine"]:
        pr = pr_for.get(n)
        checks = pr_checks_state(pr["statusCheckRollup"]) if pr else None
        issue = by_num.get(n)                # None: closed — only open issues are gathered
        labels = issue["labels"] if issue else None
        turn = turns.get(n)
        held = held_turn(turn, me)
        status = claim_status(pr is not None, checks, ci_mode,
                              closed=n in closed, landing=bool(held))
        landing = held if status == "landing" else None
        batch = landing["batch"] if landing else None
        if turn and turn["batch"] and not turn["released"] and n not in closed:
            seen = batches.setdefault(turn["batch"], {
                "id": turn["batch"], "instance": turn["instance"], "members": turn["members"],
                "phase": turn["phase"], "at": turn["at"]})
            seen["at"] = max(seen["at"] or 0, turn["at"] or 0) or None
        mine.append({"number": n, "title": issue["title"] if issue else None,
                     "status": status, "board_phase": None if batch else BOARD_PHASE_OF[status],
                     "pr": pr["number"] if pr else None, "checks": checks,
                     "attempt": current_attempt(labels),
                     "starting": attempt_starting(labels),
                     "stopped": landing["stopped"] if landing else None,
                     "batch": {"id": batch, "members": [m["issue"] for m in landing["members"]],
                               "phase": landing["phase"]} if landing and batch else None,
                     "unbatched": turn["unbatched"] if turn else None})

    return {"frontier": frontier,
            "mine": mine,
            "merge_order": turn_order(mine),
            "batches": [batches[k] for k in sorted(batches)],
            "peer_live": [{"number": n, "instance": by_claim[n]["instance"]}
                          for n in part["peer_live"]],
            "stale": stale_rows(n for n in part["stale"] if n not in closed),
            "stale_closed": stale_rows(n for n in part["stale"] if n in closed),
            "free_slots": max(0, int(config["concurrency"]) - len(mine)),
            "fingerprint": fingerprint(issues, prs, claims),
            "now": now}
