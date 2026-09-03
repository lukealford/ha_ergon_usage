"""Safe domain errors exposed by the Ergon Usage add-on."""


class ErgonError(Exception):
    """An application error whose message is safe to show or log."""

    def __init__(self, code: str, safe_message: str, retryable: bool) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable


class AuthenticationError(ErgonError):
    def __init__(self, safe_message: str = "Unable to authenticate with Ergon.") -> None:
        super().__init__("authentication_error", safe_message, False)


class AccountDiscoveryError(ErgonError):
    def __init__(self, safe_message: str = "Unable to discover the Ergon account.") -> None:
        super().__init__("account_discovery_error", safe_message, False)


class ExtractionError(ErgonError):
    def __init__(self, safe_message: str = "Unable to extract Ergon usage data.") -> None:
        super().__init__("extraction_error", safe_message, True)


class ImportError(ErgonError):
    def __init__(self, safe_message: str = "Unable to import Ergon usage data.") -> None:
        super().__init__("import_error", safe_message, False)
