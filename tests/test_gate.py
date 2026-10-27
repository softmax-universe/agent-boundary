"""Integration tests for the installed hook interface and generated policies.

These tests invoke the real gate and nono. They must run with the boundary
plugin disabled because nesting this filesystem sandbox is unsupported.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from .gate_test_environment import PACKAGE, PROFILES, SKILL, WORKTREE, require_gate_integration

# Resolved, because state_dir() resolves: /tmp is a symlink on macOS.
STATE = Path(tempfile.gettempdir()).resolve() / f"agent-boundary-test-{os.getpid()}"
SESSIONS = STATE / "sessions"

# The real-gate suite exists only where the boundary can actually run: it needs
# nono plus the Softmax monorepo's authored profiles and plugin skill. In the
# public mirror (and any checkout without them) it skips at collection.
require_gate_integration()

CLI = (sys.executable, "-m", "agent_boundary.entrypoint")
GATE = (*CLI, "claude", "hook", "PreToolUse")
HOME = Path.home()

HookOutput = dict[str, Any]


@dataclass
class Harness:
    env: dict[str, str]

    def gate(
        self, payload: dict[str, Any], extra_env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            GATE,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env={**self.env, **(extra_env or {})},
        )

    def hook_output(self, payload: dict[str, Any], extra_env: Mapping[str, str] | None = None) -> HookOutput:
        proc = self.gate(payload, extra_env)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip(), proc.stderr
        return json.loads(proc.stdout)["hookSpecificOutput"]

    def bash(
        self, command: str, sid: str, extra_env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self.gate(
            {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(WORKTREE), "session_id": sid},
            extra_env,
        )

    def bash_output(self, command: str, sid: str, extra_env: Mapping[str, str] | None = None) -> HookOutput:
        return self.hook_output(
            {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(WORKTREE), "session_id": sid},
            extra_env,
        )

    def cli(
        self,
        *args: str,
        sid: str | None = None,
        cwd: Path = WORKTREE,
        input_data: dict[str, Any] | None = None,
        check: bool = True,
        extra_env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {**self.env, **(extra_env or {})}
        command = [*CLI]
        if sid:
            command += ["--session-dir", str(SESSIONS / sid)]
        command += args
        return subprocess.run(
            command,
            input=json.dumps(input_data) if input_data is not None else None,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
            check=check,
        )

    def session(self, name: str, cwd: Path = WORKTREE) -> str:
        name = re.sub(r"[^a-z0-9_-]", "-", name.lower())
        sid = f"pytest-{os.getpid()}-{name}"[:64]
        path = SESSIONS / sid
        if path.exists():
            shutil.rmtree(path)
        self.cli(
            "claude",
            "hook",
            "SessionStart",
            input_data={"session_id": sid, "cwd": str(cwd)},
        )
        return sid

    def decision(self, tool: str, path: Path, sid: str, cwd: Path = WORKTREE) -> str:
        return self.hook_output(
            {
                "tool_name": tool,
                "tool_input": {"file_path": str(path)},
                "cwd": str(cwd),
                "session_id": sid,
            }
        )["permissionDecision"]


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Harness]:
    fake_bin = tmp_path_factory.mktemp("fake-aws")
    fake_aws = fake_bin / "aws"
    fake_aws.write_text(
        "#!/bin/bash\n"
        'if [ "$1 $2" = "configure export-credentials" ]; then\n'
        '  [ -n "$FAKE_AWS_COUNT_FILE" ] && echo x >> "$FAKE_AWS_COUNT_FILE"\n'
        '  if [ "$FAKE_AWS_MODE" = "fail" ]; then\n'
        '    echo "Error when retrieving token from sso: Token has expired" >&2\n'
        "    exit 255\n"
        "  fi\n"
        '  printf \'{"Version": 1, "AccessKeyId": "FAKEKEYID", "SecretAccessKey": "FAKESECRET",'
        ' "SessionToken": "FAKETOKEN", "Expiration": "%s"}\' "${FAKE_AWS_EXPIRY:-2099-01-01T00:00:00Z}"\n'
        'elif [ "$1 $2 $3" = "configure get region" ]; then\n'
        "  echo us-east-1\n"
        "fi\n"
    )
    fake_aws.chmod(0o755)

    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/bin/bash\n"
        'if [ "$1 $2" = "auth token" ]; then\n'
        '  [ -n "$FAKE_GH_COUNT_FILE" ] && echo x >> "$FAKE_GH_COUNT_FILE"\n'
        '  if [ "$FAKE_GH_MODE" = "fail" ]; then\n'
        '    echo "not logged in to github.com" >&2\n'
        "    exit 1\n"
        "  fi\n"
        "  echo FAKEGHTOKEN\n"
        "fi\n"
    )
    fake_gh.chmod(0o755)

    pythonpath = str(PACKAGE / "src")
    if existing := os.environ.get("PYTHONPATH"):
        pythonpath += os.pathsep + existing
    env = {
        **os.environ,
        "AGENT_BOUNDARY_PROFILES_DIR": str(PROFILES),
        "CLAUDE_PLUGIN_ROOT": str(SKILL),
        # Not XDG_STATE_HOME: repointing that would also relocate nono's own
        # protected state root and break its system grants.
        "AGENT_BOUNDARY_STATE_DIR": str(STATE),
        "FAKE_AWS_COUNT_FILE": "",
        "FAKE_AWS_MODE": "",
        "FAKE_GH_COUNT_FILE": "",
        "FAKE_GH_MODE": "",
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": pythonpath,
    }
    # When the suite itself runs inside a Claude session, the CLI's ambient-session
    # fallback would resolve that live session — and its cached real credentials —
    # instead of the no-session path the plain-terminal tests exercise.
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    result = Harness(env)
    yield result
    shutil.rmtree(STATE, ignore_errors=True)


@pytest.fixture(scope="module")
def default_sid(harness: Harness) -> str:
    return harness.session("default")


def test_off_is_silent_and_on_wraps_bash(harness: Harness) -> None:
    sid = harness.session("mode")
    harness.cli("off", sid=sid)
    proc = harness.bash("ls ~/.aws", sid)
    assert proc.returncode == 0
    assert not proc.stdout.strip()

    harness.cli("on", sid=sid)
    command = harness.bash_output("ls ~/.aws", sid)["updatedInput"]["command"]
    assert "nono wrap " in command


def test_boundary_state_is_per_session(harness: Harness) -> None:
    off = harness.session("isolated-off")
    on = harness.session("isolated-on")
    harness.cli("off", sid=off)

    assert not harness.bash("ls", off).stdout.strip()
    assert harness.bash("ls", on).stdout.strip()


@pytest.mark.parametrize(
    "text",
    ['{"profile": "default", "state": "banana"}', "{ not json", ""],
    ids=["unknown-state", "invalid-json", "empty"],
)
def test_malformed_session_config_still_enforces(harness: Harness, text: str, request: pytest.FixtureRequest) -> None:
    sid = harness.session(f"malformed-{request.node.callspec.id}")
    (SESSIONS / sid / "boundary.json").write_text(text)

    assert harness.bash("ls", sid).stdout.strip()


def test_invalid_session_id_fails_closed(harness: Harness) -> None:
    output = harness.bash_output("ls", "../invalid")
    assert output["permissionDecision"] == "deny"


def test_missing_policy_fails_closed_with_recovery_command(harness: Harness) -> None:
    output = harness.bash_output("ls", "no-such-session-9999")
    assert output["permissionDecision"] == "deny"
    assert "agent-boundary reload" in output["permissionDecisionReason"]


@pytest.mark.parametrize(
    "command",
    [
        "agent-boundary off; cat ~/.aws/config",
        "agent-boundary off && sh",
        "X=1 agent-boundary off",
        "agent-boundary use ../../etc/passwd",
        "agent-boundary use $(whoami)",
        "agent-boundary use self-edit extra",
    ],
)
def test_shell_syntax_cannot_ride_the_cli_escape(harness: Harness, default_sid: str, command: str) -> None:
    wrapped = harness.bash_output(command, default_sid)["updatedInput"]["command"]
    assert "nono wrap " in wrapped


def test_absent_denied_leaf_stays_denied(harness: Harness, tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    sid = harness.session("absent-leaf", cwd=tmp_path)

    assert harness.decision("Write", tmp_path / ".claude/settings.local.json", sid, tmp_path) == "deny"
    assert harness.decision("Write", tmp_path / ".claude/settings.json", sid, tmp_path) == "deny"
    assert harness.decision("Write", tmp_path / "new.txt", sid, tmp_path) == "allow"
    assert harness.decision("Write", tmp_path / "a/b/c.txt", sid, tmp_path) == "allow"
    assert list(tmp_path.iterdir()) == [tmp_path / ".claude"]


def test_self_edit_opens_plugin_but_not_external_secrets(harness: Harness, default_sid: str) -> None:
    sid = harness.session("self-edit")
    harness.cli("use", "self-edit", sid=sid)

    assert harness.decision("Write", SESSIONS / sid / "policy.json", sid) == "allow"
    assert harness.decision("Write", PROFILES / "default.yaml", sid) == "allow"
    assert harness.decision("Write", WORKTREE / ".claude/settings.json", sid) == "allow"
    assert harness.decision("Write", HOME / ".aws/x", sid) == "deny"
    assert harness.decision("Write", HOME / ".ssh/authorized_keys2", sid) == "deny"
    assert harness.decision("Write", SESSIONS / default_sid / "policy.json", default_sid) == "deny"
    assert harness.decision("Write", STATE / "runtimes/x/bin/agent-boundary", default_sid) == "deny"


def test_aws_credentials_are_cached_without_entering_the_tool_log(harness: Harness) -> None:
    sid = harness.session("aws")
    envfile = SESSIONS / sid / "aws-credentials.env"
    count_file = Path(harness.env["PATH"].split(os.pathsep)[0]) / f"count-{sid}"
    fake_env = {"FAKE_AWS_COUNT_FILE": str(count_file)}

    def run_bash(extra_env: Mapping[str, str] | None = None) -> HookOutput:
        return harness.bash_output(
            "aws sts get-caller-identity",
            sid,
            {**fake_env, **(extra_env or {})},
        )

    def calls() -> int:
        return len(count_file.read_text().splitlines()) if count_file.exists() else 0

    output = run_bash()
    command = output["updatedInput"]["command"]
    assert f". {shlex.quote(str(envfile))}" in command
    assert "FAKESECRET" not in command
    assert "FAKEKEYID" not in command
    assert "unset AWS_PROFILE AWS_DEFAULT_PROFILE" in command
    assert "nono wrap " in command
    assert envfile.stat().st_mode & 0o777 == 0o600
    assert "AWS_ACCESS_KEY_ID=FAKEKEYID" in envfile.read_text()
    assert "AWS_CONFIG_FILE=/dev/null" in envfile.read_text()

    assert calls() == 1
    run_bash()
    assert calls() == 1

    envfile.write_text(re.sub(r"# expires_at=.*", "# expires_at=0.0", envfile.read_text()))
    run_bash()
    assert calls() == 2

    envfile.unlink()
    failure = run_bash({"FAKE_AWS_MODE": "fail"})
    command = failure["updatedInput"]["command"]
    assert failure["permissionDecision"] == "allow"
    assert "nono wrap " in command
    assert f". {shlex.quote(str(envfile))}" not in command
    assert "AWS_ACCESS_KEY_ID" not in command
    assert "aws sso login" in failure["permissionDecisionReason"]
    assert "FAKESECRET" not in failure["permissionDecisionReason"]

    failed_calls = calls()
    run_bash({"FAKE_AWS_MODE": "fail"})
    assert calls() == failed_calls

    config_path = SESSIONS / sid / "boundary.json"
    config = json.loads(config_path.read_text())
    config.pop("aws_profile")
    config_path.write_text(json.dumps(config))
    command = run_bash()["updatedInput"]["command"]
    assert "nono wrap " in command
    assert f". {shlex.quote(str(envfile))}" not in command


def test_github_token_is_cached_and_swaps_the_credential_helper(harness: Harness) -> None:
    sid = harness.session("github")
    envfile = SESSIONS / sid / "github-credentials.env"
    count_file = Path(harness.env["PATH"].split(os.pathsep)[0]) / f"gh-count-{sid}"
    fake_env = {"FAKE_GH_COUNT_FILE": str(count_file)}

    def run_bash(extra_env: Mapping[str, str] | None = None) -> HookOutput:
        return harness.bash_output("git push origin HEAD", sid, {**fake_env, **(extra_env or {})})

    def calls() -> int:
        return len(count_file.read_text().splitlines()) if count_file.exists() else 0

    output = run_bash()
    command = output["updatedInput"]["command"]
    assert f". {shlex.quote(str(envfile))}" in command
    assert "FAKEGHTOKEN" not in command
    assert "nono wrap " in command
    assert envfile.stat().st_mode & 0o777 == 0o600
    content = envfile.read_text()
    assert "GH_TOKEN=FAKEGHTOKEN" in content
    assert "GITHUB_TOKEN=FAKEGHTOKEN" in content
    assert "GIT_CONFIG_VALUE_0=''" in content
    assert "GIT_CONFIG_VALUE_1='!gh auth git-credential'" in content

    assert calls() == 1
    run_bash()
    assert calls() == 1

    envfile.write_text(re.sub(r"# exported_at=.*", "# exported_at=0.0", envfile.read_text()))
    run_bash()
    assert calls() == 2

    envfile.unlink()
    failure = run_bash({"FAKE_GH_MODE": "fail"})
    assert failure["permissionDecision"] == "allow"
    assert f". {shlex.quote(str(envfile))}" not in failure["updatedInput"]["command"]
    assert "gh auth login" in failure["permissionDecisionReason"]
    assert "FAKEGHTOKEN" not in failure["permissionDecisionReason"]

    failed_calls = calls()
    run_bash({"FAKE_GH_MODE": "fail"})
    assert calls() == failed_calls

    config_path = SESSIONS / sid / "boundary.json"
    config = json.loads(config_path.read_text())
    config.pop("github")
    config_path.write_text(json.dumps(config))
    assert f". {shlex.quote(str(envfile))}" not in run_bash()["updatedInput"]["command"]


@pytest.mark.parametrize("with_session", [True, False], ids=["session", "plain-terminal"])
def test_run_uses_the_boundary_and_injects_credentials(harness: Harness, with_session: bool) -> None:
    sid = harness.session(f"run-{with_session}") if with_session else None
    env = harness.cli("run", "env", sid=sid, check=False)

    assert env.returncode == 0, env.stderr
    assert "AWS_ACCESS_KEY_ID=FAKEKEYID" in env.stdout
    assert "GH_TOKEN=FAKEGHTOKEN" in env.stdout
    assert "GIT_CONFIG_VALUE_1=!gh auth git-credential" in env.stdout
    assert "\nAWS_PROFILE=" not in f"\n{env.stdout}"
    denied = harness.cli("run", "cat", str(HOME / ".aws/config"), sid=sid, check=False)
    assert denied.returncode != 0


def test_session_can_be_created_and_inspected_without_claude_environment(harness: Harness, tmp_path: Path) -> None:
    directory = tmp_path / "manual-session"
    env = {key: value for key, value in harness.env.items() if not key.startswith("CLAUDE_")}

    created = subprocess.run(
        [*CLI, "session", "create", str(directory), "--workdir", str(WORKTREE)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert str(directory) in created.stdout

    status = subprocess.run(
        [*CLI, "--session-dir", str(directory), "status"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert status.stdout.strip() == "profile default state on"

    env["AGENT_BOUNDARY_SESSION_DIR"] = str(directory)
    status_from_env = subprocess.run([*CLI, "status"], capture_output=True, text=True, env=env, check=True)
    assert status_from_env.stdout == status.stdout

    pwd = subprocess.run(
        [*CLI, "--session-dir", str(directory), "run", "pwd"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert pwd.stdout.strip() == str(WORKTREE)


def test_run_rejects_an_incomplete_selected_session(harness: Harness) -> None:
    result = harness.cli("run", "true", sid="missing-explicit-session", check=False)
    assert result.returncode == 1
    assert "no complete session" in result.stderr


def test_unknown_tool_asks(harness: Harness, default_sid: str) -> None:
    output = harness.hook_output(
        {"tool_name": "LSP", "tool_input": {}, "cwd": str(WORKTREE), "session_id": default_sid}
    )
    assert output["permissionDecision"] == "ask"
