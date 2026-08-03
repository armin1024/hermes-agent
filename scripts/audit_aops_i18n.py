#!/usr/bin/env python3
"""Audit AOPS-owned user-facing static strings.

The audit is deliberately sink-based.  It does not count logs, protocol
fields, command syntax, model output, tool output, or upstream payloads.
Instead it inspects message/error sinks used by AOPS command responses and
flags literal prose that bypasses ``aops_t()``/``aops_error()``.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "gateway/aops_commands.py",
    "gateway/platforms/aops.py",
    "gateway/aops_skillhub_bridge.py",
    "gateway/aops_profile_delete.py",
    "gateway/aops_skill_uninstall.py",
)
SHARED_RUNTIME_SOURCE = "gateway/run.py"
MESSAGE_CALL_ARGUMENT = {
    "_error_payload": 3,
    "_cron_single_error": 3,
    "_memory_error": 2,
    "_instruction_error_response": 3,
    "_attachment_error": 2,
}
PROSE_RE = re.compile(r"[A-Za-z\u3400-\u9fff]{2,}")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    sink: str
    status: str
    preview: str


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _preview(node: ast.AST, source: str) -> str:
    segment = ast.get_source_segment(source, node) or ""
    return " ".join(segment.split())[:180]


def _is_localized(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _call_name(node.func) in {"aops_t", "aops_error"}


def _is_literal_prose(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return bool(PROSE_RE.search(node.value))
    if isinstance(node, ast.JoinedStr):
        literal = "".join(
            part.value for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
        return bool(PROSE_RE.search(literal))
    return False


def _record(path: str, source: str, node: ast.AST, sink: str) -> Finding | None:
    if _is_localized(node):
        return Finding(path, node.lineno, sink, "localized", _preview(node, source))
    if _is_literal_prose(node):
        return Finding(path, node.lineno, sink, "unlocalized", _preview(node, source))
    return None


def audit_sources() -> list[Finding]:
    findings: list[Finding] = []
    for relative in SOURCES:
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "message"
                    ):
                        finding = _record(relative, source, value, "dict.message")
                        if finding:
                            findings.append(finding)
            elif isinstance(node, ast.Call):
                name = _call_name(node.func)
                index = MESSAGE_CALL_ARGUMENT.get(name)
                if index is not None and len(node.args) > index:
                    finding = _record(relative, source, node.args[index], f"call.{name}")
                    if finding:
                        findings.append(finding)
        # De-duplicate a literal seen both as a call argument and nested dict.
    runtime_source = (REPO_ROOT / SHARED_RUNTIME_SOURCE).read_text(encoding="utf-8")
    runtime_tree = ast.parse(runtime_source, filename=SHARED_RUNTIME_SOURCE)
    for node in ast.walk(runtime_tree):
        if (
            isinstance(node, ast.Call)
            and _call_name(node.func) == "_aops_runtime_text"
        ):
            findings.append(
                Finding(
                    SHARED_RUNTIME_SOURCE,
                    node.lineno,
                    "call._aops_runtime_text",
                    "localized",
                    _preview(node, runtime_source),
                )
            )
    unique = {(item.path, item.line, item.sink, item.preview): item for item in findings}
    return sorted(unique.values(), key=lambda item: (item.path, item.line, item.sink))


def catalog_stats() -> dict[str, Any]:
    result: dict[str, Any] = {}
    key_sets: dict[str, set[str]] = {}
    for language in ("en", "zh"):
        path = REPO_ROOT / "locales" / f"aops_{language}.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        flat: dict[str, str] = {}

        def flatten(value: Any, prefix: str = "") -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    flatten(child, f"{prefix}.{key}" if prefix else str(key))
            elif isinstance(value, str):
                flat[prefix] = value

        flatten(raw)
        key_sets[language] = set(flat)
        result[language] = {"path": str(path.relative_to(REPO_ROOT)), "keys": len(flat)}
    result["keyParity"] = key_sets["en"] == key_sets["zh"]
    result["missingInZh"] = sorted(key_sets["en"] - key_sets["zh"])
    result["extraInZh"] = sorted(key_sets["zh"] - key_sets["en"])
    return result


def report() -> dict[str, Any]:
    findings = audit_sources()
    localized = [item for item in findings if item.status == "localized"]
    unlocalized = [item for item in findings if item.status == "unlocalized"]
    return {
        "scope": [*SOURCES, SHARED_RUNTIME_SOURCE],
        "catalogs": catalog_stats(),
        "summary": {
            "userFacingSinkOccurrences": len(findings),
            "localizedOccurrences": len(localized),
            "unlocalizedOccurrences": len(unlocalized),
        },
        "unlocalized": [asdict(item) for item in unlocalized],
        "localized": [asdict(item) for item in localized],
        "exempt": [
            "protocol field names and enum values",
            "command syntax and error codes",
            "model-generated replies",
            "tool arguments, stdout, and stderr",
            "third-party payload bodies retained as raw diagnostics",
            "file logs and internal WebSocket diagnostics",
        ],
    }


def _markdown(data: dict[str, Any]) -> str:
    summary = data["summary"]
    catalogs = data["catalogs"]
    lines = [
        "# AOPS 用户文案国际化审计",
        "",
        "## 汇总",
        "",
        f"- 用户可见文案 sink 出现次数：{summary['userFacingSinkOccurrences']}",
        f"- 已接入 AOPS i18n：{summary['localizedOccurrences']}",
        f"- 未国际化静态文案：{summary['unlocalizedOccurrences']}",
        f"- 英文词条数：{catalogs['en']['keys']}",
        f"- 中文词条数：{catalogs['zh']['keys']}",
        f"- 中英文键一致：{'是' if catalogs['keyParity'] else '否'}",
        "",
        "## 未国际化项",
        "",
    ]
    if not data["unlocalized"]:
        lines.append("无。")
    else:
        lines.extend(
            f"- `{item['path']}:{item['line']}` `{item['sink']}`：`{item['preview']}`"
            for item in data["unlocalized"]
        )
    lines.extend(["", "## 统计豁免", ""])
    lines.extend(f"- {item}" for item in data["exempt"])
    lines.extend(["", "## 扫描范围", ""])
    lines.extend(f"- `{path}`" for path in data["scope"])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--fail-on-unlocalized", action="store_true")
    args = parser.parse_args()
    data = report()
    print(
        json.dumps(data, ensure_ascii=False, indent=2)
        if args.format == "json"
        else _markdown(data),
        end="",
    )
    if args.fail_on_unlocalized and data["summary"]["unlocalizedOccurrences"]:
        return 1
    if not data["catalogs"]["keyParity"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
