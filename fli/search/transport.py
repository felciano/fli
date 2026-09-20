"""Which transport a search may use.

Installing ``flights[browser]`` is the opt-in; this enum exists to *forbid*
or *force* the browser, not to enable it. The only request class routed to a
browser by default is one the HTTP transport cannot answer at all, so a
caller who never touches this gets today's behaviour wherever today's
behaviour works.
"""

from __future__ import annotations

from enum import Enum


class Transport(str, Enum):
    """How a search reaches Google Flights.

    Attributes:
        AUTO: HTTP for everything HTTP can serve, falling back to the browser
            only where the gated RPC is the only source. The default, and a
            byte-identical code path to today for one-way, round-trip and
            every date search.
        HTTP: Never touch a browser, whatever is installed. What the MCP
            server passes, so an ``flights[mcp,browser]`` install can never
            spawn Chrome by accident.
        BROWSER: Force interception even where HTTP would have worked. Useful
            for validating the browser path against a known-good HTTP result.

    """

    AUTO = "auto"
    HTTP = "http"
    BROWSER = "browser"
