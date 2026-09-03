"""Add-on entry point: wiring, lifecycle, and signal handling."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from aiohttp import web

from .config import Settings
from .coordinator import Coordinator
from .ergon import ErgonClient
from .home_assistant import HomeAssistantClient
from .ledger import Ledger
from .logging_utils import SecretRedactionFilter
from .web import create_app

logger = logging.getLogger(__name__)

_OPTIONS_PATH = Path("/data/options.json")
_LEDGER_NAME = "ergon_usage.sqlite3"
_BIND_HOST = "0.0.0.0"
_BIND_PORT = 8099


def load_settings(path: Path = _OPTIONS_PATH, environ: dict[str, str] | None = None) -> Settings:
    """Load add-on settings honoring ERGON_DATA_DIR for tests."""

    import os

    return Settings.from_file(path, environ if environ is not None else dict(os.environ))


def configure_logging(settings: Settings) -> None:
    """Install secret redaction BEFORE any logging configuration."""

    root = logging.getLogger()
    redactor = SecretRedactionFilter(
        (settings.ergon_email, settings.ergon_password, settings.supervisor_token)
    )
    for handler in root.handlers:
        handler.addFilter(redactor)
    logging.basicConfig(level=logging.INFO)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)


async def async_main() -> None:
    settings = load_settings()
    configure_logging(settings)

    ledger = Ledger.open(Path(settings.data_dir) / _LEDGER_NAME)
    try:
        ergon = ErgonClient(settings)
        home_assistant = HomeAssistantClient(settings)
        coordinator = Coordinator(settings, ergon, ledger, home_assistant)

        app = create_app(coordinator)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, _BIND_HOST, _BIND_PORT)
        await site.start()

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        import signal

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # pragma: no cover - Windows dev only
                pass

        try:
            await coordinator.serve(stop)
        finally:
            await runner.cleanup()
    finally:
        ledger.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
