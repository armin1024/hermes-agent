import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


def _embedded_python(script: str, function_name: str) -> str:
    start = script.index(f"{function_name}() {{")
    body_start = script.index("<<'PY'\n", start) + len("<<'PY'\n")
    body_end = script.index("\nPY\n", body_start)
    return script[body_start:body_end]


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
    assert "import hindsight_client" in script
    assert "hindsight_client_available=True" in script
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
    assert "aops-event-center-interface.md" in build
    assert "AOPS_WHEEL_REQUIREMENTS" in build
    assert "aiohttp==3.13.4" in build
    assert "PyMuPDF==1.26.0" in build
    assert 'PDF_SKILL_DIR="skills/productivity/ocr-and-documents"' in build
    assert "hindsight-client==0.6.1" in build
    assert "ensure_hindsight_runtime_wheels" in build
    assert "ensure_aops_runtime_wheels" in build
    assert "--platform manylinux2014_x86_64" in build
    assert "--set KEY=VALUE" in script
    assert "--template-url" in script
    assert "--template-file" in script
    assert "write_default_template" in script
    assert "AOPS_BOT_TOKEN" in script
    assert "select_profile" in script
    assert 'chown "$TARGET_USER":"$TARGET_USER" "$PROFILE_JSON"' in script
    assert 'chmod 600 "$PROFILE_JSON"' in script
    assert "resolve_aops_owner_bank_plan" in script
    assert "inject_current_owner_bank_into_payload" in script
    assert "sync_hindsight_owner_banks" in script
    assert "/other/aops/bot-token/owner-user" in script
    assert "default-all-profiles" in script
    assert "AOPS_OWNER_LOOKUP_ATTEMPTS" in script
    assert "default_create_lookup = selected_action == \"create\" and selected_profile == \"default\"" in script
    assert "timeout = 5.0 if default_create_lookup" in script
    assert "attempts = 1 if default_create_lookup" in script
    assert "named-profile-default-bank" in script
    assert "default-config-bank" in script
    assert "--sync-other-profiles true|false" in script
    assert "syncOtherProfiles is only supported when updating an existing profile" in script
    assert "build_profile_sync_payload" in script
    assert "profile_sync_unique_profiles" in script
    assert script.count("done < <(profile_sync_unique_profiles)") == 2
    assert "reserved_default_profile_directory_ignored" in script
    assert "duplicate_profile_skipped" in script
    assert "profile-sync-summary.json" in script
    assert "restart_other_profiles_after_owner_bank_sync" in script
    preflight_call = script.index("resolve_aops_owner_bank_plan | tee")
    assert "resolve_aops_owner_bank_plan | tee \"$OWNER_BANK_PLAN_LOG_JSON\"" in script
    assert "sync_hindsight_owner_banks | tee \"$OWNER_BANK_SYNC_LOG_JSON\"" in script
    assert "resolve_aops_owner_bank_plan | tee \"$OWNER_BANK_PLAN_JSON\"" not in script
    assert "sync_hindsight_owner_banks | tee \"$OWNER_BANK_SYNC_RESULT_JSON\"" not in script
    assert preflight_call < script.index("inject_current_owner_bank_into_payload", preflight_call) < script.index("BUNDLE_STAMP=")
    assert "json.dumps(sys.argv[2:]" in script
    assert "set_items = json.loads" in script
    assert "decode_markdown_escapes" in script
    assert "top_level_skills = payload.get(\"skills\")" in script
    assert "config_block[\"skills\"] = deepcopy(top_level_skills)" in script
    assert "install_preinstall_skills" in script
    assert "builtin:ocr-and-documents" in script
    assert "get_bundled_skills_dir" in script
    assert "tools.aops_vision_probe" in script
    assert "tools.aops_pdf_setup" in script
    assert "ensure_aops_pdf_capabilities_for_profile" in script
    assert 'slugs.insert(0, \'builtin:ocr-and-documents\')' in script
    assert "model-vision-probe.json" in script
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
    assert "--bundle-cache-dir DIR" in script
    assert "HERMES_BUNDLE_CACHE_DIR" in script
    assert "install-timings.json" in script
    assert "[TIMING] stage=%s durationMs=%s" in script
    assert "wait_for_replacement" in script
    assert "running_replacement_after_timeout" in script
    assert "except subprocess.TimeoutExpired" in script
    assert "timed out and no running replacement" in script
    assert "active_agents = int(before.get(\"active_agents\") or 0)" in script
    assert "drain_timeout = 185 if active_agents > 0 else 30" in script
    assert "AOPS not connected within 15s" in script
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


def test_oneclick_installs_bundled_pdf_skill_without_clawhub(tmp_path):
    script = Path("packaging/offline/tec01_oneclick_install.sh").read_text(encoding="utf-8")
    installer = tmp_path / "install_builtin.py"
    start = script.index("install_preinstall_skills() {")
    body_start = script.index("<<'PY'\n", start) + len("<<'PY'\n")
    body_end = script.index("\nPY\"", body_start)
    installer.write_text(script[body_start:body_end], encoding="utf-8")
    profile = tmp_path / "profile"
    payload = tmp_path / "payload.json"
    # The audited PDF skill is mandatory even for old/custom payloads that do
    # not yet contain the new skills.preinstall template entry.
    payload.write_text(json.dumps({}), encoding="utf-8")
    env = os.environ.copy()
    env["HERMES_BUNDLED_SKILLS"] = str(Path("skills").resolve())

    completed = subprocess.run(
        [sys.executable, str(installer), str(payload), str(profile)],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    result = json.loads(completed.stdout)
    assert result["installed"][0]["status"] == "success"
    assert (profile / "skills" / "productivity" / "ocr-and-documents" / "SKILL.md").is_file()


def test_dist_oneclick_aops_validation_matches_source_script():
    source = Path("packaging/offline/tec01_oneclick_install.sh").read_text(encoding="utf-8")
    dist = Path("dist-aops-latest/install-oneclick.sh").read_text(encoding="utf-8")

    assert dist == source


def test_tec01_multiuser_update_caches_inputs_reports_timings_and_limits_parallelism():
    script = Path("packaging/offline/tec01_multiuser_update.sh").read_text(encoding="utf-8")

    assert "--jobs N" in script
    assert 'JOBS="${HERMES_MULTIUSER_JOBS:-1}"' in script
    assert "--jobs must be an integer from 1 to 8" in script
    assert "prefetch_inputs" in script
    assert 'CACHED_INSTALLER="$RUN_DIR/install-oneclick.sh"' in script
    assert 'CACHED_TEMPLATE="$RUN_DIR/template.yaml"' in script
    assert 'bash "$UPDATE_INSTALLER_FILE" "${args[@]}"' in script
    assert '--sync-other-profiles true' in script
    assert "default_token_missing" in script
    assert 'process_profile "$user" "default" "$env_path"' in script
    assert 'process_profile "$user" "$profile"' not in script
    assert '--bundle-cache-dir "$UPDATE_BUNDLE_CACHE_DIR"' in script
    assert "run_user_worker" in script
    assert "merge_worker_results" in script
    assert "durationMs" in script
    assert "totalDurationMs" in script
    assert 'if [[ "$JOBS" -eq 1 ]]' in script


@pytest.mark.parametrize("overwrite_all", [False, True])
def test_oneclick_profile_sync_payload_filters_tokens_and_skips_skills(tmp_path, overwrite_all):
    script = Path("packaging/offline/tec01_oneclick_install.sh").read_text(encoding="utf-8")
    builder = tmp_path / "builder.py"
    builder.write_text(_embedded_python(script, "build_profile_sync_payload"), encoding="utf-8")
    home = tmp_path / "home"
    (home / ".hermes" / "profiles" / "ops-1").mkdir(parents=True)
    (home / ".hermes" / "profiles" / "default").mkdir(parents=True)
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({
        "config": {
            "env": {"AOPS_BOT_TOKEN": "secret", "AOPS_BOT_URL": "https://new.example"},
            "configYaml": {
                "model": {"model": "gpt-test"},
                "platforms": {"aops": {"token": "yaml-secret"}},
                "gateway": {"platforms": {"aops": {"token": "gateway-secret"}}},
            },
            "soul": "shared soul",
            "hindsight": {"bank_id": "shared-bank"},
        },
        "skills": {"preinstall": ["must-not-sync"]},
        "options": {
            "overwriteExistingConfig": overwrite_all,
            "overwriteFields": [
                "env.AOPS_BOT_TOKEN", "env.AOPS_BOT_URL",
                "configYaml.model", "configYaml.platforms.aops.token",
                "configYaml.gateway.platforms.aops.token", "soul", "hindsight",
            ],
        },
    }), encoding="utf-8")
    output = tmp_path / "sync.json"
    profiles = tmp_path / "profiles.json"
    summary = tmp_path / "summary.json"
    subprocess.run(
        [sys.executable, str(builder), str(payload), str(output), str(profiles), str(summary), "default", str(home)],
        check=True,
    )
    sync = json.loads(output.read_text(encoding="utf-8"))
    assert sync["config"]["env"] == {"AOPS_BOT_URL": "https://new.example"}
    assert "token" not in sync["config"]["configYaml"].get("platforms", {}).get("aops", {})
    assert "token" not in sync["config"]["configYaml"].get("gateway", {}).get("platforms", {}).get("aops", {})
    assert sync["config"]["soul"] == "shared soul"
    assert sync["config"]["hindsight"]["bank_id"] == "shared-bank"
    assert "skills" not in sync
    assert json.loads(profiles.read_text(encoding="utf-8")) == ["default", "ops-1"]
    protected = json.loads(summary.read_text(encoding="utf-8"))["protectedFields"]
    assert "env.AOPS_BOT_TOKEN" in protected
    assert "configYaml.platforms.aops.token" in protected
    assert "configYaml.gateway.platforms.aops.token" in protected
    warnings = json.loads(summary.read_text(encoding="utf-8"))["warnings"]
    assert warnings == [
        {
            "code": "reserved_default_profile_directory_ignored",
            "profile": "default",
            "path": str(home / ".hermes" / "profiles" / "default"),
            "message": "ignored reserved named-profile directory; default uses ~/.hermes",
        }
    ]


def test_oneclick_profile_sync_consumer_deduplicates_stale_entries(tmp_path):
    script = Path("packaging/offline/tec01_oneclick_install.sh").read_text(
        encoding="utf-8"
    )
    deduper = tmp_path / "dedupe.py"
    deduper.write_text(
        _embedded_python(script, "profile_sync_unique_profiles"),
        encoding="utf-8",
    )
    profiles = tmp_path / "profiles.json"
    profiles.write_text(
        json.dumps(["default", "ops-1", "default", "ops-1"]),
        encoding="utf-8",
    )
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"warnings": []}), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(deduper), str(profiles), str(summary)],
        check=True,
        text=True,
        capture_output=True,
    )

    assert completed.stdout.splitlines() == ["default", "ops-1"]
    assert completed.stderr.count("duplicate_profile_skipped") == 2
    assert json.loads(summary.read_text(encoding="utf-8"))["warnings"] == [
        {
            "code": "duplicate_profile_skipped",
            "profile": "default",
            "message": "duplicate profile entry skipped during synchronized update",
        },
        {
            "code": "duplicate_profile_skipped",
            "profile": "ops-1",
            "message": "duplicate profile entry skipped during synchronized update",
        },
    ]


def test_aops_profile_template_defaults_to_terminal_linux_toolsets():
    template = Path("packaging/offline/templates/aops-profile-template.yaml").read_text(encoding="utf-8")
    assert "platform_toolsets:" in template
    assert "    - terminal" in template
    assert "    - file" in template
    assert "    - code_execution" in template
    assert "    - messaging" in template
    assert "    - vision" in template
    assert "supports_vision: true" in template
    assert "builtin:ocr-and-documents" in template
    assert "disabled:" in template
    assert "          - browser" in template
    assert "          - vision" not in template
    assert "          - web" in template
    assert "unsupportedReasons:" in template
    assert "bank_id_template: users-{user}" not in template
    assert "authoritative static bank_id" in template
    assert "hindsight.bank_id:" in template
    assert "bank_id: ${hindsight.bank_id}" in template


def test_oneclick_default_update_reuses_default_owner_bank_for_all_profiles(tmp_path):
    script = Path("packaging/offline/tec01_oneclick_install.sh").read_text(encoding="utf-8")
    resolver = tmp_path / "resolver.py"
    resolver.write_text(_embedded_python(script, "resolve_aops_owner_bank_plan"), encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        owners = {"default-token": "user_default", "profile-token": "user_profile"}

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
            owner = self.owners.get(body.get("bot_token"))
            if owner is None:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"status":404,"msg":"missing","data":null}')
                return
            encoded = json.dumps({"status": 200, "msg": "ok", "data": {"owner_user_id": owner}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}/"
        hermes = tmp_path / ".hermes"
        child = hermes / "profiles" / "ops-1"
        child.mkdir(parents=True)
        (child / ".env").write_text(
            f"AOPS_BOT_TOKEN=profile-token\nAOPS_BOT_URL={base_url}\n",
            encoding="utf-8",
        )
        payload = tmp_path / "payload.json"
        payload.write_text(json.dumps({"config": {"env": {"AOPS_BOT_TOKEN": "default-token", "AOPS_BOT_URL": base_url}}}), encoding="utf-8")
        profile = tmp_path / "profile.json"
        profile.write_text(json.dumps({"profile": "default", "action": "update"}), encoding="utf-8")
        output = tmp_path / "plan.json"
        env = os.environ | {
            "HOME": str(tmp_path),
            "PAYLOAD_JSON": str(payload),
            "PROFILE_JSON": str(profile),
            "OWNER_BANK_PLAN_JSON": str(output),
            "AOPS_OWNER_LOOKUP_ATTEMPTS": "1",
        }
        subprocess.run([sys.executable, str(resolver)], check=True, env=env, capture_output=True, text=True)
        plan = json.loads(output.read_text(encoding="utf-8"))
        assert plan["mode"] == "default-all-profiles"
        assert [(item["profile"], item["bankId"]) for item in plan["profiles"]] == [
            ("default", "aops-tec01-user_default"),
            ("ops-1", "aops-tec01-user_default"),
        ]
        assert [item["source"] for item in plan["profiles"]] == ["default-owner-api", "default-owner-api"]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_oneclick_manual_bank_skips_owner_api_and_updates_default_profiles(tmp_path):
    script = Path("packaging/offline/tec01_oneclick_install.sh").read_text(encoding="utf-8")
    resolver = tmp_path / "resolver.py"
    resolver.write_text(_embedded_python(script, "resolve_aops_owner_bank_plan"), encoding="utf-8")
    hermes = tmp_path / ".hermes"
    child = hermes / "profiles" / "ops-1"
    child.mkdir(parents=True)
    (child / ".env").write_text("AOPS_BOT_TOKEN=profile-token\n", encoding="utf-8")
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"config": {
        "env": {"AOPS_BOT_TOKEN": "default-token", "AOPS_BOT_URL": "http://127.0.0.1:1"},
        "hindsight": {"bank_id": "manual_bank_001"},
    }}), encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"profile": "default", "action": "update"}), encoding="utf-8")
    output = tmp_path / "plan.json"
    env = os.environ | {
        "HOME": str(tmp_path),
        "PAYLOAD_JSON": str(payload),
        "PROFILE_JSON": str(profile),
        "OWNER_BANK_PLAN_JSON": str(output),
        "AOPS_OWNER_LOOKUP_ATTEMPTS": "1",
    }
    subprocess.run([sys.executable, str(resolver)], check=True, env=env, capture_output=True, text=True)
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["mode"] == "manual-default-all-profiles"
    assert [(item["profile"], item["bankId"], item["source"]) for item in plan["profiles"]] == [
        ("default", "manual_bank_001", "manual"),
        ("ops-1", "manual_bank_001", "manual"),
    ]


def test_oneclick_default_owner_api_failure_uses_existing_default_bank(tmp_path):
    script = Path("packaging/offline/tec01_oneclick_install.sh").read_text(encoding="utf-8")
    resolver = tmp_path / "resolver.py"
    resolver.write_text(_embedded_python(script, "resolve_aops_owner_bank_plan"), encoding="utf-8")
    hermes = tmp_path / ".hermes"
    (hermes / "hindsight").mkdir(parents=True)
    (hermes / "hindsight" / "config.json").write_text(json.dumps({"bank_id": "saved_default_bank"}), encoding="utf-8")
    child = hermes / "profiles" / "ops-1"
    child.mkdir(parents=True)
    (child / ".env").write_text("AOPS_BOT_TOKEN=profile-token\n", encoding="utf-8")
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"config": {"env": {
        "AOPS_BOT_TOKEN": "default-token", "AOPS_BOT_URL": "http://127.0.0.1:1",
    }}}), encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"profile": "default", "action": "update"}), encoding="utf-8")
    output = tmp_path / "plan.json"
    env = os.environ | {
        "HOME": str(tmp_path),
        "PAYLOAD_JSON": str(payload),
        "PROFILE_JSON": str(profile),
        "OWNER_BANK_PLAN_JSON": str(output),
        "AOPS_OWNER_LOOKUP_ATTEMPTS": "1",
        "AOPS_OWNER_LOOKUP_TIMEOUT": "0.1",
    }
    subprocess.run([sys.executable, str(resolver)], check=True, env=env, capture_output=True, text=True)
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert [(item["profile"], item["bankId"], item["source"]) for item in plan["profiles"]] == [
        ("default", "saved_default_bank", "default-config-fallback"),
        ("ops-1", "saved_default_bank", "default-config-fallback"),
    ]


@pytest.mark.parametrize("action", ["create", "update"])
def test_oneclick_named_profile_reuses_default_bank_without_owner_api(tmp_path, action):
    script = Path("packaging/offline/tec01_oneclick_install.sh").read_text(encoding="utf-8")
    resolver = tmp_path / "resolver.py"
    resolver.write_text(_embedded_python(script, "resolve_aops_owner_bank_plan"), encoding="utf-8")
    hermes = tmp_path / ".hermes"
    (hermes / "hindsight").mkdir(parents=True)
    (hermes / "hindsight" / "config.json").write_text(
        json.dumps({"bank_id": "default_shared_bank"}), encoding="utf-8"
    )
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps({
            "config": {
                "env": {
                    "AOPS_BOT_TOKEN": "named-token",
                    "AOPS_BOT_URL": "http://127.0.0.1:1",
                }
            }
        }),
        encoding="utf-8",
    )
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"profile": "ops-2", "action": action}), encoding="utf-8")
    output = tmp_path / "plan.json"
    env = os.environ | {
        "HOME": str(tmp_path),
        "PAYLOAD_JSON": str(payload),
        "PROFILE_JSON": str(profile),
        "OWNER_BANK_PLAN_JSON": str(output),
    }
    completed = subprocess.run([sys.executable, str(resolver)], check=True, env=env, capture_output=True, text=True)
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["mode"] == "named-profile-default-bank"
    assert plan["profiles"] == [{
        "profile": "ops-2",
        "hermesHome": str(hermes / "profiles" / "ops-2"),
        "ownerUserId": None,
        "bankId": "default_shared_bank",
        "previousBankId": "",
        "source": "default-config-bank",
    }]
    assert "owner-user" not in completed.stdout


def test_oneclick_new_named_profile_reuses_legacy_default_bank_field(tmp_path):
    script = Path("packaging/offline/tec01_oneclick_install.sh").read_text(encoding="utf-8")
    resolver = tmp_path / "resolver.py"
    resolver.write_text(_embedded_python(script, "resolve_aops_owner_bank_plan"), encoding="utf-8")
    hermes = tmp_path / ".hermes"
    (hermes / "hindsight").mkdir(parents=True)
    (hermes / "hindsight" / "config.json").write_text(
        json.dumps({"banks": {"hermes": {"bankId": "legacy_shared_bank"}}}), encoding="utf-8"
    )
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"config": {"env": {}}}), encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"profile": "ops-legacy", "action": "create"}), encoding="utf-8")
    output = tmp_path / "plan.json"
    env = os.environ | {
        "HOME": str(tmp_path),
        "PAYLOAD_JSON": str(payload),
        "PROFILE_JSON": str(profile),
        "OWNER_BANK_PLAN_JSON": str(output),
    }
    subprocess.run([sys.executable, str(resolver)], check=True, env=env, capture_output=True, text=True)
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["profiles"][0]["bankId"] == "legacy_shared_bank"
    assert plan["profiles"][0]["source"] == "default-config-bank"


def test_oneclick_new_named_profile_fails_without_valid_default_bank(tmp_path):
    script = Path("packaging/offline/tec01_oneclick_install.sh").read_text(encoding="utf-8")
    resolver = tmp_path / "resolver.py"
    resolver.write_text(_embedded_python(script, "resolve_aops_owner_bank_plan"), encoding="utf-8")
    (tmp_path / ".hermes").mkdir()
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"config": {"env": {}}}), encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"profile": "ops-missing", "action": "create"}), encoding="utf-8")
    output = tmp_path / "plan.json"
    env = os.environ | {
        "HOME": str(tmp_path),
        "PAYLOAD_JSON": str(payload),
        "PROFILE_JSON": str(profile),
        "OWNER_BANK_PLAN_JSON": str(output),
    }
    completed = subprocess.run([sys.executable, str(resolver)], env=env, capture_output=True, text=True)
    assert completed.returncode != 0
    assert "default Hindsight bank_id is missing or invalid" in completed.stderr
    assert not output.exists()


def test_oneclick_new_default_creation_is_fail_closed_when_owner_api_fails(tmp_path):
    script = Path("packaging/offline/tec01_oneclick_install.sh").read_text(encoding="utf-8")
    resolver = tmp_path / "resolver.py"
    resolver.write_text(_embedded_python(script, "resolve_aops_owner_bank_plan"), encoding="utf-8")
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"config": {"env": {
        "AOPS_BOT_TOKEN": "default-token",
        "AOPS_BOT_URL": "http://127.0.0.1:1",
    }}}), encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"profile": "default", "action": "create"}), encoding="utf-8")
    output = tmp_path / "plan.json"
    env = os.environ | {
        "HOME": str(tmp_path),
        "PAYLOAD_JSON": str(payload),
        "PROFILE_JSON": str(profile),
        "OWNER_BANK_PLAN_JSON": str(output),
        "AOPS_OWNER_LOOKUP_ATTEMPTS": "9",
        "AOPS_OWNER_LOOKUP_TIMEOUT": "0.01",
    }
    completed = subprocess.run([sys.executable, str(resolver)], env=env, capture_output=True, text=True)
    assert completed.returncode != 0
    assert "AOPS owner lookup failed" in completed.stderr or "<urlopen error" in completed.stderr
    assert not output.exists()


def test_oneclick_owner_bank_sync_preserves_existing_hindsight_fields(tmp_path):
    script = Path("packaging/offline/tec01_oneclick_install.sh").read_text(encoding="utf-8")
    synchronizer = tmp_path / "synchronizer.py"
    synchronizer.write_text(_embedded_python(script, "sync_hindsight_owner_banks"), encoding="utf-8")
    config_path = tmp_path / ".hermes" / "hindsight" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({
            "mode": "local_external",
            "api_url": "http://hindsight.internal",
            "retain_tags": "team:ops",
            "bank_id": "users-old",
            "bank_id_template": "users-{user}",
            "banks": {"hermes": {"bankId": "users-old", "budget": "high", "enabled": True}},
        }),
        encoding="utf-8",
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"profiles": [{
        "profile": "default",
        "hermesHome": str(tmp_path / ".hermes"),
        "ownerUserId": "user_001",
        "bankId": "aops-tec01-user_001",
    }]}), encoding="utf-8")
    result_path = tmp_path / "result.json"
    env = os.environ | {
        "OWNER_BANK_PLAN_JSON": str(plan_path),
        "OWNER_BANK_SYNC_RESULT_JSON": str(result_path),
    }
    subprocess.run([sys.executable, str(synchronizer)], check=True, env=env, capture_output=True, text=True)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["api_url"] == "http://hindsight.internal"
    assert config["retain_tags"] == "team:ops"
    assert config["bank_id"] == "aops-tec01-user_001"
    assert config["bank_id_template"] == ""
    assert config["banks"]["hermes"]["bankId"] == "aops-tec01-user_001"
    assert json.loads(result_path.read_text(encoding="utf-8"))["changedProfiles"][0]["profile"] == "default"

    subprocess.run([sys.executable, str(synchronizer)], check=True, env=env, capture_output=True, text=True)
    assert json.loads(result_path.read_text(encoding="utf-8"))["unchangedProfiles"][0]["profile"] == "default"


def test_aops_channel_interface_document_is_present():
    doc = Path("docs/aops-channel-interface.md").read_text(encoding="utf-8")
    assert "Runtime Agent Report" in doc
    assert "Silent Slash Commands" in doc
    assert "Toolsets" in doc
    assert "Curl One-Click Profile" in doc
    assert "必须同步更新本文档" in doc
