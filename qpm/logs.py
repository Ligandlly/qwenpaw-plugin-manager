# -*- coding: utf-8 -*-
"""Opt-in debug logging for ``qpm --verbose``.

Every debug line goes to stderr, prefixed with ``debug:``, so it never mixes
with the tables or JSON documents on stdout: ``qpm install x --json -v`` still
produces a single parseable JSON document.  Nothing is emitted unless the
operator asked for it; the modules below call ``logger.debug`` freely.
"""

from __future__ import annotations

import logging
import sys
import threading

__all__ = ["debug", "get_logger", "is_enabled", "reset", "set_enabled"]

LOGGER_NAME = "qpm"
_FORMAT = "debug: %(message)s"
_LOCK = threading.Lock()


class _StderrHandler(logging.Handler):
    """Write to whatever ``sys.stderr`` is at emit time.

    Resolving the stream lazily keeps the handler usable when the process
    (or a test harness) swaps ``sys.stderr`` after the handler was installed.
    """

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            print(self.format(record), file=sys.stderr, flush=True)
        except Exception:  # pragma: no cover - logging never breaks a run
            self.handleError(record)


def _logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def get_logger(module: str) -> logging.Logger:
    """Return the ``qpm.<module>`` logger used for verbose tracing."""
    return logging.getLogger(f"{LOGGER_NAME}.{module}")


def is_enabled() -> bool:
    """Return whether debug output is currently being written."""
    return _logger().isEnabledFor(logging.DEBUG)


def set_enabled(enabled: bool) -> None:
    """Turn debug output on or off (idempotent).

    The handler is installed once and then reused; the level controls whether
    anything reaches it, so repeated CLI invocations in one process (tests,
    the web console) cannot accumulate handlers or leak state.
    """
    logger = _logger()
    with _LOCK:
        if enabled and not any(
            isinstance(handler, _StderrHandler) for handler in logger.handlers
        ):
            handler = _StderrHandler()
            handler.setFormatter(logging.Formatter(_FORMAT))
            logger.addHandler(handler)
        logger.setLevel(logging.DEBUG if enabled else logging.WARNING)
        # Keep debug lines out of whatever handlers the host app installed.
        logger.propagate = False


def debug(message: str, *args: object) -> None:
    """Write one debug line when verbose mode is on."""
    logger = _logger()
    if not logger.isEnabledFor(logging.DEBUG):
        return
    logger.debug(message, *args)


def reset() -> None:
    """Remove the handler (used by tests to restore a clean logger)."""
    logger = _logger()
    with _LOCK:
        for handler in list(logger.handlers):
            if isinstance(handler, _StderrHandler):
                logger.removeHandler(handler)
                handler.close()
        logger.setLevel(logging.WARNING)
