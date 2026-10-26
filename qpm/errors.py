# -*- coding: utf-8 -*-
"""Exception hierarchy for the plugin manager.

Every exception carries the process exit code the CLI should use, so the
argparse layer never has to translate error types.
"""

from __future__ import annotations


class QpmError(Exception):
    """Base class for expected, user-facing failures."""

    exit_code = 1


class UsageError(QpmError):
    """Invalid command line, state file, or inventory file."""

    exit_code = 2


class DiscoveryError(QpmError):
    """Runtime discovery failed (docker missing, inventory unreadable)."""

    exit_code = 2


class SourceError(QpmError):
    """A plugin bundle could not be read or is malformed."""

    exit_code = 2


class RuntimeUnreachable(QpmError):
    """The runtime API is not reachable (stopped, wrong port, firewall)."""

    exit_code = 1


class RuntimeApiError(QpmError):
    """The runtime answered with an HTTP error status."""

    exit_code = 1

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        detail: object = None,
        body: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.body = body

    @property
    def is_busy(self) -> bool:
        """Return whether the runtime rejected the call as currently busy."""
        return self.status == 409 and _detail_code(self.detail) == "plugin_busy"


def _detail_code(detail: object) -> str:
    if isinstance(detail, dict):
        return str(detail.get("code") or "")
    return ""


class RuntimeAuthError(RuntimeApiError):
    """The runtime rejected the boundary token (401)."""


class PluginNotFound(RuntimeApiError):
    """The runtime does not know the requested plugin (404)."""


class PluginAlreadyInstalled(RuntimeApiError):
    """A non-forced install hit an existing plugin (409)."""
