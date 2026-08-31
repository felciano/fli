"""Guard against third-party modules being imported but never declared.

`fli/` has twice shipped an import that only resolved because some *other*
dependency happened to pull the package in transitively (``click`` via Typer,
then ``rich`` and ``mcp``). When the upstream package drops that edge, every
fresh install dies with ``ModuleNotFoundError``. These tests statically compare
what ``fli/`` imports against what ``pyproject.toml`` declares, so the bug class
cannot recur silently.
"""

import ast
import re
import sys
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):  # pragma: no cover - version-dependent import
    import tomllib
else:  # pragma: no cover - version-dependent import
    tomllib = pytest.importorskip(
        "tomli",
        reason="Python 3.10 needs the tomli backport; it is declared in the dev extra",
    )

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
PACKAGE_ROOT = PROJECT_ROOT / "fli"

#: Modules that are never expected to come from a distribution requirement.
IGNORED_MODULES = frozenset({"__future__", "fli"})


def _normalize(name: str) -> str:
    """Normalize a distribution name per PEP 503.

    Args:
        name: Raw distribution or module name.

    Returns:
        The lowercased, dash-separated canonical form.

    """
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement: str) -> str | None:
    """Extract the distribution name from a PEP 508 requirement string.

    Args:
        requirement: A requirement such as ``"rich>=13.8.0"`` or ``"flights[mcp]"``.

    Returns:
        The normalized distribution name, or ``None`` if none could be parsed.

    """
    match = re.match(r"^([A-Za-z0-9._-]+)", requirement.strip())
    return _normalize(match.group(1)) if match else None


def _load_pyproject() -> dict:
    """Read and parse ``pyproject.toml``.

    Returns:
        The parsed TOML document.

    """
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _declared_names(*requirement_lists: list[str]) -> set[str]:
    """Collect normalized distribution names from requirement lists.

    Args:
        *requirement_lists: Lists of PEP 508 requirement strings.

    Returns:
        The set of normalized distribution names.

    """
    names: set[str] = set()
    for requirements in requirement_lists:
        for requirement in requirements:
            name = _requirement_name(requirement)
            if name:
                names.add(name)
    return names


def _runtime_declared_names() -> set[str]:
    """Collect only the unconditional runtime dependencies.

    Deliberately excludes every extra. A package declared solely in ``dev`` (or
    any other extra) must not make a runtime import under ``fli/`` look
    satisfied — that is the blind spot this module exists to close.

    Returns:
        The set of normalized names in ``[project.dependencies]``.

    """
    return _declared_names(_load_pyproject()["project"].get("dependencies", []))


def _all_declared_names() -> set[str]:
    """Collect every distribution declared by the project, extras included.

    Returns:
        The set of normalized distribution names across ``dependencies`` and
        every list in ``optional-dependencies``.

    """
    project = _load_pyproject()["project"]
    extras = project.get("optional-dependencies", {})
    return _declared_names(project.get("dependencies", []), *extras.values())


def _imported_top_level_modules() -> dict[str, set[str]]:
    """Statically collect top-level modules imported anywhere under ``fli/``.

    Relative imports are skipped, since they can never name a third-party
    distribution.

    Returns:
        A mapping of top-level module name to the set of repo-relative paths
        that import it.

    """
    modules: dict[str, set[str]] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.setdefault(alias.name.split(".")[0], set()).add(relative)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.setdefault(node.module.split(".")[0], set()).add(relative)
    return modules


def _third_party_modules() -> dict[str, set[str]]:
    """Filter imported modules down to plausible third-party ones.

    Returns:
        The import map with stdlib, ``__future__`` and first-party ``fli``
        entries removed.

    """
    return {
        module: files
        for module, files in _imported_top_level_modules().items()
        if module not in sys.stdlib_module_names and module not in IGNORED_MODULES
    }


def _distributions_for(module: str) -> set[str]:
    """Map an importable module name to the distributions that provide it.

    Falls back to the normalized module name when the module is not installed,
    which is what a name like ``curl_cffi`` -> ``curl-cffi`` needs anyway.

    Args:
        module: Top-level module name.

    Returns:
        The set of normalized distribution names that could provide it.

    """
    providers = packages_distributions().get(module)
    if providers:
        return {_normalize(name) for name in providers}
    return {_normalize(module)}


def test_all_imported_third_party_modules_are_declared():
    """Every third-party module imported under ``fli/`` must be declared."""
    declared = _all_declared_names()
    undeclared: dict[str, set[str]] = {}
    for module, files in _third_party_modules().items():
        if not (_distributions_for(module) & declared):
            undeclared[module] = files

    assert not undeclared, "Imported but not declared in pyproject.toml:\n" + "\n".join(
        f"  {module}: imported by {', '.join(sorted(files))}"
        for module, files in sorted(undeclared.items())
    )


def test_rich_and_mcp_are_declared_directly():
    """``rich`` and ``mcp`` must be direct declarations, not transitive luck."""
    project = _load_pyproject()["project"]
    runtime = _declared_names(project.get("dependencies", []))
    mcp_extra = _declared_names(project["optional-dependencies"]["mcp"])

    assert "rich" in runtime, "fli/cli imports rich; declare it in [project].dependencies"
    assert "mcp" in mcp_extra, "fli/mcp/server.py imports mcp.types; declare it in the mcp extra"


def test_dev_only_declarations_do_not_satisfy_runtime_imports():
    """A package declared only in ``dev`` must not count as declared for ``fli/``.

    ``_all_declared_names`` used to union every requirement list, so a runtime
    import satisfied by a dev-only package looked fine — exactly the blind spot
    that lets the ``ModuleNotFoundError`` class recur.
    """
    project = _load_pyproject()["project"]
    extras = project.get("optional-dependencies", {})
    dev_only = _declared_names(extras.get("dev", [])) - _runtime_declared_names()
    assert dev_only, "expected at least one dev-only package to make this test meaningful"
    assert not (dev_only & _runtime_declared_names())


def test_core_imports_are_declared_in_runtime_dependencies():
    """Modules imported outside ``fli/mcp/`` must be runtime dependencies."""
    runtime = _runtime_declared_names()
    undeclared: dict[str, set[str]] = {}
    for module, files in _third_party_modules().items():
        core_files = {f for f in files if not f.startswith("fli/mcp/")}
        if core_files and not (_distributions_for(module) & runtime):
            undeclared[module] = core_files

    assert not undeclared, (
        "Imported outside fli/mcp/ but not in [project.dependencies]:\n"
        + "\n".join(
            f"  {module}: imported by {', '.join(sorted(files))}"
            for module, files in sorted(undeclared.items())
        )
    )


def test_mcp_only_imports_are_declared_in_runtime_or_the_mcp_extra():
    """Modules imported only under ``fli/mcp/`` may live in the ``mcp`` extra."""
    project = _load_pyproject()["project"]
    allowed = _runtime_declared_names() | _declared_names(
        project.get("optional-dependencies", {}).get("mcp", [])
    )
    undeclared: dict[str, set[str]] = {}
    for module, files in _third_party_modules().items():
        if all(f.startswith("fli/mcp/") for f in files) and not (
            _distributions_for(module) & allowed
        ):
            undeclared[module] = files

    assert not undeclared, (
        "Imported under fli/mcp/ but declared in neither dependencies nor the mcp extra:\n"
        + "\n".join(
            f"  {module}: imported by {', '.join(sorted(files))}"
            for module, files in sorted(undeclared.items())
        )
    )
