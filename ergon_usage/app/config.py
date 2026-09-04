"""Configuration loading and validation for the Ergon Usage add-on."""

from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


_RANGES = {
    "poll_interval_hours": (6, 48),
    "initial_history_days": (1, 730),
    "backfill_batch_days": (1, 60),
    "request_delay_seconds": (0, 60),
    "retry_limit": (0, 10),
}

_DEFAULTS = {
    "poll_interval_hours": 12,
    "initial_history_days": 365,
    "backfill_batch_days": 30,
    "request_delay_seconds": 3,
    "retry_limit": 5,
    "tariff_name_overrides": {},
    "backfill_current_rate": False,
    "tou_tariffs": ["Tariff 11"],
}


@dataclass(frozen=True)
class Settings:
    ergon_email: str
    ergon_password: str
    supervisor_token: str
    poll_interval_hours: int
    initial_history_days: int
    backfill_batch_days: int
    request_delay_seconds: int
    retry_limit: int
    tariff_name_overrides: Mapping[str, str]
    backfill_current_rate: bool
    tou_tariffs: tuple[str, ...]
    data_dir: Path

    @classmethod
    def from_file(cls, path: Path, environ: Mapping[str, str]) -> "Settings":
        try:
            options = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("Unable to read add-on options JSON.") from error

        if not isinstance(options, dict):
            raise ValueError("Add-on options JSON must be an object.")

        email = _required_string(options, "ergon_email")
        password = _required_string(options, "ergon_password")
        supervisor_token = _required_environment_string(environ, "SUPERVISOR_TOKEN")

        values = {**_DEFAULTS, **options}
        for option, (lower, upper) in _RANGES.items():
            value = values[option]
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise ValueError(f"{option} must be an integer between {lower} and {upper}.")

        # HA's options schema has no `dict` type: the override map arrives
        # as a JSON object string ({"Tariff 11": "T11"}) and is parsed here.
        # A real object is still accepted for backward compatibility.
        overrides = values["tariff_name_overrides"]
        if isinstance(overrides, str):
            try:
                overrides = json.loads(overrides or "{}")
            except json.JSONDecodeError as error:
                raise ValueError(
                    "tariff_name_overrides must be valid JSON."
                ) from error
        if not isinstance(overrides, dict):
            raise ValueError("tariff_name_overrides must be an object.")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in overrides.items()):
            raise ValueError("tariff_name_overrides must contain string keys and values.")

        backfill_current_rate = values["backfill_current_rate"]
        if not isinstance(backfill_current_rate, bool):
            raise ValueError("backfill_current_rate must be a boolean.")

        tou_tariffs = values["tou_tariffs"]
        # HA's list(str) schema delivers a bare string when a single value
        # is entered in the UI, or a list when multiple are set.
        if isinstance(tou_tariffs, str):
            tou_tariffs = [tou_tariffs]
        if not isinstance(tou_tariffs, list) or not all(
            isinstance(item, str) and item.strip() for item in tou_tariffs
        ):
            raise ValueError("tou_tariffs must be a list of non-empty strings.")

        return cls(
            ergon_email=email,
            ergon_password=password,
            supervisor_token=supervisor_token,
            poll_interval_hours=values["poll_interval_hours"],
            initial_history_days=values["initial_history_days"],
            backfill_batch_days=values["backfill_batch_days"],
            request_delay_seconds=values["request_delay_seconds"],
            retry_limit=values["retry_limit"],
            tariff_name_overrides=MappingProxyType(dict(overrides)),
            backfill_current_rate=backfill_current_rate,
            tou_tariffs=tuple(tou_tariffs),
            data_dir=Path(environ.get("ERGON_DATA_DIR", "/data")),
        )


def _required_string(options: Mapping[str, object], name: str) -> str:
    value = options.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required.")
    return value


def _required_environment_string(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required.")
    return value
