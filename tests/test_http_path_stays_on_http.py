"""Structural guards that the browser stayed where it was put.

ADR 001 bought an optional browser transport on one condition: that it serve
only the requests plain HTTP cannot serve, and that everything else reach
byte-identical code to what it reached before. That is a claim about the whole
package, not about any one module, and it is the kind of claim a well-meaning
patch erodes one call site at a time — a ``transport=`` forwarded "for
symmetry", a convenience import, a date sweep quietly routed through a browser
because it was already there.

So these tests read the source rather than the behaviour. Behavioural tests
prove the browser is not used *on the paths they exercise*; these prove there
is no path. Two of the guarantees below cannot be tested any other way:

* **An absent knob cannot be flipped.** ADR 001 requires that ``SearchDates``
  have no ``transport`` parameter *at all* — not one defaulting to HTTP —
  because the 600-combination sweep cap is premised on the 10 req/sec token
  bucket and a browser does not draw on it. Only introspection can assert the
  absence of a parameter.
* **MCP never spawns Chrome.** Every MCP search pins the transport explicitly,
  so an ``flights[mcp,browser]`` install cannot launch a browser by accident.
  A behavioural test proves today's two call sites behave; this proves a third
  one added later cannot forget.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from fli.search import SearchDates, SearchFlights

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_SERVER = REPO_ROOT / "fli" / "mcp" / "server.py"

#: Modules that need, or can reach, the optional extra.
BROWSER_MODULES = frozenset(
    {
        "fli.search._browser",
        "fli.search._capture",
        "fli.search.multi_city",
    }
)

#: The only files under ``fli/`` allowed to reach them. ``explore`` and
#: ``flights`` route to the browser for the one request class each that HTTP
#: cannot serve; ``multi.py`` is the CLI command that offers the board. The
#: browser modules themselves are deliberately absent: they import no other
#: browser module, so listing them would be a decorative entry, and a
#: decorative entry is exactly how an allow-list stops catching anything.
BROWSER_AWARE_FILES = frozenset(
    {
        "fli/search/__init__.py",
        "fli/search/multi_city.py",
        "fli/search/explore.py",
        "fli/search/flights.py",
        "fli/cli/commands/multi.py",
    }
)


def _module_of(path: Path) -> str:
    return ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts)


def _imported_modules(path: Path) -> set[str]:
    """Every module name a file imports, however deeply qualified.

    Args:
        path: The file to read.

    Returns:
        Absolute module names, with relative imports resolved against the
        file's own package.

    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = _module_of(path).rsplit(".", 1)[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module or ""
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


def _python_files() -> list[Path]:
    return sorted(p for p in (REPO_ROOT / "fli").rglob("*.py") if "__pycache__" not in p.parts)


class TestAbsentKnobs:
    """A parameter that does not exist cannot acquire a wrong value."""

    @pytest.mark.parametrize("method", ["search", "search_durations"])
    def test_search_dates_has_no_transport_parameter(self, method):
        """Sweeps stay on HTTP permanently — ADR 001 is explicit about it.

        ``MAX_DURATION_SWEEP_COMBINATIONS`` is 600 because 600 requests is
        about a minute at 10 req/sec. That arithmetic does not survive a
        browser, so the knob must not be there to turn.
        """
        parameters = inspect.signature(getattr(SearchDates, method)).parameters
        assert "transport" not in parameters

    def test_get_booking_options_has_no_transport_parameter(self):
        """Booking options still POST an ungated endpoint; untouched."""
        parameters = inspect.signature(SearchFlights.get_booking_options).parameters
        assert "transport" not in parameters

    def test_search_flights_does_have_one_and_it_defaults_to_auto(self):
        """The counterpart: the one place the knob belongs, defaulted safely."""
        from fli.search.transport import Transport

        parameter = inspect.signature(SearchFlights.search).parameters["transport"]
        assert parameter.default is Transport.AUTO


class TestMcpNeverSpawnsABrowser:
    def test_the_server_imports_nothing_that_needs_the_extra(self):
        imported = _imported_modules(MCP_SERVER)
        assert not (imported & BROWSER_MODULES)

    def test_no_mcp_module_imports_anything_that_needs_the_extra(self):
        for path in sorted((REPO_ROOT / "fli" / "mcp").rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            assert not (_imported_modules(path) & BROWSER_MODULES), path

    def test_every_flight_search_in_the_server_pins_the_transport(self):
        """Explicit at every call site, so a third one cannot inherit AUTO."""
        tree = ast.parse(MCP_SERVER.read_text(encoding="utf-8"))
        checked = 0
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            constructs_flight_search = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "SearchFlights"
                for node in ast.walk(function)
            )
            if not constructs_flight_search:
                continue
            for node in ast.walk(function):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr != "search":
                    continue
                pinned = [kw for kw in node.keywords if kw.arg == "transport"]
                assert pinned, f"{function.name}: search() left transport to the default"
                assert ast.unparse(pinned[0].value) == "Transport.HTTP", function.name
                checked += 1
        assert checked >= 2, "expected the two flight-search tools to be checked"

    def test_no_transport_other_than_http_appears_in_the_server(self):
        source = MCP_SERVER.read_text(encoding="utf-8")
        for name in ("Transport.BROWSER", "Transport.AUTO"):
            assert name not in source

    def test_the_server_exposes_no_multi_city_tool(self):
        """v1 ships none, for the same long-lived-process reason."""
        source = MCP_SERVER.read_text(encoding="utf-8")
        assert "SearchMultiCity" not in source


class TestContainment:
    """Only the files that argued for it may reach the browser modules."""

    def test_nothing_else_under_fli_imports_a_browser_module(self):
        offenders = []
        for path in _python_files():
            relative = path.relative_to(REPO_ROOT).as_posix()
            if relative in BROWSER_AWARE_FILES:
                continue
            if _imported_modules(path) & BROWSER_MODULES:
                offenders.append(relative)
        assert not offenders

    def test_the_allow_list_has_not_rotted(self):
        """An entry naming a file that no longer exists hides a real leak."""
        for relative in BROWSER_AWARE_FILES:
            assert (REPO_ROOT / relative).is_file(), relative

    def test_every_allow_list_entry_earns_its_place(self):
        """A waiver for a file that needs no waiver is a hole with a name.

        If a file stops importing the browser modules, its entry has to go —
        otherwise the day something else is added to that file, the guard
        stays silent about it.
        """
        for relative in BROWSER_AWARE_FILES:
            hit = _imported_modules(REPO_ROOT / relative) & BROWSER_MODULES
            assert hit, f"{relative} no longer needs a waiver; drop it from the allow-list"

    @pytest.mark.parametrize("command", ["flights", "dates", "airports"])
    def test_the_other_cli_commands_cannot_reach_a_browser(self, command):
        path = REPO_ROOT / "fli" / "cli" / "commands" / f"{command}.py"
        assert not (_imported_modules(path) & BROWSER_MODULES)

    def test_the_multi_city_url_builder_needs_no_extra(self):
        """It is the CLI's fallback and the board's ``booking_url``.

        If ``_tfs`` ever imported the transport, a no-browser install would
        lose the one thing it can still always offer.
        """
        assert not (_imported_modules(REPO_ROOT / "fli" / "search" / "_tfs.py") & BROWSER_MODULES)

    def test_the_decoders_stay_below_the_seam(self):
        """URL in, bytes out: the seam only works if it points one way."""
        for name in ("_decoders.py", "_wire.py", "_proto.py"):
            imported = _imported_modules(REPO_ROOT / "fli" / "search" / name)
            assert not (imported & BROWSER_MODULES), name

    def test_the_browser_module_knows_nothing_about_flights(self):
        """The reason ``SearchExplore`` reused it with no changes at all."""
        imported = _imported_modules(REPO_ROOT / "fli" / "search" / "_browser.py")
        leaked = {
            name
            for name in imported
            if name.startswith(("fli.models", "fli.search._decoders", "fli.search._tfs"))
        }
        assert not leaked


def test_importing_the_mcp_server_loads_no_browser_module():
    """A runtime check the AST allow-list cannot make.

    The static guard walks import statements and can be defeated by spelling:
    ``from fli.search import SearchMultiCity`` records the name
    ``fli.search``, not ``fli.search.multi_city``, so an eager re-export in
    ``fli/search/__init__.py`` pulled the browser modules into every MCP
    process while this file still passed. Importing in a subprocess and
    asking ``sys.modules`` afterwards is not fooled by any of that.
    """
    import subprocess
    import sys

    probe = (
        "import fli.mcp.server, sys, json;"
        "print(json.dumps([m for m in ("
        "'fli.search._browser','fli.search.multi_city','fli.search._capture','playwright'"
        ") if m in sys.modules]))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()
    import json as _json

    loaded = _json.loads(out)
    assert loaded == [], f"MCP server pulled in browser modules: {loaded}"


def test_mcp_forces_http_on_every_search_that_could_route_to_a_browser():
    """The MCP server must pin transport, not inherit AUTO.

    `SearchFlights.search` and `SearchExplore.search` default to
    `Transport.AUTO`, and AUTO routes to the browser for anything HTTP cannot
    serve. The MCP server is a long-lived process that must never spawn one,
    so it passes `Transport.HTTP` explicitly. Deleting those arguments would
    start launching browsers on a path nothing else covers.

    Only clients that *accept* `transport` are checked — `SearchDates` has no
    such parameter and is safe by construction, so requiring it there would be
    a false alarm that trains people to weaken the test.

    The module allow-list cannot see any of this: it checks imports, and the
    server legitimately imports these classes either way.
    """
    import ast
    import inspect

    import fli.search as search_pkg

    transport_capable = {
        name
        for name in ("SearchFlights", "SearchExplore", "SearchMultiCity")
        if hasattr(getattr(search_pkg, name, None), "search")
        and "transport" in inspect.signature(getattr(search_pkg, name).search).parameters
    }
    assert transport_capable, "precondition: at least one client takes transport"

    tree = ast.parse(Path("fli/mcp/server.py").read_text())
    offenders = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        # Which client classes are constructed in this function?
        built = {
            node.value.func.id
            for node in ast.walk(func)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
        }
        if not (built & transport_capable):
            continue
        for node in ast.walk(func):
            if not (isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "search"):
                continue
            if not any(
                kw.arg == "transport" and ast.unparse(kw.value).endswith("Transport.HTTP")
                for kw in node.keywords
            ):
                offenders.append(f"{func.name}: {ast.unparse(node)[:70]}")
    assert not offenders, (
        f"MCP .search() calls inheriting AUTO on a transport-capable client: {offenders}"
    )
