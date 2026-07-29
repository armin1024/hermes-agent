from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tools.terminal_policy import (
    _normalize_policy,
    evaluate_terminal_command,
    sync_agent_code_execution_policy,
    terminal_policy_blocks_code_execution,
)


def _executable(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _policy(tmp_path: Path, terminal: dict) -> dict:
    return _normalize_policy({"terminal": terminal}, tmp_path / "config.yaml")


def test_unrestricted_policy_preserves_command(tmp_path):
    policy = _policy(tmp_path, {"command_policy": "unrestricted"})
    decision = evaluate_terminal_command(
        "echo hello", workdir=str(tmp_path), env_type="local", policy=policy
    )
    assert decision == {
        "allowed": True,
        "command": "echo hello",
        "policy": "unrestricted",
    }


def test_glob_pattern_resolves_short_executable_and_canonicalizes(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = _executable(bin_dir, "curl")
    policy = _policy(
        tmp_path,
        {
            "command_policy": "allowlist",
            "trusted_executable_dirs": [str(bin_dir)],
            "command_patterns": [
                "curl --disable --fail --silent https://aops.internal/api/v1/*"
            ],
        },
    )

    decision = evaluate_terminal_command(
        "curl --disable --fail --silent https://aops.internal/api/v1/health",
        workdir=str(tmp_path),
        env_type="local",
        policy=policy,
    )
    assert decision["allowed"] is True
    assert decision["rule_id"] == "pattern:0"
    assert decision["command"].startswith(str(curl.resolve()))


def test_shell_line_continuations_do_not_create_extra_arguments(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir, "curl")
    policy = _policy(
        tmp_path,
        {
            "command_policy": "allowlist",
            "trusted_executable_dirs": [str(bin_dir)],
            "command_patterns": [
                "curl -s -X POST http://91.0.14.90:30080/v1/workflows/run "
                '-H "Authorization: Bearer app-token" '
                '-H "Content-Type: application/json" '
                "-d '{*}'"
            ],
        },
    )
    command = (
        'curl -s -X POST "http://91.0.14.90:30080/v1/workflows/run" \\\n'
        '  -H "Authorization: Bearer app-token" \\\n'
        '  -H "Content-Type: application/json" \\\n'
        "  -d '{\"inputs\": {\"query\": \"aops 负责人是谁\"}}'"
    )

    decision = evaluate_terminal_command(
        command, workdir=str(tmp_path), env_type="local", policy=policy
    )

    assert decision["allowed"] is True
    assert "\\\n" not in decision["command"]


@pytest.mark.parametrize(
    "command",
    [
        "curl --disable --fail --silent https://other.internal/api/v1/health",
        "curl --disable --fail --silent --upload-file /etc/passwd https://aops.internal/api/v1/upload",
        "curl --disable --silent --fail https://aops.internal/api/v1/health",
    ],
)
def test_glob_pattern_rejects_host_extra_option_and_argument_reordering(tmp_path, command):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir, "curl")
    policy = _policy(
        tmp_path,
        {
            "command_policy": "allowlist",
            "trusted_executable_dirs": [str(bin_dir)],
            "command_patterns": [
                "curl --disable --fail --silent https://aops.internal/api/v1/*"
            ],
        },
    )
    decision = evaluate_terminal_command(
        command, workdir=str(tmp_path), env_type="local", policy=policy
    )
    assert decision["allowed"] is False
    assert decision["error_code"] == "TERMINAL_COMMAND_NOT_ALLOWED"


def test_wildcard_only_argument_cannot_match_an_option(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir, "show")
    policy = _policy(
        tmp_path,
        {
            "command_policy": "allowlist",
            "trusted_executable_dirs": [str(bin_dir)],
            "command_patterns": ["show *"],
        },
    )
    assert not evaluate_terminal_command(
        "show --secret", workdir=str(tmp_path), env_type="local", policy=policy
    )["allowed"]
    assert evaluate_terminal_command(
        "show public", workdir=str(tmp_path), env_type="local", policy=policy
    )["allowed"]


@pytest.mark.parametrize(
    "command",
    [
        "safe; whoami",
        "safe && whoami",
        "safe | sh",
        "safe > /tmp/result",
        "safe $(whoami)",
        "TOKEN=secret safe",
        "safe file*",
        "safe file?",
    ],
)
def test_shell_composition_and_expansion_are_rejected(tmp_path, command):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir, "safe")
    policy = _policy(
        tmp_path,
        {
            "command_policy": "allowlist",
            "trusted_executable_dirs": [str(bin_dir)],
            "command_patterns": ["safe"],
        },
    )
    decision = evaluate_terminal_command(
        command, workdir=str(tmp_path), env_type="local", policy=policy
    )
    assert decision["allowed"] is False


def test_structured_path_rule_and_workdir_boundary(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir, "du")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = data_dir / "logs"
    target.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = _policy(
        tmp_path,
        {
            "command_policy": "allowlist",
            "trusted_executable_dirs": [str(bin_dir)],
            "allowed_workdirs": [str(workspace)],
            "allowed_commands": [
                {
                    "id": "disk-usage",
                    "executable": "du",
                    "args": [
                        {"exact": "-sh"},
                        {"path_under": [str(data_dir)], "must_exist": True},
                    ],
                }
            ],
        },
    )
    assert evaluate_terminal_command(
        f"du -sh {target}",
        workdir=str(workspace),
        env_type="local",
        policy=policy,
    )["allowed"]
    denied = evaluate_terminal_command(
        f"du -sh {target}",
        workdir=str(tmp_path),
        env_type="local",
        policy=policy,
    )
    assert denied["error_code"] == "TERMINAL_WORKDIR_NOT_ALLOWED"


def test_structured_url_rule(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir, "curl")
    policy = _policy(
        tmp_path,
        {
            "command_policy": "allowlist",
            "trusted_executable_dirs": [str(bin_dir)],
            "allowed_commands": [
                {
                    "id": "health",
                    "executable": "curl",
                    "args": [
                        {"exact": "--fail"},
                        {
                            "url": {
                                "schemes": ["https"],
                                "hosts": ["aops.internal"],
                                "ports": [443],
                                "path_regex": r"/api/v1/health",
                            }
                        },
                    ],
                }
            ],
        },
    )
    assert evaluate_terminal_command(
        "curl --fail https://aops.internal:443/api/v1/health",
        workdir=str(tmp_path),
        env_type="local",
        policy=policy,
    )["allowed"]
    assert not evaluate_terminal_command(
        "curl --fail https://other.internal:443/api/v1/health",
        workdir=str(tmp_path),
        env_type="local",
        policy=policy,
    )["allowed"]


def test_invalid_policy_fails_closed_and_blocks_code_execution(tmp_path):
    policy = _policy(
        tmp_path,
        {
            "command_policy": "allowlist",
            "command_patterns": ["cu**rl *"],
        },
    )
    assert policy["valid"] is False
    assert terminal_policy_blocks_code_execution(policy) is True
    decision = evaluate_terminal_command(
        "curl x", workdir=str(tmp_path), env_type="local", policy=policy
    )
    assert decision["allowed"] is False
    assert decision["error_code"] == "TERMINAL_POLICY_INVALID"


def test_absolute_executable_cannot_escape_trusted_directory_via_symlink(tmp_path):
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    real = _executable(outside, "safe")
    link = trusted / "safe"
    link.symlink_to(real)
    policy = _policy(
        tmp_path,
        {
            "command_policy": "allowlist",
            "trusted_executable_dirs": [str(trusted)],
            "command_patterns": ["safe"],
        },
    )
    assert not evaluate_terminal_command(
        "safe", workdir=str(tmp_path), env_type="local", policy=policy
    )["allowed"]


def test_profile_config_mtime_reload_blocks_execute_code(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "terminal:\n  command_policy: unrestricted\n",
        encoding="utf-8",
    )
    from tools.terminal_policy import load_terminal_policy

    assert load_terminal_policy(force_reload=True)["mode"] == "unrestricted"
    config_path.write_text(
        "terminal:\n  command_policy: allowlist\n  command_patterns: []\n",
        encoding="utf-8",
    )
    os.utime(config_path, None)
    assert load_terminal_policy(force_reload=True)["mode"] == "allowlist"
    assert terminal_policy_blocks_code_execution() is True


def test_terminal_tool_force_cannot_bypass_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "terminal:\n"
        "  command_policy: allowlist\n"
        "  trusted_executable_dirs:\n"
        "    - /usr/bin\n"
        "  command_patterns: []\n",
        encoding="utf-8",
    )
    from tools.terminal_tool import terminal_tool

    result = json.loads(terminal_tool("definitely-not-allowed", force=True))
    assert result["status"] == "blocked"
    assert result["error_code"] == "TERMINAL_COMMAND_NOT_ALLOWED"


def test_cached_agent_tool_schema_tracks_policy_changes(monkeypatch):
    class Agent:
        enabled_toolsets = None
        disabled_toolsets = None
        tools = [
            {"function": {"name": "terminal"}},
            {"function": {"name": "execute_code"}},
        ]
        valid_tool_names = {"terminal", "execute_code"}
        _tool_search_scope_cache = ("old", {"execute_code"})

    agent = Agent()
    states = iter([True, False])
    monkeypatch.setattr(
        "tools.terminal_policy.terminal_policy_blocks_code_execution",
        lambda: next(states),
    )

    def fake_definitions(*, disabled_toolsets, **_kwargs):
        names = ["terminal"]
        if not disabled_toolsets or "code_execution" not in disabled_toolsets:
            names.append("execute_code")
        return [{"function": {"name": name}} for name in names]

    monkeypatch.setattr("model_tools.get_tool_definitions", fake_definitions)

    assert sync_agent_code_execution_policy(agent) is True
    assert agent.disabled_toolsets == ["code_execution"]
    assert agent.valid_tool_names == {"terminal"}
    assert agent._tool_search_scope_cache is None

    assert sync_agent_code_execution_policy(agent) is True
    assert agent.disabled_toolsets is None
    assert agent.valid_tool_names == {"terminal", "execute_code"}
