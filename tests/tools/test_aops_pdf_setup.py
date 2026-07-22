from copy import deepcopy

import yaml

from tools.aops_pdf_setup import apply_to_config, ensure_pdf_capabilities


def test_pdf_setup_preserves_model_and_appends_required_tools():
    config = {
        "model": {
            "provider": "custom",
            "model": "internal-model",
            "default": "fallback-name",
            "base_url": "https://gateway.internal/v1",
            "api_mode": "chat_completions",
            "api_key_env": "MODEL_KEY",
        },
        "platform_toolsets": {"aops": ["terminal"], "cli": ["skills"]},
        "aops": {"toolsets": {"disabled": ["browser", "vision", "video"]}},
    }
    original_model = deepcopy(config["model"])

    changed = ensure_pdf_capabilities(config)

    assert config["model"] == {**original_model, "supports_vision": True}
    assert config["platform_toolsets"]["aops"] == ["terminal", "file", "vision"]
    assert config["platform_toolsets"]["cli"] == ["skills", "file", "vision"]
    assert config["aops"]["toolsets"]["disabled"] == ["browser", "video"]
    assert "model.supports_vision" in changed


def test_pdf_setup_is_idempotent_and_writes_existing_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model:\n  model: qwen-from-config\n", encoding="utf-8")

    first = apply_to_config(config_path)
    first_content = config_path.read_text(encoding="utf-8")
    second = apply_to_config(config_path)

    assert first["changed"] is True
    assert first["model"] == "qwen-from-config"
    assert second["changed"] is False
    assert config_path.read_text(encoding="utf-8") == first_content
    loaded = yaml.safe_load(first_content)
    assert loaded["model"]["model"] == "qwen-from-config"
    assert "file" in loaded["platform_toolsets"]["aops"]
    assert "vision" in loaded["platform_toolsets"]["aops"]
    assert "terminal" in loaded["platform_toolsets"]["cli"]
