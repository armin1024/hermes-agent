from __future__ import annotations

from pathlib import Path

import yaml

from agent import i18n
from gateway import aops_i18n
from gateway.aops_commands import aops_text_command_lines, block_message
from gateway.aops_i18n import aops_error, aops_t
from gateway.config import Platform
from gateway.run import _aops_activity_label, _aops_runtime_text
from scripts.audit_aops_i18n import report


def _set_language(monkeypatch, language: str) -> None:
    monkeypatch.setenv("HERMES_LANGUAGE", language)
    i18n.reset_language_cache()
    aops_i18n.reset_aops_language_cache()


def test_aops_catalog_keys_match() -> None:
    root = Path(__file__).resolve().parents[2] / "locales"

    def flatten(value, prefix=""):
        result = {}
        if isinstance(value, dict):
            for key, child in value.items():
                result.update(flatten(child, f"{prefix}.{key}" if prefix else str(key)))
        elif isinstance(value, str):
            result[prefix] = value
        return result

    en = flatten(yaml.safe_load((root / "aops_en.yaml").read_text(encoding="utf-8")))
    zh = flatten(yaml.safe_load((root / "aops_zh.yaml").read_text(encoding="utf-8")))
    assert set(en) == set(zh)
    assert len(en) >= 90


def test_aops_static_message_switches_between_english_and_chinese(monkeypatch) -> None:
    _set_language(monkeypatch, "en")
    assert block_message("debug") == "Command `/debug` is blocked by AOPS config."
    assert "Show current profile" in aops_text_command_lines()[0]

    _set_language(monkeypatch, "zh")
    assert block_message("debug") == "命令 `/debug` 已被 AOPS 配置禁止。"
    assert "查看当前 profile" in aops_text_command_lines()[0]


def test_aops_other_languages_fall_back_to_english(monkeypatch) -> None:
    _set_language(monkeypatch, "ja")
    assert aops_t("skills.name_required") == "Skill name is required."


def test_aops_dynamic_error_retains_raw_diagnostic(monkeypatch) -> None:
    _set_language(monkeypatch, "zh")
    error = aops_error(
        "SILENT_COMMAND_FAILED",
        "common.operation_failed",
        raw_message="HTTP 502 upstream reset",
    )
    assert error["message"] == "操作失败。"
    assert error["details"]["rawMessage"] == "HTTP 502 upstream reset"


def test_aops_slash_confirmation_catalog_is_localized(monkeypatch) -> None:
    _set_language(monkeypatch, "zh")
    prompt = aops_t(
        "slash_confirm.prompt",
        command="new",
        detail=aops_t("slash_confirm.new_detail"),
        prefix="/",
    )
    assert "确认 /new" in prompt
    assert "这将创建一个全新会话" in prompt
    assert "仅本次允许" in prompt
    assert "Approve Once" not in prompt

    _set_language(monkeypatch, "en")
    prompt = aops_t(
        "slash_confirm.prompt",
        command="new",
        detail=aops_t("slash_confirm.new_detail"),
        prefix="/",
    )
    assert "Confirm /new" in prompt
    assert "Approve Once" in prompt


def test_aops_shared_runtime_notices_are_localized(monkeypatch) -> None:
    _set_language(monkeypatch, "zh")

    home = _aops_runtime_text(
        Platform.AOPS,
        "home_channel_missing",
        "No home channel for {platform}; use {command}.",
        platform="AOPS",
        command="/sethome",
    )
    heartbeat = _aops_runtime_text(
        Platform.AOPS,
        "long_running",
        "Working for {minutes} min{detail}",
        minutes=3,
        detail=" — 迭代 3/90，等待补充信息",
    )

    assert "当前未设置 AOPS 的主会话" in home
    assert "/sethome" in home
    assert "处理中 — 3 分钟" in heartbeat
    assert "Working" not in heartbeat
    assert _aops_activity_label(Platform.AOPS, "clarify") == "等待补充信息"


def test_shared_runtime_notices_keep_non_aops_text(monkeypatch) -> None:
    _set_language(monkeypatch, "zh")
    text = _aops_runtime_text(
        Platform.TELEGRAM,
        "long_running",
        "⏳ Working — {minutes} min{detail}",
        minutes=3,
        detail=" — iteration 3/90, clarify",
    )
    assert text == "⏳ Working — 3 min — iteration 3/90, clarify"
    assert _aops_activity_label(Platform.TELEGRAM, "clarify") == "clarify"


def test_aops_busy_and_automatic_notices_are_localized(monkeypatch) -> None:
    _set_language(monkeypatch, "zh")
    assert "正在中断当前任务" in aops_t(
        "runtime.busy_interrupting", detail=""
    )
    assert "已加入下一轮队列" in aops_t("runtime.queue_next")
    assert "连续 5 分钟没有活动" in aops_t(
        "runtime.inactivity_warning", elapsed=5, remaining=10
    )
    assert "会话已自动重置" in aops_t(
        "runtime.auto_reset", reason=aops_t("runtime.reset_reason_suspended")
    )
    assert "上下文压缩已中止" in aops_t(
        "runtime.compression_aborted", error="timeout"
    )


def test_aops_i18n_audit_has_no_unlocalized_static_messages() -> None:
    data = report()
    assert data["catalogs"]["keyParity"] is True
    assert data["summary"]["unlocalizedOccurrences"] == 0
