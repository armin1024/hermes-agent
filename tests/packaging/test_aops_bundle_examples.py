from pathlib import Path


def test_aops_bundle_example_config_defaults_busy_input_mode_queue():
    script = Path("packaging/offline/build_aops_bundle.sh").read_text(encoding="utf-8")
    assert 'display:' in script
    assert 'busy_input_mode: queue' in script


def test_aops_offline_installer_self_checks_cache_layout():
    script = Path("packaging/offline/install_aops_offline.sh").read_text(encoding="utf-8")
    assert "run_post_install_self_check" in script
    assert 'HERMES_HOME="$tmp_home/.hermes" "$VENV_DIR/bin/python"' in script
    assert "AOPS_SELF_CHECK_TIMEOUT_SECS" in script
    assert "selfcheck_stage=timeout" in script
    assert "Post-install self-check timed out after" in script
    assert "continuing because runtime files were already installed" in script
    assert "selfcheck_mode=aops-overlay-light" in script
    assert "from hermes_cli.config import ensure_hermes_home" in script
    assert "hermes_cli.config=" in script
    assert "import cron.jobs as cron_jobs_mod" in script
    assert "import cron.scheduler as cron_scheduler_mod" in script
    assert 'home / "cache" / "images"' in script
    assert 'home / "cache" / "audio"' in script
    assert 'home / "cache" / "videos"' in script
    assert 'home / "cache" / "documents"' in script
    assert 'home / "image_cache"' in script
    assert 'home / "audio_cache"' in script
    assert "legacy cache dirs were created" in script
    assert "AIOHTTP_AVAILABLE" in script
    assert "aops_aiohttp_available=" in script
    assert "AOPS runtime dependency is missing: aiohttp is not importable" in script
    assert "AOPS env mapping failed" in script
    assert "load_gateway_config" not in script
    assert "discover_plugins" not in script
    assert "get_connected_platforms" not in script
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
    assert "AOPS_WHEEL_REQUIREMENTS" in build
    assert "aiohttp==3.13.4" in build
    assert "ensure_aops_runtime_wheels" in build
    assert "--platform manylinux2014_x86_64" in build
    assert "--set KEY=VALUE" in script
    assert "--template-url" in script
    assert "--template-file" in script
    assert "write_default_template" in script
    assert "AOPS_BOT_TOKEN" in script
    assert "select_profile" in script
    assert "json.dumps(sys.argv[2:]" in script
    assert "set_items = json.loads" in script
    assert "decode_markdown_escapes" in script
    assert "top_level_skills = payload.get(\"skills\")" in script
    assert "config_block[\"skills\"] = deepcopy(top_level_skills)" in script
    assert "install_preinstall_skills" in script
    assert "--skip-skills" in script
    assert "skills-preinstall-result.json" in script
    assert "hub.create_source_router = lambda auth=None: [ClawHubSource()]" in script
    assert 'name = "default"' in script
    assert "restartOtherRunningProfilesAfterUpgrade" in script
    assert "restart_other_running_profiles_after_upgrade" in script
    assert "gateway.pid" in script
    assert "CURRENT_PROFILE=$(shell_quote \"$PROFILE_NAME\")" in script
    assert '("default", root)' in script
    assert "hermes-gateway.service" in script
    assert "controlled_gateway_lifecycle" in script
    assert "skip SIGUSR1 restart" in script
    assert "runtime did not become ready within" in script
    assert "startup is blocked by a lazy dependency install" in script
    assert "systemd is active with MainPID" in script
    assert "systemd-active-runtime" in script
    assert "::recover::" in script
    assert "Recovering Hermes gateway profile" in script
    assert "restart-summary.json" in script
    assert "allowGatewayLazyInstalls" in script
    assert "security.allow_lazy_installs" in script
    assert "set security.allow_lazy_installs=false" in script
    assert "record_lazy_installs_change_if_needed" in script
    assert "gateway-lazy-installs-other.json" in script
    assert "validate_aops_gateway_config_for_profile" in script
    assert "AOPS platform is absent from gateway_state.json" in script
    assert "runtime is running with AOPS connected" in script
    assert "REQUIRE_AOPS=$(shell_quote \"$required\")" in script
    assert '"platforms": {' in script
    assert '"aops": {' in script
    assert '"base_url": "${env.AOPS_BOT_URL}"' in script
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
    assert "gateway install --force --no-start-now --start-on-login" in script
    assert "ensure_gateway_service_installed \"start\"" in script
    assert "ensure_gateway_service_installed \"restart\"" in script
    assert '"platform_toolsets"' in script
    assert '"terminal"' in script
    assert '"browser"' in script
    assert "--config-payload" not in script


def test_tec01_oneclick_aops_validation_is_lightweight():
    script = Path("packaging/offline/tec01_oneclick_install.sh").read_text(encoding="utf-8")
    start = script.index("validate_aops_gateway_config_for_profile()")
    end = script.index("print_restart_summary()", start)
    validation = script[start:end]
    python_body = validation.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]

    assert "from gateway.config import" not in validation
    assert "load_gateway_config" not in validation
    assert "discover_plugins" not in validation
    assert "get_connected_platforms" not in validation
    assert "${" not in python_body
    assert '"' not in python_body
    assert "chr(34)" in python_body
    assert "chr(36) + chr(123)" in python_body
    assert "yaml.safe_load" in validation
    assert "AOPS_BOT_TOKEN" in validation
    assert "AOPS_BOT_URL" in validation
    assert "AOPS_BASE_URL" in validation
    assert "connectedPlatforms" in validation


def test_dist_oneclick_aops_validation_matches_source_script():
    source = Path("packaging/offline/tec01_oneclick_install.sh").read_text(encoding="utf-8")
    dist = Path("dist-aops-latest/install-oneclick.sh").read_text(encoding="utf-8")

    assert dist == source


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
