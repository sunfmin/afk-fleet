"""Offline regressions for progressive gates, exact-head evidence and shell lookup."""
import os
from pathlib import Path
import shutil
from unittest.mock import patch

import pytest

import afk
from test_afk_cli import ME, NOW, R, _merge, git, issue, local_gate, with_pr, world


def _merge_in_process(w, args, **patches):
    """Inject deterministic races at effect boundaries while retaining real git."""
    from contextlib import ExitStack

    a = afk.build_parser().parse_args([*args, "--config", "{}"])
    cwd = os.getcwd()
    try:
        os.chdir(w.cwd)
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, w.env, clear=True))
            stack.enter_context(patch.object(afk, "_GIT_ENV", w.env))
            for name, replacement in patches.items():
                stack.enter_context(patch.object(afk, name, replacement))
            return afk.cmd_merge(a)
    finally:
        os.chdir(cwd)


def test_no_checks_rebuild_reaches_explicit_progressive_gate():
    with world(issues=[issue(1, "ready-for-agent")]) as w:
        _, head = with_pr(w, 1, 10, conclusion=None)
        [row] = w.afk("rebuild", *ME, *R, *NOW)["mine"]
        assert (row["status"], row["board_phase"], row["checks"]) == (
            "awaiting_merge", "pr_open", None)
        result = w.afk(*_merge(1))
        assert result["outcome"] == "no_checks"
        assert w.claimed_by(1) == "me" and w.pr(10).get("state", "open") == "open"
        assert w.afk(*_merge(1, "--allow-no-checks"))["head"] == head
        assert w.pr(10)["state"] == "merged"


@pytest.mark.parametrize("old_checks", ["SUCCESS", None])
def test_old_check_snapshot_cannot_certify_a_newer_head(old_checks):
    with world(issues=[issue(1, "ready-for-agent")]) as w:
        d, old = with_pr(w, 1, 10, conclusion=old_checks)
        original = afk._open_prs
        advanced = []

        def read_then_push(repo):
            rows = original(repo)
            advanced.append(w.work(d["worktree"], "untested.txt"))
            state = w.state()
            state["prs"][0]["statusCheckRollup"] = [
                {"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}]
            w.set(prs=state["prs"])
            return rows

        result = _merge_in_process(w, _merge(1, "--allow-no-checks"),
                                   _open_prs=read_then_push)
        assert result["outcome"] == "awaiting_ci"
        assert result["checks_head"] == old and result["head"] == advanced[0] != old
        assert result["synced"] is False
        assert w.claimed_by(1) == "me" and w.pr(10).get("state", "open") == "open"
        assert not any(c[:2] == ["pr", "merge"] for c in w.calls())
        # A new, head-matched read observes that head's actual red verdict.
        assert w.afk(*_merge(1, "--allow-no-checks"))["outcome"] == "gate_red"


def test_missing_check_snapshot_head_fails_closed():
    with world(issues=[issue(1, "ready-for-agent")]) as w:
        with_pr(w, 1, 10)
        original = afk._open_prs

        def missing_head(repo):
            rows = original(repo)
            rows[0].pop("headRefOid")
            return rows

        result = _merge_in_process(w, _merge(1), _open_prs=missing_head)
        assert result["outcome"] == "awaiting_ci" and result["checks_head"] is None
        assert w.pr(10).get("state", "open") == "open"


@pytest.mark.parametrize("sync", [True, False])
@pytest.mark.parametrize("dirty", ["tracked", "staged", "untracked"])
def test_gate_rejects_uncommitted_inputs_even_without_sync(sync, dirty):
    with world(issues=[issue(1, "ready-for-agent")]) as w:
        d, head = with_pr(w, 1, 10, name="module.txt", text="BROKEN")
        path = Path(d["worktree"])
        changed = path / ("module.txt" if dirty != "untracked" else "required.py")
        changed.write_text("FIXED\n")
        if dirty == "staged":
            git(str(path), "add", "module.txt")
        marker = Path(w.sb.root) / "gate-ran"
        error = w.error(*_merge(1, *local_gate(f"touch {marker}"), "--set",
                               f"merge.sync_before_merge={str(sync).lower()}"))
        assert "uncommitted changes or untracked files" in error
        assert not marker.exists() and changed.read_text() == "FIXED\n"
        assert w.sb.remote_ref("refs/heads/" + d["branch"]) == head
        assert w.claimed_by(1) == "me" and w.pr(10).get("state", "open") == "open"


@pytest.mark.parametrize("command, expected", [
    ("printf altered > feature1.txt", "uncommitted"),
    ("printf altered > feature1.txt && git add feature1.txt", "uncommitted"),
    ("printf extra > untracked.py", "untracked files"),
    ("git -c core.hooksPath=/dev/null commit --allow-empty -qm during-gate", "HEAD changed"),
])
def test_gate_mutation_cannot_certify_the_original_head(command, expected):
    with world(issues=[issue(1, "ready-for-agent")]) as w:
        d, head = with_pr(w, 1, 10)
        error = w.error(*_merge(1, *local_gate(command), "--set", "merge.sync_before_merge=false"))
        assert expected in error
        assert w.sb.remote_ref("refs/heads/" + d["branch"]) == head
        assert w.claimed_by(1) == "me" and os.path.isdir(d["worktree"])
        assert w.pr(10).get("state", "open") == "open"
        assert not any(c[:2] == ["pr", "merge"] for c in w.calls())


def test_concurrent_worker_edit_after_gate_is_caught():
    with world(issues=[issue(1, "ready-for-agent")]) as w:
        d, _ = with_pr(w, 1, 10)
        original = afk._run_gate

        def run_then_edit(*args, **kwargs):
            verdict = original(*args, **kwargs)
            assert verdict["status"] == "green"
            Path(d["worktree"], "feature1.txt").write_text("changed after gate\n")
            return verdict

        with pytest.raises(RuntimeError, match="uncommitted"):
            _merge_in_process(w, _merge(1, *local_gate("true")), _run_gate=run_then_edit)
        assert w.claimed_by(1) == "me" and w.pr(10).get("state", "open") == "open"


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh"])
def test_real_shell_lookup_is_safe_and_preserves_failed_exit(shell, monkeypatch, tmp_path):
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is not installed")
    monkeypatch.setenv("SHELL", executable)
    monkeypatch.delenv("QODERCN_CLI", raising=False)
    monkeypatch.chdir(tmp_path)
    parser = afk.build_parser()
    assert afk.cmd_worker_command(parser.parse_args(["worker-command", "--check", "ls -la"]))[
        "status"] == "confirmed"
    for candidate in ("x;touch${IFS}pwned", "$(touch${IFS}pwned)", "-V"):
        result = afk.cmd_worker_command(parser.parse_args([
            "worker-command", "--check=" + candidate]))
        assert result["status"] == "unresolved"
    assert not (tmp_path / "pwned").exists()
    assert afk._login_shell("printf 'unknown command'; exit 1") == ""
    assert "login-interactive" in afk._login_shell(
        "case $- in *i*) "
        + ("shopt -q login_shell && " if shell == "bash" else "")
        + "printf login-interactive;; esac")


def test_zsh_reads_login_and_interactive_profiles_without_running_alias(monkeypatch, tmp_path):
    shell = shutil.which("zsh")
    if shell is None:
        pytest.skip("zsh is not installed")
    (tmp_path / ".zprofile").write_text("alias afk_profile_only='touch pwned; claude --dangerously-skip-permissions'\n")
    (tmp_path / ".zshrc").write_text("alias afk_rc_only='claude'\n")
    monkeypatch.setenv("SHELL", shell)
    monkeypatch.setenv("ZDOTDIR", str(tmp_path))
    monkeypatch.delenv("QODERCN_CLI", raising=False)
    monkeypatch.chdir(tmp_path)
    parser = afk.build_parser()
    login = afk.cmd_worker_command(parser.parse_args([
        "worker-command", "--check", "afk_profile_only"]))
    assert (login["status"], login["yolo"]) == ("confirmed", True)
    rc = afk.cmd_worker_command(parser.parse_args([
        "worker-command", "--check", "afk_rc_only"]))
    assert (rc["status"], rc["yolo"]) == ("confirmed", False)
    assert not (tmp_path / "pwned").exists()
