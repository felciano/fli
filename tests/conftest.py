import pytest


def pytest_addoption(parser) -> None:
    """Add options to pytest."""
    parser.addoption("--fuzz", action="store_true", help="Run fuzz tests")
    parser.addoption("--mcp", action="store_true", help="Run MCP tests")
    parser.addoption("--all", action="store_true", help="Run all tests")


def pytest_runtest_setup(item) -> None:
    """Skip fuzz tests unless --fuzz or --all is specified."""
    fuzz_marker = item.get_closest_marker("fuzz")
    if fuzz_marker is not None:
        if not item.config.getoption("--fuzz") and not item.config.getoption("--all"):
            pytest.skip("need --fuzz or --all option to run this test")


def pytest_collection_modifyitems(config, items) -> None:
    """Modify collection based on custom flags."""
    if config.getoption("--mcp"):
        # Only keep MCP tests when --mcp flag is used
        items[:] = [item for item in items if "mcp" in item.nodeid]
    elif config.getoption("--fuzz"):
        # Only keep fuzz tests when --fuzz flag is used (and not --all)
        if not config.getoption("--all"):
            items[:] = [item for item in items if item.get_closest_marker("fuzz")]
    elif not config.getoption("--all"):
        # Remove fuzz tests from normal runs
        items[:] = [item for item in items if not item.get_closest_marker("fuzz")]


@pytest.fixture
def tz_override():
    """Run a test under a chosen ``TZ``, restoring the original afterwards.

    The past-date validators can only compare against the clock of the machine
    running them, so the bug they guard against (a server whose local date has
    already rolled past the traveler's) only reproduces when the process
    timezone differs from UTC. Yield a setter so a test can pick the offset it
    needs, e.g. ``tz_override("Etc/GMT-14")`` for UTC+14.
    """
    import os
    import time

    if not hasattr(time, "tzset"):  # pragma: no cover - non-POSIX platforms
        pytest.skip("time.tzset() is unavailable on this platform")

    original = os.environ.get("TZ")

    def _set(zone: str) -> None:
        os.environ["TZ"] = zone
        time.tzset()

    yield _set

    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()
