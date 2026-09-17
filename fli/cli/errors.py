"""CLI error reporting helpers.

Turns ugly tracebacks into a one-line message for the user plus a
self-contained log file under ~/.fli/logs/ that captures the full
traceback for debugging.
"""

from __future__ import annotations

import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import typer

from fli.cli.console import console
from fli.search.exceptions import (
    SearchClientError,
    SearchConnectionError,
    SearchHTTPError,
    SearchTimeoutError,
)

_LOG_DIR = Path.home() / ".fli" / "logs"
_logger = logging.getLogger("fli")


def _friendly_message(exc: BaseException) -> str:
    """Return the short, user-facing message for ``exc``."""
    if isinstance(exc, SearchTimeoutError):
        return f"Request timed out. {exc}"
    if isinstance(exc, SearchConnectionError):
        return f"Network error. {exc}"
    if isinstance(exc, SearchHTTPError):
        return f"Google Flights error. {exc}"
    if isinstance(exc, SearchClientError):
        return f"Search failed. {exc}"
    return f"Unexpected error: {exc.__class__.__name__}: {exc}"


def _write_log(exc: BaseException, *, command: str | None = None) -> Path | None:
    """Write the full traceback for ``exc`` to a log file and return the path.

    Returns None when the log could not be written. This module exists to
    replace an ugly traceback with a clean message, so it must not raise:
    creating ``~/.fli/logs`` fails on a read-only home, in a locked-down
    container, or under a sandbox, and letting that escape meant the user
    saw a ``PermissionError`` about ``~/.fli`` instead of the error they
    actually hit. Losing the log file is a much smaller harm than losing
    the diagnosis.
    """
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        _logger.debug("could not create log directory %s", _LOG_DIR, exc_info=True)
        return None
    # Microsecond precision so rapid-fire errors (e.g. tests, parallel
    # legs) don't collide on the same filename and silently overwrite.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    log_path = _LOG_DIR / f"fli-error-{timestamp}.log"

    lines: list[str] = []
    lines.append(f"timestamp: {datetime.now(timezone.utc).isoformat()}")
    if command:
        lines.append(f"command: {command}")
    lines.append(f"argv: {sys.argv}")
    lines.append(f"error_type: {exc.__class__.__module__}.{exc.__class__.__name__}")
    lines.append(f"error_message: {exc}")
    lines.append("")
    lines.append("traceback:")
    lines.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))

    try:
        log_path.write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        _logger.debug("could not write log file %s", log_path, exc_info=True)
        return None
    return log_path


def report_cli_error(
    exc: BaseException,
    *,
    command: str | None = None,
    exit_code: int = 1,
) -> typer.Exit:
    """Print a clean message for ``exc``, write a log file, and return a ``typer.Exit``.

    Callers should ``raise`` the returned :class:`typer.Exit` so typer
    handles the exit code (and the original exception is suppressed from
    the user's terminal).
    """
    log_path = _write_log(exc, command=command)
    message = _friendly_message(exc)

    console.print(f"[red]Error:[/red] {message}")
    if log_path is not None:
        console.print(f"[dim]Full traceback written to {log_path}[/dim]")

    # Still log at debug for anyone who wired up python logging.
    _logger.debug("CLI error", exc_info=exc)

    return typer.Exit(exit_code)


def json_error_payload(
    exc: BaseException, *, command: str | None = None
) -> tuple[str, str, Path | None]:
    """Return ``(message, error_type, log_path)`` for JSON-mode error output.

    ``log_path`` is None when no log file could be written; callers should
    emit JSON null rather than the string "None".
    """
    log_path = _write_log(exc, command=command)
    if isinstance(exc, SearchTimeoutError):
        return str(exc), "timeout", log_path
    if isinstance(exc, SearchConnectionError):
        return str(exc), "connection_error", log_path
    if isinstance(exc, SearchHTTPError):
        return str(exc), "http_error", log_path
    if isinstance(exc, SearchClientError):
        return str(exc), "search_error", log_path
    return f"{exc.__class__.__name__}: {exc}", "unexpected_error", log_path
