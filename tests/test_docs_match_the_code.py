"""Claims the documentation makes that the code has to keep true.

Docs rot silently. Three kinds of rot are worth a test because each one has
already happened once in this repo or costs a user real time:

* **An index that disagrees with the record it indexes.** ADR 001 was flipped
  to ``Accepted`` while ``docs/decisions/README.md`` still advertised it as
  ``Proposed``. A reader who trusts the table reads the wrong status without
  ever opening the record.
* **An install command naming an extra that does not exist.** ``uv add
  "flights[browser]"`` appears in the README, in ``docs/index.md`` and inside
  the error message a user sees when the extra is missing. If the extra were
  renamed, all three would send people to a resolver error.
* **A marketing claim the implementation outgrew.** The README and the docs
  index both used to promise "no browser automation" unconditionally. Shipping
  an optional browser makes that false for one path, and a false claim about
  exactly the thing a privacy-minded user is checking costs trust out of all
  proportion to its size.

These read files rather than behaviour on purpose: nothing else notices when
prose and code drift apart.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):  # pragma: no cover - version-dependent import
    import tomllib
else:  # pragma: no cover - version-dependent import
    tomllib = pytest.importorskip(
        "tomli",
        reason="Python 3.10 needs the tomli backport; it is declared in the dev extra",
    )

REPO_ROOT = Path(__file__).resolve().parent.parent
DECISIONS = REPO_ROOT / "docs" / "decisions"
README = REPO_ROOT / "README.md"
DOCS_INDEX = REPO_ROOT / "docs" / "index.md"
MCP_GUIDE = REPO_ROOT / "docs" / "guides" / "mcp.md"

#: Both steps, because the wheel is not the browser and a user needs each.
INSTALL_STEPS = ("flights[browser]", "playwright install chromium")


def _record_paths() -> list[Path]:
    return sorted(p for p in DECISIONS.glob("*.md") if p.name != "README.md")


def _declared_status(record: Path) -> str:
    """Read the status word out of a record's header.

    Args:
        record: The ADR file.

    Returns:
        The single status word, e.g. ``"Accepted"``.

    """
    match = re.search(r"^\*\*Status:\*\*\s*([A-Za-z]+)", record.read_text(encoding="utf-8"), re.M)
    assert match, f"{record.name} has no **Status:** header"
    return match.group(1)


def _index_rows() -> dict[str, str]:
    """Map each indexed record's filename to the status the table claims."""
    rows = {}
    for line in (DECISIONS / "README.md").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\|\s*\[\d+\]\(([^)]+)\)\s*\|[^|]*\|\s*([A-Za-z ]+?)\s*\|", line)
        if match:
            rows[match.group(1)] = match.group(2)
    return rows


class TestDecisionIndex:
    def test_every_record_is_indexed(self):
        indexed = set(_index_rows())
        on_disk = {p.name for p in _record_paths()}
        assert on_disk == indexed

    def test_the_index_status_matches_the_record(self):
        """The drift this test exists because of: flipped here, stale there."""
        for name, claimed in _index_rows().items():
            assert claimed == _declared_status(DECISIONS / name), name

    def test_adr_001_is_accepted(self):
        """The browser dependency was accepted; the record has to say so."""
        record = DECISIONS / "001-optional-browser-backed-transport.md"
        assert _declared_status(record) == "Accepted"

    def test_the_accepted_record_still_carries_its_original_date(self):
        """Acceptance annotates the header; it does not rewrite the record.

        The convention is explicit that a record is not rewritten after it is
        written, so the date it was written stays readable alongside the date
        it was accepted.
        """
        header = (DECISIONS / "001-optional-browser-backed-transport.md").read_text(
            encoding="utf-8"
        )[:400]
        assert "2026-09-18" in header

    def test_no_record_claims_a_status_outside_the_vocabulary(self):
        allowed = {"Proposed", "Accepted", "Superseded", "Deferred", "Rejected"}
        for record in _record_paths():
            assert _declared_status(record) in allowed, record.name


class TestInstallCommandsResolve:
    @pytest.fixture(scope="class")
    def extras(self) -> dict:
        with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
            return tomllib.load(handle)["project"]["optional-dependencies"]

    def test_the_browser_extra_the_docs_name_actually_exists(self, extras):
        assert "browser" in extras

    def test_it_is_an_extra_and_not_a_runtime_dependency(self, extras):
        """The promise the whole design rests on: `uv add flights` is clean."""
        with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
            runtime = tomllib.load(handle)["project"]["dependencies"]
        assert not [d for d in runtime if "playwright" in d.lower()]
        assert [d for d in extras["browser"] if "playwright" in d.lower()]

    @pytest.mark.parametrize("page", ["README.md", "docs/index.md"])
    def test_the_docs_name_the_extra_exactly_as_declared(self, page):
        assert "flights[browser]" in (REPO_ROOT / page).read_text(encoding="utf-8")

    def test_the_readme_gives_both_install_steps(self):
        """One without the other leaves the user with a confident error."""
        text = README.read_text(encoding="utf-8")
        for step in INSTALL_STEPS:
            assert step in text, step

    def test_the_error_message_gives_the_same_two_steps(self):
        """Docs and diagnostics must not disagree about the fix."""
        from fli.search._browser import INSTALL_HINT

        for step in INSTALL_STEPS:
            assert step in INSTALL_HINT, step


class TestNoUnqualifiedNoBrowserClaim:
    @pytest.mark.parametrize("page", ["README.md", "docs/index.md"])
    def test_the_blanket_claim_is_gone(self, page):
        text = (REPO_ROOT / page).read_text(encoding="utf-8")
        assert "no browser automation" not in text.lower()

    @pytest.mark.parametrize("page", ["README.md", "docs/index.md"])
    def test_what_replaced_it_says_where_the_browser_is_and_is_not(self, page):
        """A qualified claim is only better than a false one if it qualifies."""
        text = (REPO_ROOT / page).read_text(encoding="utf-8").lower()
        assert "no browser by default" in text
        assert "flights[browser]" in text

    @pytest.mark.parametrize("page", ["README.md", "docs/index.md"])
    def test_the_no_scraping_claim_survives_because_it_is_still_true(self, page):
        """The browser intercepts the wire response; it never reads the DOM."""
        text = (REPO_ROOT / page).read_text(encoding="utf-8").lower()
        assert "no scraping" in text or "no html parsing" in text

    def test_the_mcp_guide_says_it_starts_no_browser(self):
        """Readers arrive here from a README that just advertised the extra."""
        text = MCP_GUIDE.read_text(encoding="utf-8")
        assert "never starts a browser" in text
        assert "no multi-city mcp tool" in text.lower()
