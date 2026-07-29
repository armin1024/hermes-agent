"""Profile-scoped terminal command policy.

This module implements the restrictive counterpart to ``command_allowlist``.
The latter only skips approval prompts; ``terminal.command_policy=allowlist``
is a hard execution boundary and is evaluated before approvals, YOLO, or the
terminal ``force`` flag.
"""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import logging
import os
import re
import shlex
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hermes_constants import get_config_path
from utils import fast_safe_load

logger = logging.getLogger(__name__)

_DEFAULT_TRUSTED_DIRS = ("/usr/bin", "/bin", "/usr/local/bin")
_VALID_MODES = {"unrestricted", "allowlist"}
_GLOB_MAGIC = re.compile(r"[*?[]")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_POLICY_CACHE: dict[str, tuple[int, int, dict[str, Any]]] = {}
_CACHE_LOCK = threading.RLock()


def _invalid_policy(config_path: Path, *errors: str) -> dict[str, Any]:
    return {
        "mode": "allowlist",
        "valid": False,
        "errors": [str(error) for error in errors if str(error)],
        "config_path": str(config_path),
        "trusted_executable_dirs": [],
        "allowed_workdirs": [],
        "command_patterns": [],
        "allowed_commands": [],
    }


def _normalize_string_list(value: Any, field: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"terminal.{field} must be a list")
        return []
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"terminal.{field}[{index}] must be a non-empty string")
            continue
        result.append(item.strip())
    return result


def _contains_unquoted_shell_syntax(text: str, *, pattern: bool = False) -> str | None:
    """Return a reason when *text* contains shell composition/expansion.

    Quotes are honoured so a literal HTTP header may contain spaces. Glob
    metacharacters are allowed only while validating configured patterns.
    """

    quote: str | None = None
    escaped = False
    i = 0
    while i < len(text):
        char = text[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            i += 1
            continue
        if quote:
            if char == quote:
                quote = None
            i += 1
            continue
        if char in {"'", '"'}:
            quote = char
            i += 1
            continue
        if char in "\r\n":
            return "newlines are not allowed"
        if char in ";|&<>`":
            return f"shell operator {char!r} is not allowed"
        if char == "$":
            return "shell variable and command expansion are not allowed"
        if not pattern and char in "*?[":
            return "shell glob expansion is not allowed in commands"
        i += 1
    if quote:
        return "unclosed quote"
    if escaped:
        return "trailing escape"
    return None


def _parse_argv(text: str, *, pattern: bool = False) -> tuple[list[str] | None, str | None]:
    # A backslash immediately followed by a newline is a POSIX shell line
    # continuation, not an argv token. Python's shlex keeps the newline as a
    # literal token in this shape, so normalize it before both validation and
    # splitting. Preserve indentation after the newline; shlex consumes it as
    # ordinary whitespace.
    normalized_text = re.sub(r"\\\r?\n", "", text)
    reason = _contains_unquoted_shell_syntax(normalized_text, pattern=pattern)
    if reason:
        return None, reason
    try:
        argv = shlex.split(normalized_text, posix=True)
    except ValueError as exc:
        return None, f"invalid shell quoting: {exc}"
    if not argv:
        return None, "command is empty"
    if _ENV_ASSIGNMENT.match(argv[0]):
        return None, "environment-variable prefixes are not allowed"
    if "/" in argv[0] and not os.path.isabs(argv[0]):
        return None, "relative executable paths are not allowed"
    return argv, None


def _normalize_policy(raw_config: Any, config_path: Path) -> dict[str, Any]:
    if not isinstance(raw_config, dict):
        return _invalid_policy(config_path, "config.yaml root must be a mapping")
    terminal = raw_config.get("terminal") or {}
    if not isinstance(terminal, dict):
        return _invalid_policy(config_path, "terminal must be a mapping")

    mode = str(terminal.get("command_policy") or "unrestricted").strip().lower()
    if mode not in _VALID_MODES:
        return _invalid_policy(
            config_path,
            "terminal.command_policy must be unrestricted or allowlist",
        )

    errors: list[str] = []
    trusted = _normalize_string_list(
        terminal.get("trusted_executable_dirs", list(_DEFAULT_TRUSTED_DIRS)),
        "trusted_executable_dirs",
        errors,
    )
    workdirs = _normalize_string_list(
        terminal.get("allowed_workdirs", []),
        "allowed_workdirs",
        errors,
    )
    patterns = _normalize_string_list(
        terminal.get("command_patterns", []),
        "command_patterns",
        errors,
    )
    structured = terminal.get("allowed_commands", [])
    if structured is None:
        structured = []
    if not isinstance(structured, list):
        errors.append("terminal.allowed_commands must be a list")
        structured = []

    normalized_trusted: list[str] = []
    for value in trusted:
        path = Path(os.path.expanduser(value))
        if not path.is_absolute():
            errors.append(f"trusted executable directory must be absolute: {value}")
            continue
        normalized_trusted.append(str(path.resolve(strict=False)))

    normalized_workdirs: list[str] = []
    for value in workdirs:
        path = Path(os.path.expanduser(value))
        if not path.is_absolute():
            errors.append(f"allowed workdir must be absolute: {value}")
            continue
        normalized_workdirs.append(str(path.resolve(strict=False)))

    for index, value in enumerate(patterns):
        argv, parse_error = _parse_argv(value, pattern=True)
        if parse_error:
            errors.append(f"terminal.command_patterns[{index}]: {parse_error}")
            continue
        assert argv
        if _GLOB_MAGIC.search(argv[0]):
            errors.append(
                f"terminal.command_patterns[{index}]: executable cannot contain glob syntax"
            )
        if any("**" in token for token in argv):
            errors.append(
                f"terminal.command_patterns[{index}]: ** is not supported"
            )

    for index, rule in enumerate(structured):
        if not isinstance(rule, dict):
            errors.append(f"terminal.allowed_commands[{index}] must be a mapping")
            continue
        executable = rule.get("executable")
        if not isinstance(executable, str) or not executable.strip():
            errors.append(
                f"terminal.allowed_commands[{index}].executable must be a non-empty string"
            )
        elif _GLOB_MAGIC.search(executable):
            errors.append(
                f"terminal.allowed_commands[{index}].executable cannot contain glob syntax"
            )
        elif "/" in executable and not os.path.isabs(executable):
            errors.append(
                f"terminal.allowed_commands[{index}].executable must be a name or absolute path"
            )
        args = rule.get("args", [])
        if not isinstance(args, list):
            errors.append(f"terminal.allowed_commands[{index}].args must be a list")
            continue
        for arg_index, spec in enumerate(args):
            prefix = f"terminal.allowed_commands[{index}].args[{arg_index}]"
            if isinstance(spec, str):
                continue
            if not isinstance(spec, dict):
                errors.append(f"{prefix} must be a string or matcher mapping")
                continue
            matcher_keys = {
                key for key in ("exact", "one_of", "regex", "path_under", "url")
                if key in spec
            }
            if len(matcher_keys) != 1:
                errors.append(f"{prefix} must contain exactly one matcher")
                continue
            matcher = next(iter(matcher_keys))
            value = spec[matcher]
            if matcher == "exact" and not isinstance(value, str):
                errors.append(f"{prefix}.exact must be a string")
            elif matcher == "one_of" and (
                not isinstance(value, list)
                or not value
                or any(not isinstance(item, str) for item in value)
            ):
                errors.append(f"{prefix}.one_of must be a non-empty string list")
            elif matcher == "regex":
                if not isinstance(value, str):
                    errors.append(f"{prefix}.regex must be a string")
                else:
                    try:
                        re.compile(value)
                    except re.error as exc:
                        errors.append(f"{prefix}.regex is invalid: {exc}")
            elif matcher == "path_under":
                roots = [value] if isinstance(value, str) else value
                if (
                    not isinstance(roots, list)
                    or not roots
                    or any(
                        not isinstance(root, str)
                        or not Path(os.path.expanduser(root)).is_absolute()
                        for root in roots
                    )
                ):
                    errors.append(f"{prefix}.path_under must contain absolute paths")
                if "must_exist" in spec and not isinstance(spec["must_exist"], bool):
                    errors.append(f"{prefix}.must_exist must be boolean")
            elif matcher == "url":
                if not isinstance(value, dict):
                    errors.append(f"{prefix}.url must be a mapping")
                else:
                    for list_key in ("schemes", "hosts", "ports"):
                        if list_key in value and not isinstance(value[list_key], list):
                            errors.append(f"{prefix}.url.{list_key} must be a list")
                    if "path_regex" in value:
                        try:
                            re.compile(str(value["path_regex"]))
                        except re.error as exc:
                            errors.append(f"{prefix}.url.path_regex is invalid: {exc}")

    policy = {
        "mode": mode,
        "valid": not errors,
        "errors": errors,
        "config_path": str(config_path),
        "trusted_executable_dirs": normalized_trusted,
        "allowed_workdirs": normalized_workdirs,
        "command_patterns": patterns,
        "allowed_commands": copy.deepcopy(structured),
    }
    if errors and mode == "unrestricted":
        # A malformed terminal policy must not silently downgrade to an
        # unrestricted terminal.
        policy["mode"] = "allowlist"
    return policy


def load_terminal_policy(*, force_reload: bool = False) -> dict[str, Any]:
    """Load the active profile's policy, cached by config path + stat."""

    config_path = get_config_path()
    path_key = str(config_path)
    try:
        stat = config_path.stat()
        cache_key = (stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        return _normalize_policy({}, config_path)
    except OSError as exc:
        return _invalid_policy(config_path, f"cannot stat config.yaml: {exc}")

    with _CACHE_LOCK:
        cached = _POLICY_CACHE.get(path_key)
        if not force_reload and cached and cached[:2] == cache_key:
            return copy.deepcopy(cached[2])
        try:
            with config_path.open(encoding="utf-8") as handle:
                raw = fast_safe_load(handle) or {}
            policy = _normalize_policy(raw, config_path)
        except Exception as exc:
            policy = _invalid_policy(config_path, f"cannot parse config.yaml: {exc}")
        _POLICY_CACHE[path_key] = (cache_key[0], cache_key[1], copy.deepcopy(policy))
        return policy


def terminal_policy_blocks_code_execution(policy: dict[str, Any] | None = None) -> bool:
    policy = policy or load_terminal_policy()
    return policy.get("mode") == "allowlist" or not policy.get("valid", True)


def terminal_policy_status() -> dict[str, Any]:
    policy = load_terminal_policy()
    return {
        "mode": policy["mode"],
        "valid": bool(policy["valid"]),
        "patternCount": len(policy["command_patterns"]),
        "structuredRuleCount": len(policy["allowed_commands"]),
        "trustedExecutableDirCount": len(policy["trusted_executable_dirs"]),
        "allowedWorkdirCount": len(policy["allowed_workdirs"]),
        "codeExecutionBlocked": terminal_policy_blocks_code_execution(policy),
        "configPath": policy["config_path"],
        "errors": list(policy["errors"]),
        "effectiveImmediately": True,
        "restartRequired": False,
    }


def sync_agent_code_execution_policy(agent: Any) -> bool:
    """Refresh a cached agent's execute_code visibility after policy changes.

    Returns True when the model-visible tool schema changed. The execute_code
    handler remains independently guarded even if this refresh cannot run.
    """

    blocked = terminal_policy_blocks_code_execution()
    disabled = list(getattr(agent, "disabled_toolsets", None) or [])
    injected = bool(
        getattr(agent, "_terminal_policy_injected_code_execution_block", False)
    )
    changed = False
    if blocked and "code_execution" not in disabled:
        disabled.append("code_execution")
        injected = True
        changed = True
    elif not blocked and injected and "code_execution" in disabled:
        disabled = [name for name in disabled if name != "code_execution"]
        injected = False
        changed = True

    agent.disabled_toolsets = disabled or None
    agent._terminal_policy_injected_code_execution_block = injected
    if not changed:
        return False

    try:
        import model_tools

        agent.tools = model_tools.get_tool_definitions(
            enabled_toolsets=getattr(agent, "enabled_toolsets", None),
            disabled_toolsets=agent.disabled_toolsets,
            quiet_mode=True,
        )
        agent.valid_tool_names = {
            item["function"]["name"]
            for item in (agent.tools or [])
            if isinstance(item, dict)
            and isinstance(item.get("function"), dict)
            and item["function"].get("name")
        }
        agent._tool_search_scope_cache = None
        logger.info(
            "Refreshed agent tools for terminal policy codeExecutionBlocked=%s",
            blocked,
        )
    except Exception as exc:
        logger.warning("Could not refresh agent tools for terminal policy: %s", exc)
    return True


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_executable(
    configured: str,
    supplied: str,
    trusted_dirs: list[str],
    *,
    env_type: str,
) -> tuple[str | None, str | None]:
    configured = configured.strip()
    configured_path = Path(configured)
    supplied_path = Path(supplied)
    roots = [Path(value).resolve(strict=False) for value in trusted_dirs]

    if configured_path.is_absolute():
        target = configured_path.resolve(strict=False)
        if roots and not any(_is_within(target, root) for root in roots):
            return None, "configured executable is outside trusted directories"
    else:
        target = None
        for root in roots:
            candidate = (root / configured).resolve(strict=False)
            if not _is_within(candidate, root):
                continue
            if env_type != "local" or (candidate.is_file() and os.access(candidate, os.X_OK)):
                target = candidate
                break
        if target is None:
            return None, f"trusted executable {configured!r} was not found"

    if supplied_path.is_absolute():
        supplied_real = supplied_path.resolve(strict=False)
        if supplied_real != target:
            return None, "absolute executable does not match the allowed executable"
    elif supplied != configured_path.name:
        return None, "executable name does not match"

    if env_type == "local":
        if not target.is_file() or not os.access(target, os.X_OK):
            return None, f"executable is unavailable or not executable: {target}"
        real = target.resolve(strict=True)
        if roots and not any(_is_within(real, root) for root in roots):
            return None, "executable symlink escapes trusted directories"
        target = real
    return str(target), None


def _match_glob_token(pattern: str, value: str) -> bool:
    if value.startswith("-") and not pattern.startswith("-") and _GLOB_MAGIC.search(pattern):
        return False
    return fnmatch.fnmatchcase(value, pattern)


def _match_path_rule(spec: dict[str, Any], value: str, cwd: str) -> bool:
    roots = spec.get("path_under")
    if isinstance(roots, str):
        roots = [roots]
    if not isinstance(roots, list) or not roots:
        return False
    candidate = Path(os.path.expanduser(value))
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    candidate = candidate.resolve(strict=False)
    allowed = False
    for root_value in roots:
        if not isinstance(root_value, str):
            continue
        root = Path(os.path.expanduser(root_value)).resolve(strict=False)
        if _is_within(candidate, root):
            allowed = True
            break
    if not allowed:
        return False
    return not spec.get("must_exist", False) or candidate.exists()


def _match_url_rule(spec: dict[str, Any], value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    schemes = spec.get("schemes")
    hosts = spec.get("hosts")
    ports = spec.get("ports")
    path_regex = spec.get("path_regex")
    if schemes and parsed.scheme.lower() not in {str(v).lower() for v in schemes}:
        return False
    if hosts and (parsed.hostname or "").lower() not in {str(v).lower() for v in hosts}:
        return False
    if ports and port not in {int(v) for v in ports}:
        return False
    if path_regex:
        try:
            if re.fullmatch(str(path_regex), parsed.path + (f"?{parsed.query}" if parsed.query else "")) is None:
                return False
        except re.error:
            return False
    return bool(parsed.scheme and parsed.hostname)


def _match_arg_spec(spec: Any, value: str, cwd: str) -> bool:
    if isinstance(spec, str):
        return value == spec
    if not isinstance(spec, dict):
        return False
    if "exact" in spec:
        return isinstance(spec["exact"], str) and value == spec["exact"]
    if "one_of" in spec:
        choices = spec["one_of"]
        return isinstance(choices, list) and value in choices
    if "regex" in spec:
        try:
            return re.fullmatch(str(spec["regex"]), value) is not None
        except re.error:
            return False
    if "path_under" in spec:
        return _match_path_rule(spec, value, cwd)
    if "url" in spec:
        return isinstance(spec["url"], dict) and _match_url_rule(spec["url"], value)
    return False


def _allowed_workdir(policy: dict[str, Any], cwd: str) -> bool:
    roots = policy["allowed_workdirs"]
    if not roots:
        return True
    candidate = Path(os.path.expanduser(cwd)).resolve(strict=False)
    return any(_is_within(candidate, Path(root)) for root in roots)


def evaluate_terminal_command(
    command: str,
    *,
    workdir: str,
    env_type: str,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate and canonicalize one terminal command."""

    policy = copy.deepcopy(policy) if policy is not None else load_terminal_policy()
    if policy.get("mode") != "allowlist" and policy.get("valid", True):
        return {"allowed": True, "command": command, "policy": "unrestricted"}
    if not policy.get("valid", False):
        return {
            "allowed": False,
            "error": "Invalid terminal allowlist configuration: " + "; ".join(policy.get("errors") or []),
            "error_code": "TERMINAL_POLICY_INVALID",
            "policy": "allowlist",
        }
    argv, error = _parse_argv(command)
    if error:
        return {
            "allowed": False,
            "error": error,
            "error_code": "TERMINAL_COMMAND_NOT_ALLOWED",
            "policy": "allowlist",
        }
    assert argv
    if not _allowed_workdir(policy, workdir):
        return {
            "allowed": False,
            "error": "Working directory is outside terminal.allowed_workdirs.",
            "error_code": "TERMINAL_WORKDIR_NOT_ALLOWED",
            "policy": "allowlist",
        }

    rules: list[tuple[str, str, list[Any]]] = []
    for index, pattern in enumerate(policy["command_patterns"]):
        pattern_argv, parse_error = _parse_argv(pattern, pattern=True)
        if not parse_error and pattern_argv:
            rules.append((f"pattern:{index}", pattern_argv[0], pattern_argv[1:]))
    for index, rule in enumerate(policy["allowed_commands"]):
        if isinstance(rule, dict):
            rules.append((
                str(rule.get("id") or f"rule:{index}"),
                str(rule.get("executable") or ""),
                list(rule.get("args") or []),
            ))

    for rule_id, executable, arg_specs in rules:
        resolved, _ = _resolve_executable(
            executable,
            argv[0],
            policy["trusted_executable_dirs"],
            env_type=env_type,
        )
        if not resolved or len(arg_specs) != len(argv) - 1:
            continue
        if all(
            _match_glob_token(spec, value)
            if isinstance(spec, str) and _GLOB_MAGIC.search(spec)
            else _match_arg_spec(spec, value, workdir)
            for spec, value in zip(arg_specs, argv[1:])
        ):
            canonical = shlex.join([resolved, *argv[1:]])
            logger.info(
                "Terminal allowlist decision=allow rule=%s executable=%s",
                rule_id,
                resolved,
            )
            return {
                "allowed": True,
                "command": canonical,
                "policy": "allowlist",
                "rule_id": rule_id,
                "executable": resolved,
            }

    logger.warning(
        "Terminal allowlist decision=deny executable=%s commandHash=%s",
        Path(argv[0]).name,
        hashlib.sha256(command.encode("utf-8")).hexdigest()[:12],
    )
    return {
        "allowed": False,
        "error": "Command is not allowed by profile terminal policy.",
        "error_code": "TERMINAL_COMMAND_NOT_ALLOWED",
        "policy": "allowlist",
    }
