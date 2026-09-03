"""Logging helpers that keep configured secrets out of log output."""

import logging
from collections.abc import Iterable


class SecretRedactionFilter(logging.Filter):
    """Replace configured non-empty secrets in each log record."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = tuple(
            sorted(
                {secret for secret in secrets if secret},
                key=lambda secret: (-len(secret), secret),
            )
        )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, "[REDACTED]")
        record.msg = message
        record.args = ()
        return True
