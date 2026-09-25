class LisaError(Exception):
    """An expected failure, reported to the user as a one-line error without a traceback."""


class ConfigError(LisaError):
    """The action's inputs or workflow event are missing or invalid."""


class ApiError(LisaError):
    """A GitHub or TypeSafe request failed after any retries."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AuthError(ApiError):
    """A credential was rejected. Retrying or continuing cannot help, so the review stops."""
