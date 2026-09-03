import logging

from ergon_usage.app.logging_utils import SecretRedactionFilter


def test_redacts_direct_message_text_and_clears_arguments() -> None:
    record = logging.LogRecord(
        "ergon", logging.INFO, __file__, 0, "Login failed for customer@example.com", (), None
    )

    assert SecretRedactionFilter(["customer@example.com"]).filter(record) is True
    assert record.msg == "Login failed for [REDACTED]"
    assert record.args == ()


def test_redacts_percent_style_arguments_after_formatting() -> None:
    record = logging.LogRecord(
        "ergon", logging.INFO, __file__, 0, "Login failed for %s with %s", ("customer@example.com", "token"), None
    )

    SecretRedactionFilter(["customer@example.com", "token"]).filter(record)

    assert record.msg == "Login failed for [REDACTED] with [REDACTED]"
    assert record.args == ()


def test_ignores_empty_secrets() -> None:
    record = logging.LogRecord("ergon", logging.INFO, __file__, 0, "The message is safe", (), None)

    SecretRedactionFilter(["", "safe"]).filter(record)

    assert record.msg == "The message is [REDACTED]"
    assert record.args == ()
