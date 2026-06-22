from pathlib import Path


def test_aops_bundle_example_config_defaults_busy_input_mode_queue():
    script = Path("packaging/offline/build_aops_bundle.sh").read_text(encoding="utf-8")
    assert 'display:' in script
    assert 'busy_input_mode: queue' in script


def test_aops_offline_installer_self_checks_cache_layout():
    script = Path("packaging/offline/install_aops_offline.sh").read_text(encoding="utf-8")
    assert "run_post_install_self_check" in script
    assert 'HERMES_HOME="$tmp_home/.hermes" "$VENV_DIR/bin/python"' in script
    assert "from hermes_cli.config import ensure_hermes_home" in script
    assert "hermes_cli.config=" in script
    assert 'home / "cache" / "images"' in script
    assert 'home / "cache" / "audio"' in script
    assert 'home / "cache" / "videos"' in script
    assert 'home / "cache" / "documents"' in script
    assert 'home / "image_cache"' in script
    assert 'home / "audio_cache"' in script
    assert "legacy cache dirs were created" in script
    assert "Post-install self-check failed" in script
    assert "Venv Python: $VENV_DIR/bin/python" in script


def test_aops_offline_installer_supports_tec01_payload():
    script = Path("packaging/offline/install_aops_offline.sh").read_text(encoding="utf-8")
    assert "--config-payload" in script
    assert "--apply-config" in script
    assert "--upgrade" in script
    assert "--preserve-config" in script
    assert "python\" -m hermes_cli.remote_config apply --payload" in script


def test_aops_bundle_includes_tec01_oneclick_script():
    build = Path("packaging/offline/build_aops_bundle.sh").read_text(encoding="utf-8")
    script = Path("packaging/offline/tec01_oneclick_install.sh").read_text(encoding="utf-8")
    assert 'cp "$SCRIPT_DIR/tec01_oneclick_install.sh" "$BUNDLE_DIR/tec01_oneclick_install.sh"' in build
    assert "aops-channel-interface.md" in build
    assert "--set KEY=VALUE" in script
    assert "--template-url" in script
    assert "--template-file" in script
    assert "write_default_template" in script
    assert "AOPS_BOT_TOKEN" in script
    assert "select_profile" in script
    assert "json.dumps(sys.argv[2:]" in script
    assert "set_items = json.loads" in script
    assert "decode_markdown_escapes" in script
    assert 'name = "default"' in script
    assert "restartOtherRunningProfilesAfterUpgrade" in script
    assert "restart_other_running_profiles_after_upgrade" in script
    assert "gateway.pid" in script
    assert "'hermes', '-p', profile, 'gateway', 'restart'" in script
    assert "hermes-gateway-{profile}.service" in script
    assert ".aops_bundle_sha256" in script
    assert "RUNTIME_UPDATE_NEEDED" in script
    assert "Hermes runtime bundle sha changed; will upgrade runtime" in script
    assert "RUNTIME_CHANGED=true" in script
    assert "install_skill_zips" in script
    assert "zipfile.is_zipfile" in script
    assert "--profile '$PROFILE_NAME'" in script
    assert "useradd -m -s /bin/bash" in script
    assert "loginctl enable-linger" in script
    assert "gateway install --force" in script
    assert "ensure_gateway_service_installed \"start\"" in script
    assert "ensure_gateway_service_installed \"restart\"" in script
    assert '"platform_toolsets"' in script
    assert '"terminal"' in script
    assert '"browser"' not in script
    assert "--config-payload" not in script


def test_aops_profile_template_defaults_to_terminal_linux_toolsets():
    template = Path("packaging/offline/templates/aops-profile-template.yaml").read_text(encoding="utf-8")
    assert "platform_toolsets:" in template
    assert "    - terminal" in template
    assert "    - file" in template
    assert "    - code_execution" in template
    assert "    - messaging" in template
    assert "disabled:" in template
    assert "          - browser" in template
    assert "          - web" in template
    assert "unsupportedReasons:" in template


def test_aops_channel_interface_document_is_present():
    doc = Path("docs/aops-channel-interface.md").read_text(encoding="utf-8")
    assert "Runtime Agent Report" in doc
    assert "Silent Slash Commands" in doc
    assert "Toolsets" in doc
    assert "Curl One-Click Profile" in doc
    assert "必须同步更新本文档" in doc
