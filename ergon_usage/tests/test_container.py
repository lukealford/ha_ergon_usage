"""Tests for add-on packaging: metadata, container, runtime, and docs."""

from pathlib import Path

import yaml

ADDON_DIR = Path(__file__).resolve().parents[2] / "ergon_usage"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ADDON_DIR / relative).read_text(encoding="utf-8")


def test_addon_metadata_has_required_permissions():
    config = yaml.safe_load(_read("config.yaml"))
    assert config["homeassistant_api"] is True
    assert config["ingress"] is True
    assert config["ingress_port"] == 8099
    assert set(config["arch"]) == {"amd64", "aarch64"}
    assert config.get("full_access", False) is False
    assert "hassio_api" not in config


def test_addon_metadata_identity_fields():
    config = yaml.safe_load(_read("config.yaml"))
    assert config["name"]
    assert config["version"]
    assert config["slug"]
    assert config["description"]
    assert config["url"].startswith("https://")


def test_addon_schema_ranges_match_settings_validation():
    config = yaml.safe_load(_read("config.yaml"))
    schema = config["schema"]
    assert schema["poll_interval_hours"] == "int(6,48)"
    assert schema["initial_history_days"] == "int(1,730)"
    assert schema["backfill_batch_days"] == "int(1,60)"
    assert schema["request_delay_seconds"] == "int(0,60)"
    assert schema["retry_limit"] == "int(0,10)"
    assert schema["ergon_email"] == "email"
    assert schema["ergon_password"] == "password"
    assert schema["tariff_name_overrides"] == "str"  # JSON object string; HA schema has no dict type


def test_build_yaml_pins_playwright_image_per_arch():
    build = yaml.safe_load(_read("build.yaml"))
    expected = "mcr.microsoft.com/playwright/python:v1.62.0-noble"
    assert build["build_from"]["amd64"] == expected
    assert build["build_from"]["aarch64"] == expected


def test_dockerfile_uses_build_from_and_installs_chromium():
    dockerfile = _read("Dockerfile")
    assert "$BUILD_FROM" in dockerfile
    assert "playwright install --with-deps chromium" in dockerfile
    assert "COPY app/ /opt/ergon_usage/app" in dockerfile
    assert "ENV PYTHONPATH=/opt/ergon_usage" in dockerfile
    assert 'CMD ["/run.sh"]' in dockerfile
    assert "run.sh" in dockerfile


def test_run_sh_execs_entrypoint_with_safety_flags():
    run_sh = _read("run.sh")
    assert run_sh.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in run_sh
    assert "exec python -m app.main" in run_sh


def test_ci_workflow_runs_pytest_and_compileall():
    workflow = (REPO_ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    assert "pytest" in workflow
    assert "compileall" in workflow


def test_readme_documents_addon_store_install_and_not_hacs():
    readme = _read("README.md")
    assert "Add-on Store" in readme
    assert "HACS" in readme


def test_readme_documents_safe_first_run_procedure():
    readme = _read("README.md")
    assert "initial_history_days: 1" in readme
    assert "backfill_batch_days: 1" in readme
    assert "initial_history_days: 365" in readme
    assert "backfill_batch_days: 30" in readme


def test_readme_documents_energy_dashboard_statistics():
    readme = _read("README.md")
    assert "ergon:" in readme
    assert "_cost" in readme
    assert "Energy Dashboard" in readme
