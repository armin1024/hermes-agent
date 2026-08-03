"""Tests for GatewayRunner._format_session_info — session config surfacing."""

import pytest
from unittest.mock import patch

from gateway.run import GatewayRunner


@pytest.fixture()
def runner():
    """Create a bare GatewayRunner without __init__."""
    return GatewayRunner.__new__(GatewayRunner)


def _patch_info(tmp_path, config_yaml, model, runtime):
    """Return a context-manager stack that patches _format_session_info deps."""
    cfg_path = tmp_path / "config.yaml"
    if config_yaml is not None:
        cfg_path.write_text(config_yaml)
    return (
        patch("gateway.run._hermes_home", tmp_path),
        patch("gateway.run._resolve_gateway_model", return_value=model),
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value=runtime),
    )


class TestFormatSessionInfo:

    def test_includes_model_name(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: anthropic/claude-opus-4.6\n  provider: openrouter\n",
                                  "anthropic/claude-opus-4.6",
                                  {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key": "k"})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "claude-opus-4.6" in info

    def test_includes_provider(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: test-model\n  provider: openrouter\n",
                                  "test-model",
                                  {"provider": "openrouter", "base_url": "", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "openrouter" in info

    def test_config_context_length(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: test-model\n  context_length: 32768\n",
                                  "test-model",
                                  {"provider": "custom", "base_url": "", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "32K" in info
        assert "config" in info

    def test_default_fallback_hint(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: unknown-model-xyz\n",
                                  "unknown-model-xyz",
                                  {"provider": "", "base_url": "", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "256K" in info
        assert "model.context_length" in info

    def test_local_endpoint_shown(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(
            tmp_path,
            "model:\n  default: qwen3:8b\n  provider: custom\n  base_url: http://localhost:11434/v1\n  context_length: 8192\n",
            "qwen3:8b",
            {"provider": "custom", "base_url": "http://localhost:11434/v1", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "localhost:11434" in info
        assert "8K" in info

    def test_cloud_endpoint_hidden(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: test-model\n  provider: openrouter\n",
                                  "test-model",
                                  {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key": "k"})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "Endpoint" not in info

    def test_million_context_format(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: test-model\n  context_length: 1000000\n",
                                  "test-model",
                                  {"provider": "", "base_url": "", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "1.0M" in info

    def test_missing_config(self, runner, tmp_path):
        """No config.yaml should not crash."""
        p1, p2, p3 = _patch_info(tmp_path, None,  # don't create config
                                  "anthropic/claude-sonnet-4.6",
                                  {"provider": "openrouter", "base_url": "", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "Model" in info
        assert "Context" in info

    def test_runtime_resolution_failure_doesnt_crash(self, runner, tmp_path):
        """If runtime resolution raises, should still produce output."""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("model:\n  default: test-model\n  context_length: 4096\n")
        with patch("gateway.run._hermes_home", tmp_path), \
             patch("gateway.run._resolve_gateway_model", return_value="test-model"), \
             patch("gateway.run._resolve_runtime_agent_kwargs", side_effect=RuntimeError("no creds")):
            info = runner._format_session_info()
        assert "4K" in info
        assert "config" in info

    def test_aops_chinese_session_info_uses_localized_labels_and_config_source(
        self, runner, tmp_path, monkeypatch
    ):
        from agent import i18n
        from gateway import aops_i18n

        monkeypatch.setenv("HERMES_LANGUAGE", "zh")
        i18n.reset_language_cache()
        aops_i18n.reset_aops_language_cache()
        p1, p2, p3 = _patch_info(
            tmp_path,
            (
                "model:\n"
                "  default: qwen-test\n"
                "  provider: custom\n"
                "  base_url: http://localhost:11434/v1\n"
                "  context_length: 131072\n"
            ),
            "qwen-test",
            {
                "provider": "custom",
                "base_url": "http://localhost:11434/v1",
                "api_key": "",
            },
        )
        try:
            with p1, p2, p3:
                info = runner._format_session_info(aops=True)
        finally:
            i18n.reset_language_cache()
            aops_i18n.reset_aops_language_cache()
        assert "◆ 模型：`qwen-test`" in info
        assert "◆ 提供方：custom" in info
        assert "◆ 上下文：131K 令牌（配置指定）" in info
        assert "◆ 服务地址：http://localhost:11434/v1" in info
        assert "◆ Model:" not in info
        assert "detected" not in info

    def test_aops_chinese_session_info_localizes_detected_context_source(
        self, runner, tmp_path, monkeypatch
    ):
        from agent import i18n
        from gateway import aops_i18n

        monkeypatch.setenv("HERMES_LANGUAGE", "zh")
        i18n.reset_language_cache()
        aops_i18n.reset_aops_language_cache()
        p1, p2, p3 = _patch_info(
            tmp_path,
            "model:\n  default: qwen-detected\n  provider: custom\n",
            "qwen-detected",
            {"provider": "custom", "base_url": "", "api_key": ""},
        )
        try:
            with p1, p2, p3, patch(
                "agent.model_metadata.get_model_context_length",
                return_value=131072,
            ):
                info = runner._format_session_info(aops=True)
        finally:
            i18n.reset_language_cache()
            aops_i18n.reset_aops_language_cache()
        assert "◆ 上下文：131K 令牌（自动检测）" in info

    def test_aops_chinese_session_info_localizes_default_context_source(
        self, runner, tmp_path, monkeypatch
    ):
        from agent import i18n
        from agent.model_metadata import DEFAULT_FALLBACK_CONTEXT
        from gateway import aops_i18n

        monkeypatch.setenv("HERMES_LANGUAGE", "zh")
        i18n.reset_language_cache()
        aops_i18n.reset_aops_language_cache()
        p1, p2, p3 = _patch_info(
            tmp_path,
            "model:\n  default: unknown-model\n  provider: custom\n",
            "unknown-model",
            {"provider": "custom", "base_url": "", "api_key": ""},
        )
        try:
            with p1, p2, p3, patch(
                "agent.model_metadata.get_model_context_length",
                return_value=DEFAULT_FALLBACK_CONTEXT,
            ):
                info = runner._format_session_info(aops=True)
        finally:
            i18n.reset_language_cache()
            aops_i18n.reset_aops_language_cache()
        assert "默认值；可在配置中设置 model.context_length 进行覆盖" in info

    def test_aops_english_session_info_remains_english(
        self, runner, tmp_path, monkeypatch
    ):
        from agent import i18n
        from gateway import aops_i18n

        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        aops_i18n.reset_aops_language_cache()
        p1, p2, p3 = _patch_info(
            tmp_path,
            "model:\n  default: test-model\n  context_length: 32768\n",
            "test-model",
            {"provider": "custom", "base_url": "", "api_key": ""},
        )
        try:
            with p1, p2, p3:
                info = runner._format_session_info(aops=True)
        finally:
            i18n.reset_language_cache()
            aops_i18n.reset_aops_language_cache()
        assert "◆ Model: `test-model`" in info
        assert "◆ Provider: custom" in info
        assert "◆ Context: 32K tokens (config)" in info


class TestResetNoticeSessionInfo:
    """#59003: the auto-reset banner must report the serving profile's config,
    not the multiplexer's base config."""

    _RUNTIME = {"provider": "", "base_url": "", "api_key": ""}

    def _source(self):
        from gateway.config import Platform
        from gateway.session import SessionSource
        return SessionSource(
            platform=Platform.TELEGRAM, chat_id="123", user_id="u1",
            profile="planner",
        )

    def _aops_source(self):
        from gateway.config import Platform
        from gateway.session import SessionSource

        return SessionSource(
            platform=Platform.AOPS,
            chat_id="conv-123",
            user_id="u1",
            profile="planner",
        )

    def _homes(self, tmp_path):
        base = tmp_path / "base"
        profile = tmp_path / "profiles" / "planner"
        profile.mkdir(parents=True)
        base.mkdir()
        base.joinpath("config.yaml").write_text(
            "model:\n  default: base-model\n  provider: custom\n  context_length: 1000\n")
        profile.joinpath("config.yaml").write_text(
            "model:\n  default: profile-model\n  provider: anthropic\n  context_length: 2000\n")
        return base, profile

    def test_multiplex_uses_profile_config(self, runner, tmp_path):
        from types import SimpleNamespace
        base, profile = self._homes(tmp_path)
        runner.config = SimpleNamespace(multiplex_profiles=True)
        with patch("gateway.run._hermes_home", base), \
             patch.object(GatewayRunner, "_resolve_profile_home_for_source", return_value=profile), \
             patch("gateway.run._resolve_runtime_agent_kwargs", return_value=self._RUNTIME):
            info = runner._reset_notice_session_info(self._source())
        assert "profile-model" in info
        assert "anthropic" in info
        assert "base-model" not in info

    def test_single_profile_uses_base_config(self, runner, tmp_path):
        from types import SimpleNamespace
        base, _profile = self._homes(tmp_path)
        runner.config = SimpleNamespace(multiplex_profiles=False)
        with patch("gateway.run._hermes_home", base), \
             patch("gateway.run._resolve_runtime_agent_kwargs", return_value=self._RUNTIME):
            info = runner._reset_notice_session_info(self._source())
        assert "base-model" in info
        assert "profile-model" not in info

    def test_aops_source_selects_aops_formatter(self, runner):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        runner.config = SimpleNamespace(multiplex_profiles=False)
        runner._format_session_info = MagicMock(return_value="localized")

        assert runner._reset_notice_session_info(self._aops_source()) == "localized"
        runner._format_session_info.assert_called_once_with(aops=True)
