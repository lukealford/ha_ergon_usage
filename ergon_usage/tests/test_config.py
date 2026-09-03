import json
from pathlib import Path

import pytest
import yaml

from ergon_usage.app.config import Settings
from ergon_usage.app import config as config_module
from ergon_usage.app.errors import (
    AccountDiscoveryError,
    AuthenticationError,
    ErgonError,
    ExtractionError,
    ImportError,
)


def write_options(tmp_path: Path, **overrides: object) -> Path:
    options = {
        "ergon_email": "customer@example.com",
        "ergon_password": "correct-horse-battery-staple",
    }
    options.update(overrides)
    path = tmp_path / "options.json"
    path.write_text(json.dumps(options), encoding="utf-8")
    return path


def environment(**overrides: str) -> dict[str, str]:
    values = {"SUPERVISOR_TOKEN": "super-secret-token"}
    values.update(overrides)
    return values


def test_settings_uses_required_values_and_defaults(tmp_path: Path) -> None:
    settings = Settings.from_file(write_options(tmp_path), environment())

    assert settings.ergon_email == "customer@example.com"
    assert settings.ergon_password == "correct-horse-battery-staple"
    assert settings.supervisor_token == "super-secret-token"
    assert settings.poll_interval_hours == 12
    assert settings.initial_history_days == 365
    assert settings.backfill_batch_days == 30
    assert settings.request_delay_seconds == 3
    assert settings.retry_limit == 5
    assert settings.tariff_name_overrides == {}
    assert settings.data_dir == Path("/data")


@pytest.mark.parametrize(
    ("option", "lower", "upper"),
    [
        ("poll_interval_hours", 6, 48),
        ("initial_history_days", 1, 730),
        ("backfill_batch_days", 1, 60),
        ("request_delay_seconds", 0, 60),
        ("retry_limit", 0, 10),
    ],
)
def test_settings_accepts_every_numeric_boundary(
    tmp_path: Path, option: str, lower: int, upper: int
) -> None:
    assert getattr(Settings.from_file(write_options(tmp_path, **{option: lower}), environment()), option) == lower
    assert getattr(Settings.from_file(write_options(tmp_path, **{option: upper}), environment()), option) == upper


@pytest.mark.parametrize(
    ("option", "invalid_value"),
    [
        ("poll_interval_hours", 5),
        ("poll_interval_hours", 49),
        ("initial_history_days", 0),
        ("initial_history_days", 731),
        ("backfill_batch_days", 0),
        ("backfill_batch_days", 61),
        ("request_delay_seconds", -1),
        ("request_delay_seconds", 61),
        ("retry_limit", -1),
        ("retry_limit", 11),
    ],
)
def test_settings_rejects_values_just_outside_numeric_boundaries(
    tmp_path: Path, option: str, invalid_value: int
) -> None:
    with pytest.raises(ValueError, match=option):
        Settings.from_file(write_options(tmp_path, **{option: invalid_value}), environment())


def test_settings_copies_tariff_name_overrides_and_respects_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = {
        "ergon_email": "customer@example.com",
        "ergon_password": "correct-horse-battery-staple",
        "tariff_name_overrides": {"Peak": "peak"},
    }
    path = tmp_path / "options.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config_module.json, "loads", lambda _: options)

    settings = Settings.from_file(path, environment(ERGON_DATA_DIR="/custom-data"))
    options["tariff_name_overrides"]["Shoulder"] = "shoulder"

    assert settings.tariff_name_overrides == {"Peak": "peak"}
    assert settings.data_dir == Path("/custom-data")


@pytest.mark.parametrize(
    "options_text",
    ["", "not json", "[]"],
)
def test_settings_rejects_missing_or_invalid_json_without_leaking_secrets(
    tmp_path: Path, options_text: str
) -> None:
    path = tmp_path / "options.json"
    path.write_text(options_text, encoding="utf-8")

    with pytest.raises(ValueError) as error:
        Settings.from_file(path, environment())

    assert "super-secret-token" not in str(error.value)


@pytest.mark.parametrize(
    "options",
    [
        {"ergon_password": "correct-horse-battery-staple"},
        {"ergon_email": "customer@example.com"},
        {"ergon_email": "", "ergon_password": "correct-horse-battery-staple"},
        {"ergon_email": "customer@example.com", "ergon_password": ""},
    ],
)
def test_settings_rejects_missing_or_empty_credentials_without_leaking_secrets(
    tmp_path: Path, options: dict[str, str]
) -> None:
    path = tmp_path / "options.json"
    path.write_text(json.dumps(options), encoding="utf-8")

    with pytest.raises(ValueError) as error:
        Settings.from_file(path, environment())

    assert "correct-horse-battery-staple" not in str(error.value)
    assert "super-secret-token" not in str(error.value)


def test_settings_rejects_missing_supervisor_token_without_leaking_credentials(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as error:
        Settings.from_file(write_options(tmp_path), {})

    assert "correct-horse-battery-staple" not in str(error.value)
    assert "customer@example.com" not in str(error.value)


@pytest.mark.parametrize(
    ("error_type", "code", "retryable"),
    [
        (AuthenticationError, "authentication_error", False),
        (AccountDiscoveryError, "account_discovery_error", False),
        (ExtractionError, "extraction_error", True),
        (ImportError, "import_error", False),
    ],
)
def test_domain_errors_expose_safe_codes_and_retryability(
    error_type: type[ErgonError], code: str, retryable: bool
) -> None:
    error = error_type()

    assert isinstance(error, ErgonError)
    assert error.code == code
    assert error.retryable is retryable
    assert str(error) == error.safe_message


def test_base_domain_error_retains_its_safe_contract() -> None:
    error = ErgonError("example", "Safe explanation.", True)

    assert (error.code, error.safe_message, error.retryable) == ("example", "Safe explanation.", True)


def test_addon_metadata_declares_the_required_safe_configuration() -> None:
    root = Path(__file__).parents[2]
    repository = yaml.safe_load((root / "repository.yaml").read_text(encoding="utf-8"))
    config = yaml.safe_load((root / "ergon_usage" / "config.yaml").read_text(encoding="utf-8"))

    assert repository["name"] == "Ergon Usage Add-ons"
    assert config["ingress"] is True
    assert config["ingress_port"] == 8099
    assert config["homeassistant_api"] is True
    assert config["startup"] == "application"
    assert config["boot"] == "auto"
    assert config["init"] is False
    assert config["arch"] == ["amd64", "aarch64"]
    assert "hassio_api" not in config
    assert "host_network" not in config
    assert "privileged" not in config
    assert "full_access" not in config
    assert "docker_api" not in config
    assert config["options"] == {
        "poll_interval_hours": 12,
        "initial_history_days": 365,
        "backfill_batch_days": 30,
        "request_delay_seconds": 3,
        "retry_limit": 5,
        "tariff_name_overrides": {},
    }
    assert config["schema"]["ergon_password"] == "password"
    assert config["schema"]["poll_interval_hours"] == "int(6,48)"
    assert config["schema"]["initial_history_days"] == "int(1,730)"
    assert config["schema"]["backfill_batch_days"] == "int(1,60)"
    assert config["schema"]["request_delay_seconds"] == "int(0,60)"
    assert config["schema"]["retry_limit"] == "int(0,10)"
