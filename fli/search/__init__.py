from typing import TYPE_CHECKING, Any

from .dates import DatePrice, SearchDates
from .exceptions import (
    BrowserAttestationRejectedError,
    BrowserConsentRequiredError,
    BrowserDecodeError,
    BrowserRpcTimeoutError,
    BrowserTransportUnavailableError,
    BrowserUnreachableError,
    SearchClientError,
    SearchConnectionError,
    SearchHTTPError,
    SearchTimeoutError,
)
from .explore import SearchExplore
from .flights import SearchFlights
from .transport import Transport

__all__ = [
    "SearchFlights",
    "SearchMultiCity",
    "board_total",
    "SearchDates",
    "DatePrice",
    "SearchClientError",
    "SearchTimeoutError",
    "SearchConnectionError",
    "SearchHTTPError",
    "SearchExplore",
    "Transport",
    "BrowserOptions",
    "RpcCapture",
    "browser_available",
    "capture_rpc_body",
    "BrowserTransportUnavailableError",
    "BrowserUnreachableError",
    "BrowserConsentRequiredError",
    "BrowserRpcTimeoutError",
    "BrowserAttestationRejectedError",
    "BrowserDecodeError",
]


# The browser-backed names are resolved on first access, not on import.
#
# Importing them eagerly pulled ``fli.search._browser``, ``.multi_city`` and
# ``._capture`` into every process that did ``import fli.search`` — including
# ``fli.mcp.server``, which must never reach for a browser. Playwright itself
# stayed unimported thanks to the lazy import inside those modules, so nothing
# broke; but the guard test asserting the MCP server "imports nothing that
# needs the extra" was false at runtime, and a guard that does not hold is
# worse than none. PEP 562 keeps the public names working while the modules
# load only when something actually asks for one.
_LAZY = {
    "BrowserOptions": "._browser",
    "RpcCapture": "._browser",
    "browser_available": "._browser",
    "capture_rpc_body": "._browser",
    "SearchMultiCity": ".multi_city",
    "board_total": ".multi_city",
}

if TYPE_CHECKING:  # pragma: no cover — for type checkers only.
    from ._browser import BrowserOptions, RpcCapture, browser_available, capture_rpc_body
    from .multi_city import SearchMultiCity, board_total


def __getattr__(name: str) -> Any:
    """Resolve a browser-backed export on first use (PEP 562)."""
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module, __name__), name)


def __dir__() -> list[str]:
    """Include the lazily-resolved names in ``dir()`` and tab completion."""
    return sorted(set(__all__) | set(globals()))
