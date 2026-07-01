import json

import pytest


def test_remote_config_applies_allowed_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    payload = {
        "taskId": "task-1",
        "config": {
            "modelGateway": {
                "baseUrl": "http://llm.example/v1",
                "model": "qwen3-32b",
                "apiKey": "model-key-1",
            },
            "aops": {
                "AOPS_BOT_TOKEN": "token-1",
                "AOPS_BOT_URL": "http://aops.example",
                "AOPS_HOME_CHANNEL": "conv-1",
                "AOPS_HOME_CHANNEL_NAME": "AOPS Home",
                "CLAWHUB_REGISTRY": "http://tec01.example/clawhub",
                "AOPS_BASE_URL": "http://base.example",
                "AOPS_API_KEY": "api-key-1",
                "AOPS_CONNECT_TIMEOUT": "90",
                "HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT": "90",
            },
            "approvals": {"mode": "off"},
            "display": {"busy_input_mode": "queue"},
            "userInstructions": {
                "title": "用户初始化提示词",
                "sections": [
                    {
                        "heading": "角色偏好",
                        "content": "请优先使用中文回答。",
                        "bullets": ["回答要简洁", "遇到风险先提示"],
                    }
                ],
            },
            "hindsight": {
                "mode": "local_external",
                "apiUrl": "http://hindsight.example",
                "apiKey": "hindsight-key-1",
                "bankIdTemplate": "users-{user}",
                "budget": "mid",
                "timeout": 120,
            },
        },
    }
    path = tmp_path / "task.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    from hermes_cli.remote_config import apply_payload
    from hermes_cli.config import load_config, load_env

    result = apply_payload(str(path), skip_skills=True)

    assert result["ok"] is True
    env = load_env()
    assert env["AOPS_BOT_TOKEN"] == "token-1"
    assert env["AOPS_BOT_URL"] == "http://aops.example"
    assert env["AOPS_HOME_CHANNEL"] == "conv-1"
    assert env["AOPS_HOME_CHANNEL_NAME"] == "AOPS Home"
    assert env["CLAWHUB_REGISTRY"] == "http://tec01.example/clawhub"
    assert env["AOPS_BASE_URL"] == "http://base.example"
    assert env["AOPS_API_KEY"] == "api-key-1"
    assert env["AOPS_CONNECT_TIMEOUT"] == "90"
    assert env["HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT"] == "90"
    assert env["MODEL_GATEWAY_API_KEY"] == "model-key-1"
    assert "OPENAI_API_KEY" not in env
    assert env["HINDSIGHT_API_KEY"] == "hindsight-key-1"
    assert env["HINDSIGHT_API_URL"] == "http://hindsight.example"

    cfg = load_config()
    assert cfg["model"]["provider"] == "custom"
    assert cfg["model"]["base_url"] == "http://llm.example/v1"
    assert cfg["model"]["default"] == "qwen3-32b"
    assert cfg["model"]["api_key_env"] == "MODEL_GATEWAY_API_KEY"
    assert cfg["approvals"]["mode"] == "off"
    assert cfg["display"]["busy_input_mode"] == "queue"
    assert cfg["memory"]["provider"] == "hindsight"
    assert cfg["platforms"]["aops"]["enabled"] is True
    assert cfg["platforms"]["aops"]["extra"]["base_url"] == "http://aops.example"

    user_md = tmp_path / "memories" / "USER.md"
    assert "请优先使用中文回答。" in user_md.read_text(encoding="utf-8")
    hindsight_cfg = json.loads((tmp_path / "hindsight" / "config.json").read_text(encoding="utf-8"))
    assert hindsight_cfg["mode"] == "local_external"
    assert hindsight_cfg["api_url"] == "http://hindsight.example"
    assert hindsight_cfg["api_key"] == "hindsight-key-1"
    assert hindsight_cfg["bank_id_template"] == "users-{user}"
    assert hindsight_cfg["timeout"] == 120


def test_remote_config_installs_top_level_preinstall_skills(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    calls = []

    def fake_do_install(slug, **kwargs):
        calls.append((slug, kwargs))
        kwargs["console"].print("installed")

    monkeypatch.setattr("hermes_cli.skills_hub.do_install", fake_do_install)
    path = tmp_path / "task.json"
    path.write_text(
        json.dumps({"skills": {"preinstall": ["tec01-review", "tec01-ops"]}}),
        encoding="utf-8",
    )

    from hermes_cli.remote_config import apply_payload

    result = apply_payload(str(path))

    assert result["skills"] == [
        {"slug": "tec01-review", "status": "success", "returnCode": 0, "source": "clawhub", "output": "installed"},
        {"slug": "tec01-ops", "status": "success", "returnCode": 0, "source": "clawhub", "output": "installed"},
    ]
    assert [slug for slug, _kwargs in calls] == ["tec01-review", "tec01-ops"]
    assert all(kwargs["source"] == "clawhub" for _slug, kwargs in calls)
    assert all(kwargs["ignore_scan_policy"] is True for _slug, kwargs in calls)


def test_remote_config_installs_config_preinstall_skills(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    calls = []

    def fake_do_install(slug, **kwargs):
        calls.append((slug, kwargs))
        kwargs["console"].print("installed")

    monkeypatch.setattr("hermes_cli.skills_hub.do_install", fake_do_install)
    path = tmp_path / "task.json"
    path.write_text(
        json.dumps({"config": {"skills": {"preinstall": ["tec01-review"]}}}),
        encoding="utf-8",
    )

    from hermes_cli.remote_config import apply_payload

    result = apply_payload(str(path))

    assert result["skills"][0]["slug"] == "tec01-review"
    assert len(calls) == 1


def test_remote_config_rejects_invalid_official_enum(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "task.json"
    path.write_text(
        json.dumps({"config": {"approvals": {"mode": "never"}}}),
        encoding="utf-8",
    )

    from hermes_cli.remote_config import RemoteConfigError, apply_payload

    with pytest.raises(RemoteConfigError, match="config.approvals.mode"):
        apply_payload(str(path), skip_skills=True)


def test_remote_config_preserves_existing_values_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "memories").mkdir(parents=True)
    (tmp_path / "hindsight").mkdir(parents=True)
    (tmp_path / ".env").write_text(
        "AOPS_BOT_URL=http://old-aops\nOPENAI_API_KEY=old-key\n",
        encoding="utf-8",
    )
    (tmp_path / "config.yaml").write_text(
        "model:\n  base_url: http://old-llm/v1\n  default: old-model\n"
        "approvals:\n  mode: smart\n",
        encoding="utf-8",
    )
    (tmp_path / "memories" / "USER.md").write_text("existing user prompt\n", encoding="utf-8")
    (tmp_path / "hindsight" / "config.json").write_text('{"api_url":"http://old-hindsight"}\n', encoding="utf-8")
    payload = {
        "options": {"upgrade": True},
        "config": {
            "modelGateway": {
                "baseUrl": "http://new-llm/v1",
                "model": "new-model",
                "apiKey": "new-key",
            },
            "aops": {"AOPS_BOT_URL": "http://new-aops"},
            "approvals": {"mode": "off"},
            "userInstructions": {"content": "new prompt"},
            "hindsight": {"apiUrl": "http://new-hindsight"},
        }
    }
    path = tmp_path / "task.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    from hermes_cli.remote_config import apply_payload
    from hermes_cli.config import load_config, load_env

    result = apply_payload(str(path), skip_skills=True)

    env = load_env()
    assert env["AOPS_BOT_URL"] == "http://old-aops"
    assert env["OPENAI_API_KEY"] == "old-key"
    assert env["MODEL_GATEWAY_API_KEY"] == "new-key"
    cfg = load_config()
    assert cfg["model"]["base_url"] == "http://old-llm/v1"
    assert cfg["model"]["default"] == "old-model"
    assert cfg["model"]["api_key_env"] == "MODEL_GATEWAY_API_KEY"
    assert cfg["approvals"]["mode"] == "smart"
    assert (tmp_path / "memories" / "USER.md").read_text(encoding="utf-8") == "existing user prompt\n"
    assert json.loads((tmp_path / "hindsight" / "config.json").read_text(encoding="utf-8"))["api_url"] == "http://old-hindsight"
    assert result["envChanged"] == ["MODEL_GATEWAY_API_KEY"]
    assert result["userInstructions"] is None
    assert result["hindsight"] is None


def test_remote_config_preserves_existing_model_gateway_key_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=openai-key\nMODEL_GATEWAY_API_KEY=old-model-gateway-key\n",
        encoding="utf-8",
    )
    payload = {
        "options": {"upgrade": True},
        "config": {
            "modelGateway": {
                "baseUrl": "http://llm.example/v1",
                "model": "qwen3-32b",
                "apiKey": "new-model-gateway-key",
            },
        },
    }
    path = tmp_path / "task.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    from hermes_cli.remote_config import apply_payload
    from hermes_cli.config import load_config, load_env

    result = apply_payload(str(path), skip_skills=True)

    env = load_env()
    assert env["OPENAI_API_KEY"] == "openai-key"
    assert env["MODEL_GATEWAY_API_KEY"] == "old-model-gateway-key"
    assert load_config()["model"]["api_key_env"] == "MODEL_GATEWAY_API_KEY"
    assert "MODEL_GATEWAY_API_KEY" not in result["envChanged"]


def test_remote_config_overwrites_existing_values_when_requested(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "memories").mkdir(parents=True)
    (tmp_path / ".env").write_text("AOPS_BOT_URL=http://old-aops\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("approvals:\n  mode: smart\n", encoding="utf-8")
    (tmp_path / "memories" / "USER.md").write_text("existing prompt\n", encoding="utf-8")
    payload = {
        "options": {"upgrade": True, "overwriteExistingConfig": True},
        "config": {
            "aops": {"AOPS_BOT_URL": "http://new-aops"},
            "approvals": {"mode": "off"},
            "userInstructions": {"content": "new prompt"},
        },
    }
    path = tmp_path / "task.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    from hermes_cli.remote_config import apply_payload
    from hermes_cli.config import load_config, load_env

    result = apply_payload(str(path), skip_skills=True)

    assert load_env()["AOPS_BOT_URL"] == "http://new-aops"
    assert load_config()["approvals"]["mode"] == "off"
    assert (tmp_path / "memories" / "USER.md").read_text(encoding="utf-8") == "new prompt\n"
    assert result["overwriteExistingConfig"] is True


def test_remote_config_applies_env_configyaml_soul_and_user_memory(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    payload = {
        "profile": {"name": "hermes-1", "noBundledSkills": True},
        "config": {
            "env": {
                "AOPS_BOT_TOKEN": "tok",
                "MODEL_GATEWAY_API_KEY": "model-key",
                "CUSTOM_FLAG": "enabled",
            },
            "configYaml": {
                "model": {
                    "provider": "custom",
                    "model": "qwen3-32b",
                    "base_url": "http://llm/v1",
                    "api_key_env": "MODEL_GATEWAY_API_KEY",
                },
                "display": {"busy_input_mode": "queue"},
                "checkpoints": {"enabled": True},
            },
            "userMemory": "# User\n\nPrefer Chinese.",
            "soul": "# Soul\n\nFocused AOPS agent.",
            "hindsight": {
                "mode": "local_external",
                "api_url": "http://hindsight",
                "api_key": "hk",
            },
        },
    }
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    from hermes_cli.remote_config import _activate_profile_for_payload, apply_payload
    from hermes_cli.config import load_config, load_env, get_hermes_home

    assert _activate_profile_for_payload(payload, None) == "hermes-1"
    result = apply_payload(str(path), skip_skills=True)

    profile_home = tmp_path / ".hermes" / "profiles" / "hermes-1"
    assert get_hermes_home() == profile_home
    assert result["envChanged"] == ["AOPS_BOT_TOKEN", "MODEL_GATEWAY_API_KEY", "CUSTOM_FLAG", "HINDSIGHT_API_KEY", "HINDSIGHT_API_URL"]
    env = load_env()
    assert env["CUSTOM_FLAG"] == "enabled"
    cfg = load_config()
    assert cfg["model"]["model"] == "qwen3-32b"
    assert cfg["model"]["base_url"] == "http://llm/v1"
    assert cfg["checkpoints"]["enabled"] is True
    assert (profile_home / "memories" / "USER.md").read_text(encoding="utf-8") == "# User\n\nPrefer Chinese.\n"
    assert (profile_home / "SOUL.md").read_text(encoding="utf-8") == "# Soul\n\nFocused AOPS agent.\n"
    hindsight = json.loads((profile_home / "hindsight" / "config.json").read_text(encoding="utf-8"))
    assert hindsight["bank_id_template"] == "users-{user}"
    assert hindsight["api_key"] == "hk"


def test_remote_config_rejects_invalid_custom_env_name(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "payload.json"
    path.write_text(json.dumps({"config": {"env": {"BAD-NAME": "x"}}}), encoding="utf-8")

    from hermes_cli.remote_config import RemoteConfigError, apply_payload

    with pytest.raises(RemoteConfigError, match="invalid env var name"):
        apply_payload(str(path), skip_skills=True)


def test_remote_config_schema_exposes_official_values():
    from hermes_cli.remote_config import schema

    data = schema()
    assert data["approvals.mode"]["values"] == ["manual", "smart", "off"]
    assert data["display.busy_input_mode"]["values"] == ["interrupt", "queue", "steer"]
