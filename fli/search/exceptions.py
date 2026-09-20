"""Typed errors raised by the search client.

These exist so the CLI (and library consumers) can react to network
failures with a clear, user-facing message instead of a raw curl-cffi
traceback. They are intentionally light wrappers — the original
exception is kept as ``__cause__`` for logging.
"""

from __future__ import annotations


class SearchClientError(Exception):
    """Base class for errors talking to the Google Flights backend."""


class SearchTimeoutError(SearchClientError):
    """The request to Google Flights timed out before any data arrived."""


class SearchConnectionError(SearchClientError):
    """A network/DNS issue prevented us from reaching Google Flights."""


class SearchHTTPError(SearchClientError):
    """Google Flights returned a non-2xx HTTP response."""

    def __init__(self, message: str, *, status_code: int | None = None):
        """Store the HTTP status alongside the message for richer logging."""
        super().__init__(message)
        self.status_code = status_code


class SearchRejectedError(SearchClientError):
    """Google answered HTTP 200 but declined to serve results.

    The response carries a ``wrb.fr`` row with no payload and an error
    code (13 = INTERNAL). Since 2026-08 ``GetShoppingResults`` requires an
    ``x-goog-batchexecute-bgr`` header signed by the page's own JavaScript
    over the exact request bytes, so a plain HTTP client always lands here.
    Without this error the caller saw an empty list and reported "no
    flights found", which is indistinguishable from a route with no service.
    """

    #: gRPC canonical status codes, which Google reuses here. Observed: 3 for
    #: a payload it cannot decode, 13 for a request it declines to serve.
    STATUS_NAMES = {
        1: "CANCELLED",
        2: "UNKNOWN",
        3: "INVALID_ARGUMENT",
        4: "DEADLINE_EXCEEDED",
        5: "NOT_FOUND",
        6: "ALREADY_EXISTS",
        7: "PERMISSION_DENIED",
        8: "RESOURCE_EXHAUSTED",
        9: "FAILED_PRECONDITION",
        10: "ABORTED",
        11: "OUT_OF_RANGE",
        12: "UNIMPLEMENTED",
        13: "INTERNAL",
        14: "UNAVAILABLE",
        15: "DATA_LOSS",
        16: "UNAUTHENTICATED",
    }

    def __init__(self, code: int | None = None):
        """Record the numeric error code alongside the user-facing message."""
        self.code = code
        name = self.STATUS_NAMES.get(code) if code is not None else None
        suffix = f" (error {code}{f' {name}' if name else ''})" if code is not None else ""
        # The two observed codes mean different things, so do not blame the
        # gate for both: 13 is the signature of a request Google declines to
        # serve (the bgr gate); 3 is a payload it could not decode, which is
        # this client's bug, not Google's policy.
        if code == 3:
            cause = (
                "It could not decode the request payload, which usually means a "
                "malformed or empty f.req rather than a blocked endpoint."
            )
        else:
            cause = (
                "Its API now requires a browser-signed x-goog-batchexecute-bgr "
                "header, which this client cannot produce. "
                "See github.com/punitarani/fli#223."
            )
        super().__init__(
            f"Google Flights declined the request{suffix} and returned no data. {cause}"
        )


class SearchUnsupportedError(SearchClientError):
    """The requested search cannot be served by the current transport.

    Distinct from an empty result: the query is well formed and Google
    would answer it in a browser, but the public search page carries no
    inline payload for it, so this client has nothing to read.
    """


class SearchParseError(Exception):
    """Raised when a successful HTTP response cannot be parsed into flights.

    Distinct from network / HTTP errors raised by the underlying client —
    use this to tell "Google responded but the shape changed" apart from
    "Google didn't respond at all".

    Deliberately **not** a :class:`SearchClientError`. It lives here so the
    browser transport's decode failure can subclass it without
    :mod:`fli.search.exceptions` importing :mod:`fli.search.flights` (which
    imports this module), but its base class is unchanged from when it was
    defined there, so the CLI keeps reporting it exactly as before.
    :mod:`fli.search.flights` re-exports it for existing importers.
    """


# ---------------------------------------------------------------------------
# Optional browser-backed transport (ADR 001)
# ---------------------------------------------------------------------------
#
# Every one of these is reachable only from the optional browser transport.
# They subclass the error the CLI already has an arm for, so a browser
# failure degrades through the same paths as its HTTP equivalent rather than
# arriving as an unhandled traceback.


class BrowserTransportUnavailableError(SearchUnsupportedError):
    """The browser transport is needed but cannot run on this install.

    Two distinct causes, kept distinct in the message because telling a user
    to install a package they already have is how support tickets start:
    the ``browser`` extra is not installed at all, or Playwright is present
    but its Chromium binary has never been downloaded.
    """


class BrowserUnreachableError(SearchClientError):
    """A browser could not be obtained — neither attached to nor launched.

    Raised when an explicitly configured CDP endpoint refuses the
    connection, and when launching a browser fails (commonly a sandbox or
    seccomp restriction rather than a broken install).
    """


class BrowserConsentRequiredError(SearchClientError):
    """Google's cookie-consent interstitial blocked the page.

    fli dismisses consent only in a profile it created itself, and only with
    the privacy-preserving "Reject all". Whenever that is not both possible
    and fli's call to make — the browser is the user's own, consent handling
    is disabled, or the control could not be located — it stops here and
    says what single manual step unblocks it.
    """


class BrowserRpcTimeoutError(SearchTimeoutError):
    """The page loaded but never issued the RPC we were waiting for.

    Raised only after the single search-control nudge has also timed out.
    Carries the RPC URLs the page *did* request, which is what separates
    "the page never got going" from "Google served a cached board".
    """

    def __init__(self, message: str, *, rpc_urls: list[str] | None = None):
        """Record the observed RPC URLs alongside the message."""
        super().__init__(message)
        self.rpc_urls = list(rpc_urls or [])


class BrowserAttestationRejectedError(SearchRejectedError):
    """Google declined the request even though it came from a real browser.

    Distinguishable from an HTTP-path rejection, and it means something
    different: the browser itself was refused, so this is bot detection or a
    flagged profile rather than the known ``bgr`` gate. Never retried
    automatically — retrying a request the service just declined is exactly
    the behaviour the gate exists to punish.
    """

    def __init__(self, message: str, *, code: int | None = None):
        """Set a browser-specific message instead of the HTTP-gate one."""
        self.code = code
        SearchClientError.__init__(self, message)


class BrowserDecodeError(SearchParseError):
    """A body was intercepted but carried no rows in the expected shape.

    The failure ADR 001 most wants reported rather than swallowed: it is the
    reopen trigger "the multi-city response does not reuse the one-way row
    shape". Crisply distinct from Google reporting no itineraries, which is
    an empty result and not an error.
    """
