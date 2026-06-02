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
    assert "useradd -m -s /bin/bash" in script
    assert "loginctl enable-linger" in script
    assert "hermes gateway install --force" in script
    assert "ensure_gateway_service_installed \"start\"" in script
    assert "ensure_gateway_service_installed \"restart\"" in script
    assert "--config-payload" in script
